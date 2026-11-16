"""Tool: what makes walkstat, walktar, walksync, walkfind and walkrm different, in one
place per tool (no MPI, cheap to import: a tool imports its worker class only in
make_worker()).

cli.py and options.py see a tool through this interface only, so another tool is one
<tool>/tool.py and one <tool>/worker.py, registered in walk/tools.py.
"""

import argparse


class Tool:

    name = ''                # the console script
    description = ''         # argparse's
    examples = ()            # command lines for the end of --help
    verb = 'wrote'           # what a tool that writes does, for the progress line's rate
                             # ('| wrote S ...'; walksync: 'copied')
    batches_bytes = False    # True: --batch-size closes a batch by bytes too (walktar, walksync)

    # what --help says and shows (options.py); none of these changes what is parsed
    dirs_help = 'The directories to walk'
    batch_help = ''          # what this tool does with a batch of --batch-files names
    reports = True           # writes walkstat's summary, userlists and --spreadsheet
    indexes = True           # has --index
    selects = 'counted'      # what the selecting filters decide; None: the tool takes no filters
    errors_exit = None       # the exit status when anything failed: None for 0, or 2 with
                             # --fail-on-errors (walkstat, walktar); 1 for the tools whose
                             # result is wrong if anything was missed (walksync, walkfind, walkrm)

    def epilog(self) -> str:
        """The end of --help: how to launch, the examples, where the manual is."""
        lines = ['Run under MPI with at least 2 ranks; rank 0 is the Manager, the others walk.']
        if self.examples:
            lines += ['', 'Examples:'] + [f'  {e}' for e in self.examples]
        lines += ['', 'The walk README (hpc_file_utils/walk/README.md in the source) is the manual.']
        return '\n'.join(lines)

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """The tool's own options, after the common ones and the filters."""

    def check(self, parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
        """After parsing, once the --dirs exist: the tool's consistency checks (parser.error)
        and derived settings; FileNotFoundError for a path of its own that is missing."""

    def banner(self, args: argparse.Namespace) -> list[str]:
        """Lines rank 0 prints before 'Running <tool> on N MPI ranks'."""
        if args.filters is None:
            return []
        return [f'Filters: {args.filters.describe()}']

    def finish(self, walker) -> None:
        """Every rank (the Manager and the workers), after the walk ended and before the
        end-of-walk collectives: a tool's own collective phase (walkrm's rmdir pass, level by
        level). Errors counted here are in the exit status."""

    def report(self, manager, stats) -> None:
        """Rank 0, after the walk's balance lines: the tool's own reports."""

    def exit_status(self, total_errors: int, options: argparse.Namespace) -> int:
        """The job's exit status after a walk, by the tool's errors_exit: errors are only
        counted unless --fail-on-errors makes them fatal (2); or any error is 1."""
        if self.errors_exit is not None:
            return self.errors_exit if total_errors else 0
        return 2 if total_errors and options.fail_on_errors else 0

    def make_worker(self):
        """This tool's worker, for ranks 1..n (imports MPI)."""
        raise NotImplementedError
