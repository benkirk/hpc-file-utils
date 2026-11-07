"""Console scripts: walkstat and walktar (see [project.scripts] in pyproject.toml).

Rank 0 runs the Manager, every other rank a worker of the requested tool.
"""

import sys

from hpc_file_utils.walk.common.options import parse_options


def make_worker(app):
    if app == 'walkstat':
        from hpc_file_utils.walk.stat.worker import StatWorker
        return StatWorker()
    if app == 'walktar':
        from hpc_file_utils.walk.tar.worker import TarWorker
        return TarWorker()
    raise NotImplementedError('unrecognized tool: {}'.format(app))


def main(app, argv=None):
    from mpi4py import MPI

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

    # options are parsed on rank 0 only; every rank learns whether that worked, so a bad
    # option (or --help) ends every rank with rank 0's exit status rather than a hang.
    args = None
    exit_status = 0
    if 0 == rank:
        try:
            args = parse_options(app, argv)
        except SystemExit as e:          # argparse errors and --help
            exit_status = e.code if isinstance(e.code, int) else 1
        except Exception as e:           # e.g. 'ERROR: no such directory'
            print('ERROR: {}: {}'.format(type(e).__name__, e), file=sys.stderr)
            exit_status = 1
        if args is None and exit_status == 0:
            exit_status = None           # --help: done, successfully

    exit_status = comm.bcast(exit_status, root=0)
    if exit_status is None:
        return 0
    if exit_status:
        return exit_status

    assert size > 1

    comm.Barrier()

    # workers on ranks [1,size)
    if rank:
        walker = make_worker(app)
        walker.run()
        walker.summary()

    # manager on rank 0
    else:
        from hpc_file_utils.walk.common.manager import Manager
        print('Running {} on {} MPI ranks'.format(app, size))
        sys.stdout.flush()
        walker = Manager(options=args)
        walker.run()
        walker.summary()

    # exit-status policy: summary() summed the error counts on every rank
    if walker.options.fail_on_errors and walker.total_errors:
        return 2
    return 0


def walkstat():
    return main('walkstat')


def walktar():
    return main('walktar')
