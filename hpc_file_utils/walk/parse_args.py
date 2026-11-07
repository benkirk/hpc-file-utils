#!/usr/bin/env python3

import argparse
import os



def parse_options(appname=None):

    parser = argparse.ArgumentParser(description='List Defaults CLI')

    # common arguments first
    parser.add_argument('-d', '--dirs', nargs='+', help='List of Directories', required=True)
    parser.add_argument('-p', '--progress', type=int, default=5, help='Frequency to report progress (default:5 seconds, <=0 disables)')
    parser.add_argument('-s', '--spreadsheet', default=None, help='Summary spreadsheet', type=str, required=False)
    parser.add_argument('--summary-prefix', default=None, help='Summary spreadsheet entity prefix', required=False)
    parser.add_argument('-e', '--errors', default=None, help='Error log file name (default: stderr)', type=str, required=False)
    parser.add_argument('-v', '--verbose', action='store_true', help='Print detailed information')
    parser.add_argument('--heap-size', default=500, type=int, required=False, help='Per-rank heap size used for tracking large files / directories')
    parser.add_argument('--threshold-size', default='10MB', type=str, required=False, help='Only consider directories larger than this threshold when tracking mtimes/atimes (string')
    parser.add_argument('--threshold-count', default=10000, type=int, required=False, help='Only consider directories larger than this threshold when tracking mtimes/atimes (count)')
    parser.add_argument('--local-queue-max', default=1000, type=int, required=False, help='Subdirectories a worker keeps for itself; past this it sends the oldest half to the manager (default: 1000)')
    parser.add_argument('--dirs-per-dispatch', default=8, type=int, required=False, help='Directories the manager hands an idle worker at once (default: 8)')
    parser.add_argument('--fail-on-errors', action='store_true', help='Exit with status 2 if any directory or entry could not be scanned (default: exit 0, errors are only counted)')

    # tool-specific arguments follow
    if 'walktar' == appname:
        parser.add_argument('--tar-queue-depth', default=50000, help='Execution thread queue depth.', type=int, required=False)

    args = parser.parse_args()

    # consistency checks
    if args.local_queue_max < 2:
        parser.error('--local-queue-max must be at least 2')
    if args.dirs_per_dispatch < 1:
        parser.error('--dirs-per-dispatch must be at least 1')

    if 0 >= args.progress:
        args.progress = float('inf')

    if '0' == args.threshold_size:
        args.threshold_size = 0
    else:
        from humanfriendly import parse_size
        args.threshold_size = parse_size(args.threshold_size, binary=True)

    for d in args.dirs:
        if not os.path.isdir(d):
            print('ERROR: no such directory: {}'.format(d))
            assert(False)

    # report options
    if args.verbose:
        print('All Options: {}'.format(args))
    else:
        print(f'Processing Directories: {args.dirs}')

    return args



if __name__ == '__main__':
    opts = parse_options()

    print('\n\n',opts)
