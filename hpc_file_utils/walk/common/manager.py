"""The Manager (rank 0) hands directories and file batches to the workers until the walk is done.

Workers walk their own local stack of directories, depth-first, and talk to the Manager
only when it overflows, when it runs dry, or when the Manager asks them to share
(phase 3.5; docs/walk-load-balancing.md, section 5).

Phase 5: the workers also turn a directory's files into batches, kept on the scanning
worker's own list and processed there, batches before directories: walktar's are
FileBatch(paths, nbytes), archived into one tar each; walkstat's are StatBatch(dirname,
names, tail), the names still to stat (stat/worker.py); walksync's are CompareBatch
and FileBatch (sync/worker.py). A batch leaves its worker only in a share_reply, and
the Manager hands batches out before directories. To the Manager and to this argument,
a batch is opaque: one unit of work.

Messages (tags in protocol.py; 'payload' is the (items, bytes, queued, extra) tuple):

  worker -> manager
    ready        payload           "I hold nothing (no directory, no batch) and my last
                                   directory or batch is done, give me work". The worker
                                   then waits for the answer.
    dir_reply    (dirs, payload)   overflow: the oldest half of a stack that grew past
                                   --local-queue-max.
    share_reply  (dirs, batches, payload)
                                   the answer to a 'share': the oldest half of the
                                   stack and of the closed batches (rounded up), either
                                   possibly empty.
    progress     payload           running counts, every PROGRESS_INCREMENT entries of
                                   a huge directory, or --progress seconds between ones.
  manager -> worker
    execute      (dirs, batches)   the answer to a 'ready': one batch if the Manager
                                   holds any, otherwise directories to walk (at most
                                   --dirs-per-dispatch)
    terminate    None              the answer to a 'ready': the walk is done
    share        None              sent to a busy worker while the Manager holds idle
                                   'ready's and no work; answered with a
                                   share_reply at the worker's next look (every
                                   directory or batch, every 1,000 entries of a big
                                   directory and between a batch's files), or
                                   not at all if the worker goes idle first: its 'ready'
                                   cancels the request

How the Manager decides the walk is done:

  1. A worker is idle only while the Manager holds its 'ready' unanswered. A worker
     sends 'ready' only when its local stack is empty, it holds no batch (none closed,
     and the open one, if any, has been closed and processed), and its last directory
     or batch is done; process_batch() runs on the worker's only thread, so done means
     returned. It then waits for the answer, so it cannot send anything else.
  2. The Manager receives with ANY_SOURCE and ANY_TAG, so every message matches the
     same receive and it gets each worker's messages in the order they were sent
     (MPI's non-overtaking rule applies to messages from one sender that match the
     same receive). When it gets a worker's 'ready', it has already received every
     dir_reply and share_reply that worker sent before it, and so every batch: batches
     reach the Manager only inside a share_reply, and a worker only inside an execute,
     the answer to a 'ready'. (The old loop probed each tag separately,
     so it could see a 'ready' before the dir_reply sent just ahead of it, decide
     everyone was idle, and lose or hang on that dir_reply.)
  3. So: no directories and no batches queued here, and every worker idle, means no
     directory or batch is queued, in flight, being scanned or being processed
     anywhere. The Manager answers each held 'ready' with 'terminate'.

Processing a batch creates no directories. It may create batches (walksync's compare
batches close copy batches), but only on the worker processing it, which keeps them on
its own list and processes them (point 1) before its next 'ready'. A 'share' never
carries work, so it cannot upset this either. A worker that receives one while
it waits for the answer to its 'ready' ignores it (its wait is a receive from rank 0
with ANY_TAG, so it also gets the share before any execute or terminate sent after
it; it holds no batch either, so nothing is kept back), and the Manager forgets the
request when that 'ready' arrives. The Manager has at
most one unanswered 'share' per worker, and never sends one to a worker whose 'ready'
it holds.

Fairness: MPI promises no order between senders, but the Manager takes every message
as it comes, whatever its tag, and each costs it microseconds, so a busy worker's
dir_reply/progress messages delay a 'ready' only briefly. (One blocking receive per
message replaced a pass that probed every worker in turn: at 384 ranks over 4 nodes
those probes kept the Manager busy and made workers wait; see walk/README.md.)
"""

import heapq
import sys
from collections import deque

from hpc_file_utils.walk.common.formatting import format_number, format_size
from hpc_file_utils.walk.common.mpiclass import MPIClass
from hpc_file_utils.walk.common.progress import ProgressTally, progress_line, written_status
from hpc_file_utils.walk.tools import TOOLS

def _ms(seconds):
    return '{:.3f} ms'.format(1e3 * seconds)


