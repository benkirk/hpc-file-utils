"""BaseWorker.process_directory() on its own, in-process (no MPI), via walkstat's StatWorker."""

import os
import types

import pytest

from hpc_file_utils.walk.common import worker as worker_mod
from hpc_file_utils.walk.common.stats import WalkStats
from hpc_file_utils.walk.stat.worker import StatWorker


class _Entry:
    """A scandir entry whose stat() reports a different owner."""

    def __init__(self, entry, uid):
        self._entry, self._uid = entry, uid
        self.path, self.name = entry.path, entry.name

    def stat(self, follow_symlinks=True):
        s = self._entry.stat(follow_symlinks=follow_symlinks)
        fields = {k: getattr(s, k) for k in dir(s) if k.startswith("st_")}
        fields["st_uid"] = self._uid
        return types.SimpleNamespace(**fields)


def _worker():
    w = object.__new__(StatWorker)   # skip MPIClass.__init__: no MPI here
    w.rank, w.local_dirs, w.local_batches, w.batch, w.batch_bytes = 1, [], [], [], 0
    w.max_local_dirs = 0
    w.options = types.SimpleNamespace(threshold_count=1, threshold_size=0, local_queue_max=1000)
    w.stats = WalkStats(heap_size=10)
    w.check_share = w.report_progress = lambda *a, **k: None
    return w


@pytest.mark.parametrize("uid", [0, 1234])
def test_single_owner_dirs_are_listed_for_that_uid(tmp_path, monkeypatch, uid):
    """A directory whose entries all belong to one uid goes on that user's lists,
    uid 0 included: root runs the scans (it was dropped when the check was truthiness)."""
    for name in ("a.dat", "b.dat"):
        (tmp_path / name).write_bytes(b"x" * 4096)
    real_scandir = os.scandir
    monkeypatch.setattr(worker_mod.os, "scandir",
                        lambda d: (_Entry(e, uid) for e in real_scandir(d)))

    w = _worker()
    w.process_directory(str(tmp_path))
    assert w.stats.num_errors == 0

    for name in ("nitems_dirs", "nbytes_dirs"):
        per_user = w.stats.user_heaps[name]
        assert list(per_user) == [uid], name
        assert [e.path for _, e in per_user[uid].top()] == [str(tmp_path)], name
    assert w.stats.uids[uid].nitems == 2


@pytest.mark.parametrize("progress, interval", [(600, 10), (float("inf"), 10), (5, 5)])
def test_worker_updates_have_their_own_timer(progress, interval):
    """--progress paces the screen; a worker walking its own stack still sends its counts
    (and stack length, which the Manager uses to pick whom to ask for a 'share') at least
    every WORKER_UPDATE_SECONDS."""
    clock, sent = [0.0], []
    w = _worker()
    w.options.progress = progress
    w.MPI = types.SimpleNamespace(Wtime=lambda: clock[0])
    w.comm = types.SimpleNamespace(send=lambda msg, dest, tag: sent.append(clock[0]))
    w.tags = {"progress": 0}
    w.last_progress_time = 0.0
    del w.report_progress                       # the real method, not _worker()'s stub
    for t in range(0, 31):
        clock[0] = float(t)
        w.report_progress(only_if_due=True)
    assert sent == [float(t) for t in range(interval, 31, interval)]


# ---------------------------------------------------------------------------
# phase 4 filters
# ---------------------------------------------------------------------------

def _filtered_worker(tmp_path, *args):
    from hpc_file_utils.walk.common.options import parse_options
    f = parse_options("walkstat", ["--dirs", str(tmp_path), *args]).filters
    w = _worker()
    w.exclude, w.select = f.exclude_matcher(), f.predicate()
    return w


def _walk(w, root):
    w.local_dirs.append(str(root))
    while w.local_dirs:
        w.process_directory(w.local_dirs.pop())


