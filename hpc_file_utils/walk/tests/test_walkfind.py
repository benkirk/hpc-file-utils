"""walkfind, end to end under MPI, with find(1) as the oracle.

walkfind's list (--print0, joined by --output) is the set of paths that
    find TREE -mindepth 1 [( -name X ... ) -prune -o] [( -name G -o ... )] [-prune] [( preds )] -print0
prints, and its walked count is every entry find visits (pruned directories listed, their
contents not). Only predicates GNU and BSD find both accept (see test_filtering.py).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from hpc_file_utils.walk.common.index import closure_errors, entries

pytestmark = pytest.mark.mpi

FIND_RE = re.compile(r"^--> Find: walked ([\d,]+) entries below the roots \(([\d,]+) directories scanned\); "
                     r"matched ([\d,]+)", re.M)


def _alt(flag, values):
    """( flag v1 -o flag v2 ... ), or nothing for no values."""
    if not values:
        return []
    expr = []
    for v in values:
        expr += ["-o", flag, v]
    return ["(", *expr[1:], ")"]


def find(root, names=(), prune=False, preds=(), excludes=(), walked=False):
    """The paths find prints for these options; walked=True: every entry it visits."""
    cmd = ["find", str(root), "-mindepth", "1"]
    if excludes:
        cmd += [*_alt("-name", excludes), "-prune", "-o"]
    if walked:
        cmd += [*_alt("-name", names), "-prune", "-print0", "-o", "-print0"] if prune and names else ["-print0"]
    else:
        cmd += _alt("-name", names) + (["-prune"] if prune else []) + (["(", *preds, ")"] if preds else []) + ["-print0"]
    out = subprocess.run(cmd, capture_output=True, check=True, env=dict(os.environ, TZ="UTC")).stdout
    return {os.fsdecode(p) for p in out.split(b"\0") if p}


def walkfind(walk, root, cwd, *args):
    """Run walkfind with --print0 --output; (result, listed paths)."""
    r = walk.run("walkfind", [root], cwd=cwd, args=["--print0", "--output", "found.list", *args])
    listed = None
    out = Path(cwd) / "found.list"
    if out.exists():
        data = out.read_bytes()
        listed = [os.fsdecode(p) for p in data.split(b"\0") if p]
        assert data == b"" or data.endswith(b"\0")
    return r, listed


CASES = {
    "dat":            (["--name", "*.dat"], dict(names=["*.dat"])),
    "two-names":      (["--name", "*.txt", "--name", "*link*"], dict(names=["*.txt", "*link*"])),
    "cold-prune":     (["--name", "COLD_STORAGE*", "--prune-matches"], dict(names=["COLD_STORAGE*"], prune=True)),
    "cold-no-prune":  (["--name", "COLD_STORAGE*"], dict(names=["COLD_STORAGE*"])),
    "name-and-type":  (["--name", "*a*", "--type", "f"], dict(names=["*a*"], preds=["-type", "f"])),
    "prune-and-type": (["--name", "*e*", "--prune-matches", "--type", "d"],
                       dict(names=["*e*"], prune=True, preds=["-type", "d"])),
    "exclude-name":   (["--exclude", "odd names", "--name", "*.dat"], dict(names=["*.dat"], excludes=["odd names"])),
    "everything":     ([], dict()),
    "old-files":      (["--mtime-older", "3y", "--type", "f"], dict(preds=["-type", "f", "-mtime", "+1095"])),
}


@pytest.mark.parametrize("batches", [[], ["--batch-files", "1"]], ids=["default", "split"])
@pytest.mark.parametrize("case", CASES)
def test_walkfind_lists_what_find_prints(walk, golden_tree, tmp_path, case, batches):
    args, kw = CASES[case]
    r, listed = walkfind(walk, golden_tree.root, tmp_path, *args, *batches)
    assert r.ok, r.report()
    assert len(listed) == len(set(listed)), "a path was listed twice"
    expected = find(golden_tree.root, **kw)
    assert set(listed) == expected
    walked, _, matched = r.ints(FIND_RE)
    assert matched == len(expected)
    visit = {k: v for k, v in kw.items() if k in ("names", "prune", "excludes")}
    assert walked == len(find(golden_tree.root, walked=True, **visit)), r.report()
    assert not list(tmp_path.glob("walkfind-r*.list")), "--output leaves no per-rank list"


def test_walkfind_on_a_wide_directory_shares_its_batches(walk, wide_tree, tmp_path):
    """Every one of the 20,000 files matches: 4,000 find batches from one directory, so idle
    ranks take some (110 matches in ~20 batches were all done before any share on a CI runner)."""
    r, listed = walkfind(walk, wide_tree.root, tmp_path, "--name", "file-*", "--name", "subdir-001*",
                         "--batch-files", "5", "--verbose")
    assert r.ok, r.report()
    assert set(listed) == find(wide_tree.root, names=["file-*", "subdir-001*"])
    assert r.ints(FIND_RE)[0] == wide_tree.nitems
    (shared,) = r.ints(r"--> Batches: ([\d,]+) received in share replies")
    assert shared > 0, r.report()


def test_per_rank_lists_are_printable_lines(walk, golden_tree, tmp_path):
    """Without --output, one list per worker; one escaped line per path, a newline in a name
    as \\n."""
    stale = tmp_path / "walkfind-r099.list"
    stale.write_text("from an earlier run\n")
    r = walk.run("walkfind", [golden_tree.root], cwd=tmp_path, args=["--name", "*line*"])
    assert r.ok, r.report()
    assert not stale.exists(), "an earlier run's list is removed"
    lines = [line for f in sorted(tmp_path.glob("walkfind-r*.list")) for line in f.read_text().splitlines()]
    assert lines == [f"{golden_tree.root}/odd names/new\\nline.dat"]


def test_output_exists_when_nothing_matches(walk, golden_tree, tmp_path):
    r, listed = walkfind(walk, golden_tree.root, tmp_path, "--name", "no-such-name-*")
    assert r.ok, r.report()
    assert listed == [] and r.ints(FIND_RE)[2] == 0


def test_index_records_the_matches(walk, golden_tree, tmp_path):
    r, listed = walkfind(walk, golden_tree.root, tmp_path, "--name", "*.dat", "--index=gz")
    assert r.ok, r.report()
    names = sorted(str(p) for p in tmp_path.glob("index-r*.tsv.gz"))
    assert names
    for name in names:
        assert closure_errors(name) == []
    paths, repeated = entries(names)
    assert not repeated and paths == set(listed)


def test_an_unreadable_directory_fails_the_run(walk, unreadable_tree, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 directory")
    r, listed = walkfind(walk, unreadable_tree.root, tmp_path, "--name", "*.dat")
    assert not r.timed_out and walk.exit_status_matches(r.returncode, 1), r.report()
    assert "Cannot scan" in r.stderr and listed == [f"{unreadable_tree.root}/ok/a.dat"]


@pytest.mark.parametrize("args, message", [
    (["--prune-matches"], "--prune-matches needs --name"),
    (["--name", "a/b"], "cannot be empty or hold a '/'"),
    (["--output", "/no/such/dir/out.list"], "no such directory"),
])
def test_bad_options_are_refused(walk, golden_tree, tmp_path, args, message):
    r = walk.run("walkfind", [golden_tree.root], cwd=tmp_path, args=args)
    assert not r.timed_out and r.returncode != 0 and message in r.stderr, r.report()
