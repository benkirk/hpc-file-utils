"""Command-line options for the walk tools (no MPI; parsed on rank 0 only): the common
ones here, each tool's own from its Tool (walk/tools.py, common/tool.py)."""

import argparse
import os
import time

from hpc_file_utils.walk.common.filters import add_filter_options, compile_filters, parse_size_option
from hpc_file_utils.walk.tools import TOOLS


# the options only some tools take (parse_options()), at their defaults
DEFAULTS = dict(
    spreadsheet=None, summary_prefix=None, heap_size=500, threshold_size='10MB', threshold_count=10000,   # the reports
    batch_size=None,          # bytes per batch: only the tools that batch by bytes too (walktar, walksync)
    fail_on_errors=False,     # walkstat, walktar
    index=None,               # every tool but walkrm (each adds its own --index)
)


class HelpFormatter(argparse.HelpFormatter):
    """argparse's, but text with line breaks of its own (the examples) is left as written."""

    def _fill_text(self, text, width, indent):
        if '\n' in text:
            return '\n'.join(indent + line for line in text.splitlines())
        return super()._fill_text(text, width, indent)


def parse_options(appname: str, argv: list[str] | None = None) -> argparse.Namespace:
    """Parse argv (default: sys.argv[1:]) for the tool named appname; args.app names it.

    Usage errors and --help raise SystemExit, as argparse does; a --dirs entry that is
    not a directory raises FileNotFoundError.
    """
    tool = TOOLS[appname]
    # allow_abbrev=False: '--summary x' used to be taken silently as '--summary-prefix x'
    parser = argparse.ArgumentParser(prog=appname, description=tool.description, epilog=tool.epilog(), allow_abbrev=False,
                                     formatter_class=HelpFormatter)

    # the options every tool takes
    parser.add_argument('-d', '--dirs', nargs='+', metavar='DIR', required=True, help=tool.dirs_help)
    parser.add_argument('-p', '--progress', type=int, default=5, metavar='SECONDS', help='Seconds between progress lines; 0 or less prints none (default: 5)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Print detailed information (per-rank counts, Manager counters)')
    parser.add_argument('--local-queue-max', default=1000, type=int, metavar='N', help='Subdirectories a worker keeps for itself; past this it sends the oldest half to the Manager (default: 1000)')
    parser.add_argument('--dirs-per-dispatch', default=8, type=int, metavar='N', help='Directories the Manager hands an idle worker at once (default: 8)')
    parser.add_argument('--batch-files', default=1000, type=int, metavar='N', help=f'Names per batch, the unit of work an idle rank can take from a busy one: {tool.batch_help} (default: 1000)')
    parser.add_argument('--profile', action='store_true', help='Every rank writes a cProfile of its run to profile-r{rank}.prof in the working directory (read with python -m pstats)')

    # the options only some tools take. A tool that does not use one does not accept it;
    # the options still carry every name, at its default, so no reader has to ask which tool runs.
    parser.set_defaults(**DEFAULTS)
    summary_prefix = dict(metavar='PREFIX', help='Name this run\'s outputs: ' + ('summary-PREFIX, userlists/PREFIX/, the spreadsheet\'s sheets and ' if tool.reports else '') + 'index-PREFIX-r{rank}.tsv (default: ' + ('summary-output, userlists/ and ' if tool.reports else '') + 'index-r{rank}.tsv)')
    if tool.reports:
        g = parser.add_argument_group('reports', tool.reports_help)
        g.add_argument('--summary-prefix', **summary_prefix)
        g.add_argument('-s', '--spreadsheet', type=str, metavar='FILE', help='Also write the summary\'s lists to this .xlsx workbook, as sheets added to it or replaced in it (needs pandas and openpyxl)')
        g.add_argument('--heap-size', type=int, metavar='N', help=f'Length of the largest-files and largest-directories lists kept, overall and per user (default: {DEFAULTS["heap_size"]})')
        g.add_argument('--threshold-size', type=str, metavar='SIZE', help=f'With --threshold-count, what is too small to list: a directory below both stays out of the per-user and by-age directory lists, and a user or group below both gets no row and no userlist (default: {DEFAULTS["threshold_size"]})')
        g.add_argument('--threshold-count', type=int, metavar='N', help=f'The same threshold, as a number of entries (default: {DEFAULTS["threshold_count"]})')
    elif tool.indexes:
        parser.add_argument('--summary-prefix', **summary_prefix)
    if tool.batches_bytes:
        parser.add_argument('--batch-size', default='16GiB', type=str, metavar='SIZE', help='Bytes per batch (st_size), the other limit; a bigger file is a batch on its own. The walk ends one batch after the last rank takes its last, so aim for at least ~5 batches per worker: total bytes / (5 x workers), within about 1GiB to 16GiB (the walk README, "Sizing the batches") (default: 16GiB)')
    if tool.errors_exit is None:
        parser.add_argument('--fail-on-errors', action='store_true', help='Exit with status 2 if any directory or entry could not be read (default: exit 0; errors are counted and reported)')

    add_filter_options(parser, tool)

    # the tool's own arguments follow
    tool.add_arguments(parser)

    args = parser.parse_args(argv)
    args.app = appname
    args.verb = tool.verb

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
        if args.batch_size is not None:
            args.batch_size = parse_size_option(args.batch_size, '--batch-size')
            if args.batch_size < 1:
                parser.error('--batch-size must be at least 1 byte')

        if args.progress <= 0:
            args.progress = float('inf')

        if '0' == args.threshold_size:
            args.threshold_size = 0
        else:
            args.threshold_size = parse_size_option(args.threshold_size, '--threshold-size')

        # one "now" for the whole job: the filters' ages and the reports' 3/5/7-year
        # buckets measure from it on every rank (it travels with the options)
        args.now = time.time()
        args.filters = compile_filters(args, args.now)   # None without a filter
    except ValueError as e:
        parser.error(str(e))

    for d in args.dirs:
        if not os.path.isdir(d):
            raise FileNotFoundError(f'no such directory: {d}')

    tool.check(parser, args)
    return args
