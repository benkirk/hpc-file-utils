"""walksync's copy decision (sync/compare.py): pure, no MPI."""

import stat
from types import SimpleNamespace

import pytest

from hpc_file_utils.walk.sync.compare import extraneous, needs_copy


def st(kind="f", size=10, mtime=1000.0, rdev=0):
    mode = {"f": stat.S_IFREG, "d": stat.S_IFDIR, "l": stat.S_IFLNK, "p": stat.S_IFIFO,
            "c": stat.S_IFCHR}[kind] | 0o644
    return SimpleNamespace(st_mode=mode, st_size=size, st_mtime=mtime, st_rdev=rdev)


@pytest.mark.parametrize("src,dest,reason", [
    (st(), None, "new"),
    (st("d"), None, "new"),
    (st(), st(), None),
    (st(), st("d"), "type"),
    (st("d"), st(), "type"),
    (st("l"), st(), "type"),
    (st(size=10), st(size=11), "size"),
    (st(mtime=1000.0), st(mtime=1001.0), "mtime"),
    (st(mtime=1000.2), st(mtime=1000.9), None),      # whole seconds, as rsync compares
    (st("d", mtime=1), st("d", mtime=2), None),     # directory times: the final rsync's job
    (st("d", size=1), st("d", size=2), None),
    (st("p", mtime=1), st("p", mtime=2), None),
    (st("c", rdev=1), st("c", rdev=1), None),
    (st("c", rdev=1), st("c", rdev=2), "rdev"),
])
def test_needs_copy(src, dest, reason):
    assert needs_copy(src, dest) == reason


def test_symlinks_compare_targets_not_times():
    assert needs_copy(st("l", mtime=1), st("l", mtime=2), "a", "a") is None
    assert needs_copy(st("l"), st("l"), "a", "b") == "link"
    assert needs_copy(st("l", size=1), st("l", size=2), "a", "a") is None


def test_extraneous():
    assert extraneous(["a", "b"], ["b", "c", "a", "COLD_STORAGE"]) == ["COLD_STORAGE", "c"]
    assert extraneous(["a"], ["a", "c", "COLD_STORAGE"], exclude=lambda n: n.startswith("COLD")) == ["c"]
    assert extraneous([], []) == []
