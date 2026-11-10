"""walksync: copy what is new or changed, with batched rsync --files-from (common/tool.py)."""

import os
import shlex

from hpc_file_utils.walk.common.tool import Tool
from hpc_file_utils.walk.sync.rsync import RSYNC_OPTS, choose_rsync, rsync_base_opts


class SyncTool(Tool):

    name = 'walksync'
    description = ('Walk a directory tree in parallel (MPI) and copy what is new or changed to a destination, '
                   'with batched rsync --files-from. Never deletes; hard links, deletions and final '
                   'directory modes and times are left to a final serial rsync -aHAX --delete.')
    writes = ('copied', 'rsync batches')
    batches_bytes = True

    def add_arguments(self, parser):
        parser.add_argument('--dest', required=True, help='Destination directory: the source --dirs DIR is copied into it (rsync DIR/ DEST/)')
        parser.add_argument('-n', '--dry-run', action='store_true', help='Copy nothing: list what would be copied (N new, C changed) and what is only in the destination (X), one file per rank: walksync-dryrun-r*.txt')
        parser.add_argument('--rsync', default=None, help='The rsync to run (default: rsync on PATH, or /usr/bin/rsync if rsync on PATH lacks ACLs or xattrs and /usr/bin/rsync has both)')
        parser.add_argument('--rsync-opts', default='', help='More rsync options for every copy batch, one string, given as --rsync-opts="..." (appended to ' + ' '.join(RSYNC_OPTS) + ')')
        parser.add_argument('--rsyncs', default=8, type=int, help='rsyncs per copy batch, side by side, each over a K-th of its files: a cold file costs one storage round trip whatever rsync does with it, and K rsyncs overlap them; 1 is one rsync per batch (default: 8, the fastest or within noise of it at 1 x 32 on every tree measured, see the walk README)')

    def check(self, parser, args):
        """One source, its own rsync and filter rules, an existing destination that
        neither holds nor is held by the source."""
        if len(args.dirs) != 1:
            parser.error(f'walksync copies one directory: give one --dirs (got {len(args.dirs)})')
        if args.rsyncs < 1:
            parser.error('--rsyncs must be at least 1')
        try:
            args.rsync_opts = shlex.split(args.rsync_opts)
        except ValueError as e:
            parser.error(f'--rsync-opts: {e}')
        rsync, acls, xattrs, args.rsync_note = choose_rsync(args.rsync)
        if rsync is None:
            parser.error(f"--rsync: no such program: {args.rsync or 'rsync'}")
        args.rsync = rsync
        # an rsync built without ACLs (or xattrs) refuses -A (-X) outright: drop it, and say so
        args.rsync_base = rsync_base_opts(acls, xattrs)
        args.rsync_missing = [name for name, ok in (('ACLs', acls), ('xattrs', xattrs)) if not ok]

        if not os.path.isdir(args.dest):
            raise FileNotFoundError(f'no such destination directory: {args.dest}')
        # paths as the walk will build them: no trailing '/', so a relative path is a suffix
        args.dirs = [os.path.normpath(args.dirs[0])]
        args.dest = os.path.normpath(args.dest)
        src, dest = os.path.realpath(args.dirs[0]), os.path.realpath(args.dest)
        if src == dest or dest.startswith(src.rstrip('/') + '/') or src.startswith(dest.rstrip('/') + '/'):
            parser.error(f'the source and the destination must not hold each other: {src} -> {dest}')

    def banner(self, args):
        lines = [f"Syncing {args.dirs[0]}/ -> {args.dest}/{' (dry run: copying nothing)' if args.dry_run else ''}"]
        if args.rsync_note:
            lines.append(f'rsync: {args.rsync_note}')
        for missing in args.rsync_missing:
            lines.append(f'WARNING: {args.rsync} was built without {missing}: they are not copied')
        lines += super().banner(args)
        if args.filters is not None and args.filters.selects:
            lines.append('    a partial copy: only the files the filters select (every directory is created)')
        return lines

    def report(self, manager, stats):
        """The sync summary; walkstat's reports (of what was compared) only when asked for."""
        from hpc_file_utils.walk.stat.report import write_reports
        from hpc_file_utils.walk.sync.report import sync_lines
        options = manager.options
        print('\n'.join(sync_lines(stats, manager.walk_elapsed, options)), flush=True)
        if options.summary_prefix or options.spreadsheet:
            write_reports(stats, options)

    def exit_status(self, total_errors, options):
        """Always 1 on errors: an incomplete copy must not look like a finished one."""
        return 1 if total_errors else 0

    def make_worker(self):
        from hpc_file_utils.walk.sync.worker import SyncWorker
        return SyncWorker()
