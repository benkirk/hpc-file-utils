#!/usr/bin/env python3

"""The Manager (rank 0) hands directories to the workers until the walk is done.

Messages (tags in MPIClass.tags):

  worker -> manager
    ready      [items, bytes]          "I am idle, give me work". Sent with sendrecv, so
                                       the worker then blocks until the Manager answers.
    dir_reply  [dir, ..., items, bytes] subdirectories found while scanning: every
                                       --max-dirs-per-send of them mid-directory, and
                                       the rest when the directory is done.
    progress   [items, bytes]          running counts, every PROGRESS_INCREMENT entries
                                       of a huge directory.
  manager -> worker (the answer to a 'ready')
    execute    dirname                 scan this directory
    terminate  None                    the walk is done

How the Manager decides the walk is done:

  1. A worker is idle only while the Manager holds its 'ready' unanswered. A worker
     that sent 'ready' is blocked in sendrecv, so it cannot send anything else.
  2. The Manager receives from one worker with ANY_TAG, so it gets that worker's
     messages in the order they were sent (MPI's non-overtaking rule applies to
     messages that match the same receive). When it gets a worker's 'ready', it has
     already received every dir_reply that worker sent before it. (The old loop
     probed each tag separately, so it could see a 'ready' before the dir_reply sent
     just ahead of it, decide everyone was idle, and lose or hang on that dir_reply.)
  3. So: no directories queued here and every worker idle means no directory is
     queued, in flight, or being scanned anywhere. The Manager answers each held
     'ready' with 'terminate'.

Fairness: each pass of the loop probes every worker in turn and receives at most one
message from each, so every rank is heard from on every pass and a busy worker's
stream of dir_reply/progress messages cannot crowd out the others.
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
        self.dirs = self.options.dirs                 # directories waiting for a worker
        self.workers = list(range(1, self.nranks))
        self.idle = deque()                            # workers whose 'ready' we hold, unanswered
        self.progress_sizes = [0 for p in range(0,self.nranks)]
        self.progress_counts = [0 for p in range(0,self.nranks)]
        self.progress_time = self.start_time = MPI.Wtime()
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def finished(self):
        """Nothing queued here and every worker idle: nothing left anywhere (see module doc)."""
        return not self.dirs and len(self.idle) == len(self.workers)



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def update_progress(self, rank, counts):
        """counts: the [items, bytes] a worker appends to every message."""
        self.progress_sizes[rank] = counts.pop()
        self.progress_counts[rank] = counts.pop()
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
            status += ' [dirlen={}, sends={}, recvs={}]'.format(format_number(len(self.dirs)),
                                                                format_number(self.nsends),
                                                                format_number(self.nrecvs))

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
        msg = self.comm.recv(source=rank, tag=tag); self.nrecvs += 1

        if tag == self.tags['dir_reply']:
            # subdirectories to hand out; the worker is still busy
            assert len(msg) >= 2
            self.update_progress(rank, msg)
            self.dirs.extend(msg)
            self.maxnumdirs = max(self.maxnumdirs, len(self.dirs))

        elif tag == self.tags['progress']:
            assert len(msg) == 2
            self.update_progress(rank, msg)

        elif tag == self.tags['ready']:
            # the worker is now blocked in sendrecv, waiting for our answer
            assert len(msg) == 2
            self.update_progress(rank, msg)
            self.idle.append(rank)

        else:
            raise RuntimeError('Manager: unexpected tag {} from rank {}'.format(tag, rank))

        return True



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def dispatch(self):
        """Answer idle workers' 'ready' with a directory, while we have both."""
        while self.dirs and self.idle:
            rank = self.idle.popleft()
            self.comm.send(self.dirs.pop(), dest=rank, tag=self.tags['execute']); self.nsends += 1
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):

        self.comm.Barrier()

        # one pass = one look at every worker, in rank order
        while not self.finished():
            self.report_progress()

            heard = 0
            for rank in self.workers:
                if self.receive_from(rank):
                    heard += 1
                    self.dispatch()

            if not heard:
                time.sleep(IDLE_SLEEP)

        # every worker is waiting on its 'ready': answer each with 'terminate'
        print('  --> Progress loop completed ({} sends / {} recvs)'.format(format_number(self.nsends),
                                                                           format_number(self.nrecvs)))
        print('  --> Maximum # of dirs at once on manager: {}'.format(format_number(self.maxnumdirs)))
        print('  --> Finished dispatch, Terminating ranks')
        while self.idle:
            self.comm.send(None, dest=self.idle.popleft(), tag=self.tags['terminate']); self.nsends += 1

        # OK, messages sent, report final progress
        self.report_progress(forceprint=True)

        return
