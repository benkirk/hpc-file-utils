"""Phase 4 filters, end to end under MPI, with find(1) as the oracle.

A filtered walkstat counts what `find TREE -mindepth 1 <same predicates>` lists: its Total
Count is find's count, and its Total Size is the sum of min(st_size, st_blocks*512) over
find's paths. Only predicates that both GNU find (ubuntu) and BSD find (macOS) accept are
used: -type with one letter (OR'ed with -o), -mtime/-atime/-ctime +N/-N in days,
-newermt YYYY-MM-DD, -size ±Nc, -user, -group, -name ... -prune. The fixture's times sit
far from every cutoff used, so find's day rounding does not matter.
"""

import grp
import os
import pwd
import re
import subprocess
import tarfile

import pytest

from hpc_file_utils.walk.common.formatting import format_size

pytestmark = pytest.mark.mpi

ME = pwd.getpwuid(os.getuid()).pw_name
TYPE_RE = re.compile(r"^\s+(\w+) :\s+([\d,]+)$", re.M)
SIZE_LINE_RE = re.compile(r"^Total Size:  (.+)$", re.M)


def group_of(root):
    gid = os.lstat(root).st_gid
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def find(root, excludes=(), preds=(), pruned=False, mindepth=1):
    """Paths find lists for these predicates; pruned=True: the entries the excludes prune."""
    cmd = ["find", str(root), "-mindepth", str(mindepth)]
    if excludes:
        names = []
        for glob in excludes:
            names += ["-o", "-name", glob]
        cmd += ["(", *names[1:], ")", "-prune"]
        if pruned:
            cmd += ["-print0"]
        else:
            cmd += ["-o"]
    if not pruned:
        if preds:
            cmd += ["(", *preds, ")"]
        cmd += ["-print0"]
    out = subprocess.run(cmd, capture_output=True, check=True, env=dict(os.environ, TZ="UTC")).stdout
    return [os.fsdecode(p) for p in out.split(b"\0") if p]


def walk_size(paths):
    total = 0
    for p in paths:
        s = os.lstat(p)
        total += min(s.st_size, s.st_blocks * 512)
    return total


def type_counts(summary):
    return {k: int(v.replace(",", "")) for k, v in TYPE_RE.findall(summary)}


# (walkstat args, find predicates); "{me}" and "{group}" are filled in per tree
CASES = {
    "type-f":         (["--type", "f"], ["-type", "f"]),
    "type-l,p":       (["--type", "l,p"], ["-type", "l", "-o", "-type", "p"]),
    "type-d":         (["--type", "d"], ["-type", "d"]),
    "mtime-older":    (["--mtime-older", "400d"], ["-mtime", "+400"]),
    "mtime-newer":    (["--mtime-newer", "2015-06-01"], ["-newermt", "2015-06-01"]),
    "mtime-window":   (["--mtime-older", "3y", "--mtime-newer", "5y"], ["-mtime", "+1095", "-mtime", "-1825"]),
    "atime-older":    (["--atime-older", "5y"], ["-atime", "+1825"]),
    "atime-newer":    (["--atime-newer", "365"], ["-atime", "-365"]),
    "ctime-older":    (["--ctime-older", "1d"], ["-ctime", "+1"]),
    "min-size":       (["--min-size", "2MB"], ["-size", "+2097151c"]),
    "max-size":       (["--max-size", "1MB"], ["-size", "-1048577c"]),
    "user":           (["--user", "{me}"], ["-user", "{me}"]),
    "unused-uid":     (["--user", "987654"], ["-user", "987654"]),
    "group":          (["--group", "{group}"], ["-group", "{group}"]),
    "combined":       (["--type", "f", "--mtime-older", "3y", "--min-size", "1MB", "--user", "{me}"],
                       ["-type", "f", "-mtime", "+1095", "-size", "+1048575c", "-user", "{me}"]),
}
EXCLUDE_CASES = {
    "exclude":            (["COLD_STORAGE*"], [], []),
    "exclude-two":        (["COLD_STORAGE*", "odd names"], [], []),
    "exclude-files":      (["*.dat"], [], []),
    "exclude-and-select": (["COLD_STORAGE*", "links"], ["--type", "f", "--mtime-older", "3y"],
                           ["-type", "f", "-mtime", "+1095"]),
}


def _fill(items, tree):
    return [s.format(me=ME, group=group_of(tree.root)) for s in items]


