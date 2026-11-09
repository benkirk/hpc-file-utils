"""walksync (phase 6): copy what is new or changed, with batched rsync, spread over the ranks.

The gate (docs/HANDOFF.md, phase 6): after a run, the oracle
    rsync -naxAX --delete -i [--chmod=Du+w] [--exclude ...] SRC/ DEST/
lists only directory-time lines, and a rerun copies nothing.
"""

import os
import re
import shutil
import stat
import subprocess
import time

import pytest

from hpc_file_utils.walk.common.options import choose_rsync

pytestmark = [
    pytest.mark.mpi,
    pytest.mark.skipif(shutil.which("rsync") is None, reason="walksync needs rsync"),
]

SYNC_RE = re.compile(r"--> Sync: ([\d,]+) new, ([\d,]+) changed, ([\d,]+) unchanged; ([\d,]+) only in the destination")
COPIED_RE = re.compile(r"--> Copied ([\d,]+) entries, .* in ([\d,]+) rsync batches .*; ([\d,]+) failed")
RANK_ROW_RE = re.compile(r"^\s+(\d+)\s+\S+\s+[\d,]+\s+([\d,]+)\s", re.M)
# A directory whose only differences are its time, permissions or ACL (YXcstpoguax): the final
# serial rsync's job. --chmod=Du+w changes directories' modes, and with a default ACL rsync also
# changes a new subdirectory's default ACL (plain serial rsync does too).
DIR_ATTRS_RE = re.compile(r"^\.d\.\.[.t][.p]\.\.\.[.a]\. ")


def _int(s):
    return int(s.replace(",", ""))


def sync_counts(r):
    """(new, changed, unchanged, extraneous) from a run's summary."""
    m = SYNC_RE.search(r.stdout)
    assert m, r.report()
    return tuple(map(_int, m.groups()))


def copied(r):
    """(entries copied, rsync batches, failed batches)."""
    m = COPIED_RE.search(r.stdout)
    assert m, r.report()
    return tuple(map(_int, m.groups()))


def oracle(src, dest, *extra, delete=True, final=False):
    """rsync's own itemized dry run: every line that is not a directory's time, permissions or
    ACL (DIR_ATTRS_RE). The rsync walksync chooses, with -A and -X only where it has them
    (conda-forge's Linux rsync has no ACLs; on derecho walksync then takes /usr/bin/rsync).
    final=True: after the final serial rsync, as -aHAX without --chmod, every line."""
    rsync, acls, xattrs, _ = choose_rsync()
    flags = "-nax" + ("H" if final else "") + ("A" if acls else "") + ("X" if xattrs else "")
    chmod = [] if final else ["--chmod=Du+w"]
    cmd = [rsync, flags, "-i", *chmod, *extra] + (["--delete"] if delete else [])
    out = subprocess.run(cmd + [f"{src}/", f"{dest}/"], capture_output=True, check=True).stdout
    lines = out.decode(errors="surrogateescape").splitlines()
    return lines if final else [line for line in lines if not DIR_ATTRS_RE.match(line)]


def final_rsync(src, dest):
    """The serial pass walksync leaves to the user: rsync -aHAX --delete (-A/-X where this rsync
    has them). It restores the directories' modes, ACLs and times."""
    rsync, acls, xattrs, _ = choose_rsync()
    flags = "-aH" + ("A" if acls else "") + ("X" if xattrs else "")
    subprocess.run([rsync, flags, "--delete", f"{src}/", f"{dest}/"], capture_output=True, check=True)


def same_contents(src, dest, skip=lambda rel: False):
    """Every regular file below src has the same bytes below dest."""
    for dirpath, _, filenames in os.walk(src):
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, src)
            if skip(rel) or not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            with open(path, "rb") as a, open(os.path.join(dest, rel), "rb") as b:
                assert a.read() == b.read(), rel


