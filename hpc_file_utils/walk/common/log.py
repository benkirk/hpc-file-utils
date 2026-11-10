"""Diagnostics on stderr, through the stdlib logging (the sweep's Part D).

Every rank writes its own warnings and errors ('[rank] LEVEL message', one write per
line, so lines from many ranks splice less under mpiexec); the results (the banner,
the progress line, the '-->' lines, the summary and the userlists) stay print() on
rank 0's stdout: they are the parsed contract. BaseWorker.error() stays the one place
that counts errors; logging never is the counter.

  setup(rank, verbose)   once per rank, in cli.main(): one stderr handler on the
                         'hpc_file_utils.walk' logger, DEBUG with --verbose, else INFO
  logging.getLogger(__name__) in the modules, as usual
"""

import logging
import sys

ROOT = 'hpc_file_utils.walk'
FORMAT = '[%(rank)3d] %(levelname)s %(message)s'


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is at the time, not the one of setup()'s moment
    (pytest's capture swaps it per test), as logging's own last-resort handler does."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


class _RankFilter(logging.Filter):
    """Puts the rank on every record, for the format."""

    def __init__(self, rank):
        super().__init__()
        self.rank = rank

    def filter(self, record):
        record.rank = self.rank
        return True


def setup(rank: int, verbose: bool = False) -> logging.Logger:
    """This rank's diagnostics: to stderr, prefixed by the rank; DEBUG with --verbose.
    Calling it again replaces the handler (the level is known only once the options are)."""
    logger = logging.getLogger(ROOT)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = _StderrHandler()
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.addFilter(_RankFilter(rank))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    return logger
