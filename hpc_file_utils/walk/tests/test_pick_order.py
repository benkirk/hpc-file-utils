"""What a worker gives away, what it does next, and what the Manager hands out first
(docs/walk-tail-4F.md), in-process.

- A share hands over the oldest half of the local stack rounded up, so a rank busy with one
  long batch gives away the last directory it holds (4F: rounded down, it kept it).
- walksync walks before it copies: compare batches, then directories, then copy batches.
- walksync copies its costliest batches first, on a rank, in a share and at the Manager.
"""

from hpc_file_utils.walk.common.manager import BatchQueue
from hpc_file_utils.walk.common.worker import BaseWorker, FileBatch
from hpc_file_utils.walk.sync.worker import CompareBatch, CopyBatch, copy_cost
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


def _copy(name, cost=0):
    return CopyBatch([name], 1, None, (), cost)


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


def test_copy_cost_is_the_longest_stream():
    """One file is one rsync stream; the rest of a batch is spread over --rsyncs."""
    GiB = 1 << 30
    assert copy_cost(900 * GiB, 900 * GiB, 8) == 900 * GiB       # one huge file
    assert copy_cost(16 * GiB, 16 * GiB // 1000, 8) == 2 * GiB   # many small files: nbytes / rsyncs
    assert copy_cost(16 * GiB, 10 * GiB, 8) == 10 * GiB          # its largest file is the floor
    assert copy_cost(10, 3, 3) == 4                              # rounded up
    assert copy_cost(0, 0, 8) == 0                               # directories only


def test_walksync_batches_get_their_cost(tmp_path):
    w = _sync_worker(tmp_path, tmp_path, index=False)             # batch_files=2, 3 rsyncs
    for name, size in [("a", 90), ("b", 3), ("c", 30), ("d", 30), ("e", 5)]:
        w.want_copy(name, size, "new", [])
    w.close_batch()
    assert [(b.paths, b.nbytes, b.cost) for b in w.local_batches] == [
        (["a", "b"], 93, 90), (["c", "d"], 60, 30), (["e"], 5, 5)]
    assert w.batch_largest == 0


def test_walksync_copies_the_costliest_batch_first(tmp_path):
    w = _sync_worker(tmp_path, tmp_path, index=False)
    done = []
    w.process_batch = lambda batch: done.append(batch.paths[0])
    w.local_dirs = []
    w.local_batches = [_copy("small", 1), _copy("huge", 900), _copy("mid", 16), _copy("mid2", 16)]
    w.drain_local_work()
    assert done == ["huge", "mid2", "mid", "small"]


def test_a_walksync_share_takes_compares_then_the_costliest_copies(tmp_path):
    w = _sync_worker(tmp_path, tmp_path, index=False)
    w.local_batches = [_copy("small", 1), _compare("x1"), _copy("huge", 900), _copy("mid", 16), _compare("x2")]
    assert w.oldest_batches() == [_compare("x2"), _compare("x1"), _copy("huge", 900)]   # half, rounded up
    assert w.local_batches == [_copy("small", 1), _copy("mid", 16)]
    assert w.last_batch() == _copy("mid", 16)
    assert w.oldest_batches() == [_copy("small", 1)]              # the only other it holds
    assert w.oldest_batches() == [] and w.local_batches == []


def test_a_batch_closed_after_a_sort_is_still_picked_in_order(tmp_path):
    w = _sync_worker(tmp_path, tmp_path, index=False)
    w.local_batches = [_copy("mid", 16), _copy("small", 1)]
    assert w.last_batch() == _copy("mid", 16)
    w.want_copy("late", 500, "new", [])
    w.close_batch()
    assert w.last_batch().paths == ["late"] and w.last_batch() == _copy("small", 1)


def test_the_manager_hands_out_the_walk_then_the_costliest_copies():
    q = BatchQueue()
    assert not q and len(q) == 0
    tar1, tar2 = FileBatch(["t1"], 5), FileBatch(["t2"], 500)
    q.extend([_copy("small", 1), tar1, _copy("huge", 900), _compare("x1")])
    q.extend([_copy("mid", 16), tar2, _copy("mid2", 16)])
    assert q and len(q) == 7 and len(q.fifo) == 3
    # batches without a cost in arrival order, whatever their size; then by cost, ties in arrival order
    assert [q.pop() for _ in range(7)] == [tar1, _compare("x1"), tar2, _copy("huge", 900), _copy("mid", 16),
                                           _copy("mid2", 16), _copy("small", 1)]
    assert not q


def test_other_tools_still_take_batches_first():
    w = object.__new__(BaseWorker)
    w.local_batches = ["b1", "b2"]
    assert w.next_batch() == "b2" and w.next_batch() == "b1" and w.next_batch() is None
