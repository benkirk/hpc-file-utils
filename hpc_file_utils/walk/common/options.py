"""Command-line options for walkstat and walktar (no MPI; parsed on rank 0 only)."""

import argparse
import os

DESCRIPTIONS = {
    'walkstat': 'Walk directory trees in parallel (MPI) and summarize their usage.',
    'walktar':  'Walk directory trees in parallel (MPI) and archive them into per-rank tar files.',
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
    parser.add_argument('--max-dirs-per-send', default=200, type=int, required=False, help='Subdirectories a worker collects before sending them to the manager (default: 200)')
    parser.add_argument('--fail-on-errors', action='store_true', help='Exit with status 2 if any directory or entry could not be scanned (default: exit 0, errors are only counted)')

    # tool-specific arguments follow
    if 'walktar' == appname:
        parser.add_argument('--tar-queue-depth', default=50000, help='Execution thread queue depth.', type=int, required=False)

    args = parser.parse_args(argv)

    # consistency checks
    if args.max_dirs_per_send < 1:
        parser.error('--max-dirs-per-send must be at least 1')
    if args.heap_size < 1:
        parser.error('--heap-size must be at least 1')

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

    for d in args.dirs:
        if not os.path.isdir(d):
            raise FileNotFoundError('no such directory: {}'.format(d))

    return args
