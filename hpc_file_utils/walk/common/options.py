"""Command-line options for the walk tools (no MPI; parsed on rank 0 only): the common
ones here, each tool's own from its Tool (walk/tools.py, common/tool.py)."""

import argparse
import os

from hpc_file_utils.walk.common.filters import add_filter_options, compile_filters, parse_size_option
from hpc_file_utils.walk.tools import TOOLS


def parse_options(appname: str, argv: list[str] | None = None) -> argparse.Namespace:
    """Parse argv (default: sys.argv[1:]) for the tool named appname; args.app names it.

    Usage errors and --help raise SystemExit, as argparse does; a --dirs entry that is
    not a directory raises FileNotFoundError.
    """
    tool = TOOLS[appname]
    # allow_abbrev=False: '--summary x' used to be taken silently as '--summary-prefix x'
    parser = argparse.ArgumentParser(prog=appname, description=tool.description, allow_abbrev=False)

    # common arguments first
    parser.add_argument('-d', '--dirs', nargs='+', help='List of Directories', required=True)
    parser.add_argument('-p', '--progress', type=int, default=5, help='Frequency to report progress (default:5 seconds, <=0 disables)')
    parser.add_argument('-s', '--spreadsheet', default=None, help='Summary spreadsheet', type=str, required=False)
    parser.add_argument('--summary-prefix', default=None, help='Summary spreadsheet entity prefix', required=False)
    parser.add_argument('-v', '--verbose', action='store_true', help='Print detailed information (per-rank counts, Manager counters)')
    parser.add_argument('--heap-size', default=500, type=int, required=False, help='Length of the largest-files/directories lists kept, overall and per user (default: 500)')
    parser.add_argument('--threshold-size', default='10MB', type=str, required=False, help='Only consider directories larger than this threshold when tracking mtimes/atimes (string')
    parser.add_argument('--threshold-count', default=10000, type=int, required=False, help='Only consider directories larger than this threshold when tracking mtimes/atimes (count)')
    parser.add_argument('--local-queue-max', default=1000, type=int, required=False, help='Subdirectories a worker keeps for itself; past this it sends the oldest half to the manager (default: 1000)')
    parser.add_argument('--dirs-per-dispatch', default=8, type=int, required=False, help='Directories the manager hands an idle worker at once (default: 8)')
    parser.add_argument('--batch-files', default=1000, type=int, required=False, help='Files per batch, the unit of work idle ranks can take: walkstat stats a batch, walktar writes it as one tar, walksync compares one and copies one with one rsync (default: 1000)')
    if tool.batches_bytes:
        parser.add_argument('--batch-size', default='16GiB', type=str, required=False, help='Bytes per batch (st_size), the other limit; a bigger file is a batch on its own (default: 16GiB)')
    parser.add_argument('--fail-on-errors', action='store_true', help='Exit with status 2 if any directory or entry could not be scanned (default: exit 0, errors are only counted)')
    parser.add_argument('--profile', action='store_true', help='Every rank writes a cProfile of its run to profile-r{rank}.prof in the working directory (read with python -m pstats)')

    add_filter_options(parser)

    # the tool's own arguments follow
    tool.add_arguments(parser)

    args = parser.parse_args(argv)
    args.app = appname

    # consistency checks
    if args.local_queue_max < 2:
        parser.error('--local-queue-max must be at least 2')
    if args.dirs_per_dispatch < 1:
        parser.error('--dirs-per-dispatch must be at least 1')
    if args.heap_size < 1:
        parser.error('--heap-size must be at least 1')
    if args.batch_files < 1:
        parser.error('--batch-files must be at least 1')

    try:
        if tool.batches_bytes:
            args.batch_size = parse_size_option(args.batch_size, '--batch-size')
            if args.batch_size < 1:
                parser.error('--batch-size must be at least 1 byte')

        if args.progress <= 0:
            args.progress = float('inf')

        if '0' == args.threshold_size:
            args.threshold_size = 0
        else:
            args.threshold_size = parse_size_option(args.threshold_size, '--threshold-size')

        args.filters = compile_filters(args)   # None without a filter
    except ValueError as e:
        parser.error(str(e))

    for d in args.dirs:
        if not os.path.isdir(d):
            raise FileNotFoundError(f'no such directory: {d}')

    tool.check(parser, args)
    return args