def dryrun_lines(cwd):
    """--dry-run's lists, every rank's."""
    lines = []
    for f in sorted(cwd.glob("walksync-dryrun-r*.txt")):
        lines += f.read_text().splitlines()
    return lines


def sync(walk, src, dest, cwd, *args, nranks=4):
    dest.mkdir(exist_ok=True)
    return walk.run("walksync", [src], cwd=cwd, nranks=nranks, args=["--dest", dest, *args])


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "dest"
    yield d
    # a read-only source's files keep their modes (directories get u+w): let pytest clean up
    for dirpath, dirnames, _ in os.walk(d):
        for n in dirnames:
            os.chmod(os.path.join(dirpath, n), 0o755)


def test_copy_passes_the_oracle_and_a_rerun_copies_nothing(walk, private_golden_tree, dest, tmp_path):
    src = private_golden_tree.root
    r = sync(walk, src, dest, tmp_path / "run1", "--batch-files", "5")
    assert r.ok, r.report()
    new, changed, unchanged, extra = sync_counts(r)
    assert (new, changed, unchanged, extra) == (private_golden_tree.nitems, 0, 0, 0), r.report()
    ncopied, nbatches, failed = copied(r)
    assert ncopied == new and nbatches > 1 and failed == 0, r.report()
    assert oracle(src, dest) == [], r.report()
    same_contents(src, dest)
    # the read-only source (and its ACL) copied by a non-root user; its directories get u+w
    assert (dest / "readonly/sub/ro2.dat").exists()
    assert os.stat(dest / "readonly/sub").st_mode & stat.S_IWUSR
    assert not (dest / "summary-output").exists() and not (tmp_path / "run1/summary-output").exists()

    r = sync(walk, src, dest, tmp_path / "run2")
    assert r.ok, r.report()
    assert sync_counts(r) == (0, 0, private_golden_tree.nitems, 0), r.report()
    assert copied(r)[:2] == (0, 0), r.report()

    # the whole workflow: after the final serial rsync, the copy is the source, directories too
    try:
        final_rsync(src, dest)
        assert oracle(src, dest, final=True) == []
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(dest)], check=False)     # read-only dirs again


def test_exclude_prunes_cold_storage(walk, private_golden_tree, dest, tmp_path):
    src = private_golden_tree.root
    r = sync(walk, src, dest, tmp_path / "run", "--exclude", "COLD_STORAGE*")
    assert r.ok, r.report()
    assert "2 excluded by name" in r.stdout, r.report()
    assert (dest / "project/live.dat").exists()
    assert not any(n.startswith("COLD_STORAGE") for n in os.listdir(dest / "project"))
    assert oracle(src, dest, "--exclude", "COLD_STORAGE*") == []
    same_contents(src, dest, skip=lambda rel: "COLD_STORAGE" in rel)


YEAR_AGO = time.time() - 365 * 86400
MiB = 1024 * 1024


@pytest.mark.parametrize("args,wanted", [
    (["--mtime-newer", "1y"], lambda s: s.st_mtime > YEAR_AGO),
    (["--min-size", "2MiB"], lambda s: s.st_size >= 2 * MiB),
    (["--min-size", "1MiB", "--max-size", "3MiB"], lambda s: MiB <= s.st_size <= 3 * MiB),
], ids=["mtime", "min-size", "size-range"])
def test_selecting_filter_copies_only_matching_files(walk, private_golden_tree, dest, tmp_path, args, wanted):
    src = private_golden_tree.root
    r = sync(walk, src, dest, tmp_path / "run", *args)
    assert r.ok, r.report()
    assert "a partial copy" in r.stdout
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        assert os.path.isdir(os.path.join(dest, rel)), rel           # every directory is created
        for n in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            selected = wanted(os.lstat(os.path.join(dirpath, n)))
            assert os.path.lexists(os.path.join(dest, rel, n)) == selected, os.path.join(rel, n)


