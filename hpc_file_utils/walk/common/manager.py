#!/usr/bin/env python3

"""The Manager (rank 0) hands directories to the workers until the walk is done.

Workers walk their own local stack of directories, depth-first, and talk to the Manager
only when it overflows, when it runs dry, or when the Manager asks them to share
(phase 3.5; docs/walk-load-balancing.md, section 5).

Messages (tags in protocol.py; 'payload' is the (items, bytes, queued, extra) tuple):

  worker -> manager
    ready        payload           "My stack is empty and my last directory is done,
                                   give me work". The worker then waits for the answer.
    dir_reply    (dirs, payload)   overflow: the oldest half of a stack that grew past
                                   --local-queue-max.
    share_reply  (dirs, payload)   the answer to a 'share': the oldest half of the
                                   stack, possibly empty.
    progress     payload           running counts, every PROGRESS_INCREMENT entries of
                                   a huge directory, or --progress seconds between ones.
  manager -> worker
    execute      [dir, ...]        the answer to a 'ready': walk these (at most
                                   --dirs-per-dispatch)
    terminate    None              the answer to a 'ready': the walk is done
    share        None              sent to a busy worker while the Manager holds idle
                                   'ready's and has no directories; answered with a
                                   share_reply at the worker's next look (every
                                   directory, and every 1,000 entries of a big one), or
                                   not at all if the worker goes idle first: its 'ready'
                                   cancels the request

How the Manager decides the walk is done:

  1. A worker is idle only while the Manager holds its 'ready' unanswered. A worker
     sends 'ready' only when its local stack is empty and its last directory is done,
     and it then waits for the answer, so it cannot send anything else.
  2. The Manager receives with ANY_SOURCE and ANY_TAG, so every message matches the
     same receive and it gets each worker's messages in the order they were sent
     (MPI's non-overtaking rule applies to messages from one sender that match the
     same receive). When it gets a worker's 'ready', it has already received every
     dir_reply and share_reply that worker sent before it. (The old loop probed each tag separately,
     so it could see a 'ready' before the dir_reply sent just ahead of it, decide
     everyone was idle, and lose or hang on that dir_reply.)
  3. So: no directories queued here and every worker idle means no directory is
     queued, in flight, or being scanned anywhere. The Manager answers each held
     'ready' with 'terminate'.

A 'share' never carries work, so it cannot upset this. A worker that receives one while
it waits for the answer to its 'ready' ignores it (its wait is a receive from rank 0
with ANY_TAG, so it also gets the share before any execute or terminate sent after
it), and the Manager forgets the request when that 'ready' arrives. The Manager has at
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

from hpc_file_utils.walk.common.formatting import format_number
from hpc_file_utils.walk.common.mpiclass import MPIClass
from hpc_file_utils.walk.common.progress import ProgressTally, progress_line

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
        self.nworkers = self.nranks - 1
        self.idle = deque()                            # (rank, when) of each 'ready' we hold, unanswered
        self.idle_set = set()                          # ... just the ranks
        self.share_pending = set()                     # workers with an unanswered 'share'
        self.progress = ProgressTally(self.nranks)
        self.progress_time = self.start_time = MPI.Wtime()

        # where the Manager's directories came from (printed after every walk)
        self.ndispatches = 0         # 'execute' messages
        self.ndirs_overflow = 0      # directories received as overflow (dir_reply)
        self.ndirs_shared = 0        # ... in share_reply
        self.nshares = 0             # 'share' requests sent
        self.nshares_empty = 0       # ... answered with no directories
        self.walk_elapsed = 0.0

        # instrumentation (--verbose)
        self.wait_time = 0.0         # blocked in recv, waiting for any message
        self.busy_time = 0.0         # handling messages and answering 'ready's
        self.max_handle_time = 0.0
        self.nexecute = 0            # 'ready' answered with a directory
        self.sum_ready_wait = 0.0    # ... and how long each was held first
        self.max_ready_wait = 0.0
        self.max_terminate_wait = 0.0
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def finished(self):
        """Nothing queued here and every worker idle: nothing left anywhere (see module doc)."""
        return not self.dirs and len(self.idle) == self.nworkers



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def update_progress(self, rank, payload):
        """payload: the (items, bytes, extra) every worker message carries (protocol.py)."""
        self.progress.update(rank, payload)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def progress_extra_status(self):
        """Derived tools: text for their workers' 'extra' payloads, appended to the line."""
        return ''



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def report_progress(self, forceprint=False):
        curtime = self.MPI.Wtime()
        deltaprog = (curtime - self.progress_time)

        if not forceprint:
            if deltaprog < self.options.progress: return

        self.progress_time = curtime
        status = progress_line(self.progress.total_items, self.progress.total_bytes,
                               curtime - self.start_time)
        status += self.progress_extra_status()

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
            dirs, payload = msg
            self.update_progress(rank, payload)
            self.share_pending.discard(rank)
            self.dirs.extend(dirs)
            self.ndirs_shared += len(dirs)
            if not dirs: self.nshares_empty += 1
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
        """Answer idle workers' 'ready' with directories, while we have both.

        Each gets up to --dirs-per-dispatch, fewer when there are not enough to go round
        the idle workers.
        """
        while self.dirs and self.idle:
            n = max(1, min(self.options.dirs_per_dispatch, len(self.dirs) // len(self.idle)))
            give = self.dirs[-n:]
            del self.dirs[-n:]
            rank, when = self.idle.popleft()
            self.idle_set.discard(rank)
            self.comm.send(give, dest=rank, tag=self.tags['execute']); self.nsends += 1
            self.ndispatches += 1
            wait = self.MPI.Wtime() - when
            self.nexecute += 1
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
        if want <= 0 or self.dirs:
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
            if self.idle and not self.dirs:
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
            format_number(self.nexecute), _ms(self.sum_ready_wait / max(self.nexecute, 1)),
            _ms(self.max_ready_wait)))
        sys.stdout.flush()
        return
