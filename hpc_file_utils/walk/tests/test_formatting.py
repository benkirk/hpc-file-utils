"""Formatting for the reports and the progress line (no MPI)."""

import os
import subprocess
import sys
from datetime import datetime

import pytest

from hpc_file_utils.walk.common.formatting import (format_date, format_number, format_size,
                                                   printable)
from hpc_file_utils.walk.common.progress import ProgressTally, progress_line


def test_sizes_and_numbers():
    assert format_size(0) == "0 bytes"
    assert format_size(29 * 1024 * 1024) == "29 MiB"
    assert format_size(1536) == "1.5 KiB"
    assert format_number(1234567) == "1,234,567"


def test_dates():
    t = datetime(2020, 3, 4, 12, 0, 0).timestamp()
    assert format_date(t) == "2020-03-04"
    assert format_date(-1) == "-"       # a directory with no files
    assert format_date(None) == "-"


@pytest.mark.parametrize("path,shown", [
    ("/plain/ünïcödé path.dat", "/plain/ünïcödé path.dat"),
    ("/a/new\nline.dat", "/a/new\\nline.dat"),
    ("/a/tab\there\r", "/a/tab\\there\\r"),
    ("/a/bell\x07", "/a/bell\\x07"),
    (os.fsdecode(b"/a/bad-\xff\xfe-name.dat"), "/a/bad-\\xff\\xfe-name.dat"),
    (b"/a/bytes-\xff", "/a/bytes-\\xff"),
])
def test_printable(path, shown):
    assert printable(path) == shown
    printable(path).encode("utf-8")      # always valid UTF-8, never surrogates
    assert "\n" not in printable(path)


def test_progress_tally_and_line():
    p = ProgressTally(4)
    p.update(1, (10, 1024, None))
    p.update(2, (5, 2048, {"copied": 1}))
    p.update(1, (12, 4096, None))        # the latest payload replaces the last one
    assert (p.total_items, p.total_bytes) == (17, 6144)
    assert p.extra == [None, None, {"copied": 1}, None]
    line = progress_line(100, 2 * 1024 ** 3, 4.0, now=datetime(2026, 1, 2, 3, 4, 5))
    assert line == "[2026-01-02 03:04:05] Walked 100 items / 2 GiB in 4 seconds (25 items/sec)"
    assert "(0 items/sec)" in progress_line(0, 0, 0.0)


def test_pure_modules_do_not_import_mpi4py():
    mods = ["heaps", "formatting", "options", "progress", "protocol", "stats"]
    code = ("import sys\n"
            + "".join(f"import hpc_file_utils.walk.common.{m}\n" for m in mods)
            + "import hpc_file_utils.walk.stat.report, hpc_file_utils.walk.stat.spreadsheet\n"
            + "import hpc_file_utils.walk.common.manager, hpc_file_utils.walk.common.worker\n"
            + "import hpc_file_utils.walk.cli\n"
            + "assert 'mpi4py' not in sys.modules, sorted(m for m in sys.modules if 'mpi' in m)\n")
    subprocess.run([sys.executable, "-c", code], check=True)
