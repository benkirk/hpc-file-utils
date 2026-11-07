"""Fixtures for the walkstat/walktar tests.

The tests run the console scripts the way the scans do:
``mpiexec -n 4 walkstat --dirs <tree>``, in a scratch working directory
(walkstat writes ``summary-<prefix>`` and ``userlists/`` into its cwd).

Trees:
  golden_tree  odd names, links (file, dir, dangling), a fifo, hard links, empty dirs,
               a read-only subtree (plus ACLs where setfacl works), COLD_STORAGE* dirs.
               Data files are whole MiB, so a directory's own st_size (which differs
               between APFS, ext4, tmpfs and GPFS) rounds away in the printed sizes.
  wide_tree    one directory with 2,000 subdirectories and 20,000 files (the termination race).

``walk.normalize()`` masks what legitimately differs between runs and machines: the
"Summary as of" time, the tree root, the owner/group names, the fixture dates (which
are relative to today), and sizes under 1 MiB (directory entries, symlinks, fifos).
"""

import calendar
import difflib
import functools
import grp
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

WALK_DIR = Path(__file__).resolve().parents[1]
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
MiB = 1024 * 1024
DAY = 86400
YEAR = 365 * DAY  # walkstat's SECONDS_IN_YEAR
SEP_RE = re.compile(r"^#-{20,}$")
SIZE_RE = re.compile(r"\b(\d+(?:\.\d+)?) (bytes?|KiB|MiB|GiB|TiB|PiB)\b")
STAMP_RE = re.compile(r"(# Summary as of )\d{4}-\d\d-\d\d \d\d:\d\d")
COUNT_RE = re.compile(r"^Total Count: ([\d,]+) items$", re.M)


# ---------------------------------------------------------------------------
# running the CLIs
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _mpiexec_base():
    override = os.environ.get("HFU_MPIEXEC")
    if override:
        return shlex.split(override)
    exe = shutil.which("mpiexec")
    v = subprocess.run([exe, "--version"], capture_output=True, text=True)
    if "Open MPI" in v.stdout + v.stderr or "OpenRTE" in v.stdout + v.stderr:
        return [exe, "--oversubscribe"]
    return [exe]


def exit_status_matches(returncode, wanted):
    """Open MPI passes the ranks' exit status through; Cray PALS SIGTERMs the other
    ranks once one exits non-zero and reports 143. So exact only under Open MPI."""
    if wanted == 0 or "--oversubscribe" in _mpiexec_base():
        return returncode == wanted
    return returncode != 0


def mpiexec(nranks):
    """mpiexec command prefix: HFU_MPIEXEC overrides, --oversubscribe for Open MPI."""
    return _mpiexec_base() + ["-n", str(nranks)]