def test_changed_and_extraneous_destination_entries(walk, private_golden_tree, dest, tmp_path):
    src = private_golden_tree.root
    assert sync(walk, src, dest, tmp_path / "run1").ok

    with open(dest / "data/big.dat", "ab") as f:                      # size
        f.write(b"x")
    os.utime(dest / "data/medium.dat", (1, 1))                       # whole-second mtime
    os.remove(dest / "links/file-link")                              # symlink target
    os.symlink("elsewhere", dest / "links/file-link")
    os.remove(dest / "data/empty.txt")                               # type: a directory instead
    (dest / "data/empty.txt").mkdir()
    (dest / "data/only-in-dest.txt").write_text("keep me")           # extraneous
    os.remove(dest / "odd names/with space.dat")                     # missing

    r = sync(walk, src, dest, tmp_path / "run2", "--dry-run")
    assert r.ok, r.report()
    assert sync_counts(r)[:2] == (1, 4) and sync_counts(r)[3] == 1, r.report()
    listed = sorted(dryrun_lines(tmp_path / "run2"))
    assert listed == ["C data/big.dat", "C data/empty.txt", "C data/medium.dat", "C links/file-link",
                      "N odd names/with space.dat", "X data/only-in-dest.txt"], r.report()
    assert not (dest / "odd names/with space.dat").exists()          # the dry run copied nothing

    os.rmdir(dest / "data/empty.txt")       # rsync without --delete cannot replace a directory
    r = sync(walk, src, dest, tmp_path / "run3")
    assert r.ok, r.report()
    assert sync_counts(r)[:2] == (2, 3), r.report()
    assert (dest / "data/only-in-dest.txt").read_text() == "keep me"  # never deleted
    assert oracle(src, dest, delete=False) == []
    same_contents(src, dest)


def test_dry_run_on_the_wide_tree_terminates_and_copies_nothing(walk, wide_tree, dest, tmp_path):
    """The old prototype's --dry-run hung on this tree (the termination race)."""
    r = sync(walk, wide_tree.root, dest, tmp_path / "run", "--dry-run", "--batch-files", "500")
    assert r.ok, r.report()
    assert os.listdir(dest) == []
    assert sync_counts(r) == (wide_tree.nitems, 0, 0, 0), r.report()
    listed = dryrun_lines(tmp_path / "run")
    assert len(listed) == wide_tree.nitems and all(line.startswith("N ") for line in listed)
    assert len(set(listed)) == len(listed)


NFILES = 400


@pytest.fixture
def big_dir(tmp_path):
    root = tmp_path / "src"
    (root / "big").mkdir(parents=True)
    data = os.urandom(64 * 1024)
    for i in range(NFILES):
        (root / "big" / f"file-{i:04d}.dat").write_bytes(data)
    return root


def test_big_directory_is_copied_by_several_ranks(walk, big_dir, dest, tmp_path):
    r = sync(walk, big_dir, dest, tmp_path / "run", "--batch-files", "10", "--verbose")
    assert r.ok, r.report()
    assert copied(r)[0] == NFILES + 1, r.report()
    # each rank's 'files' column is what it copied; how many take part depends on timing
    files = {int(rank): _int(n) for rank, n in RANK_ROW_RE.findall(r.stdout)}
    assert sum(files.values()) == NFILES + 1, f"{files}\n{r.report()}"
    assert sum(1 for n in files.values() if n) >= 2, f"{files}\n{r.report()}"
    m = re.search(r"--> Batches: ([\d,]+) received in share replies", r.stdout)
    assert m and _int(m.group(1)) > 0, r.report()
    assert oracle(big_dir, dest) == []


def test_a_failing_rsync_fails_the_run(walk, big_dir, dest, tmp_path):
    r = sync(walk, big_dir, dest, tmp_path / "run", "--rsync-opts=--no-such-rsync-option")
    assert not r.timed_out, r.report()
    assert walk.exit_status_matches(r.returncode, 1), r.report()
    assert copied(r)[2] > 0 and copied(r)[0] == 0, r.report()
    assert "rsync exit" in r.stderr
