"""walkrm: the tree is gone, nothing outside it is touched, and every
refusal leaves the tree as it was. MPI end to end on a private golden tree (its read-only
subtree, symlinks, a fifo, hard links), find(1) as the oracle for the counts and the dry
run's list; the scan's guards in-process."""

import os
import re
import stat
import subprocess
import types
from pathlib import Path

import pytest

from hpc_file_utils.walk.common import worker as common_worker
from hpc_file_utils.walk.common.stats import WalkStats
from hpc_file_utils.walk.rm.worker import RmBatch, RmWorker, rmdir_pass
from hpc_file_utils.walk.tests.helpers import make_options

REMOVED_RE = re.compile(r"^--> Removed ([\d,]+) files, links and specials and ([\d,]+) directories \(([\d,]+) made", re.M)
DRY_RE = re.compile(r"^--> Dry run: would remove ([\d,]+) files, links and specials and ([\d,]+) directories", re.M)


def find(root, *preds, mindepth=0):
    out = subprocess.run(["find", str(root), "-mindepth", str(mindepth), *preds, "-print0"],
                         capture_output=True, check=True).stdout
    return {os.fsdecode(p) for p in out.split(b"\0") if p}


def listing(root):
    """Every path below root with its lstat mode and size: what 'untouched' means."""
    return {p: (os.lstat(p).st_mode, os.lstat(p).st_size) for p in find(root)}


def outside_links(tree, tmp_path):
    """A file and a directory outside the tree, and symlinks to them inside it."""
    out = tmp_path / "outside"
    (out / "dir").mkdir(parents=True)
    (out / "file.dat").write_bytes(b"keep me")
    (out / "dir" / "inner.dat").write_bytes(b"keep me too")
    os.symlink(out / "file.dat", tree / "data" / "out-file")
    os.symlink(out / "dir", tree / "data" / "out-dir")
    return out


def walkrm(walk, tree, cwd, *args, root=None):
    root = root or tree.parent
    return walk.run("walkrm", [tree], cwd=cwd, args=["--allowed-root", root, *args])


@pytest.fixture
def rm_tree(private_golden_tree, tmp_path):
    tree = private_golden_tree.root
    out = outside_links(tree, tmp_path)
    return tree, out


@pytest.mark.mpi
def test_walkrm_removes_the_tree_and_nothing_outside(walk, rm_tree, tmp_path):
    tree, out = rm_tree
    nondirs, dirs = len(find(tree, "!", "-type", "d")), len(find(tree, "-type", "d"))
    before_out = listing(out)
    r = walkrm(walk, tree, tmp_path / "run")
    assert r.ok, r.report()
    assert not os.path.lexists(tree)
    assert listing(out) == before_out and (out / "file.dat").read_bytes() == b"keep me"
    removed, rmdirs, chmods = r.ints(REMOVED_RE)
    assert (removed, rmdirs) == (nondirs, dirs)
    assert chmods == 2                                   # readonly/ and readonly/sub/ (0555)


@pytest.mark.mpi
def test_keep_root_empties_the_target(walk, rm_tree, tmp_path):
    tree, _ = rm_tree
    dirs = len(find(tree, "-type", "d", mindepth=1))
    r = walkrm(walk, tree, tmp_path / "run", "--keep-root")
    assert r.ok, r.report()
    assert tree.is_dir() and not any(tree.iterdir())
    assert r.ints(REMOVED_RE)[1] == dirs


@pytest.mark.mpi
@pytest.mark.parametrize("keep", [False, True], ids=["whole", "keep-root"])
def test_dry_run_lists_what_find_lists_and_changes_nothing(walk, rm_tree, tmp_path, keep):
    tree, _ = rm_tree
    before = listing(tree)
    cwd = tmp_path / "run"
    r = walkrm(walk, tree, cwd, "--dry-run", *(["--keep-root"] if keep else []))
    assert r.ok, r.report()
    assert listing(tree) == before
    listed = [line for f in sorted(cwd.glob("walkrm-dryrun-r*.txt")) for line in f.read_text().splitlines()]
    expected = find(tree, mindepth=1 if keep else 0)
    printable = {p.replace("\n", "\\n") for p in expected}
    assert len(listed) == len(set(listed)) and set(listed) == printable
    assert r.ints(DRY_RE) == (len(find(tree, "!", "-type", "d")), len(find(tree, "-type", "d", mindepth=1 if keep else 0)))


