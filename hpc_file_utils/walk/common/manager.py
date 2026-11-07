#!/usr/bin/env python3

"""The Manager (rank 0) hands directories to the workers until the walk is done.

Messages (tags in protocol.py; 'payload' is the (items, bytes, extra) progress tuple):

  worker -> manager
    ready      payload                 "I am idle, give me work". Sent with sendrecv, so
                                       the worker then blocks until the Manager answers.
    dir_reply  (dirs, payload)         subdirectories found while scanning: every
                                       --max-dirs-per-send of them mid-directory, and
                                       the rest when the directory is done.
    progress   payload                 running counts, every PROGRESS_INCREMENT entries
                                       of a huge directory.
  manager -> worker (the answer to a 'ready')
    execute    dirname                 scan this directory
    terminate  None                    the walk is done

How the Manager decides the walk is done:

  1. A worker is idle only while the Manager holds its 'ready' unanswered. A worker
     that sent 'ready' is blocked in sendrecv, so it cannot send anything else.
  2. The Manager receives with ANY_SOURCE and ANY_TAG, so every message matches the
     same receive and it gets each worker's messages in the order they were sent
     (MPI's non-overtaking rule applies to messages from one sender that match the
     same receive). When it gets a worker's 'ready', it has already received every
     dir_reply that worker sent before it. (The old loop probed each tag separately,
     so it could see a 'ready' before the dir_reply sent just ahead of it, decide
     everyone was idle, and lose or hang on that dir_reply.)
  3. So: no directories queued here and every worker idle means no directory is
     queued, in flight, or being scanned anywhere. The Manager answers each held
     'ready' with 'terminate'.

Fairness: MPI promises no order between senders, but the Manager takes every message
as it comes, whatever its tag, and each costs it microseconds, so a busy worker's
dir_reply/progress messages delay a 'ready' only briefly. (One blocking receive per
message replaced a pass that probed every worker in turn: at 384 ranks over 4 nodes
those probes kept the Manager busy and made workers wait; see walk/README.md.)
"""

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
        from mpi4py import MPI
        MPIClass.__init__(self,options)
        self.MPI = MPI
        self.nsends = 0
        self.nrecvs = 0
        self.dirs = list(self.options.dirs)           # directories waiting for a worker
        self.nworkers = self.nranks - 1
        self.idle = deque()                            # (rank, when) of each 'ready' we hold, unanswered
        self.progress = ProgressTally(self.nranks)
        self.progress_time = self.start_time = MPI.Wtime()

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
            status += ' [dirlen={}, sends={}, recvs={}]'.format(format_number(len(self.dirs)),
                                                                format_number(self.nsends),
                                                                format_number(self.nrecvs))

        print(status)

        sys.stdout.flush()
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def handle(self, rank, tag, msg):
        """Act on one message from worker 'rank'."""
        if tag == self.tags['dir_reply']:
            # subdirectories to hand out; the worker is still busy
            dirs, payload = msg
            self.update_progress(rank, payload)
            self.dirs.extend(dirs)
            self.maxnumdirs = max(self.maxnumdirs, len(self.dirs))

        elif tag == self.tags['progress']:
            self.update_progress(rank, msg)

        elif tag == self.tags['ready']:
            # the worker is now blocked in sendrecv, waiting for our answer
            self.update_progress(rank, msg)
            self.idle.append((rank, self.MPI.Wtime()))

        else:
            raise RuntimeError('Manager: unexpected tag {} from rank {}'.format(tag, rank))
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def dispatch(self):
        """Answer idle workers' 'ready' with a directory, while we have both."""
        while self.dirs and self.idle:
            rank, when = self.idle.popleft()
            self.comm.send(self.dirs.pop(), dest=rank, tag=self.tags['execute']); self.nsends += 1
            wait = self.MPI.Wtime() - when
            self.nexecute += 1
            self.sum_ready_wait += wait
            self.max_ready_wait = max(self.max_ready_wait, wait)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):
        Wtime = self.MPI.Wtime

        self.comm.Barrier()

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
            t2 = Wtime()
            self.wait_time += t1 - t0
            self.busy_time += t2 - t1
            self.max_handle_time = max(self.max_handle_time, t2 - t1)

        # every worker is waiting on its 'ready': answer each with 'terminate'
        print('  --> Progress loop completed ({} sends / {} recvs)'.format(format_number(self.nsends),
                                                                           format_number(self.nrecvs)))
        print('  --> Maximum # of dirs at once on manager: {}'.format(format_number(self.maxnumdirs)))
        if self.options.verbose:
            self.print_counters()
        print('  --> Finished dispatch, Terminating ranks')
        while self.idle:
            rank, when = self.idle.popleft()
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
