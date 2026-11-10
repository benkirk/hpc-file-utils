"""The diagnostics' logging (common/log.py): the rank prefix, the levels, and that
BaseWorker.error() still counts. In-process, no MPI."""

import logging

import pytest

from hpc_file_utils.walk.common import log as walk_log
from hpc_file_utils.walk.common.stats import WalkStats
from hpc_file_utils.walk.common.worker import BaseWorker


@pytest.fixture(autouse=True)
def _reset_logger():
    """Leave the package logger as the other tests find it: no handler, propagating
    (so their diagnostics reach logging's last-resort handler and capsys)."""
    yield
    logger = logging.getLogger(walk_log.ROOT)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


def test_records_carry_the_rank_and_level(capfd):
    logger = walk_log.setup(7)
    logging.getLogger("hpc_file_utils.walk.some.module").error("Cannot scan: boom")
    logging.getLogger("hpc_file_utils.walk.some.module").warning("directory '/x' vanished")
    logging.getLogger("hpc_file_utils.walk.some.module").debug("not without --verbose")
    out, err = capfd.readouterr()
    assert out == ""
    assert err == "[  7] ERROR Cannot scan: boom\n[  7] WARNING directory '/x' vanished\n"
    assert logger.level == logging.INFO and not logger.propagate


def test_verbose_enables_debug_and_setup_replaces_the_handler(capfd):
    walk_log.setup(3)
    walk_log.setup(3, verbose=True)             # once the options are known
    logging.getLogger("hpc_file_utils.walk.m").debug("detail")
    assert capfd.readouterr().err == "[  3] DEBUG detail\n"      # once, not twice


def test_error_counts_and_logs(capfd):
    walk_log.setup(1)
    w = object.__new__(BaseWorker)   # skip MPIClass.__init__: no MPI here
    w.rank, w.stats = 1, WalkStats(heap_size=10)
    w.error("stat", FileNotFoundError(2, "No such file or directory", "/gone"))
    assert w.stats.num_errors == 1
    assert capfd.readouterr().err == "[  1] ERROR Cannot stat: [Errno 2] No such file or directory: '/gone'\n"