@pytest.mark.mpi
def test_a_big_directory_is_unlinked_by_several_ranks(walk, tmp_path):
    root = tmp_path / "allowed"
    big = root / "victim" / "big"
    big.mkdir(parents=True)
    for i in range(2000):                  # 400 unlink batches: idle ranks take some, even on a quick runner
        (big / f"f{i:04d}").write_bytes(b"x")
    r = walkrm(walk, root / "victim", tmp_path / "run", "--batch-files", "5", "--verbose", root=root)
    assert r.ok, r.report()
    assert not (root / "victim").exists() and root.is_dir()
    (shared,) = r.ints(r"--> Batches: ([\d,]+) received in share replies")
    assert shared > 0, r.report()
    assert r.ints(REMOVED_RE)[:2] == (2000, 2)


def _refusals(tree, tmp_path):
    allowed = str(tree.parent)
    link = tree.parent / "link-to-tree"
    link.symlink_to(tree)
    return {   # case: (args, dirs, what stderr says)
        "no-allowed-root":  ([], [tree], "--allowed-root"),
        "target-is-root":   (["--allowed-root", allowed], [tree.parent], "is not strictly below"),
        "outside-the-root": (["--allowed-root", str(tree / "data")], [tree], "is not strictly below"),
        "a-symlink":        (["--allowed-root", allowed], [link], "a symlink"),
        "slash":            (["--allowed-root", allowed], ["/"], "will not remove '/'"),
        "slash-root":       (["--allowed-root", "/"], [tree], "cannot be '/'"),
        "nested":           (["--allowed-root", allowed], [tree, tree / "data"], "give the outer one only"),
        "twice":            (["--allowed-root", allowed], [tree, f"{tree}/"], "is given twice"),
        "exclude":          (["--allowed-root", allowed, "--exclude", "COLD*"], [tree], "takes no filters"),
        "type":             (["--allowed-root", allowed, "--type", "f"], [tree], "takes no filters"),
    }


REFUSALS = ["no-allowed-root", "target-is-root", "outside-the-root", "a-symlink", "slash", "slash-root",
            "nested", "twice", "exclude", "type"]


@pytest.mark.mpi
@pytest.mark.parametrize("case", REFUSALS)
def test_refusals_touch_nothing(walk, rm_tree, tmp_path, case):
    tree, out = rm_tree
    args, dirs, message = _refusals(tree, tmp_path)[case]
    before, before_out = listing(tree.parent), listing(out)
    r = walk.run("walkrm", dirs, cwd=tmp_path / "run", args=args)
    assert not r.timed_out and r.returncode != 0 and message in r.stderr, r.report()
    assert listing(tree.parent) == before and listing(out) == before_out
    assert not list((tmp_path / "run").glob("walkrm-dryrun-r*.txt"))


# ---------------------------------------------------------------------------
# in-process: the scan's guards (no MPI)
# ---------------------------------------------------------------------------

def _rm_worker(target, dry_run=False):
    w = object.__new__(RmWorker)   # skip MPIClass.__init__
    w.rank, w.local_dirs, w.local_batches, w.batch, w.batch_bytes = 1, [], [], [], 0
    w.max_local_dirs, w.files = 0, []
    w.options = make_options("walkstat", batch_files=1000, local_queue_max=1000)
    w.stats = WalkStats(heap_size=10)
    w.check_share = w.report_progress = lambda *a, **k: None
    w.exclude = w.select = None
    w.dry_run, w.keep, w.uid = dry_run, frozenset(), os.geteuid()
    w.target_devs = {str(target): os.lstat(target).st_dev}
    w.targets = [(str(target) + "/", os.lstat(target).st_dev)]
    w.scanned = {}
    w.dryrun = types.SimpleNamespace(write=lambda text: None)
    return w


