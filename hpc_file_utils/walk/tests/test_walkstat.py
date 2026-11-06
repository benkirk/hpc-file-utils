"""walkstat/walktar behaviour, pinned before phase 2 changes anything.

Phase 1 note: every walk here is exposed to the termination race (see
test_termination_race.py). Under load (pytest -n auto) the Manager can receive a
worker's 'ready' before the 'dir_reply' it sent just before, and stop early, so an
occasional run reports fewer items. The total-count checks make that failure obvious.
"""

import os
import tarfile

import pytest

pytestmark = pytest.mark.mpi

GOLDEN_ARGS = ["--summary-prefix", "golden", "--threshold-size", "1MB"]


def test_walkstat_golden_summary(walk, golden_tree, tmp_path):
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path, args=GOLDEN_ARGS)
    assert r.ok, r.report()
    assert r.total_count() == golden_tree.nitems, r.report()
    summary = walk.normalize(r.summary("golden"), golden_tree.root, golden_tree.dates)
    walk.compare_golden("summary-golden.txt", summary)
    # rank 0 echoes the summary file to stdout
    assert r.summary("golden") in r.stdout


def test_walkstat_golden_userlist(walk, golden_tree, tmp_path):
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path, args=GOLDEN_ARGS)
    assert r.ok, r.report()
    assert r.total_count() == golden_tree.nitems, r.report()
    lists = r.userlists("golden")
    assert len(lists) == 1, sorted(lists)   # one owner: us
    (text,) = lists.values()
    walk.compare_golden("userlist-golden.txt", walk.normalize(text, golden_tree.root, golden_tree.dates))


def test_walkstat_default_prefix(walk, golden_tree, tmp_path):
    """No --summary-prefix: summary-output, and userlists/<user> directly."""
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path)
    assert r.ok, r.report()
    assert r.total_count() == golden_tree.nitems, r.report()
    assert (tmp_path / "summary-output").is_file()
    assert len(r.userlists("")) == 1


@pytest.mark.xfail(reason="phase 2: termination race; under load the Manager can see a worker's "
                          "ready before its dir_reply and drop those subdirectories", strict=False)
def test_walkstat_rank_counts_do_not_matter(walk, golden_tree, tmp_path):
    """2 ranks (one worker) and 6 ranks give the same normalized summary as 4."""
    texts = []
    for n in (2, 6):
        r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path / f"n{n}", nranks=n, args=GOLDEN_ARGS)
        assert r.ok, r.report()
        assert r.total_count() == golden_tree.nitems, r.report()
        texts.append(walk.blocks(walk.normalize(r.summary("golden"), golden_tree.root, golden_tree.dates)))
    assert texts[0] == texts[1]


def test_walkstat_spreadsheet(walk, golden_tree, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    pytest.importorskip("pandas")
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path,
                 args=GOLDEN_ARGS + ["--spreadsheet", "summary.xlsx"])
    assert r.ok, r.report()
    assert r.total_count() == golden_tree.nitems, r.report()
    wb = openpyxl.load_workbook(tmp_path / "summary.xlsx")
    assert wb.sheetnames == ["golden - Directories", "golden - Files", "golden - Users & Groups"]


def test_walkstat_unreadable_dir_is_not_an_error(walk, unreadable_tree, tmp_path):
    """Today: 'Cannot scan' goes to stderr, nothing counts it, and the exit status is 0."""
    if os.geteuid() == 0:
        pytest.skip("root can read a mode-000 directory")
    r = walk.run("walkstat", [unreadable_tree.root], cwd=tmp_path)
    assert r.ok, r.report()
    assert "Cannot scan" in r.stderr
    assert r.total_count() == 3   # ok/, ok/a.dat, locked/ ; nothing inside locked/


@pytest.mark.xfail(reason="non-UTF-8 names: the summary file is opened as strict UTF-8, "
                          "so rank 0 dies writing it (not fixed in phase 2)", strict=False)
def test_walkstat_non_utf8_names(walk, non_utf8_tree, tmp_path):
    r = walk.run("walkstat", [non_utf8_tree.root], cwd=tmp_path, timeout=60)
    assert r.ok, r.report()
    assert "bad-" in r.summary()


@pytest.mark.timeout(120)
@pytest.mark.xfail(reason="phase 2: parse_options() fails on rank 0 only, "
                          "leaving the other ranks waiting (hang)", strict=True)
def test_walkstat_bad_dir_exits_promptly(walk, tmp_path):
    r = walk.run("walkstat", [tmp_path / "does-not-exist"], cwd=tmp_path, timeout=30)
    assert not r.timed_out, r.report()
    assert r.returncode != 0
    assert "no such directory" in r.stdout + r.stderr


def test_walktar_archives_the_tree(walk, private_golden_tree, tmp_path):
    r = walk.run("walktar", [private_golden_tree.root], cwd=tmp_path)
    assert r.ok, r.report()
    assert r.total_count() == private_golden_tree.nitems, r.report()
    tars = sorted(tmp_path.glob("output-r*-f*.tar"))
    assert tars, r.report()
    names = []
    for t in tars:
        with tarfile.open(t) as tf:
            names += tf.getnames()
    rel = str(private_golden_tree.root).lstrip("/")
    expected = {rel}
    for dirpath, dirnames, filenames in os.walk(private_golden_tree.root):
        for n in dirnames + filenames:
            expected.add(os.path.join(dirpath, n).lstrip("/"))
    assert len(names) == len(set(names)), "an entry was archived twice"
    assert set(names) == expected