def test_failing_directories_are_still_walked(tmp_path):
    """--type f: no directory is counted, but every one is walked, and only the
    directories that hold a matching entry go on the lists."""
    (tmp_path / "only-dirs/sub").mkdir(parents=True)
    (tmp_path / "only-dirs/sub/a.dat").write_bytes(b"x" * 4096)
    (tmp_path / "top.dat").write_bytes(b"x" * 4096)
    w = _filtered_worker(tmp_path, "--type", "f")
    _walk(w, tmp_path)
    st = w.stats
    assert st.num_errors == 0
    assert st.num_dirs == 3                          # walked: the root, only-dirs, sub
    assert (st.num_items, dict(st.st_modes)) == (2, {"reg": 2})
    assert st.num_unmatched == 2                     # only-dirs, sub
    listed = {e.path for _, e in st.heaps["nitems_dirs"].top()}
    assert listed == {str(tmp_path), str(tmp_path / "only-dirs/sub")}


def test_unfiltered_lists_every_directory(tmp_path):
    (tmp_path / "empty").mkdir()
    w = _worker()
    _walk(w, tmp_path)
    assert {e.path for _, e in w.stats.heaps["nitems_dirs"].top()} == {str(tmp_path), str(tmp_path / "empty")}
    assert dict(w.stats.st_modes) == {"dir": 2}      # the root too, as before phase 4


def test_matching_directories_count_as_dirs(tmp_path):
    (tmp_path / "d1").mkdir()
    (tmp_path / "f1").touch()
    w = _filtered_worker(tmp_path, "--type", "d,f")
    _walk(w, tmp_path)
    assert dict(w.stats.st_modes) == {"dir": 1, "reg": 1}   # not the root: find's -mindepth 1
    assert w.stats.num_items == 2


def test_share_checks_count_entries_scanned(tmp_path, monkeypatch):
    """A huge directory with few matches must still answer the Manager's 'share'."""
    for i in range(7):
        (tmp_path / f"f{i}").touch()
    monkeypatch.setattr(worker_mod, "SHARE_CHECK_INCREMENT", 3)
    monkeypatch.setattr(worker_mod, "PROGRESS_INCREMENT", 2)
    w = _filtered_worker(tmp_path, "--type", "d", "--exclude", "f0")
    shares, updates = [], []
    w.check_share = lambda: shares.append(1)
    w.report_progress = lambda *a, **k: updates.append(1)
    w.process_directory(str(tmp_path))
    assert w.stats.num_items == 0 and w.stats.num_excluded == 1 and w.stats.num_unmatched == 6
    assert (len(shares), len(updates)) == (2, 3)


class _Unstattable:
    def __init__(self, entry):
        self.path, self.name = entry.path, entry.name

    def stat(self, follow_symlinks=True):
        raise AssertionError(f"stat() of excluded {self.name}")


def test_excluded_entries_are_never_stated(tmp_path, monkeypatch):
    (tmp_path / "COLD_STORAGE").mkdir()
    (tmp_path / "COLD_STORAGE/x.dat").touch()
    (tmp_path / "keep.dat").touch()
    real_scandir = os.scandir
    monkeypatch.setattr(worker_mod.os, "scandir",
                        lambda d: (_Unstattable(e) if e.name.startswith("COLD") else e for e in real_scandir(d)))
    w = _filtered_worker(tmp_path, "--exclude", "COLD*")
    _walk(w, tmp_path)
    st = w.stats
    assert st.num_errors == 0
    assert (st.num_items, st.num_excluded, st.num_dirs) == (1, 1, 1)
    assert dict(st.st_modes) == {"dir": 1, "reg": 1}   # pruning alone keeps the root in 'dir'


# ---------------------------------------------------------------------------
# phase 5 batches
# ---------------------------------------------------------------------------

class _Batcher(StatWorker):
    """walkstat's worker plus walktar's process_file: files go into batches."""

    def process_file(self, filename, statinfo):
        self.add_to_batch(filename, statinfo.st_size)


