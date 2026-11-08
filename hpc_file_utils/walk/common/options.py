"""Command-line options for walkstat, walktar and walksync (no MPI; parsed on rank 0 only)."""

import argparse
import os
import re
import shlex
import shutil
import subprocess

from hpc_file_utils.walk.common.filters import add_filter_options, compile_filters

DESCRIPTIONS = {
    'walkstat': 'Walk directory trees in parallel (MPI) and summarize their usage.',
    'walktar':  'Walk directory trees in parallel (MPI) and archive them: one tar file per batch of files, plus one per rank for the directories.',
    'walksync': 'Walk a directory tree in parallel (MPI) and copy what is new or changed to a destination, '
                'with batched rsync --files-from. Never deletes; hard links, deletions and final '
                'directory modes and times are left to a final serial rsync -aHAX --delete.',
}

# walksync's rsync for each copy batch; --rsync-opts are appended. Du+w: batches on
# different ranks create the same parent directories, and a read-only source's would
# refuse the files of the next batch.
RSYNC_OPTS = ['-aAX', '--numeric-ids', '--chmod=Du+w']


def parse_rsync_capabilities(version_text):
    """(acls, xattrs) from 'rsync --version': False only for an explicit 'no ACLs' /
    'no xattrs' in its Capabilities list (conda-forge's Linux rsync has no ACLs); an
    unfamiliar format counts as supported, and rsync itself will say if it is not."""
    acls = not re.search(r'\bno ACLs\b', version_text)
    xattrs = not re.search(r'\bno xattrs\b', version_text)
    return acls, xattrs


def rsync_capabilities(rsync):
    """(acls, xattrs) the rsync at path 'rsync' was built with."""
    try:
        out = subprocess.run([rsync, '--version'], capture_output=True, text=True, errors='replace',
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ''
    return parse_rsync_capabilities(out)


def rsync_base_opts(acls, xattrs):
    """RSYNC_OPTS without -A / -X where rsync lacks them."""
    flags = '-a' + ('A' if acls else '') + ('X' if xattrs else '')
    return [flags] + RSYNC_OPTS[1:]


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
    parser.add_argument('--batch-files', default=1000, type=int, required=False, help='Files per batch, the unit of work idle ranks can take: walkstat stats a batch, walktar writes it as one tar, walksync compares one and copies one with one rsync (default: 1000)')
    parser.add_argument('--fail-on-errors', action='store_true', help='Exit with status 2 if any directory or entry could not be scanned (default: exit 0, errors are only counted)')

    add_filter_options(parser)

    # tool-specific arguments follow
    if appname in ('walktar', 'walksync'):
        parser.add_argument('--batch-size', default='16GiB', type=str, required=False, help='Bytes per batch (st_size), the other limit; a bigger file is a batch on its own (default: 16GiB)')
    if 'walktar' == appname:
        parser.add_argument('--read-ahead', default=16, type=int, required=False, help='Files each rank opens and reads (whole, up to 16 MiB) ahead of the one it is archiving, in as many reader threads: a small file costs one storage round trip, ~35 ms on campaign GPFS and scratch, and these overlap them (default: 16; 0 reads one file at a time)')
    if 'walksync' == appname:
        parser.add_argument('--dest', required=True, help='Destination directory: the source --dirs DIR is copied into it (rsync DIR/ DEST/)')
        parser.add_argument('-n', '--dry-run', action='store_true', help='Copy nothing: list what would be copied (N new, C changed) and what is only in the destination (X), one file per rank: walksync-dryrun-r*.txt')
        parser.add_argument('--rsync', default='rsync', help='The rsync to run (default: rsync on PATH)')
        parser.add_argument('--rsync-opts', default='', help='More rsync options for every copy batch, one string, given as --rsync-opts="..." (appended to {})'.format(' '.join(RSYNC_OPTS)))

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
    if appname in ('walktar', 'walksync'):
        from humanfriendly import InvalidSize, parse_size
        try:
            args.batch_size = parse_size(args.batch_size, binary=True)
        except InvalidSize as e:
            parser.error('--batch-size: {}'.format(e))
        if args.batch_size < 1:
            parser.error('--batch-size must be at least 1 byte')
    if 'walktar' == appname and args.read_ahead < 0:
        parser.error('--read-ahead must be at least 0')

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

    if 'walksync' == appname:
        check_sync_options(parser, args)

    for d in args.dirs:
        if not os.path.isdir(d):
            raise FileNotFoundError('no such directory: {}'.format(d))

    if 'walksync' == appname:
        check_sync_paths(parser, args)

    return args


def check_sync_options(parser, args):
    """walksync: one source, its own rsync and filter rules."""
    if len(args.dirs) != 1:
        parser.error('walksync copies one directory: give one --dirs (got {})'.format(len(args.dirs)))
    try:
        args.rsync_opts = shlex.split(args.rsync_opts)
    except ValueError as e:
        parser.error('--rsync-opts: {}'.format(e))
    rsync = shutil.which(args.rsync)
    if rsync is None:
        parser.error('--rsync: no such program: {}'.format(args.rsync))
    args.rsync = rsync
    # an rsync built without ACLs (or xattrs) refuses -A (-X) outright: drop it, and say so
    acls, xattrs = rsync_capabilities(rsync)
    args.rsync_base = rsync_base_opts(acls, xattrs)
    args.rsync_missing = [name for name, ok in (('ACLs', acls), ('xattrs', xattrs)) if not ok]
    # the progress line and the Manager's last line say what the batches did
    args.written_verb, args.written_units = 'copied', 'rsync batches'
    return


def check_sync_paths(parser, args):
    """walksync: the source and destination exist and neither holds the other."""
    if not os.path.isdir(args.dest):
        raise FileNotFoundError('no such destination directory: {}'.format(args.dest))
    # paths as the walk will build them: no trailing '/', so a relative path is a suffix
    args.dirs = [os.path.normpath(args.dirs[0])]
    args.dest = os.path.normpath(args.dest)
    src, dest = os.path.realpath(args.dirs[0]), os.path.realpath(args.dest)
    if src == dest or dest.startswith(src.rstrip('/') + '/') or src.startswith(dest.rstrip('/') + '/'):
        parser.error('the source and the destination must not hold each other: {} -> {}'.format(src, dest))
    return
