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
