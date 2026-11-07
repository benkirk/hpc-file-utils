#!/usr/bin/env python3

from mpi4py import MPI
from manager import Manager
from worker_factory import worker_factory
import os, sys, copy
from parse_args import parse_options

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()


# An uncaught exception on any rank aborts the whole job. Otherwise the other ranks
# wait forever in their next collective or receive (a hang, not a failure).
def abort_on_exception(exc_type, exc, tb):
    sys.__excepthook__(exc_type, exc, tb)
    sys.stdout.flush()
    sys.stderr.flush()
    comm.Abort(1)

sys.excepthook = abort_on_exception


# infer the requested action from the calling executable name
appname = os.path.basename(sys.argv[0])

# options are parsed on rank 0 only; every rank learns whether that worked, so a bad
# option (or --help) ends every rank with rank 0's exit status rather than a hang.
args = None
exit_status = 0
if 0 == rank:
    try:
        args = parse_options(appname)
    except SystemExit as e:          # argparse errors and --help
        exit_status = e.code if isinstance(e.code, int) else 1
    except Exception as e:           # e.g. 'ERROR: no such directory'
        print('ERROR: {}: {}'.format(type(e).__name__, e), file=sys.stderr)
        exit_status = 1
    if args is None and exit_status == 0:
        exit_status = None           # --help: done, successfully

exit_status = comm.bcast(exit_status, root=0)
if exit_status is None:
    sys.exit(0)
if exit_status:
    sys.exit(exit_status)

assert size > 1

comm.Barrier()

# workers on ranks [1,size)
if rank:
    worker = worker_factory(appname)
    worker.run()
    worker.summary()
    walker = worker

# manager on rank 0
else:
    print('Running {} on {} MPI ranks'.format(appname,size))
    sys.stdout.flush()
    manager = Manager(options=args)
    manager.run()
    manager.summary()
    walker = manager

# exit-status policy: summary() summed the error counts on every rank
if walker.options.fail_on_errors and walker.total_errors:
    sys.exit(2)
