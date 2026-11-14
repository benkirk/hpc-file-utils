"""Console scripts: walkstat, walktar, walksync and walkfind (see [project.scripts] in pyproject.toml).

Rank 0 runs the Manager, every other rank a worker of the requested tool. What differs
between the tools comes from their Tool (walk/tools.py, common/tool.py).
"""

import logging
import os
import sys
import time
from datetime import datetime

from hpc_file_utils.walk.common import log as walk_log
from hpc_file_utils.walk.common.formatting import format_number
from hpc_file_utils.walk.common.options import parse_options
from hpc_file_utils.walk.tools import TOOLS

log = logging.getLogger(__name__)


def main(app, argv=None):
    # Pickled receives go through Probe + Recv, not mpi4py's default matched probe
    # (Mprobe + Mrecv): on derecho a walktar over two nodes hung every time with every
    # rank in PMPI_Mprobe, the Manager's any-source receive never seeing a message that
    # had been sent (perf-1, step B), and the same run with this switch finished; one
    # thread per rank talks to MPI, so the unmatched probe is safe. HFU_RECV_MPROBE=1
    # restores the matched probe, for experiments.
    if os.environ.get('HFU_RECV_MPROBE') != '1':
        import mpi4py
        mpi4py.rc.recv_mprobe = False
    from mpi4py import MPI

    tool = TOOLS[app]
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()
    walk_log.setup(rank)                 # diagnostics on stderr, '[rank] LEVEL ...' (common/log.py)

    # An uncaught exception on any rank aborts the whole job. Otherwise the other ranks
    # wait forever in their next collective or receive (a hang, not a failure).
    def abort_on_exception(exc_type, exc, tb):
        sys.__excepthook__(exc_type, exc, tb)
        sys.stdout.flush()
        sys.stderr.flush()
        comm.Abort(1)

    sys.excepthook = abort_on_exception

    # a rank that seems stuck can be asked where it is: 'kill -USR1 <pid>' writes every
    # thread's Python stack to its stderr and the rank goes on (perf-1, step B: a run that
    # hung at 2 nodes with every rank spinning in MPI's receive had no other way to say
    # which receive)
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)

    # options are parsed on rank 0 only; every rank learns whether that worked, so a bad
    # option (or --help) ends every rank with rank 0's exit status rather than a hang.
    args = None
    exit_status = 0
    if rank == 0:
        try:
            args = parse_options(app, argv)
        except SystemExit as e:          # argparse errors and --help
            exit_status = e.code if isinstance(e.code, int) else 1
        except Exception as e:           # e.g. 'no such directory'
            log.error('%s: %s', type(e).__name__, e)
            exit_status = 1
        if args is None and exit_status == 0:
            exit_status = None           # --help: done, successfully

    exit_status = comm.bcast(exit_status, root=0)
    if exit_status is None:
        return 0
    if exit_status:
        return rank_exit_status(comm, rank, exit_status)

    if size < 2:
        if rank == 0:
            log.error('%s needs at least 2 MPI ranks (a manager and a worker)', app)
        return 2

    cpus = cpus_per_rank(comm)
    comm.Barrier()

    # workers on ranks [1,size)
    if rank:
        walker = tool.make_worker()

    # manager on rank 0
    else:
        from hpc_file_utils.walk.common.manager import Manager
        if args.verbose:
            print(f'All Options: {args}')
        else:
            print(f'Processing Directories: {args.dirs}')
        for line in tool.banner(args):
            print(line)
        print(f'Running {app} on {size} MPI ranks', flush=True)
        if cpus is not None:
            print(f"CPUs per rank: {cpus} (the launcher's binding; a rank's threads share them: "
                  f"mpiexec --cpu-bind depth --depth 2 gives each rank two cores, the walk README's 'CPU binding')", flush=True)
        walker = Manager(options=args)

    walk_log.setup(rank, walker.options.verbose)   # now that every rank has the options
    run_walker(walker, rank)

    stats = walker.collect()     # collective; the combined stats on rank 0 only
    if rank == 0:
        report(walker, stats, tool)

    # exit-status policy: collect() summed the error counts on every rank
    return rank_exit_status(comm, rank, tool.exit_status(walker.total_errors, walker.options))


# rank 0 waits this long, after every rank finalized MPI, before it exits with a non-zero status
EXIT_GRACE_SECONDS = float(os.environ.get('HFU_EXIT_GRACE', 2))


def rank_exit_status(comm, rank, status):
    """What this rank returns: 'status' on rank 0, 0 on the others.

    Every rank knows the status, but Cray PALS SIGTERMs every rank still running once one
    exits non-zero, and then reports 143 for the job, not the status (cgd-walksync, 4F:
    every rank exited 1 at once). So only rank 0 exits with it, and last: after a Barrier
    every rank finalizes MPI; the workers leave at once (os._exit, their output flushed and
    their files closed by now), and rank 0 waits EXIT_GRACE_SECONDS before it returns. A
    Barrier alone was not enough on derecho: rank 0 exited 2 while a worker was still in its
    shutdown, which PALS killed (143). With status 0 nothing is killed: a plain return."""
    comm.Barrier()
    if not status:
        return 0
    from mpi4py import MPI
    sys.stdout.flush()
    sys.stderr.flush()
    MPI.Finalize()
    if rank != 0:
        os._exit(0)
    time.sleep(EXIT_GRACE_SECONDS)
    return status


def cpus_per_rank(comm):
    """The CPUs each rank may run on (its affinity mask, which the launcher's binding
    sets: PALS gives each rank one by default, 'mpiexec --cpu-bind depth --depth N' N),
    gathered on rank 0 as '4' or '1-4'; None where there is no mask to read (macOS). The
    binding bounds what a rank's threads (walktar's readers, walkstat's --stat-threads)
    can do, so every run records it (perf-1, step A)."""
    getaffinity = getattr(os, 'sched_getaffinity', None)
    counts = comm.gather(len(getaffinity(0)) if getaffinity else None, root=0)
    if counts is None or None in counts:
        return None
    lo, hi = min(counts), max(counts)
    return str(lo) if lo == hi else f'{lo}-{hi}'


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
        profile.dump_stats(f'profile-r{rank:03d}.prof')


def report(manager, stats, tool):
    """Rank 0, after the walk: a cross-check of the counts, the balance lines, the
    tool's own reports, and the last lines."""
    from hpc_file_utils.walk.common.report import balance_lines, rank_lines

    options = manager.options
    # every worker's last 'ready' carried its final counts, so these must agree
    if (manager.progress.total_items, manager.progress.total_bytes) != (stats.num_items, stats.total_size):
        log.warning('the Manager counted %d items / %d bytes, the workers %d / %d',
                    manager.progress.total_items, manager.progress.total_bytes, stats.num_items, stats.total_size)

    print('\n'.join(balance_lines(stats, manager.walk_elapsed)))
    if options.verbose:
        print('\n'.join(rank_lines(stats)), flush=True)

    tool.report(manager, stats)

    if stats.num_errors:
        print(f'--> {format_number(stats.num_errors)} entries could not be scanned (see stderr)')
    print(f"--> Done at {datetime.now().isoformat(sep=' ', timespec='seconds')}", flush=True)


def walkstat():
    return main('walkstat')


def walktar():
    return main('walktar')


def walksync():
    return main('walksync')


def walkfind():
    return main('walkfind')
