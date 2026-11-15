"""What a worker gives away and what it does next (tail-fixes; docs/walk-tail-4F.md), in-process.

- A share hands over the oldest half of the local stack rounded up, so a rank busy with one
  long batch gives away the last directory it holds (4F: rounded down, it kept it).
- walksync walks before it copies: compare batches, then directories, then copy batches.
"""

from hpc_file_utils.walk.common.worker import BaseWorker
from hpc_file_utils.walk.sync.worker import CompareBatch, CopyBatch
from hpc_file_utils.walk.tests.test_index import _sync_worker


def _stack(dirs):
    w = object.__new__(BaseWorker)
    w.local_dirs = list(dirs)
    return w


def test_a_share_takes_the_last_directory():
    w = _stack(["only"])
    assert w.oldest_half() == ["only"] and w.local_dirs == []


def test_a_share_takes_the_oldest_half_rounded_up():
    w = _stack(["a", "b", "c"])
    assert w.oldest_half() == ["a", "b"] and w.local_dirs == ["c"]
    w = _stack(["a", "b", "c", "d"])
    assert w.oldest_half() == ["a", "b"] and w.local_dirs == ["c", "d"]
    assert _stack([]).oldest_half() == []


def _copy(name):
    return CopyBatch([name], 1, None, ())


def _compare(name):
    return CompareBatch(name, [])


def test_walksync_picks_compare_batches_first(tmp_path):
    w = _sync_worker(tmp_path, tmp_path, index=False)
    w.local_batches = [_copy("c1"), _compare("x1"), _copy("c2"), _compare("x2")]
    assert w.next_batch() == _compare("x2")
    assert w.next_batch() == _compare("x1")
    assert w.next_batch() is None                     # only copies left: walk first
    assert w.local_batches == [_copy("c1"), _copy("c2")]


def test_walksync_walks_before_it_copies(tmp_path):
    """drain_local_work(): the compare batch, then the directory (whose scan makes another
    compare batch, done before any copy), then the copy batches, newest first."""
    w = _sync_worker(tmp_path, tmp_path, index=False)
    w.max_local_dirs = 0
    done = []

    def process_batch(batch):
        done.append(("compare", batch.reldir) if isinstance(batch, CompareBatch) else ("copy", batch.paths[0]))

    def process_directory(d):
        done.append(("dir", d))
        w.local_batches.append(_compare(d + "/found"))

    w.process_batch, w.process_directory = process_batch, process_directory
    w.local_dirs = ["d1"]
    w.local_batches = [_copy("c1"), _compare("x1"), _copy("c2")]
    w.drain_local_work()
    assert done == [("compare", "x1"), ("dir", "d1"), ("compare", "d1/found"), ("copy", "c2"), ("copy", "c1")]


def test_other_tools_still_take_batches_first():
    w = object.__new__(BaseWorker)
    w.local_batches = ["b1", "b2"]
    assert w.next_batch() == "b2" and w.next_batch() == "b1" and w.next_batch() is None
