"""walkstat/walktar behaviour on the fixture trees.

Every walk checks walkstat's Total Count against the tree: before phase 2, the
termination race could end a walk early under load and drop directories silently.
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
    walk.compare_golden("summary-golden.txt", summary, sort="ties")
    # rank 0 echoes the summary file to stdout
    assert r.summary("golden") in r.stdout


def test_walkstat_golden_userlist(walk, golden_tree, tmp_path):
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path, args=GOLDEN_ARGS)
    assert r.ok, r.report()
    assert r.total_count() == golden_tree.nitems, r.report()
    lists = r.userlists("golden")
    assert len(lists) == 1, sorted(lists)   # one owner: us
    (text,) = lists.values()
    walk.compare_golden("userlist-golden.txt", walk.normalize(text, golden_tree.root, golden_tree.dates),
                        sort="ties")


def test_walkstat_default_prefix(walk, golden_tree, tmp_path):
    """No --summary-prefix: summary-output, and userlists/<user> directly."""
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path)
    assert r.ok, r.report()
    assert r.total_count() == golden_tree.nitems, r.report()
    assert (tmp_path / "summary-output").is_file()
    assert len(r.userlists("")) == 1


@pytest.mark.parametrize("heap_size", [500, 3])
def test_walkstat_rank_counts_do_not_matter(walk, golden_tree, tmp_path, heap_size):
    """2 ranks (one worker) and 6 ranks give exactly the same summary, line for line,
    also when --heap-size cuts the lists off among entries of equal size."""
    texts = []
    for n in (2, 6):
        r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path / f"n{n}", nranks=n,
                     args=GOLDEN_ARGS + ["--heap-size", heap_size])
        assert r.ok, r.report()
        assert r.total_count() == golden_tree.nitems, r.report()
        texts.append(walk.normalize(r.summary("golden"), golden_tree.root, golden_tree.dates))
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


def test_walkstat_unreadable_dir_is_counted(walk, unreadable_tree, tmp_path):
    """'Cannot scan' goes to stderr and is counted; by default the exit status stays 0."""
    if os.geteuid() == 0:
        pytest.skip("root can read a mode-000 directory")
    r = walk.run("walkstat", [unreadable_tree.root], cwd=tmp_path)
    assert r.ok, r.report()
    assert "Cannot scan" in r.stderr
    assert r.total_count() == 3   # ok/, ok/a.dat, locked/ ; nothing inside locked/
    assert "Scan Errors: 1 entries could not be scanned" in r.summary()
    assert "--> 1 entries could not be scanned" in r.stdout


def test_walkstat_fail_on_errors(walk, unreadable_tree, golden_tree, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read a mode-000 directory")
    r = walk.run("walkstat", [unreadable_tree.root], cwd=tmp_path / "bad", args=["--fail-on-errors"])
    assert not r.timed_out and walk.exit_status_matches(r.returncode, 2), r.report()
    assert (tmp_path / "bad" / "summary-output").is_file()   # the summary is still written
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path / "good", args=["--fail-on-errors"])
    assert r.ok, r.report()
    assert "Scan Errors" not in r.summary()


def test_walkstat_non_utf8_names(walk, non_utf8_tree, tmp_path):
    """Undecodable bytes are shown as \\xNN; the published files stay valid UTF-8."""
    r = walk.run("walkstat", [non_utf8_tree.root], cwd=tmp_path, timeout=60,
                 args=["--threshold-count", "1"])   # not size: GPFS may not show the blocks yet
    assert r.ok, r.report()
    raw = (tmp_path / "summary-output").read_bytes()
    raw.decode("utf-8")                                   # strict: no stray bytes
    assert rb"dir/bad-\xff\xfe-name.dat" in raw
    (userlist,) = (tmp_path / "userlists").iterdir()
    userlist.read_bytes().decode("utf-8")
    assert rb"bad-\xff\xfe-name.dat" in userlist.read_bytes()


@pytest.mark.timeout(120)
def test_walkstat_bad_dir_exits_promptly(walk, tmp_path):
    """parse_options() runs on rank 0 only; its failure must end every rank."""
    r = walk.run("walkstat", [tmp_path / "does-not-exist"], cwd=tmp_path, timeout=30)
    assert not r.timed_out, r.report()
    assert walk.exit_status_matches(r.returncode, 1), r.report()
    assert "no such directory" in r.stdout + r.stderr


@pytest.mark.timeout(120)
@pytest.mark.parametrize("args,status", [(["--help"], 0), (["--no-such-option"], 2),
                                         (["--max-dirs-per-send", "0"], 2)])
def test_walkstat_option_exits_promptly(walk, golden_tree, tmp_path, args, status):
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path, args=args, timeout=30)
    assert not r.timed_out, r.report()
    assert walk.exit_status_matches(r.returncode, status), r.report()


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
