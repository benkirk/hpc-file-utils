"""File batches as units of work (phase 5): one big directory of files, spread over all ranks
(walktar's tars, and walkstat's stats).

walktar's scanning rank closes a batch every --batch-files files and keeps it; the
Manager's 'share' moves the batches to idle ranks. Each batch is one tar file named by
the rank that archived it (output-r{rank}-b{n}.tar), so the tars show each rank's share.
"""

import collections
import os
import re
import tarfile

import pytest

from hpc_file_utils.walk.tests.conftest import BIG_DIR_FILES as NFILES   # the big_dir_tree fixture's

pytestmark = pytest.mark.mpi

BATCHES_RE = re.compile(r"--> Batches: ([\d,]+) received in share replies, ([\d,]+) handed out; "
                        r"wrote ([\d,]+) files, .+ \(([\d,]+) bytes\) in ([\d,]+) archives")
TAR_RE = re.compile(r"^output-r(\d+)-(b\d+|dirs)\.tar$")


def per_rank(cwd):
    """{rank: (files, bytes)} archived in batch tars; {rank: names} in -dirs tars."""
    files, dirs = collections.defaultdict(lambda: [0, 0]), collections.defaultdict(list)
    for t in sorted(os.listdir(cwd)):
        m = TAR_RE.match(t)
        if not m:
            continue
        with tarfile.open(os.path.join(cwd, t)) as tf:
            members = tf.getmembers()
        if m.group(2) == "dirs":
            assert all(x.isdir() for x in members), t
            dirs[int(m.group(1))] += [x.name for x in members]
        else:
            assert members and not any(x.isdir() for x in members), t
            files[int(m.group(1))][0] += len(members)
            files[int(m.group(1))][1] += sum(x.size for x in members)
    return files, dirs


def test_big_directory_is_spread_over_every_rank(walk, big_dir_tree, tmp_path):
    r = walk.run("walktar", [big_dir_tree], cwd=tmp_path, nranks=4, args=["--batch-files", "10"])
    assert r.ok, r.report()
    names = walk.tar_names(tmp_path)
    assert len(names) == len(set(names)), "an entry was archived twice"
    assert set(names) == walk.tree_names(big_dir_tree)

    files, dirs = per_rank(tmp_path)
    nfiles = NFILES + 3
    assert sum(n for n, _ in files.values()) == nfiles
    assert sorted(files) == [1, 2, 3], f"a worker archived nothing: {dict(files)}\n{r.report()}"
    # every worker took at least one batch. How many each takes depends on timing: on CI's
    # loaded runners one rank has kept 270-363 of the 403 files, with or without --read-ahead,
    # so the share is not bounded here. Balance at scale is measured on derecho (walk README).
    assert min(n for n, _ in files.values()) >= 10, f"{dict(files)}\n{r.report()}"
    assert sum(len(v) for v in dirs.values()) == 5     # tree, big, a, a/b, c: one -dirs tar each walker

    shared, handed, written_files, written_bytes, archives = r.ints(BATCHES_RE)
    assert shared > 0 and handed > 0, r.report()      # the work moved by sharing batches
    assert written_files == nfiles
    batch_tars = [t for t in tmp_path.glob("output-r*-b*.tar")]
    assert archives == len(batch_tars)
    assert written_bytes == sum(t.stat().st_size for t in batch_tars)
    final = [line for line in r.stdout.splitlines() if " | wrote " in line]
    assert final and "/s avg)" in final[-1], r.report()


def test_one_batch_per_directory_tree_without_limits(walk, big_dir_tree, tmp_path):
    """With limits above the tree, one worker's files make one batch: small and big
    directories combine on the scanning rank instead of each becoming a tar."""
    r = walk.run("walktar", [big_dir_tree], cwd=tmp_path, nranks=2, args=["--batch-files", "100000"])
    assert r.ok, r.report()
    assert sorted(t.name for t in tmp_path.glob("output-r*.tar")) == ["output-r001-b00000.tar",
                                                                       "output-r001-dirs.tar"]
    assert set(walk.tar_names(tmp_path)) == walk.tree_names(big_dir_tree)


RANK_ROW_RE = re.compile(r"^\s+(\d+)\s+\S+\s+[\d,]+\s+([\d,]+)\s", re.M)


def test_walkstat_stats_a_big_directory_on_every_rank(walk, big_dir_tree, tmp_path):
    """walkstat's stat batches: the big directory's stats are spread over the ranks too,
    and nothing is written."""
    r = walk.run("walkstat", [big_dir_tree], cwd=tmp_path, nranks=4, args=["--batch-files", "10", "--verbose"])
    assert r.ok, r.report()
    assert r.total_count() == NFILES + 3 + 4, r.report()       # files, and big, a, a/b, c
    # local stats take microseconds, so how many ranks take part depends on timing: at
    # least the scanning rank and one that took a shared batch
    files = {int(rank): int(n.replace(",", "")) for rank, n in RANK_ROW_RE.findall(r.stdout)}
    assert sorted(files) == [1, 2, 3], f"{files}\n{r.report()}"
    assert sum(1 for n in files.values() if n) >= 2, f"{files}\n{r.report()}"
    (shared,) = r.ints(r"--> Batches: ([\d,]+) received in share replies")
    assert shared > 0, r.report()
    assert " | wrote " not in r.stdout
