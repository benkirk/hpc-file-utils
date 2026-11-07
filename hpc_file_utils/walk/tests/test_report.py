"""The summary and userlist text, from a hand-built WalkStats (no MPI)."""

import os
from argparse import Namespace
from datetime import datetime

from hpc_file_utils.walk.common.stats import DirEntry, FileEntry, GIDCounts, UIDCounts, WalkStats
from hpc_file_utils.walk.stat.report import (LIST_LENGTH, summary_text, userlist_text,
                                             write_reports)

MiB = 1024 * 1024
NOW = datetime(2026, 11, 6, 12, 0)
T2020 = datetime(2020, 1, 2, 12, 0).timestamp()


def options(**kw):
    base = dict(dirs=["/data/proj"], threshold_count=10, threshold_size=MiB,
                summary_prefix="test", spreadsheet=None, heap_size=500)
    base.update(kw)
    return Namespace(**base)


def counts(cls, i, name, nbytes, nitems):
    c = cls()
    c.id, c.name, c.nbytes, c.nitems = i, name, nbytes, nitems
    return c


def stats():
    s = WalkStats(heap_size=500)
    s.num_items, s.total_size = 12, 7 * MiB
    s.st_modes.update({"reg": 9, "dir": 3})
    s.uids[1] = counts(UIDCounts, 1, "alice", 7 * MiB, 11)
    s.uids[2] = counts(UIDCounts, 2, "tiny", 10, 1)          # below both thresholds
    s.gids[5] = counts(GIDCounts, 5, "proj", 7 * MiB, 12)
    dirs = [DirEntry("/data/proj/a/", 5 * MiB, 3, T2020, T2020, T2020),
            DirEntry("/data/proj/empty", 0, 0, -1, -1, -1)]
    for d in dirs:
        s.heaps["nbytes_dirs"].add((d.nbytes, d))
        s.heaps["nitems_dirs"].add((d.nitems, d))
        s.user_heaps["nbytes_dirs"][1].add((d.nbytes, d))
    files = [FileEntry("/data/proj/a/big.dat", 4 * MiB, T2020, T2020, T2020),
             FileEntry("/data/proj/a/new\nline.dat", MiB, T2020, T2020, T2020),
             FileEntry(os.fsdecode(b"/data/proj/a/bad-\xff.dat"), MiB, T2020, T2020, T2020)]
    for f in files:
        s.heaps["nbytes_files"].add((f.nbytes, f))
        s.user_heaps["nbytes_files"][1].add((f.nbytes, f))
    return s


def test_summary_text():
    text = summary_text(stats(), options(), NOW)
    lines = text.splitlines()
    assert lines[1] == "# Summary as of 2026-11-06 12:00"
    assert "Scanned:     /data/proj" in lines
    assert "Total Count: 12 items" in lines
    assert "Total Size:  7 MiB" in lines
    assert "Scan Errors" not in text
    assert "     reg :            9" in lines
    assert "       alice        7 MiB            11" in lines
    assert "tiny" not in text                                # below the thresholds
    assert "        proj        7 MiB            12" in lines
    i = lines.index("# Largest Directories:")
    assert lines[i + 2] == "#      size         items  last access  path"
    assert lines[i + 3] == "      5 MiB             3  2020-01-02   /data/proj/a/"
    assert lines[i + 4] == "    0 bytes             0  -            /data/proj/empty/"
    assert "      1 MiB  2020-01-02   /data/proj/a/new\\nline.dat" in lines
    assert "      1 MiB  2020-01-02   /data/proj/a/bad-\\xff.dat" in lines
    assert "# Largest Directories, last access [7yrs+):" not in text   # empty lists are left out
    text.encode("utf-8")


def test_summary_errors_and_many_dirs():
    s = stats()
    s.num_errors = 3
    text = summary_text(s, options(dirs=["/data/proj/a/", "/data/proj/b/"]), NOW)
    assert "Scan Errors: 3 entries could not be scanned, so these totals are incomplete" in text
    assert "Scanned:     2 directories under /data/proj" in text


def test_lists_are_capped():
    s = stats()
    for i in range(LIST_LENGTH + 50):
        f = FileEntry(f"/data/proj/f{i:04d}", 2 * MiB, T2020, T2020, T2020)
        s.heaps["nbytes_files"].add((f.nbytes, f))
    lines = summary_text(s, options(), NOW).splitlines()
    start = lines.index("# Largest Files:") + 2
    assert len(lines[start + 1:]) == LIST_LENGTH


def test_userlist_text():
    s = stats()
    text = userlist_text(s, s.uids[1], options(), NOW)
    lines = text.splitlines()
    assert lines[3:6] == ["User:        alice", "Total Count: 11 items", "Total Size:  7 MiB"]
    assert "# alice - Largest Directories:" in lines
    assert "# alice - Largest Files:" in lines
    assert "# alice - Bloated Directories (file count):" not in lines     # nothing tracked
    assert "      1 MiB  2020-01-02   /data/proj/a/new\\nline.dat" in lines


def test_write_reports(tmp_path, monkeypatch, capsysbinary):
    monkeypatch.chdir(tmp_path)
    write_reports(stats(), options(), NOW)
    summary = (tmp_path / "summary-test").read_bytes()
    summary.decode("utf-8")
    assert summary in capsysbinary.readouterr().out          # echoed to stdout
    assert sorted(p.name for p in (tmp_path / "userlists" / "test").iterdir()) == ["alice"]

    monkeypatch.chdir(tmp_path / "userlists")
    write_reports(stats(), options(summary_prefix=None), NOW)
    assert (tmp_path / "userlists" / "summary-output").is_file()
    assert (tmp_path / "userlists" / "userlists" / "alice").is_file()
