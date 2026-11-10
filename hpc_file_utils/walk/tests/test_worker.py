"""The workers' scans and batches on their own, in-process (no MPI): walkstat's StatWorker
(stat batches), and walktar's TarWorker, whose stat batches fill its file batches."""

import os
import tarfile
import types

import pytest

from hpc_file_utils.walk.common import worker as worker_mod
from hpc_file_utils.walk.common.stats import WalkStats
from hpc_file_utils.walk.common.worker import FileBatch
from hpc_file_utils.walk.stat.worker import StatBatch, StatWorker
from hpc_file_utils.walk.tar.worker import TarWorker
from hpc_file_utils.walk.tests.helpers import make_options


def _with_uid(s, uid):
    """A stat result that reports a different owner."""
    fields = {k: getattr(s, k) for k in dir(s) if k.startswith("st_")}
    fields["st_uid"] = uid
    return types.SimpleNamespace(**fields)


class _Scan:
    """A stand-in for os.scandir()'s iterator, which is also a context manager."""

    def __init__(self, entries):
        self._it = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Entry:
    """A scandir entry whose stat() reports a different owner."""

    def __init__(self, entry, uid):
        self._entry, self._uid = entry, uid
        self.path, self.name = entry.path, entry.name

    def is_dir(self, follow_symlinks=True):
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def stat(self, follow_symlinks=True):
        return _with_uid(self._entry.stat(follow_symlinks=follow_symlinks), self._uid)


def _worker(cls=StatWorker, app="walkstat", **options):
    w = object.__new__(cls)   # skip MPIClass.__init__: no MPI here
    w.rank, w.local_dirs, w.local_batches, w.batch, w.batch_bytes = 1, [], [], [], 0
    w.max_local_dirs = 0
    w.options = make_options(app, threshold_count=1, threshold_size=0, **options)
    w.stats = WalkStats(heap_size=10)
    w.check_share = w.report_progress = lambda *a, **k: None
    return w


@pytest.mark.parametrize("uid", [0, 1234])
def test_single_owner_dirs_are_listed_for_that_uid(tmp_path, monkeypatch, uid):
    """A directory whose entries all belong to one uid goes on that user's lists,
    uid 0 included: root runs the scans (it was dropped when the check was truthiness)."""
    for name in ("a.dat", "b.dat"):
        (tmp_path / name).write_bytes(b"x" * 4096)
    real_scandir, real_lstat = os.scandir, os.lstat
    monkeypatch.setattr(worker_mod.os, "scandir",
                        lambda d: _Scan(_Entry(e, uid) for e in real_scandir(d)))
    monkeypatch.setattr(worker_mod.os, "lstat", lambda p: _with_uid(real_lstat(p), uid))   # the stat batches

    w = _worker()
    _walk(w, tmp_path)
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
    """Walk root on this one worker, batches first (as drain_local_work does), then list
    the directories that were split over batches (as rank 0 does after the reduction)."""
    w.local_dirs.append(str(root))
    while w.local_batches or w.local_dirs:
        if w.local_batches:
            w.process_batch(w.local_batches.pop())
        else:
            w.process_directory(w.local_dirs.pop())
    w.stats.finish_directories(w.options.threshold_count, w.options.threshold_size,
                               w.select is not None)


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
    assert (len(shares), len(updates)) == (2, 3)     # the scan: every 3 and every 2 of 7
    w.process_batch(w.local_batches.pop())           # the stats: every 3 of the 6 names
    assert w.stats.num_items == 0 and w.stats.num_excluded == 1 and w.stats.num_unmatched == 6
    assert (len(shares), len(updates)) == (4, 5)


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
                        lambda d: _Scan(_Unstattable(e) if e.name.startswith("COLD") else e for e in real_scandir(d)))
    w = _filtered_worker(tmp_path, "--exclude", "COLD*")
    _walk(w, tmp_path)
    st = w.stats
    assert st.num_errors == 0
    assert (st.num_items, st.num_excluded, st.num_dirs) == (1, 1, 1)
    assert dict(st.st_modes) == {"dir": 1, "reg": 1}   # pruning alone keeps the root in 'dir'


