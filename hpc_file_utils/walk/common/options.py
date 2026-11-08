"""Command-line options for walkstat and walktar (no MPI; parsed on rank 0 only)."""

import argparse
import os

from hpc_file_utils.walk.common.filters import add_filter_options, compile_filters

DESCRIPTIONS = {
    'walkstat': 'Walk directory trees in parallel (MPI) and summarize their usage.',
    'walktar':  'Walk directory trees in parallel (MPI) and archive them: one tar file per batch of files, plus one per rank for the directories.',
}


def parse_options(appname=None, argv=None):
    """Parse argv (default: sys.argv[1:]).

    Usage errors and --help raise SystemExit, as argparse does; a --dirs entry that is
    not a directory raises FileNotFoundError.
    """
    # allow_abbrev=False: '--summary x' used to be taken silently as '--summary-prefix x'
    parser = argparse.ArgumentParser(prog=appname, description=DESCRIPTIONS.get(appname),
                                     allow_abbrev=False)

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
    parser.add_argument('--batch-files', default=1000, type=int, required=False, help='Files per batch, the unit of work idle ranks can take: walkstat stats a batch, walktar writes it as one tar (default: 1000)')
    parser.add_argument('--fail-on-errors', action='store_true', help='Exit with status 2 if any directory or entry could not be scanned (default: exit 0, errors are only counted)')

    add_filter_options(parser)

    # tool-specific arguments follow
    if 'walktar' == appname:
        parser.add_argument('--batch-size', default='16GiB', type=str, required=False, help='Bytes per batch (st_size), the other limit; a bigger file is a batch on its own (default: 16GiB)')

    args = parser.parse_args(argv)

    # consistency checks
    if args.local_queue_max < 2:
        parser.error('--local-queue-max must be at least 2')
    if args.dirs_per_dispatch < 1:
        parser.error('--dirs-per-dispatch must be at least 1')
    if args.heap_size < 1:
        parser.error('--heap-size must be at least 1')

    if args.batch_files < 1:
        parser.error('--batch-files must be at least 1')
    if 'walktar' == appname:
        from humanfriendly import InvalidSize, parse_size
        try:
            args.batch_size = parse_size(args.batch_size, binary=True)
        except InvalidSize as e:
            parser.error('--batch-size: {}'.format(e))
        if args.batch_size < 1:
            parser.error('--batch-size must be at least 1 byte')

    if 0 >= args.progress:
        args.progress = float('inf')

    if '0' == args.threshold_size:
        args.threshold_size = 0
    else:
        from humanfriendly import InvalidSize, parse_size
        try:
            args.threshold_size = parse_size(args.threshold_size, binary=True)
        except InvalidSize as e:
            parser.error('--threshold-size: {}'.format(e))

    try:
        args.filters = compile_filters(args)   # None without a filter
    except ValueError as e:
        parser.error(str(e))

    for d in args.dirs:
        if not os.path.isdir(d):
            raise FileNotFoundError('no such directory: {}'.format(d))

    return args
