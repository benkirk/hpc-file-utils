"""Local queues with Manager-brokered sharing (phase 3.5; docs/walk-load-balancing.md).

Workers walk their own queue and only talk to the Manager on overflow, when idle, or
when asked to share. On a tree whose directories are too narrow to ever overflow, the
only way the idle ranks get work is the Manager's 'share' request, so every rank must
end up with some, and every entry must still be counted.
"""

import re

import pytest

pytestmark = pytest.mark.mpi

PER_RANK_RE = re.compile(r"^--> Directories per rank: min ([\d,]+) \(rank \d+\) / mean ([\d,]+) / max ([\d,]+) \(rank \d+\)", re.M)
WAIT_RE = re.compile(r"^--> Workers waited for the Manager ([\d.]+)% of the walk", re.M)
SHARED_RE = re.compile(r"directories received as overflow: ([\d,]+), shared: ([\d,]+) \(([\d,]+) share requests", re.M)


def per_rank(stdout):
    m = PER_RANK_RE.search(stdout)
    assert m, stdout[-3000:]
    return [int(x.replace(",", "")) for x in m.groups()]


def shared(stdout):
    m = SHARED_RE.search(stdout)
    assert m, stdout[-3000:]
    return [int(x.replace(",", "")) for x in m.groups()]


@pytest.fixture(scope="session")
def binary_tree(tmp_path_factory):
    """Depth 9, two subdirectories and one file per directory: 1,023 directories, each
    so narrow that a depth-first local queue never passes --local-queue-max."""
    root = tmp_path_factory.mktemp("binary") / "tree"
    root.mkdir(parents=True)
    ndirs = 0

    def grow(d, depth):
        nonlocal ndirs
        (d / "f").touch()
        if depth == 0:
            return
        for name in ("a", "b"):
            (d / name).mkdir()
            ndirs += 1
            grow(d / name, depth - 1)

    grow(root, 9)
    return root, ndirs, ndirs * 1 + (ndirs + 1)   # root, dirs, items (= dirs + files)


@pytest.mark.parametrize("args", [[], ["--dirs-per-dispatch", "1"], ["--local-queue-max", "2"]])
def test_every_rank_gets_work_through_sharing(walk, binary_tree, tmp_path, args):
    root, ndirs, nitems = binary_tree
    r = walk.run("walkstat", [root], cwd=tmp_path, nranks=4, args=args)
    assert r.ok, r.report()
    assert r.total_count() == nitems, r.report()
    lo, mean, hi = per_rank(r.stdout)
    assert lo >= 1, r.report()
    assert abs(mean * 3 - (ndirs + 1)) < 3, r.report()   # 3 workers walked each directory once
    overflow, nshared, nrequests = shared(r.stdout)
    if "--local-queue-max" not in args:
        assert overflow == 0, r.report()          # never overflowed...
        assert nshared > 0 and nrequests > 0, r.report()   # ... so the work moved by sharing


def test_single_worker_never_shares(walk, binary_tree, tmp_path):
    """With one worker there is nobody idle to share with: no requests, same count."""
    root, ndirs, nitems = binary_tree
    r = walk.run("walkstat", [root], cwd=tmp_path, nranks=2)
    assert r.ok, r.report()
    assert r.total_count() == nitems, r.report()
    assert per_rank(r.stdout) == [ndirs + 1, ndirs + 1, ndirs + 1]
    assert shared(r.stdout)[2] == 0


def test_wait_fraction_is_reported(walk, golden_tree, tmp_path):
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path)
    assert r.ok, r.report()
    assert r.total_count() == golden_tree.nitems, r.report()
    m = WAIT_RE.search(r.stdout)
    assert m, r.report()
    assert 0.0 <= float(m.group(1)) <= 100.0