# ---------------------------------------------------------------------------
# phase 5 batches
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A tree to walk, apart from the tars: a TarWorker writes its tars into the current
    directory, which must not be the tree."""
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path / "out")
    tree = tmp_path / "tree"
    tree.mkdir()
    return tree


def _batch_worker(batch_files, batch_size, read_ahead=0):
    """A TarWorker: walkstat's scan and stat batches, whose selected files fill the open
    FileBatch (the tars go into the current directory: the tree fixture's out/)."""
    w = _worker(TarWorker, "walktar", batch_files=batch_files, batch_size=batch_size, read_ahead=read_ahead)
    w.ntars = w.nwritten_files = w.nwritten_closed = 0
    w.batch_tar = w.dirs_tar = w.readers = None
    return w


def _tar_members(w):
    """{tar name: [member names]} of the tars w wrote into the current directory, after
    finishing its dirs tar and its readers as run() does."""
    if w.dirs_tar is not None:
        w.dirs_tar.close()
        w.dirs_tar = None
    if w.readers is not None:
        w.readers.shutdown()
        w.readers = None
    out = {}
    for t in sorted(os.listdir(".")):
        if t.endswith(".tar"):
            with tarfile.open(t) as tf:
                out[t] = tf.getnames()
    return out


def test_batches_close_at_n_files_or_b_bytes():
    w = _batch_worker(3, 100)
    for i in range(7):
        w.add_to_batch(f"f{i}", 1)
    assert all(isinstance(b, FileBatch) for b in w.local_batches)
    assert [b.paths for b in w.local_batches] == [["f0", "f1", "f2"], ["f3", "f4", "f5"]]
    assert (w.batch, w.batch_bytes) == (["f6"], 1)
    w.add_to_batch("big", 500)                 # over --batch-size: closes at once
    assert w.local_batches[-1] == (["f6", "big"], 501)
    w.add_to_batch("huge", 10**6)              # a file bigger than a batch is one alone
    assert w.local_batches[-1] == (["huge"], 10**6) and w.batch == []
    w.close_batch()                            # nothing open: no empty batch
    assert len(w.local_batches) == 4


def test_small_directories_combine_into_one_batch(tree):
    for d in ("a", "b", "c"):
        (tree / d).mkdir()
        (tree / d / "x.dat").write_bytes(b"x" * 10)
    w = _batch_worker(5000, 1 << 30)
    _walk(w, tree)
    assert w.local_batches == []               # still open, across the three directories' stat batches
    assert sorted(os.path.basename(os.path.dirname(p)) for p in w.batch) == ["a", "b", "c"]
    assert w.batch_bytes == 30
    assert w.stats.num_items == 6 and w.stats.num_dirs == 4


def test_walktar_scan_stats_nothing_but_subdirectories(tree, monkeypatch):
    """walktar's scan is walkstat's: the files wait in a StatBatch (no stat, so no size
    yet), and the open FileBatch fills only when that batch is processed, by whichever
    rank takes it."""
    for i in range(3):
        (tree / f"f{i}").write_bytes(b"x" * 100)
    (tree / "sub").mkdir()
    w = _batch_worker(1000, 1 << 30)
    real_lstat, stats = os.lstat, []
    monkeypatch.setattr(worker_mod.os, "lstat", lambda p: (stats.append(p), real_lstat(p))[1])
    w.process_directory(str(tree))
    files = sorted(str(tree / f"f{i}") for i in range(3))
    [batch] = w.local_batches
    assert isinstance(batch, StatBatch) and sorted(batch.names) == ["f0", "f1", "f2"]
    assert (w.batch, w.batch_bytes) == ([], 0)
    assert not set(stats) & set(files)                         # no file stat'ed yet (sub: di.stat(); the dirs tar: the root)
    assert w.stats.num_items == 1                              # sub
    other = _batch_worker(1000, 1 << 30)
    other.rank = 2
    other.process_batch(w.local_batches.pop())                 # 'shared' to another rank
    assert (sorted(other.batch), other.batch_bytes) == (files, 300)
    assert other.stats.num_items == 3 and w.stats.num_items == 1
    assert sorted(set(stats) & set(files)) == files
    assert _tar_members(w) == {"output-r001-dirs.tar": [str(tree).lstrip("/")]}


@pytest.mark.parametrize("batch_files, batch_size, expected", [
    (2, 1 << 30, [2, 2, 1]),                   # by count (and the stat batches are cut at 2 names too)
    (1000, 250, [3, 2]),                       # by the real st_size: 100-byte files close a batch at >= 250
    (1000, 1 << 30, [5]),
])
@pytest.mark.parametrize("read_ahead", [0, 4])
def test_stat_batches_close_file_batches_by_count_or_real_size(tree, batch_files, batch_size, expected, read_ahead):
    """--batch-files and --batch-size keep their meaning: the rank processing a stat batch
    closes FileBatches by the files it has stat'ed (in the reader threads with
    --read-ahead, in order), and they stay on its own list (manager.py, point 1),
    processed before its next 'ready'."""
    for i in range(5):
        (tree / f"f{i}").write_bytes(b"x" * 100)
    w = _batch_worker(batch_files, batch_size, read_ahead)
    w.local_dirs.append(str(tree))
    w.drain_local_work()
    assert (w.local_dirs, w.local_batches, w.batch) == ([], [], [])
    tars = _tar_members(w)
    batch_tars = sorted(t for t in tars if "-b" in t)
    assert sorted(len(tars[t]) for t in batch_tars) == sorted(expected)
    assert sorted(n for t in batch_tars for n in tars[t]) == sorted(str(tree / f"f{i}").lstrip("/") for i in range(5))
    assert w.nwritten_files == 5 and w.ntars == len(expected)
    assert (w.stats.num_items, w.stats.total_size) == (5, 500)
    w.stats.finish_directories(1, 0, False)    # a directory split over stat batches is listed by rank 0
    assert [e.nitems for _, e in w.stats.heaps["nitems_dirs"].top()] == [5]   # listed once, whole


@pytest.mark.parametrize("read_ahead", [0, 4])
def test_a_vanished_file_costs_walktar_one_error(tree, read_ahead, caplog):
    """A name that fails its lstat, in a reader thread or not, counts one error and the
    rest of the batch is archived in order."""
    for name in ("a", "b", "c", "d"):
        (tree / name).write_bytes(b"x")
    w = _batch_worker(1000, 1 << 30, read_ahead)
    w.process_directory(str(tree))
    (tree / "b").unlink()                      # between the scan and its stat batch
    [batch] = w.local_batches
    w.process_batch(batch)
    assert (w.stats.num_items, w.stats.num_errors) == (3, 1)
    assert w.batch == [str(tree / n) for n in batch.names if n != "b"]      # the batch's order
    assert "Cannot stat" in caplog.text
    _tar_members(w)                            # shuts the readers down


def test_a_file_that_became_a_directory_is_counted_not_archived(tree):
    (tree / "f").write_bytes(b"x")
    (tree / "g").write_bytes(b"x")
    w = _batch_worker(1000, 1 << 30)
    w.process_directory(str(tree))
    (tree / "f").unlink()
    (tree / "f").mkdir()                       # between the scan and its stat batch
    w.process_batch(w.local_batches.pop())
    assert w.batch == [str(tree / "g")]
    assert (w.stats.num_items, dict(w.stats.st_modes)) == (2, {"dir": 1, "reg": 1})


def test_walkstat_batches_names_not_paths(tmp_path):
    """walkstat's scan stats nothing but subdirectories: the names wait in a batch of the
    directory's own, which carries the subdirectories' totals (and never uses the open,
    cross-directory batch, which is walktar's)."""
    for i in range(5):
        (tmp_path / f"f{i}").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    w = _worker()
    w.process_directory(str(tmp_path))
    assert w.stats.num_items == 1                    # sub, stat'ed by the scan
    [batch] = w.local_batches
    assert isinstance(batch, StatBatch)
    subdirs, whole = batch.tail
    assert (batch.dirname, sorted(batch.names), subdirs.nitems, whole) == (str(tmp_path), [f"f{i}" for i in range(5)], 1, True)
    assert (w.batch, w.batch_bytes) == ([], 0)
    assert w.progress_payload()[2:] == (2, None)     # sub on the stack, one batch


def _dir_lists(stats):
    return {name: [(k, tuple(e)) for k, e in stats.heaps[name].top()]
            for name in ("nitems_dirs", "nbytes_dirs")}


@pytest.mark.parametrize("stat_threads", [0, 3], ids=["serial", "threads"])
@pytest.mark.parametrize("batch_files", [1, 2, 3, 7])
def test_split_directories_list_the_same_totals(tmp_path, batch_files, stat_threads):
    """A directory split over stat batches adds up to the same line as a whole one, with
    the batch's stats serial or --stat-threads at a time."""
    (tmp_path / "a/b").mkdir(parents=True)
    for i in range(7):
        (tmp_path / f"a/f{i}").write_bytes(b"x" * 1024 * (i + 1))
    (tmp_path / "a/b/g").write_bytes(b"y" * 4096)
    whole, split = _worker(), _worker(stat_threads=stat_threads)
    split.options.batch_files = batch_files
    _walk(whole, tmp_path)
    _walk(split, tmp_path)
    assert split.stats.partial_dirs == {}            # all listed by finish_directories()
    assert _dir_lists(split.stats) == _dir_lists(whole.stats)
    assert (split.stats.num_items, split.stats.total_size) == (whole.stats.num_items, whole.stats.total_size)
    assert (split.stat_threads is not None) == (stat_threads > 0)   # the pool exists only when asked for
    _shutdown(split)


def _shutdown(w):
    """Shut a worker's stat threads down as run() does (no MPI here)."""
    if w.stat_threads is not None:
        w.stat_threads.shutdown()
        w.stat_threads = None


@pytest.mark.parametrize("stat_threads", [0, 2, 16])
def test_lstats_keep_the_order_and_yield_errors_in_place(tmp_path, stat_threads):
    """lstats() yields (path, result) in the batch's order whatever the thread count, an
    OSError standing in for a name that vanished (--stat-threads, perf-1 step C)."""
    names = [f"f{i:02d}" for i in range(40)]
    for name in names:
        (tmp_path / name).write_bytes(b"x" * (int(name[1:]) + 1))
    (tmp_path / "f07").unlink()
    (tmp_path / "f30").unlink()
    w = _worker(stat_threads=stat_threads)
    out = list(w.lstats(str(tmp_path), names))
    assert [os.path.basename(p) for p, _ in out] == names
    for path, st in out:
        if os.path.basename(path) in ("f07", "f30"):
            assert isinstance(st, FileNotFoundError)
        else:
            assert st.st_size == int(os.path.basename(path)[1:]) + 1
    _shutdown(w)


def test_split_directory_parts_merge_across_ranks(tmp_path):
    """Parts of one directory processed on different ranks add up in the reduction."""
    for i in range(6):
        (tmp_path / f"f{i}").write_bytes(b"x" * 1024)
    whole = _worker()
    _walk(whole, tmp_path)
    scanner, other = _worker(), _worker()
    scanner.options.batch_files = other.options.batch_files = 2
    scanner.process_directory(str(tmp_path))
    other.process_batch(scanner.local_batches.pop(0))          # 'shared' to another rank
    while scanner.local_batches:
        scanner.process_batch(scanner.local_batches.pop())
    merged = WalkStats.merged(scanner.stats, other.stats)
    merged.finish_directories(1, 0, False)
    assert _dir_lists(merged) == _dir_lists(whole.stats)


@pytest.mark.parametrize("stat_threads", [0, 4], ids=["serial", "threads"])
def test_a_vanished_file_costs_one_error_not_the_directory(tmp_path, stat_threads):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_bytes(b"x")
    w = _worker(stat_threads=stat_threads)
    w.process_directory(str(tmp_path))
    (tmp_path / "b").unlink()                        # between the scan and the stat
    while w.local_batches:
        w.process_batch(w.local_batches.pop())
    assert (w.stats.num_items, w.stats.num_errors) == (2, 1)
    assert [e.nitems for _, e in w.stats.heaps["nitems_dirs"].top()] == [2]
    _shutdown(w)


def _share_worker(dirs, batches):
    w = _worker()
    del w.check_share                          # the real method
    sent = []
    w.comm = types.SimpleNamespace(iprobe=lambda source, tag: True, recv=lambda source, tag: None,
                                   send=lambda msg, dest, tag: sent.append((msg, tag)))
    w.local_dirs, w.local_batches = list(dirs), list(batches)
    w.progress_extra = lambda: None
    return w, sent


@pytest.mark.parametrize("ndirs, nbatches, gave_dirs, gave_batches", [
    (0, 0, 0, 0), (1, 1, 0, 1), (4, 3, 2, 2), (5, 4, 2, 2), (0, 5, 0, 3)])
def test_share_gives_oldest_half_of_dirs_and_batches_rounded_up(ndirs, nbatches, gave_dirs, gave_batches):
    dirs = [f"d{i}" for i in range(ndirs)]
    batches = [FileBatch([f"b{i}"], 1) for i in range(nbatches)]
    w, sent = _share_worker(dirs, batches)
    w.check_share()
    (gdirs, gbatches, payload), tag = sent[0]
    assert tag == 33
    assert gdirs == dirs[:gave_dirs] and gbatches == batches[:gave_batches]   # the oldest
    assert w.local_dirs == dirs[gave_dirs:] and w.local_batches == batches[gave_batches:]
    assert payload[2] == len(w.local_dirs) + len(w.local_batches)


def test_drain_processes_batches_first_then_the_open_batch(tree):
    (tree / "sub").mkdir()
    for name in ("a", "b", "c", "sub/d"):
        (tree / name).write_bytes(b"x")
    w = _batch_worker(2, 1 << 30)
    order = []
    real_batch, real_dir = w.process_batch, w.process_directory
    w.process_batch = lambda batch: (order.append(("stat", batch.dirname) if isinstance(batch, StatBatch) else
                                                  ("tar", sorted(os.path.basename(p) for p in batch.paths))),
                                     real_batch(batch))
    w.process_directory = lambda d: (order.append(("dir", d)), real_dir(d))
    w.local_dirs.append(str(tree))
    w.drain_local_work()
    # the root's scan cuts its three names into two stat batches; their stats close one
    # FileBatch of two files, done before the subdirectory; the rest (one root file,
    # sub/d) combine in the open batch, closed and done last
    root, sub = str(tree), str(tree / "sub")
    assert [o[0] for o in order] == ["dir", "stat", "stat", "tar", "dir", "stat", "tar"]
    assert [o[1] for o in order if o[0] != "tar"] == [root, root, root, sub, sub]
    tars = [o[1] for o in order if o[0] == "tar"]
    assert len(tars[0]) == 2 and sorted(tars[0] + tars[1]) == ["a", "b", "c", "d"]
    assert (w.local_dirs, w.local_batches, w.batch) == ([], [], [])
