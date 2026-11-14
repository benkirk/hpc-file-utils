"""walksync's in-flight bytes (tools-find-rm, step 0), in-process (no MPI, no rsync): while a
copy batch runs, the progress line counts what is already written of its big entries,
and replaces that with the batch's bytes when it ends, never counting anything twice."""

import os
import time

import pytest

from hpc_file_utils.walk.sync import worker as worker_mod
from hpc_file_utils.walk.sync.compare import NEW
from hpc_file_utils.walk.sync.worker import CopyBatch, find_temp
from hpc_file_utils.walk.tests.test_index import _sync_worker


class _ScriptedRsync:
    """An rsync whose wait() runs 'during' (what the test does to the destination while it
    'copies', and what it measures) and then returns rc."""

    def __init__(self, rc, during):
        self.rc, self.during = rc, during

    def wait(self, tick):
        tick()
        self.during()
        return self.rc

    def tail(self):
        return "scripted rsync"

    def close(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "INFLIGHT_CACHE_SECONDS", 0)   # measure at every look
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    (dest / "sub").mkdir(parents=True)

    def make(mode="temp"):
        w = _sync_worker(tmp_path, src, index=False)
        w.init_inflight(mode)
        return w
    return make, dest


def _write(path, n):
    path.write_bytes(b"x" * n)


def _run(w, paths, nbytes, big, script):
    """copy_batch() over parts of one path each, part i's rsync running script[i] = (rc, during)."""
    w.rsyncs = len(paths)
    steps = iter(script)
    w.start_rsync = lambda part: _ScriptedRsync(*next(steps))
    w.copy_batch(paths, nbytes, None, big)


def counted(w):
    return w.progress_extra()[0]


def test_big_files_count_while_their_rsync_writes_them(setup):
    make, dest = setup
    w = make()
    seen = []
    paths, big = ["big1", "small", "sub/big2"], ((0, 1000), (2, 2000))

    def part0():
        _write(dest / ".big1.AbCdEf", 400)
        _write(dest / "sub/.big2.123456", 3000)       # more than planned: capped at 2000
        seen.append(counted(w))
        os.unlink(dest / ".big1.AbCdEf")               # renamed into place as the part ends
        _write(dest / "big1", 1000)

    def part1():
        seen.append(counted(w))                         # big1's part returned: in full

    def part2():
        os.unlink(dest / "sub/.big2.123456")           # renamed while its rsync still runs
        seen.append(counted(w))

    _run(w, paths, 3010, big, [(0, part0), (0, part1), (0, part2)])
    assert seen == [400 + 2000, 1000 + 2000, 1000 + 2000]
    assert counted(w) == 3010 and w.inflight == []      # the batch's bytes, once
    assert w.stats.counters["copied_bytes"] == 3010


def test_a_failed_batch_leaves_nothing_counted(setup):
    make, dest = setup
    w = make()
    seen = []

    def part0():
        _write(dest / ".big1.AbCdEf", 700)
        seen.append(counted(w))

    _run(w, ["big1", "small"], 1010, ((0, 1000),), [(0, part0), (23, lambda: seen.append(counted(w)))])
    assert seen == [700, 1000]
    assert counted(w) == 0 and w.inflight == [] and w.stats.counters["rsync_failures"] == 1


def test_inplace_follows_the_destination_file(setup):
    make, dest = setup
    w = make("inplace")
    seen = []

    def part0():
        _write(dest / "big1", 250)
        seen.append(counted(w))
        _write(dest / "big1", 5000)                    # an old, bigger version: capped
        seen.append(counted(w))

    _run(w, ["big1"], 1000, ((0, 1000),), [(0, part0)])
    assert seen == [250, 1000] and counted(w) == 1000


def test_no_inflight_count_when_rsync_writes_elsewhere(setup):
    make, dest = setup
    w = make(None)
    seen = []

    def part0():
        _write(dest / ".big1.AbCdEf", 400)
        seen.append(counted(w))

    _run(w, ["big1"], 1000, ((0, 1000),), [(0, part0)])
    assert seen == [0] and counted(w) == 1000


def test_a_temp_file_not_there_yet_is_looked_for_less_often(setup, monkeypatch):
    make, dest = setup
    w = make()
    clock = [100.0]
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: clock[0])
    seen = []

    def part0():
        seen.append(counted(w))                         # no temp yet: next look in 1 s
        _write(dest / ".big1.AbCdEf", 300)
        clock[0] += 0.5
        seen.append(counted(w))                         # too soon to look again
        clock[0] += 0.6
        seen.append(counted(w))

    _run(w, ["big1"], 1000, ((0, 1000),), [(0, part0)])
    assert seen == [0, 0, 300]


def test_the_cache_spares_the_lstats(setup, monkeypatch):
    make, dest = setup
    monkeypatch.setattr(worker_mod, "INFLIGHT_CACHE_SECONDS", 1.0)
    w = make()
    seen = []

    def part0():
        _write(dest / ".big1.AbCdEf", 100)
        seen.append(counted(w))
        _write(dest / ".big1.AbCdEf", 200)
        seen.append(counted(w))                         # within the second: the cached value

    _run(w, ["big1"], 1000, ((0, 1000),), [(0, part0)])
    assert seen == [100, 100]


def test_batches_carry_their_big_entries(setup, monkeypatch):
    make, _ = setup
    monkeypatch.setattr(worker_mod, "INFLIGHT_MIN", 100)
    w = make()                                          # batch_files=2
    for name, size in [("a", 500), ("b", 10), ("c", 100), ("d", 99)]:
        w.want_copy(name, size, NEW, [])
    w.close_batch()
    assert w.local_batches == [CopyBatch(["a", "b"], 510, None, ((0, 500),)),
                               CopyBatch(["c", "d"], 199, None, ((0, 100),))]
    assert w.batch_big == []


def test_find_temp_takes_rsyncs_name_and_the_newest(tmp_path):
    for name in (".f.dat.ABCDEF", ".f.dat.toolong1", ".g.dat.ABCDEF", "f.dat"):
        (tmp_path / name).write_bytes(b"x")
    assert find_temp(tmp_path, "f.dat") == ".f.dat.ABCDEF"
    stale = tmp_path / ".f.dat.000000"                  # left by a killed run, older
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    assert find_temp(tmp_path, "f.dat") == ".f.dat.ABCDEF"
    assert find_temp(tmp_path, "missing") is None
    assert find_temp(tmp_path / "no-such-dir", "f.dat") is None