def _batch_worker(batch_files, batch_size):
    w = _worker()
    w.__class__ = _Batcher
    w.options.batch_files, w.options.batch_size = batch_files, batch_size
    return w


def test_batches_close_at_n_files_or_b_bytes():
    w = _batch_worker(3, 100)
    for i in range(7):
        w.add_to_batch(f"f{i}", 1)
    assert [p for p, _ in w.local_batches] == [["f0", "f1", "f2"], ["f3", "f4", "f5"]]
    assert (w.batch, w.batch_bytes) == (["f6"], 1)
    w.add_to_batch("big", 500)                 # over --batch-size: closes at once
    assert w.local_batches[-1] == (["f6", "big"], 501)
    w.add_to_batch("huge", 10**6)              # a file bigger than a batch is one alone
    assert w.local_batches[-1] == (["huge"], 10**6) and w.batch == []
    w.close_batch()                            # nothing open: no empty batch
    assert len(w.local_batches) == 4


def test_small_directories_combine_into_one_batch(tmp_path):
    for d in ("a", "b", "c"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.dat").write_bytes(b"x" * 10)
    w = _batch_worker(5000, 1 << 30)
    _walk(w, tmp_path)
    assert w.local_batches == []               # still open, across the three directories
    assert sorted(os.path.basename(os.path.dirname(p)) for p in w.batch) == ["a", "b", "c"]
    assert w.batch_bytes == 30


def test_walkstat_collects_nothing(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}").write_bytes(b"x")
    w = _worker()
    _walk(w, tmp_path)
    assert (w.local_batches, w.batch, w.batch_bytes) == ([], [], 0)
    assert w.progress_payload()[2:] == (0, None)


def _share_worker(dirs, batches):
    w = _worker()
    del w.check_share                          # the real method
    sent = []
    w.tags = {"share": 32, "share_reply": 33}
    w.comm = types.SimpleNamespace(iprobe=lambda source, tag: True, recv=lambda source, tag: None,
                                   send=lambda msg, dest, tag: sent.append((msg, tag)))
    w.local_dirs, w.local_batches = list(dirs), list(batches)
    w.progress_extra = lambda: None
    return w, sent


@pytest.mark.parametrize("ndirs, nbatches, gave_dirs, gave_batches", [
    (0, 0, 0, 0), (1, 1, 0, 1), (4, 3, 2, 2), (5, 4, 2, 2), (0, 5, 0, 3)])
def test_share_gives_oldest_half_of_dirs_and_batches_rounded_up(ndirs, nbatches, gave_dirs, gave_batches):
    dirs = [f"d{i}" for i in range(ndirs)]
    batches = [([f"b{i}"], 1) for i in range(nbatches)]
    w, sent = _share_worker(dirs, batches)
    w.check_share()
    (gdirs, gbatches, payload), tag = sent[0]
    assert tag == 33
    assert gdirs == dirs[:gave_dirs] and gbatches == batches[:gave_batches]   # the oldest
    assert w.local_dirs == dirs[gave_dirs:] and w.local_batches == batches[gave_batches:]
    assert payload[2] == len(w.local_dirs) + len(w.local_batches)


def test_drain_processes_batches_first_then_the_open_batch(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("a", "b", "c", "sub/d"):
        (tmp_path / name).write_bytes(b"x")
    w = _batch_worker(2, 1 << 30)
    order = []
    w.process_batch = lambda batch: order.append(sorted(os.path.basename(p) for p in batch[0]))
    real = w.process_directory
    w.process_directory = lambda d: (order.append(os.path.basename(d)), real(d))
    w.local_dirs.append(str(tmp_path))
    w.drain_local_work()
    # the root's first two files close a batch, done before the subdirectory; the
    # rest (one root file, sub/d) combine in the open batch, closed and done last
    assert order[0] == tmp_path.name and order[2] == "sub"
    assert len(order[1]) == 2 and sorted(order[1] + order[3]) == ["a", "b", "c", "d"]
    assert (w.local_dirs, w.local_batches, w.batch) == ([], [], [])
