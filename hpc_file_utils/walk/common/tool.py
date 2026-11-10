"""Tool: what makes walkstat, walktar and walksync different, in one place per tool (no
MPI, cheap to import: a tool imports its worker class only in make_worker()).

cli.py and options.py see a tool through this interface only, so a fourth tool is one
<tool>/tool.py and one <tool>/worker.py, registered in walk/tools.py.
"""


class Tool:

    name = ''                # the console script
    description = ''         # argparse's
    writes = None            # None, or (verb, units) for the progress line's rate and the Manager's
                             # last line: walktar ('wrote', 'archives'), walksync ('copied', 'rsync batches')
    batches_bytes = False    # True: --batch-size closes a batch by bytes too (walktar, walksync)

    def add_arguments(self, parser):
        """The tool's own options, after the common ones and the filters."""

    def check(self, parser, args):
        """After parsing, once the --dirs exist: the tool's consistency checks (parser.error)
        and derived settings; FileNotFoundError for a path of its own that is missing."""

    def banner(self, args):
        """Lines rank 0 prints before 'Running <tool> on N MPI ranks'."""
        if args.filters is None:
            return []
        return [f'Filters: {args.filters.describe()}']

    def report(self, manager, stats):
        """Rank 0, after the walk's balance lines: the tool's own reports (walkstat's
        summary and userlists, unless the tool says otherwise)."""
        from hpc_file_utils.walk.stat.report import write_reports
        write_reports(stats, manager.options)

    def exit_status(self, total_errors, options):
        """Errors are counted; only --fail-on-errors makes them fatal (2)."""
        return 2 if total_errors and options.fail_on_errors else 0

    def make_worker(self):
        """This tool's worker, for ranks 1..n (imports MPI)."""
        raise NotImplementedError