def _kill_tree(proc, marker):
    """Kill a hung launch: its process group, plus any rank that left the group."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    for line in ps.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if marker in cmd and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@dataclass
class WalkResult:
    cmd: list
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    elapsed: float

    @property
    def ok(self):
        return not self.timed_out and self.returncode == 0

    def report(self):
        state = f"TIMED OUT after {self.elapsed:.0f}s" if self.timed_out else f"exit {self.returncode}"
        return (f"{shlex.join(self.cmd)}\n  cwd {self.cwd}: {state}\n"
                f"--- stdout (tail) ---\n{self.stdout[-3000:]}\n--- stderr (tail) ---\n{self.stderr[-3000:]}")

    def total_count(self):
        m = COUNT_RE.search(self.stdout)
        return int(m.group(1).replace(",", "")) if m else None

    def summary(self, prefix="output"):
        return (self.cwd / f"summary-{prefix}").read_text(errors="surrogateescape")

    def userlists(self, prefix=""):
        d = self.cwd / "userlists" / prefix
        return {p.name: p.read_text(errors="surrogateescape") for p in sorted(d.iterdir())}


def console_script(app):
    """The installed walkstat/walktar ('pip install -e .' puts them beside python)."""
    exe = Path(sys.executable).parent / app
    if not exe.exists():
        pytest.fail(f"{exe} is missing: reinstall the package (source etc/config_env.sh)")
    return str(exe)


def run_walk(app, dirs, cwd, nranks=4, args=(), timeout=120):
    """Run walkstat/walktar under mpiexec; never hangs longer than ``timeout``."""
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = mpiexec(nranks) + [console_script(app), "--progress", "0",
                             *map(str, args), "--dirs", *map(str, dirs)]
    env = dict(os.environ, TZ="UTC", PYTHONDONTWRITEBYTECODE="1")
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc, str(dirs[0]))
        out, err = proc.communicate()
    return WalkResult(cmd, cwd, proc.returncode, out.decode(errors="surrogateescape"),
                      err.decode(errors="surrogateescape"), timed_out, time.monotonic() - t0)


# ---------------------------------------------------------------------------
# normalizing and comparing output
# ---------------------------------------------------------------------------

def _size(m):
    n, unit = float(m.group(1)), m.group(2)
    if unit.startswith("byte") or unit == "KiB":
        return "<small>"
    return f"{round(n)} {unit}"


def normalize(text, root, dates=None):
    """Mask run- and machine-dependent parts of walkstat output (see module doc)."""
    text = text.replace(str(root), "<ROOT>")
    text = STAMP_RE.sub(r"\1<TIME>", text)
    for date, label in (dates or {}).items():
        text = text.replace(date, label)
    names = {pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.lstat(root).st_gid).gr_name}
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", "<OWNER>", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r" +", " ", SIZE_RE.sub(_size, line)).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) + "\n"


LEAD_SIZE_RE = re.compile(r"^(<small>|\d+ (?:KiB|MiB|GiB|TiB|PiB))(?= )")


def _sort_size_ties(block):
    """Sort runs of consecutive lines that start with the same normalized size.

    Directory sizes include each subdirectory's own st_size, which differs between
    filesystems, so entries whose sizes normalize to the same value (e.g. '1 MiB',
    '<small>') may legitimately come out in a different order elsewhere.
    """
    out, run, key = [], [], None
    for line in block:
        m = LEAD_SIZE_RE.match(line)
        k = m.group(1) if m else None
        if run and (k is None or k != key):
            out += sorted(run)
            run = []
        if k is None:
            out.append(line)
        else:
            run.append(line)
        key = k
    return out + sorted(run)


def blocks(text, sort=True):
    """Split normalized output on the '#----' separators.

    sort=True sorts each block (tie-tolerant); sort="ties" keeps the order except
    within runs of equal normalized sizes; sort=False keeps the order exactly.
    """
    out, cur = [], []
    for line in text.splitlines():
        if SEP_RE.match(line):
            out.append(cur)
            cur = []
        else:
            cur.append(line)
    out.append(cur)
    order = {True: sorted, "ties": _sort_size_ties, False: list}[sort]
    return [order(b) for b in out if b]


def compare_golden(name, text, sort=True):
    """Compare normalized output with golden/<name>, block by block.

    sort: see blocks(). HFU_UPDATE_GOLDEN=1 rewrites the golden file from this run instead.
    """
    path = GOLDEN_DIR / name
    if os.environ.get("HFU_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, errors="surrogateescape")
        return
    expected = path.read_text(errors="surrogateescape")
    got_b, exp_b = blocks(text, sort), blocks(expected, sort)
    if got_b != exp_b:
        diff = difflib.unified_diff(["\n".join(b) for b in exp_b], ["\n".join(b) for b in got_b],
                                    f"golden/{name}", "this run", lineterm="")
        pytest.fail(f"walkstat output differs from golden/{name} "
                    f"(HFU_UPDATE_GOLDEN=1 regenerates it):\n" + "\n".join(diff), pytrace=False)


class Walk:
    """Helpers handed to tests through the ``walk`` fixture."""
    run = staticmethod(run_walk)
    exit_status_matches = staticmethod(exit_status_matches)
    normalize = staticmethod(normalize)
    compare_golden = staticmethod(compare_golden)
    blocks = staticmethod(blocks)


@pytest.fixture
def walk():
    return Walk()


# ---------------------------------------------------------------------------
# trees
# ---------------------------------------------------------------------------

def _noon_utc(t):
    day = time.gmtime(t)
    return calendar.timegm((day.tm_year, day.tm_mon, day.tm_mday, 12, 0, 0))


def _datestr(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


@dataclass
class Tree:
    root: Path
    dates: dict = field(default_factory=dict)   # 'YYYY-MM-DD' -> label, for normalize()
    nitems: int = 0                             # entries below root (walkstat's Total Count)
    nfiles: int = 0


def _write(path, nbytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for _ in range(nbytes // MiB):
            f.write(os.urandom(MiB))   # incompressible: real blocks on every filesystem
        # walkstat counts min(st_size, st_blocks * 512); GPFS reports the blocks of
        # freshly written data only once it is flushed, so flush it now
        f.flush()
        os.fsync(f.fileno())


def _count(root):
    n = 0
    for _, dirnames, filenames in os.walk(root):
        n += len(dirnames) + len(filenames)
    return n


def build_golden_tree(root):
    now = time.time()
    ages = {
        "RECENT": _noon_utc(now - 30 * DAY),
        "AGE4Y": _noon_utc(now - 4 * YEAR - 30 * DAY),   # [3-5yrs)
        "AGE6Y": _noon_utc(now - 6 * YEAR - 30 * DAY),   # [5-7yrs)
        "OLD": calendar.timegm((2010, 1, 1, 12, 0, 0)),  # 7yrs+
    }
    when = {}   # path -> age label, for every non-directory

    def f(rel, mib, age):
        _write(root / rel, mib * MiB)
        when[root / rel] = age

    f("data/big.dat", 4, "RECENT")
    f("data/medium.dat", 2, "RECENT")
    f("data/empty.txt", 0, "RECENT")
    f("data/nested/deeper/leaf.dat", 1, "RECENT")

    for name in ["with space.dat", "new\nline.dat", "quote'd \"name\".dat",
                 "\u00fcn\u00efc\u00f6d\u00e9.dat", "-leading-dash.dat"]:
        f(f"odd names/{name}", 1, "AGE4Y")

    f("links/target.dat", 1, "AGE6Y")
    for name, target in [("file-link", "../data/big.dat"), ("dir-link", "../data"),
                         ("dangling", "does-not-exist")]:
        os.symlink(target, root / "links" / name)
        when[root / "links" / name] = "AGE6Y"

    f("special/hard-a.dat", 3, "RECENT")
    os.link(root / "special/hard-a.dat", root / "special/hard-b.dat")
    when[root / "special/hard-b.dat"] = "RECENT"
    os.mkfifo(root / "special/fifo")
    when[root / "special/fifo"] = "RECENT"

    (root / "empty").mkdir()
    (root / "empty-parent/empty-child").mkdir(parents=True)

    f("project/live.dat", 1, "OLD")
    f("project/COLD_STORAGE/run1/archive.dat", 5, "OLD")
    f("project/COLD_STORAGE_2019/notes.dat", 1, "OLD")

    f("readonly/ro.dat", 2, "OLD")
    f("readonly/sub/ro2.dat", 1, "OLD")

    nitems = _count(root)

    # read-only subtree, plus an ACL where the filesystem supports one
    if shutil.which("setfacl"):
        subprocess.run(["setfacl", "-R", "-m", f"u:{os.getuid()}:rX", str(root / "readonly")],
                       capture_output=True)
    for p in [root / "readonly/ro.dat", root / "readonly/sub/ro2.dat"]:
        p.chmod(0o444)
    for p in [root / "readonly/sub", root / "readonly"]:
        p.chmod(0o555)

    # times last: on Linux any path lookup through a symlink (os.walk's is_dir(), a
    # stat() without follow_symlinks=False) updates that symlink's atime
    for path, age in when.items():
        os.utime(path, (ages[age], ages[age]), follow_symlinks=False)

    return Tree(root=root, dates={_datestr(t): f"<{label}>" for label, t in ages.items()},
                nitems=nitems, nfiles=len(when))


@pytest.fixture(scope="session")
def golden_tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("golden") / "tree"
    root.mkdir()
    tree = build_golden_tree(root)
    yield tree
    for p in [root / "readonly", root / "readonly/sub"]:   # let pytest clean up
        p.chmod(0o755)


@pytest.fixture
def private_golden_tree(tmp_path):
    """A golden tree of the test's own, for walks that read file contents (walktar):
    on Linux reading updates atimes (and readlink a symlink's), which would change
    the dates in the shared tree's golden output."""
    root = tmp_path / "private" / "tree"
    root.mkdir(parents=True)
    tree = build_golden_tree(root)
    yield tree
    for p in [root / "readonly", root / "readonly/sub"]:
        p.chmod(0o755)


WIDE_SUBDIRS = 2000
WIDE_FILES = 20000


@pytest.fixture(scope="session")
def wide_tree(tmp_path_factory):
    """One directory with 2,000 empty subdirectories and 20,000 empty files.

    The rank scanning it sends a dir_reply every 200 subdirectories. The files between
    them keep it busy long enough for the other ranks to drain those 200 and go idle,
    which is the termination race. Long names make each dir_reply tens of KiB, above
    the eager limit, so the send the Manager never receives blocks: a hang, not a loss.
    (Each subdirectory holding a file instead lets the others fall behind: no race.)
    """
    root = tmp_path_factory.mktemp("wide") / "tree"
    wide = root / "wide"
    wide.mkdir(parents=True)
    pad = "x" * 80
    for i in range(WIDE_SUBDIRS):
        (wide / f"subdir-{i:04d}-{pad}").mkdir()
    for i in range(WIDE_FILES):
        (wide / f"file-{i:06d}").touch()
    return Tree(root=root, nitems=_count(root), nfiles=WIDE_FILES)


@pytest.fixture
def unreadable_tree(tmp_path):
    root = tmp_path / "tree"
    _write(root / "ok/a.dat", MiB)
    (root / "locked").mkdir()
    _write(root / "locked/hidden.dat", MiB)
    (root / "locked").chmod(0)
    yield Tree(root=root)
    (root / "locked").chmod(0o755)


@pytest.fixture
def non_utf8_tree(tmp_path):
    root = tmp_path / "tree"
    (root / "dir").mkdir(parents=True)
    try:
        with open(os.path.join(os.fsencode(root / "dir"), b"bad-\xff\xfe-name.dat"), "wb") as fh:
            fh.write(os.urandom(MiB))
    except OSError as e:   # APFS refuses names that are not valid UTF-8
        pytest.skip(f"filesystem refuses non-UTF-8 names: {e}")
    return Tree(root=root)
