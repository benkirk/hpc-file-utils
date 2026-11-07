#!/usr/bin/env python3

"""The Manager (rank 0) hands directories to the workers until the walk is done.

Workers walk their own local queue and talk to the Manager only when it fills up,
when it runs dry, or when the Manager asks them to share (docs/walk-load-balancing.md,
section 5). Every worker -> manager message is a tuple (counts, dirs), where counts is
(items, bytes, local queue length) and dirs is a list of directories or None.

Messages (tags in MPIClass.tags):

  worker -> manager
    ready        (counts, None)   "My local queue is empty, give me work". The worker
                                  then blocks in recv until the Manager answers.
    dir_reply    (counts, dirs)   overflow: the oldest half of a local queue that grew
                                  past --local-queue-max.
    share_reply  (counts, dirs)   the answer to a 'share': the oldest half of the local
                                  queue, possibly empty.
    progress     (counts, None)   running counts, every PROGRESS_INCREMENT entries of a
                                  huge directory or --progress seconds between ones.
  manager -> worker
    execute      [dir, ...]       the answer to a 'ready': walk these (--dirs-per-dispatch
                                  at most)
    terminate    None             the answer to a 'ready': the walk is done
    share        None             sent to a busy worker when the Manager has idle workers
                                  and no directories; answered with share_reply at the
                                  worker's next poll point, or not at all if the worker
                                  goes idle first (its 'ready' then cancels the request)

How the Manager decides the walk is done:

  1. A worker is idle only while the Manager holds its 'ready' unanswered. A worker
     sends 'ready' only when its local queue is empty and its last directory is
     finished, and it then blocks in recv, so it cannot send anything else.
  2. The Manager receives from one worker with ANY_TAG, so it gets that worker's
     messages in the order they were sent (MPI's non-overtaking rule applies to
     messages that match the same receive). When it gets a worker's 'ready', it has
     already received every dir_reply and share_reply that worker sent before it.
  3. So: no directories queued here and every worker idle means no directory is
     queued, in flight, or being scanned anywhere. The Manager answers each held
     'ready' with 'terminate'.

A 'share' never carries work, so it cannot upset this: a worker that receives one
while it waits for the answer to its 'ready' just ignores it, and the Manager forgets
the request when that 'ready' arrives. The Manager sends at most one unanswered
'share' per worker.

Fairness: each pass of the loop probes every worker in turn and receives at most one
message from each, so every rank is heard from on every pass and a busy worker's
stream of messages cannot crowd out the others.
"""

from mpi4py import MPI
from mpiclass import MPIClass, format_size, format_number, format_timespan
import sys
import time
from collections import deque
from datetime import datetime

# when a pass over the workers finds no messages, yield the CPU this long (seconds)
IDLE_SLEEP = 0.0001


