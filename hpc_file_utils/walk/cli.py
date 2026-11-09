"""Console scripts: walkstat, walktar and walksync (see [project.scripts] in pyproject.toml).

Rank 0 runs the Manager, every other rank a worker of the requested tool. What differs
between the tools comes from their Tool (walk/tools.py, common/tool.py).
"""

import sys
from datetime import datetime

from hpc_file_utils.walk.common.formatting import format_number
from hpc_file_utils.walk.common.options import parse_options
from hpc_file_utils.walk.tools import TOOLS


def main(app, argv=None):
    from mpi4py import MPI

    tool = TOOLS[app]
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

    if size < 2:
        if 0 == rank:
            print('ERROR: {} needs at least 2 MPI ranks (a manager and a worker)'.format(app), file=sys.stderr)
        return 2

    comm.Barrier()

    # workers on ranks [1,size)
    if rank:
        walker = tool.make_worker()

    # manager on rank 0
    else:
        from hpc_file_utils.walk.common.manager import Manager
        if args.verbose:
            print('All Options: {}'.format(args))
        else:
            print('Processing Directories: {}'.format(args.dirs))
        for line in tool.banner(args):
            print(line)
        print('Running {} on {} MPI ranks'.format(app, size))
        sys.stdout.flush()
        walker = Manager(options=args)

    run_walker(walker, rank)

    stats = walker.collect()     # collective; the combined stats on rank 0 only
    if 0 == rank:
        report(walker, stats, tool)

    # exit-status policy: collect() summed the error counts on every rank
    return tool.exit_status(walker.total_errors, walker.options)


def run_walker(walker, rank):
    """The rank's walk (the Manager's loop, or a worker's), under cProfile with --profile:
    profile-r{rank}.prof in the working directory, for python -m pstats."""
    if not walker.options.profile:
        walker.run()
        return
    import cProfile
    profile = cProfile.Profile()
    profile.enable()
    try:
        walker.run()
    finally:
        profile.disable()
        profile.dump_stats('profile-r{:03d}.prof'.format(rank))


def report(manager, stats, tool):
    """Rank 0, after the walk: a cross-check of the counts, the balance lines, the
    tool's own reports, and the last lines."""
    from hpc_file_utils.walk.common.report import balance_lines, rank_lines

    options = manager.options
    # every worker's last 'ready' carried its final counts, so these must agree
    if (manager.progress.total_items, manager.progress.total_bytes) != (stats.num_items, stats.total_size):
        print('WARNING: the Manager counted {} items / {} bytes, the workers {} / {}'.format(
            manager.progress.total_items, manager.progress.total_bytes, stats.num_items, stats.total_size),
            file=sys.stderr)

    print('\n'.join(balance_lines(stats, manager.walk_elapsed)))
    if options.verbose:
        print('\n'.join(rank_lines(stats)))
    sys.stdout.flush()

    tool.report(manager, stats)

    if stats.num_errors:
        print('--> {} entries could not be scanned (see stderr)'.format(format_number(stats.num_errors)))
    print('--> Done at {}'.format(datetime.now().isoformat(sep=' ', timespec='seconds')))
    sys.stdout.flush()


def walkstat():
    return main('walkstat')


def walktar():
    return main('walktar')


def walksync():
    return main('walksync')
