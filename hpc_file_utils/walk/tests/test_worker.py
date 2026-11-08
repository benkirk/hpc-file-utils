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
    w.rank, w.queue, w.local_dirs = 1, None, []
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