################################################################################
class Manager(MPIClass):

    #~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self,options=None):
        MPIClass.__init__(self,options)
        self.nsends = 0
        self.nrecvs = 0
        self.dirs = list(self.options.dirs)           # directories waiting for a worker
        self.workers = list(range(1, self.nranks))
        self.idle = deque()                            # workers whose 'ready' we hold, unanswered
        self.idle_set = set()
        self.share_pending = set()                     # workers with an unanswered 'share'
        self.queue_lengths = [0 for p in range(0,self.nranks)]   # last reported local queue length
        self.progress_sizes = [0 for p in range(0,self.nranks)]
        self.progress_counts = [0 for p in range(0,self.nranks)]
        self.progress_time = self.start_time = MPI.Wtime()
        # statistics: where the Manager's directories came from, and how busy it was
        self.nshares = 0                 # 'share' requests sent
        self.nshares_empty = 0           # ... answered with no directories
        self.ndirs_overflow = 0          # directories received as overflow (dir_reply)
        self.ndirs_shared = 0            # directories received in share_reply
        self.ndispatches = 0             # 'execute' messages
        self.npasses = 0                 # passes over the workers
        self.npasses_heard = 0           # ... that received at least one message
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def finished(self):
        """Nothing queued here and every worker idle: nothing left anywhere (see module doc)."""
        return not self.dirs and len(self.idle) == len(self.workers)



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def update_progress(self, rank, counts):
        """counts: the (items, bytes, queue length) a worker sends with every message."""
        self.progress_counts[rank], self.progress_sizes[rank], self.queue_lengths[rank] = counts
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def report_progress(self, forceprint=False):
        curtime = MPI.Wtime()
        deltaprog = (curtime - self.progress_time)

        if not forceprint:
            if deltaprog < self.options.progress: return

        self.progress_time = curtime
        elapsed = (curtime - self.start_time)
        self.progress_counts[0] = 0
        self.progress_sizes[0] = 0
        total_count = sum(self.progress_counts)
        total_size = sum(self.progress_sizes)
        self.progress_counts[0] = total_count
        self.progress_sizes[0] = total_size
        status = '[{}] Walked {} items / {} in {} ({} items/sec)'.format(datetime.now().isoformat(sep=' ', timespec='seconds'),
                                                                                          format_number(total_count),
                                                                                          format_size(total_size),
                                                                                          format_timespan(elapsed),
                                                                                          format_number(int(float(total_count)/elapsed)))

        if not forceprint:
            status += ' [dirlen={}, queued={}, idle={}, sends={}, recvs={}, shares={}]'.format(
                format_number(len(self.dirs)),
                format_number(sum(self.queue_lengths)),
                len(self.idle),
                format_number(self.nsends),
                format_number(self.nrecvs),
                format_number(self.nshares))

        print(status)

        sys.stdout.flush()
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def receive_from(self, rank):
        """Receive and handle the next message from 'rank', if one is waiting.

        ANY_TAG from a single source: that rank's messages arrive in the order sent.
        Returns True if a message was handled.
        """
        status = MPI.Status()
        if not self.comm.iprobe(source=rank, tag=MPI.ANY_TAG, status=status):
            return False

        tag = status.Get_tag()
        counts, dirs = self.comm.recv(source=rank, tag=tag); self.nrecvs += 1
        self.update_progress(rank, counts)

        if tag == self.tags['dir_reply']:
            # overflow from a full local queue; the worker is still busy
            self.dirs.extend(dirs)
            self.ndirs_overflow += len(dirs)

        elif tag == self.tags['share_reply']:
            # the answer to our 'share'; the worker is still busy
            self.share_pending.discard(rank)
            self.dirs.extend(dirs)
            self.ndirs_shared += len(dirs)
            if not dirs: self.nshares_empty += 1

        elif tag == self.tags['progress']:
            pass

        elif tag == self.tags['ready']:
            # the worker's local queue is empty and it is blocked in recv, waiting for us.
            # a 'share' we sent it will be ignored there, so forget it.
            self.share_pending.discard(rank)
            self.queue_lengths[rank] = 0
            self.idle.append(rank)
            self.idle_set.add(rank)

        else:
            raise RuntimeError('Manager: unexpected tag {} from rank {}'.format(tag, rank))

        self.maxnumdirs = max(self.maxnumdirs, len(self.dirs))
        return True



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def dispatch(self):
        """Answer idle workers' 'ready' with directories, while we have both.

        Each gets up to --dirs-per-dispatch, fewer when there are not enough to go
        round the idle workers.
        """
        while self.dirs and self.idle:
            n = max(1, min(self.options.dirs_per_dispatch, len(self.dirs) // len(self.idle)))
            give = self.dirs[-n:]
            del self.dirs[-n:]
            rank = self.idle.popleft()
            self.idle_set.discard(rank)
            self.comm.send(give, dest=rank, tag=self.tags['execute']); self.nsends += 1
            self.ndispatches += 1
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def request_shares(self):
        """Idle workers and nothing to give them: ask busy workers to share their queues.

        One unanswered 'share' per worker, to the workers that last reported the
        longest local queues, at most as many as there are idle workers. Workers whose
        last report was an empty queue are asked too (their queue may have grown since),
        but after the others.
        """
        if not self.idle or self.dirs:
            return
        candidates = [r for r in self.workers if r not in self.idle_set and r not in self.share_pending]
        if not candidates:
            return
        candidates.sort(key=lambda r: self.queue_lengths[r], reverse=True)
        for rank in candidates[:len(self.idle)]:
            self.comm.send(None, dest=rank, tag=self.tags['share']); self.nsends += 1
            self.share_pending.add(rank)
            self.nshares += 1
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):

        self.comm.Barrier()
        self.progress_time = self.start_time = MPI.Wtime()

        # one pass = one look at every worker, in rank order
        while not self.finished():
            self.report_progress()

            heard = 0
            for rank in self.workers:
                if self.receive_from(rank):
                    heard += 1
                    self.dispatch()

            self.request_shares()

            self.npasses += 1
            if heard:
                self.npasses_heard += 1
            else:
                time.sleep(IDLE_SLEEP)

        self.walk_elapsed = MPI.Wtime() - self.start_time

        # every worker is waiting on its 'ready': answer each with 'terminate'
        print('  --> Progress loop completed ({} sends / {} recvs)'.format(format_number(self.nsends),
                                                                           format_number(self.nrecvs)))
        print('  --> Maximum # of dirs at once on manager: {}'.format(format_number(self.maxnumdirs)))
        print('  --> Dispatches: {}; directories received as overflow: {}, shared: {} ({} share requests, {} answered empty)'.format(
            format_number(self.ndispatches), format_number(self.ndirs_overflow), format_number(self.ndirs_shared),
            format_number(self.nshares), format_number(self.nshares_empty)))
        print('  --> Manager occupancy: {:.1%} of {} passes received a message'.format(
            self.npasses_heard / max(1, self.npasses), format_number(self.npasses)))
        print('  --> Finished dispatch, Terminating ranks')
        while self.idle:
            rank = self.idle.popleft()
            self.idle_set.discard(rank)
            self.comm.send(None, dest=rank, tag=self.tags['terminate']); self.nsends += 1

        # OK, messages sent, report final progress
        self.report_progress(forceprint=True)

        return