class _Entry:
    """A scandir entry whose stat() reports other fields."""

    def __init__(self, entry, **fields):
        self._entry, self._fields = entry, fields
        self.name, self.path = entry.name, entry.path

    def is_dir(self, follow_symlinks=True):
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def stat(self, follow_symlinks=True):
        s = self._entry.stat(follow_symlinks=follow_symlinks)
        return types.SimpleNamespace(**{**{k: getattr(s, k) for k in dir(s) if k.startswith("st_")}, **self._fields})


class _Scan:
    def __init__(self, entries):
        self._entries = list(entries)

    def __iter__(self):
        return iter(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_scan(monkeypatch, **fields):
    real = os.scandir
    monkeypatch.setattr(common_worker.os, "scandir",
                        lambda d: _Scan(_Entry(e, **fields) if e.is_dir(follow_symlinks=False) else e for e in real(d)))


def test_a_subdirectory_on_another_filesystem_is_left_alone(tmp_path, monkeypatch):
    target = tmp_path / "t"
    (target / "mnt").mkdir(parents=True)
    (target / "f").write_bytes(b"x")
    w = _rm_worker(target)
    _patch_scan(monkeypatch, st_dev=os.lstat(target).st_dev + 1)
    w.process_directory(str(target))
    assert w.local_dirs == [] and w.stats.counters["cross_device"] == 1 and w.stats.num_errors == 1
    assert w.local_batches == [RmBatch(str(target), ["f"], os.lstat(target).st_dev, os.lstat(target).st_ino)]


def test_a_read_only_directory_not_ours_is_an_error(tmp_path, monkeypatch):
    target = tmp_path / "t"
    (target / "theirs").mkdir(parents=True)
    (target / "theirs").chmod(0o555)
    try:
        w = _rm_worker(target)
        _patch_scan(monkeypatch, st_uid=os.geteuid() + 1)
        w.process_directory(str(target))
        assert w.local_dirs == [] and w.stats.num_errors == 1 and w.stats.counters["chmod"] == 0
        assert stat.S_IMODE(os.lstat(target / "theirs").st_mode) == 0o555
    finally:
        (target / "theirs").chmod(0o755)


def test_our_read_only_directory_gets_u_rwx(tmp_path):
    target = tmp_path / "t"
    (target / "ro").mkdir(parents=True)
    (target / "ro").chmod(0o500)
    w = _rm_worker(target)
    w.process_directory(str(target))
    assert w.local_dirs == [str(target / "ro")] and w.stats.counters["chmod"] == 1
    assert stat.S_IMODE(os.lstat(target / "ro").st_mode) == 0o700


def test_an_unlink_batch_is_refused_in_another_directory(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    (target / "f").write_bytes(b"x")
    w = _rm_worker(target)
    s = os.lstat(target)
    w.process_batch(RmBatch(str(target), ["f"], s.st_dev, s.st_ino + 1))   # replaced since its scan
    assert (target / "f").exists() and w.stats.num_errors == 1


def test_rmdir_pass_keeps_kept_and_counts_failures(tmp_path):
    for d in ("a/x", "a/y", "b", "keep"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "b" / "f").write_bytes(b"x")                 # not empty: an error
    w = types.SimpleNamespace(stats=WalkStats(heap_size=10), errors=[])
    w.error = lambda what, err: w.errors.append(what)
    rmdir_pass(w, [str(tmp_path / d) for d in ("a/y", "keep", "a/x", "b", "gone")], frozenset([str(tmp_path / "keep")]))
    assert not (tmp_path / "a/x").exists() and not (tmp_path / "a/y").exists()
    assert (tmp_path / "keep").exists() and (tmp_path / "b").exists()
    assert w.stats.counters["dirs_removed"] == 2 and w.stats.counters["vanished"] == 1
    assert w.errors == ["remove directory"]
