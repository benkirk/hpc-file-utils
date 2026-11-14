"""walkfind: list the entries that match, like find, in parallel (common/tool.py)."""

import fnmatch
import glob
import os
import shlex

from hpc_file_utils.walk.common.index import add_index_argument
from hpc_file_utils.walk.common.tool import Tool

LIST_FILE = 'walkfind-r{:03d}.list'     # each worker's list, in the working directory
LIST_GLOB = 'walkfind-r[0-9][0-9][0-9]*.list'


class FindTool(Tool):

    name = 'walkfind'
    description = ('Walk directory trees in parallel (MPI) and list the entries that match, like find: '
                   '--name GLOB (and --prune-matches) tested on the name before any stat, the selecting '
                   'filters on the matches, one list per rank or one --output file.')

    def add_arguments(self, parser):
        parser.add_argument('--name', action='append', metavar='GLOB', help='List entries whose name matches GLOB (fnmatch, case-sensitive; repeatable, the globs OR together), tested before any stat: nothing else is stat\'ed. Without --name every entry matches. The --dirs roots are never tested (find -mindepth 1)')
        parser.add_argument('--prune-matches', action='store_true', help='Do not descend into a directory that matches --name (find -name GLOB -prune -print): it is listed, its contents are not walked')
        parser.add_argument('--print0', action='store_true', help='End each listed path with a NUL, as the raw bytes of the name (find -print0); default: one line per path, with newlines, tabs, other control characters and undecodable bytes escaped')
        parser.add_argument('--output', metavar='FILE', help='Rank 0 joins the per-rank lists into FILE (written as FILE.tmp, then renamed), in rank order, and removes them; FILE exists, empty, when nothing matched. Default: walkfind-r{rank}.list in the working directory, one per worker')
        add_index_argument(parser, 'Every rank also writes an index of its matches, one line of metadata each (find -ls: type, size, allocation, mode, owner, times, inode, path), to index-r{rank}.tsv (index-<prefix>-r{rank}.tsv with --summary-prefix) in the working directory; see the walk README, "Indexes"')

    def check(self, parser, args):
        """--name as one regex (filters.py does the same for --exclude); --output's directory
        must exist. A run's lists replace an earlier run's: this removes walkfind-r*.list
        from the working directory, so a rank that matches nothing leaves no stale list."""
        args.name_regex = None
        if args.name:
            for g in args.name:
                if not g or '/' in g:
                    parser.error(f"--name matches names, so GLOB cannot be empty or hold a '/': {g!r}")
            args.name_regex = '|'.join(fnmatch.translate(g) for g in args.name)
        if args.prune_matches and not args.name:
            parser.error('--prune-matches needs --name')
        if args.output is not None:
            args.output = os.path.abspath(args.output)
            if not os.path.isdir(os.path.dirname(args.output)):
                raise FileNotFoundError(f'--output: no such directory: {os.path.dirname(args.output)}')
        for stale in glob.glob(LIST_GLOB):
            os.unlink(stale)

    def banner(self, args):
        what = ' '.join(f'--name {shlex.quote(g)}' for g in args.name or []) or 'every entry'
        if args.prune_matches:
            what += ' (--prune-matches)'
        where = args.output or LIST_FILE.replace('{:03d}', '*')
        lines = [f"Finding {what} -> {where}{' (NUL-terminated)' if args.print0 else ''}"]
        return lines + super().banner(args)

    def report(self, manager, stats):
        """The find summary, then --output joined; no walkstat summary or userlists."""
        from hpc_file_utils.walk.find.report import find_lines, join_lists
        options = manager.options
        if options.output is not None:
            join_lists(options.output, [LIST_FILE.format(r) for r in range(1, manager.nranks)])
        print('\n'.join(find_lines(stats, options, LIST_FILE)), flush=True)

    def exit_status(self, total_errors, options):
        """1 on any error, as find: a directory that could not be read may hold a match."""
        return 1 if total_errors else 0

    def make_worker(self):
        from hpc_file_utils.walk.find.worker import FindWorker
        return FindWorker()