################################################################################
class Manager(MPIClass):

    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self,options=None):
        MPIClass.__init__(self,options)
        MPI = self.MPI
        self.nsends = 0
        self.nrecvs = 0
        self.dirs = list(self.options.dirs)           # directories waiting for a worker
        self.batches = deque()                         # batches waiting for a worker (phase 5)
        self.nworkers = self.nranks - 1
        self.idle = deque()                            # (rank, when) of each 'ready' we hold, unanswered
        self.idle_set = set()                          # ... just the ranks
        self.share_pending = set()                     # workers with an unanswered 'share'
        self.progress = ProgressTally(self.nranks)
        self.progress_time = self.start_time = MPI.Wtime()
        # what the tool's batches do, for the progress line and the last line (walkstat: nothing)
        self.verb, self.units = TOOLS[self.options.app].writes or ('wrote', 'archives')

        # where the Manager's directories came from (printed after every walk)
        self.ndispatches = 0         # 'execute' messages
        self.ndirs_overflow = 0      # directories received as overflow (dir_reply)
        self.ndirs_shared = 0        # ... in share_reply
        self.nshares = 0             # 'share' requests sent
        self.nshares_empty = 0       # ... answered with no directories (nor batches)
        self.nbatches_shared = 0     # batches received in share_reply
        self.nbatch_dispatches = 0   # ... handed out, one per 'execute'
        self.last_written = 0        # bytes written at the last progress line (rate 'now')
        self.walk_elapsed = 0.0

        # instrumentation (--verbose)
        self.wait_time = 0.0         # blocked in recv, waiting for any message
        self.busy_time = 0.0         # handling messages and answering 'ready's
        self.max_handle_time = 0.0
        self.sum_ready_wait = 0.0    # how long each 'ready' was held before its 'execute'
        self.max_ready_wait = 0.0
        self.max_terminate_wait = 0.0
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def finished(self):
        """Nothing queued here and every worker idle: nothing left anywhere (see module doc)."""
        return not self.dirs and not self.batches and len(self.idle) == self.nworkers



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def update_progress(self, rank, payload):
        """payload: the (items, bytes, queued, extra) every worker message carries (protocol.py)."""
        self.progress.update(rank, payload)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def written(self):
        """(bytes, files, archives) written so far, summed over the workers' 'extra'
        payloads; None when no worker sends one (walkstat)."""
        extras = [e for e in self.progress.extra if e is not None]
        if not extras:
            return None
        return tuple(sum(col) for col in zip(*extras))



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def progress_extra_status(self, elapsed, interval=None):
        """Text appended to the progress line: tools that write (walktar) report bytes
        written and the rate, now (over 'interval', since the last line) and on average.
        Without 'interval', the final line: the totals only."""
        written = self.written()
        if written is None:
            return ''
        nbytes = written[0]
        if interval is None:
            return written_status(nbytes, elapsed, verb=self.verb)
        status = written_status(nbytes, elapsed, nbytes - self.last_written, interval, len(self.batches), self.verb)
        self.last_written = nbytes
        return status



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def report_progress(self, forceprint=False):
        curtime = self.MPI.Wtime()
        deltaprog = (curtime - self.progress_time)

        if not forceprint:
            if deltaprog < self.options.progress: return

        self.progress_time = curtime
        status = progress_line(self.progress.total_items, self.progress.total_bytes,
                               curtime - self.start_time)
        status += self.progress_extra_status(curtime - self.start_time,
                                             None if forceprint else deltaprog)

        if not forceprint:
            status += ' [dirlen={}, queued={}, idle={}, sends={}, recvs={}, shares={}]'.format(
                format_number(len(self.dirs)), format_number(self.progress.total_queued), len(self.idle),
                format_number(self.nsends), format_number(self.nrecvs), format_number(self.nshares))

        print(status)

        sys.stdout.flush()
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def handle(self, rank, tag, msg):
        """Act on one message from worker 'rank'."""
        if tag == self.tags['dir_reply']:
            # overflow from a full local stack; the worker is still busy
            dirs, payload = msg
            self.update_progress(rank, payload)
            self.dirs.extend(dirs)
            self.ndirs_overflow += len(dirs)
            self.maxnumdirs = max(self.maxnumdirs, len(self.dirs))

        elif tag == self.tags['share_reply']:
            # the answer to our 'share'; the worker is still busy
            dirs, batches, payload = msg
            self.update_progress(rank, payload)
            self.share_pending.discard(rank)
            self.dirs.extend(dirs)
            self.batches.extend(batches)
            self.ndirs_shared += len(dirs)
            self.nbatches_shared += len(batches)
            if not dirs and not batches: self.nshares_empty += 1
            self.maxnumdirs = max(self.maxnumdirs, len(self.dirs))

        elif tag == self.tags['progress']:
            self.update_progress(rank, msg)

        elif tag == self.tags['ready']:
            # its stack is empty and it waits for our answer; a 'share' we sent it will
            # be ignored there, so forget it
            self.update_progress(rank, msg)
            self.share_pending.discard(rank)
            self.idle.append((rank, self.MPI.Wtime()))
            self.idle_set.add(rank)

        else:
            raise RuntimeError('Manager: unexpected tag {} from rank {}'.format(tag, rank))
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def dispatch(self):
        """Answer idle workers' 'ready' with work, while we have both.

        Batches first, one per worker: a batch is already sized as a unit of work, and
        handing one worker several would rebuild the tail. Then directories, up to
        --dirs-per-dispatch each, fewer when there are not enough to go round the idle
        workers.
        """
        while (self.batches or self.dirs) and self.idle:
            if self.batches:
                give = ([], [self.batches.popleft()])
                self.nbatch_dispatches += 1
            else:
                n = max(1, min(self.options.dirs_per_dispatch, len(self.dirs) // len(self.idle)))
                give = (self.dirs[-n:], [])
                del self.dirs[-n:]
            rank, when = self.idle.popleft()
            self.idle_set.discard(rank)
            self.comm.send(give, dest=rank, tag=self.tags['execute']); self.nsends += 1
            self.ndispatches += 1
            wait = self.MPI.Wtime() - when
            self.sum_ready_wait += wait
            self.max_ready_wait = max(self.max_ready_wait, wait)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def request_shares(self):
        """Idle workers and nothing to give them: ask busy workers to share their stacks.

        At most one unanswered 'share' per idle worker, sent to the busy workers that
        last reported the longest stacks. Workers whose last report was an empty stack
        are asked too (it may have grown since), but after the others.
        """
        want = len(self.idle) - len(self.share_pending)
        if want <= 0 or self.dirs or self.batches:
            return
        candidates = [r for r in range(1, self.nranks)
                      if r not in self.idle_set and r not in self.share_pending]
        queued = self.progress.queued
        for rank in heapq.nlargest(want, candidates, key=lambda r: queued[r]):
            self.comm.send(None, dest=rank, tag=self.tags['share']); self.nsends += 1
            self.share_pending.add(rank)
            self.nshares += 1
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):
        Wtime = self.MPI.Wtime

        self.comm.Barrier()
        self.progress_time = self.start_time = Wtime()   # the walk starts now

        # one blocking receive per message, from any worker, any tag (see module doc)
        status = self.MPI.Status()
        ANY_SOURCE, ANY_TAG = self.MPI.ANY_SOURCE, self.MPI.ANY_TAG
        while not self.finished():
            self.report_progress()
            t0 = Wtime()
            msg = self.comm.recv(source=ANY_SOURCE, tag=ANY_TAG, status=status); self.nrecvs += 1
            t1 = Wtime()
            self.handle(status.Get_source(), status.Get_tag(), msg)
            self.dispatch()
            if self.idle and not self.dirs and not self.batches:
                self.request_shares()
            t2 = Wtime()
            self.wait_time += t1 - t0
            self.busy_time += t2 - t1
            self.max_handle_time = max(self.max_handle_time, t2 - t1)

        self.walk_elapsed = Wtime() - self.start_time

        # every worker is waiting on its 'ready': answer each with 'terminate'
        print('  --> Progress loop completed ({} sends / {} recvs)'.format(format_number(self.nsends),
                                                                           format_number(self.nrecvs)))
        print('  --> Maximum # of dirs at once on manager: {}'.format(format_number(self.maxnumdirs)))
        print('  --> Dispatches: {}; directories received as overflow: {}, shared: {} ({} share requests, {} answered empty)'.format(
            format_number(self.ndispatches), format_number(self.ndirs_overflow), format_number(self.ndirs_shared),
            format_number(self.nshares), format_number(self.nshares_empty)))
        written = self.written()
        if self.nbatches_shared or written is not None:
            line = '  --> Batches: {} received in share replies, {} handed out'.format(
                format_number(self.nbatches_shared), format_number(self.nbatch_dispatches))
            if written is not None:
                line += '; {} {} files, {} ({} bytes) in {} {}'.format(
                    self.verb, format_number(written[1]), format_size(written[0]), format_number(written[0]),
                    format_number(written[2]), self.units)
            print(line)
        if self.options.verbose:
            self.print_counters()
        print('  --> Finished dispatch, Terminating ranks')
        while self.idle:
            rank, when = self.idle.popleft()
            self.idle_set.discard(rank)
            self.comm.send(None, dest=rank, tag=self.tags['terminate']); self.nsends += 1
            self.max_terminate_wait = max(self.max_terminate_wait, Wtime() - when)
        if self.options.verbose:
            print('  --> Manager ready -> terminate: the first idle worker waited {:.2f} s for the end'.format(
                self.max_terminate_wait))

        # OK, messages sent, report final progress
        self.report_progress(forceprint=True)

        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def print_counters(self):
        """The Manager's load: how busy it was, and how long it held each 'ready'."""
        loop_time = self.wait_time + self.busy_time
        print('  --> Manager messages: {}; handling {} mean, {} max per message'.format(
            format_number(self.nrecvs), _ms(self.busy_time / max(self.nrecvs, 1)), _ms(self.max_handle_time)))
        print('  --> Manager blocked in recv: {:.2f} s of {:.2f} s in the loop ({:.1f}%)'.format(
            self.wait_time, loop_time, 100 * self.wait_time / loop_time if loop_time else 0))
        print('  --> Manager ready -> execute: {} answered, held {} mean, {} max (includes waiting for work)'.format(
            format_number(self.ndispatches), _ms(self.sum_ready_wait / max(self.ndispatches, 1)),
            _ms(self.max_ready_wait)))
        sys.stdout.flush()
        return