def check_against_find(walk, tree, tmp_path, excludes, args, preds):
    args = [a for g in excludes for a in ("--exclude", g)] + args
    r = walk.run("walkstat", [tree.root], cwd=tmp_path, args=args)
    assert r.ok, r.report()
    assert "Cannot scan" not in r.stderr, r.report()
    expected = find(tree.root, excludes, preds)
    summary = r.summary()
    assert r.total_count() == len(expected), f"find {args}: {len(expected)}\n{r.report()}"
    assert SIZE_LINE_RE.search(summary).group(1) == format_size(walk_size(expected)), summary

    assert "\nFilters:     --" in summary
    if excludes:
        assert "Excluded:    {} entries".format(len(find(tree.root, excludes, pruned=True))) in summary
    if preds:
        # with a selecting filter Type Counts count matching entries only (not the root)
        assert sum(type_counts(summary).values()) == len(expected), summary
        unmatched = len(find(tree.root, excludes)) - len(expected)
        assert "Unmatched:   {:,} entries".format(unmatched) in summary
    else:
        assert "Unmatched:" not in summary
    return r


@pytest.mark.parametrize("case", CASES)
def test_selecting_matches_find(walk, golden_tree, tmp_path, case):
    args, preds = CASES[case]
    check_against_find(walk, golden_tree, tmp_path, [], _fill(args, golden_tree), _fill(preds, golden_tree))


@pytest.mark.parametrize("case", EXCLUDE_CASES)
def test_exclude_matches_find(walk, golden_tree, tmp_path, case):
    excludes, args, preds = EXCLUDE_CASES[case]
    check_against_find(walk, golden_tree, tmp_path, excludes, args, preds)


def test_user_filter_equals_the_unfiltered_users_row(walk, golden_tree, tmp_path):
    full = walk.run("walkstat", [golden_tree.root], cwd=tmp_path / "all", args=["--threshold-size", "0"])
    assert full.ok, full.report()
    m = re.search(rf"^\s*{re.escape(ME)}\s+(.+?)\s{{2,}}([\d,]+)$", full.summary(), re.M)
    assert m, full.summary()
    mine = walk.run("walkstat", [golden_tree.root], cwd=tmp_path / "mine", args=["--user", ME])
    assert mine.ok, mine.report()
    assert mine.total_count() == int(m.group(2).replace(",", ""))
    assert SIZE_LINE_RE.search(mine.summary()).group(1) == m.group(1)


def test_userlists_and_spreadsheet_name_the_filters(walk, golden_tree, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    pytest.importorskip("pandas")
    args = ["--summary-prefix", "f", "--threshold-size", "0", "--spreadsheet", "s.xlsx"]
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path, args=args + ["--type", "f"])
    assert r.ok, r.report()
    lists = r.userlists("f")
    assert lists and all("\nFilters:     --type f\nTotal Count:" in t for t in lists.values())
    wb = openpyxl.load_workbook(tmp_path / "s.xlsx")
    assert wb.sheetnames == ["f - Directories", "f - Files", "f - Users & Groups", "f - Filters"]
    assert [c.value for c in wb["f - Filters"]["A"]] == ["filter", "Filters", "Unmatched"]

    # an unfiltered walk into the same workbook drops the stale Filters sheet
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path, args=args)
    assert r.ok, r.report()
    assert "Filters:" not in r.summary("f")
    wb = openpyxl.load_workbook(tmp_path / "s.xlsx")
    assert wb.sheetnames == ["f - Directories", "f - Files", "f - Users & Groups"]


def test_excluded_unreadable_dir_is_never_scanned(walk, unreadable_tree, tmp_path):
    """A mode-000 directory that --exclude prunes is never opened (and never stat'ed)."""
    r = walk.run("walkstat", [unreadable_tree.root], cwd=tmp_path,
                 args=["--exclude", "locked", "--fail-on-errors"])
    assert r.ok, r.report()
    assert "Cannot scan" not in r.stderr
    assert r.total_count() == 2   # ok/, ok/a.dat
    assert "Excluded:    1 entries" in r.summary()
    assert "Scan Errors" not in r.summary()


def test_walktar_archives_exactly_the_selection(walk, private_golden_tree, tmp_path):
    """The tars hold every directory walked (with its mode, owner and ACLs), the roots
    included, plus exactly the selected non-directories; nothing below an --exclude."""
    root = private_golden_tree.root
    excludes, preds = ["COLD_STORAGE*"], ["!", "-type", "d", "-mtime", "+1095"]
    r = walk.run("walktar", [root], cwd=tmp_path, args=["--exclude", "COLD_STORAGE*", "--mtime-older", "3y"])
    assert r.ok, r.report()
    names = []
    for t in sorted(tmp_path.glob("output-r*-f*.tar")):
        with tarfile.open(t) as tf:
            names += tf.getnames()
    assert len(names) == len(set(names)), "an entry was archived twice"
    dirs = find(root, excludes, ["-type", "d"], mindepth=0)
    files = find(root, excludes, preds)
    assert files and not any("COLD_STORAGE" in p for p in dirs + files)
    assert set(names) == {p.lstrip("/") for p in dirs + files}
