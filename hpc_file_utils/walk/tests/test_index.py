"""The indexes (the sweep's Part C; common/index.py): the writer and the path encoding in
process, then walkstat --index and walktar --index under MPI, checked for closure (every
record's parent earlier in the same file), exactly once (no E path twice across the files),
completeness (walkstat's E records are find's listing; walktar's are its tars' members, with
their offsets), the found atime, and the written cache's eviction (HFU_INDEX_CACHE_MAX=1);
and --index=gz, the same records through gzip."""

import errno
import gzip
import os
import pickle
import zlib
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_file_utils.walk.common import index as index_mod
from hpc_file_utils.walk.common.index import (IndexWriter, closure_errors, decode_path, encode_path,
                                              entries, index_stat, read_index)
from hpc_file_utils.walk.common.stats import WalkStats
from hpc_file_utils.walk.common.worker import FileBatch
from hpc_file_utils.walk.sync.compare import NEW
from hpc_file_utils.walk.sync.worker import CopyBatch, SyncWorker
from hpc_file_utils.walk.tar import worker as tar_worker
from hpc_file_utils.walk.tests.test_filtering import find

ODD = ["plain.dat", "with space.dat", "new\nline.dat", "tab\there", "back\\slash", "quote'd \"name\"",
       "ünïcödé.dat", "bell\x07", "del\x7f", "-leading-dash",
       os.fsdecode(b"bad-\xff\xfe-name.dat"), os.fsdecode(b"\xc3\x28mixed\xe2\x82"), "nel\x85"]


# ---------------------------------------------------------------------------
# in process
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ODD, ids=range(len(ODD)))
def test_path_encoding_round_trips(name):
    path = "/a dir/" + name
    text = encode_path(path)
    assert decode_path(text) == path
    assert "\t" not in text and "\n" not in text and "\r" not in text
    text.encode("utf-8")                                 # valid UTF-8: no surrogates
    assert decode_path(encode_path(os.fsencode(path))) == path   # bytes in, as walked out


def test_path_encoding_is_what_the_doc_says():
    assert encode_path("/a/b\\c\nd\te\rf\x07g" + os.fsdecode(b"\xff")) == "/a/b\\\\c\\nd\\te\\rf\\x07g\\xff"
    assert encode_path("/ünïcödé") == "/ünïcödé"           # valid UTF-8 stays as it is


def _tree(root):
    (root / "a/b/c").mkdir(parents=True)
    (root / "a/b/c/leaf.dat").write_bytes(b"x" * 100)
    (root / "a/b/other.dat").write_bytes(b"y")
    (root / "a/top.dat").write_bytes(b"z")
    (root / "gone/sub").mkdir(parents=True)
    (root / "gone/sub/lost.dat").write_bytes(b"w")


def _records(name):
    header, records = read_index(name)
    return header, list(records)


def _st(path):
    return os.lstat(path)


def test_writer_closes_each_file_under_ancestry(tmp_path):
    root = tmp_path / "tree"
    _tree(root)
    out = tmp_path / "index-r001.tsv"
    w = IndexWriter(str(out), "walkstat", [str(root) + "/"])     # a root given with a trailing slash
    assert not out.exists()                                       # opened at the first record
    leaf = str(root / "a/b/c/leaf.dat")
    w.add(leaf, _st(leaf))
    other = str(root / "a/b/other.dat")
    w.add(other, _st(other))                                      # its parent is in the cache: no P
    w.add(str(root / "a"), _st(root / "a"))                       # a directory counted later: E, remembered
    w.close()
    assert w.written == {}                                        # the cache is forgotten at close

    header, recs = _records(out)
    assert header.tool == "walkstat" and header.roots == {str(root)}
    assert header.columns == ("rec",) + index_mod.STAT_COLUMNS + ("path",)
    assert [(r.rec, r.path) for r in recs] == [
        ("P", str(root)), ("P", str(root / "a")), ("P", str(root / "a/b")), ("P", str(root / "a/b/c")),
        ("E", leaf), ("E", other), ("E", str(root / "a"))]
    assert all(r.type == "d" and r.ino == os.lstat(r.path).st_ino for r in recs if r.rec == "P")
    e = recs[4]
    s = os.lstat(leaf)
    assert (e.type, e.size, e.alloc, e.mode, e.uid, e.gid, e.nlink, e.ino) == \
        ("f", 100, s.st_blocks * 512, s.st_mode & 0o7777, s.st_uid, s.st_gid, s.st_nlink, s.st_ino)
    assert (e.atime, e.mtime, e.ctime) == (int(s.st_atime), int(s.st_mtime), int(s.st_ctime))
    assert (e.offset, e.offset_data) == (None, None)
    assert closure_errors(out) == []


def test_a_root_needs_no_parent_and_a_vanished_parent_gets_dash_fields(tmp_path):
    root = tmp_path / "tree"
    _tree(root)
    out = tmp_path / "idx"
    w = IndexWriter(str(out), "walktar", [str(root)], offsets=True)
    w.open()
    assert out.exists()                                           # walktar's: made with its tar, even if empty
    w.add(str(root), _st(root), (0, 512))                         # walktar's dirs tar holds the root: E, first
    lost = str(root / "gone/sub/lost.dat")
    s = _st(lost)
    os.unlink(lost)
    os.rmdir(root / "gone/sub")
    os.rmdir(root / "gone")
    w.add(lost, s, (1024, 1536))                                  # recorded from the stat it was found with
    w.close()
    _, recs = _records(out)
    assert [(r.rec, r.path, r.offset, r.offset_data) for r in recs] == [
        ("E", str(root), 0, 512), ("P", str(root / "gone"), None, None),
        ("P", str(root / "gone/sub"), None, None), ("E", lost, 1024, 1536)]
    gone = recs[1]
    assert gone.type == "d" and all(getattr(gone, c) is None for c in index_mod.STAT_COLUMNS[1:])
    text = out.read_text()
    assert text.startswith("# hfu-index v1\ttool=walktar\tcolumns=rec,type,size,alloc,mode,uid,gid,nlink,ino,"
                           f"atime,mtime,ctime,offset,offset_data,path\troot={root}\n")
    assert "P\td\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t" in text
    assert closure_errors(out) == []


def test_the_written_cache_evicts_and_never_lies(tmp_path):
    root = tmp_path / "tree"
    _tree(root)
    out = tmp_path / "idx"
    w = IndexWriter(str(out), "walkstat", [str(root)], cache_max=1)
    for rel in ("a/b/c/leaf.dat", "a/b/other.dat", "a/top.dat"):
        p = str(root / rel)
        w.add(p, _st(p))
    assert len(w.written) == 1
    w.close()
    _, recs = _records(out)
    kinds = [(r.rec, os.path.relpath(r.path, root)) for r in recs]
    assert kinds == [("P", "."), ("P", "a"), ("P", "a/b"), ("P", "a/b/c"), ("E", "a/b/c/leaf.dat"),
                     ("P", "."), ("P", "a"), ("P", "a/b"), ("E", "a/b/other.dat"),
                     ("P", "."), ("P", "a"), ("E", "a/top.dat")]
    assert closure_errors(out) == []
    paths, repeated = entries([out])
    assert len(paths) == 3 and not repeated


def test_the_checkers_see_a_broken_file(tmp_path):
    out = tmp_path / "idx"
    good = "E\tf\t1\t512\t0644\t1\t1\t1\t9\t0\t0\t0\t/r/a/x\n"
    out.write_text("# hfu-index v1\ttool=walkstat\tcolumns=rec,type,size,alloc,mode,uid,gid,nlink,ino,atime,mtime,ctime,path\troot=/r\n"
                   "P\td\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t/r\n" + good + good + "X\td\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t/r/a\n")
    errs = closure_errors(out)
    assert len(errs) == 3 and "before its parent" in errs[0] and "record kind 'X'" in errs[2], errs
    assert entries([out]) == ({"/r/a/x"}, {"/r/a/x"})
    (tmp_path / "not-an-index").write_text("x\n")
    with pytest.raises(ValueError):
        read_index(tmp_path / "not-an-index")


def _write_tree_index(root, out, **kw):
    w = IndexWriter(str(out), "walkstat", [str(root)], **kw)
    for rel in ("a/b/c/leaf.dat", "a/b/other.dat", "a/top.dat", "a", "gone/sub/lost.dat"):
        p = str(root / rel)
        w.add(p, _st(p))
    w.close()
    return w.name


def test_gz_writes_the_same_records_as_one_gzip_stream(tmp_path):
    root = tmp_path / "tree"
    _tree(root)
    plain = _write_tree_index(root, tmp_path / "index-r001.tsv")
    gz = _write_tree_index(root, tmp_path / "index-r002.tsv", how="gz")
    assert plain == str(tmp_path / "index-r001.tsv") and gz == str(tmp_path / "index-r002.tsv.gz")
    assert not (tmp_path / "index-r002.tsv").exists()
    raw = Path(gz).read_bytes()
    assert raw[:2] == index_mod.GZIP_MAGIC
    d = zlib.decompressobj(wbits=31)                              # one gzip stream: nothing after its end
    text = d.decompress(raw)
    assert d.eof and d.unused_data == b""
    assert text == Path(plain).read_bytes()
    # the readers and checkers take either, by the first bytes, whatever the name
    (tmp_path / "renamed").write_bytes(raw)
    assert _records(gz) == _records(plain) == _records(tmp_path / "renamed")
    assert closure_errors(gz) == [] and entries([gz]) == entries([plain])
    assert index_mod.main([gz]) == 0


def test_gz_files_concatenate_into_one_readable_index(tmp_path):
    """cat of .gz files is one gzip file: zcat (gzip.open) reads every member."""
    root = tmp_path / "tree"
    _tree(root)
    a = _write_tree_index(root, tmp_path / "index-r001.tsv", how="gz")
    b = _write_tree_index(root, tmp_path / "index-r002.tsv", how="gz", cache_max=1)
    both = tmp_path / "both.tsv.gz"
    both.write_bytes(Path(a).read_bytes() + Path(b).read_bytes())
    with gzip.open(both, "rt", encoding="utf-8") as f:
        text = f.read()
    with gzip.open(a, "rt", encoding="utf-8") as fa, gzip.open(b, "rt", encoding="utf-8") as fb:
        assert text == fa.read() + fb.read()


class _FakeRsync:
    """An rsync that has already exited with rc."""

    def __init__(self, rc):
        self.rc = rc

    def wait(self, tick):
        return self.rc

    def tail(self):
        return "fake rsync output"

    def close(self):
        pass


def _sync_worker(tmp_path, root, index=True, dry_run=False):
    w = object.__new__(SyncWorker)   # skip MPIClass.__init__: no MPI here
    w.rank, w.src, w.dest, w.src_prefix = 1, str(root), str(tmp_path / "dest"), len(str(root)) + 1
    w.rsyncs, w.dry_run = 3, dry_run
    w.stats = WalkStats(heap_size=10)
    w.check_share = w.report_progress = lambda *a, **k: None
    w.copied_bytes = w.copied_files = w.nrsync = 0
    w.local_batches, w.batch, w.batch_bytes = [], [], 0
    w.options = SimpleNamespace(batch_files=2, batch_size=1 << 40)
    w.index = IndexWriter(str(tmp_path / "index-r001.tsv"), "walksync", [str(root)]) if index else None
    w.batch_stats = [] if index else None
    return w


def test_walksync_records_what_each_rsync_copied(tmp_path, monkeypatch):
    """A part whose rsync exited 0 is recorded whole, one that exited 24 (vanished) only
    for the entries still in the source, one that failed not at all; an entry without a
    stat (vanished before its compare's) never."""
    root = tmp_path / "src"
    (root / "sub").mkdir(parents=True)
    for n in ("sub/a", "c", "d", "e", "f"):
        (root / n).write_bytes(b"x")
    w = _sync_worker(tmp_path, root)
    paths = ["sub", "sub/a", "c", "d", "e", "f"]
    stats = [index_stat(os.lstat(root / p)) for p in paths]
    stats[1] = None
    os.unlink(root / "d")                                         # vanished after its compare
    rcs = iter([0, 24, 23])
    monkeypatch.setattr(w, "start_rsync", lambda part: _FakeRsync(next(rcs)))
    w.copy_batch(paths, 6, stats)
    w.index.close()
    _, recs = _records(w.index.name)
    assert [(r.rec, os.path.relpath(r.path, root)) for r in recs] == [("P", "."), ("E", "sub"), ("E", "c")]
    assert w.stats.num_errors == 1 and w.stats.counters["rsync_failures"] == 1
    assert w.stats.counters["copied_files"] == 0                 # the batch failed, as a unit


@pytest.mark.parametrize("index", [True, False], ids=["index", "no-index"])
def test_walksync_copy_batches_carry_their_stats(tmp_path, index):
    root = tmp_path / "src"
    root.mkdir()
    for n in ("a", "b", "c"):
        (root / n).write_bytes(b"x" * 10)
    w = _sync_worker(tmp_path, root, index=index)
    for n in ("a", "b", "c"):
        w.want_copy(n, 10, NEW, [], os.lstat(root / n))
    w.close_batch()
    first, second = w.local_batches
    if not index:
        assert type(first) is FileBatch and first == FileBatch(["a", "b"], 20)
        return
    assert type(first) is CopyBatch and first.paths == ["a", "b"] and first.nbytes == 20
    assert first.stats == [index_stat(os.lstat(root / n)) for n in ("a", "b")]
    assert second.stats == [index_stat(os.lstat(root / "c"))] and w.batch_stats == []
    assert pickle.loads(pickle.dumps(first)) == first            # a share hands it over whole
    assert len(pickle.dumps(first.stats[0])) < 200


def test_walksync_dry_run_records_at_the_compare(tmp_path):
    root = tmp_path / "src"
    (root / "sub").mkdir(parents=True)
    (root / "sub/a").write_bytes(b"x")
    w = _sync_worker(tmp_path, root, dry_run=True)
    lines = []
    w.want_copy("sub", 0, NEW, lines, os.lstat(root / "sub"))
    w.want_copy("sub/a", 1, NEW, lines, os.lstat(root / "sub/a"))
    w.want_copy("gone", 0, NEW, lines, None)                      # vanished before its stat: listed, not recorded
    assert lines == ["N sub\n", "N sub/a\n", "N gone\n"] and not w.local_batches and not w.batch
    w.index.close()
    _, recs = _records(w.index.name)
    assert [(r.rec, os.path.relpath(r.path, root)) for r in recs] == [("P", "."), ("E", "sub"), ("E", "sub/a")]


def test_header_fields_round_trip(tmp_path):
    out = tmp_path / "idx"
    w = IndexWriter(str(out), "walksync", ["/src"], fields=[("dest", encode_path("/d\test")), ("dry-run", "yes")])
    w.open()
    w.close()
    header, _ = read_index(out)
    assert header.roots == {"/src"} and header.fields == {"dest": "/d\\test", "dry-run": "yes"}
    assert decode_path(header.fields["dest"]) == "/d\test"
    assert out.read_text().startswith("# hfu-index v1\ttool=walksync\tcolumns=")
    assert out.read_text().rstrip("\n").endswith("\troot=/src\tdest=/d\\test\tdry-run=yes")


def test_a_truncated_gz_index_is_refused(tmp_path):
    root = tmp_path / "tree"
    _tree(root)
    gz = Path(_write_tree_index(root, tmp_path / "index-r001.tsv", how="gz"))
    gz.write_bytes(gz.read_bytes()[:-8])                          # no end of stream: a killed rank's
    with pytest.raises(EOFError):
        closure_errors(gz)


def test_member_offsets_are_what_a_reader_sees(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / ("long-" + "n" * 120 + ".dat")).write_bytes(b"x" * 1000)      # a pax header before the ustar one
    (src / "empty.dat").write_bytes(b"")
    os.symlink("empty.dat", src / "link")
    written = []
    with tarfile.open(tmp_path / "x.tar", "w", format=tarfile.PAX_FORMAT) as tar:
        for p in sorted(src.iterdir()) + [src]:
            start = tar.offset
            tar.add(p, recursive=False)
            written.append(tar_worker.member_offsets(tar, start))
    with tarfile.open(tmp_path / "x.tar") as tar:
        assert written == [(m.offset, m.offset_data) for m in tar.getmembers()]


def test_noatime_falls_back_on_eperm_only(tmp_path, monkeypatch):
    path = tmp_path / "f.dat"
    path.write_bytes(b"data")
    flag = 0o1_000_000_000                                        # a bit no real flag uses
    real_open = os.open
    calls = []

    def refusing(p, flags, *a):
        calls.append(flags & flag)
        if flags & flag:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_open(p, flags, *a)

    monkeypatch.setattr(tar_worker, "O_NOATIME", flag)
    monkeypatch.setattr(os, "open", refusing)
    f, data, found = tar_worker.open_member(str(path))
    f.close()
    assert data == b"data" and found.st_size == 4
    assert calls == [flag, 0]                                     # with the flag, then without

    def denying(p, flags, *a):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "open", denying)
    with pytest.raises(PermissionError):                          # a real refusal is not retried
        tar_worker.open_member(str(path))


# ---------------------------------------------------------------------------
# under MPI
# ---------------------------------------------------------------------------

CAPS = pytest.mark.parametrize("cap", [None, "1"], ids=["cache", "cap1"])
FILTERS = pytest.mark.parametrize("filters", [[], ["--mtime-older", "3y"], ["--mtime-newer", "2015-06-01"]],
                                  ids=["all", "older", "newer"])
# find's predicate for each filter (test_filtering.CASES)
PREDS = {"--mtime-older": ["-mtime", "+1095"], "--mtime-newer": ["-newermt", "2015-06-01"]}


def _checked(files):
    """Every file closed, and the E paths across them, none repeated (a rank that counted
    nothing leaves no file: a filter may select nothing at all)."""
    for f in files:
        assert closure_errors(f) == [], f
    paths, repeated = entries(files)
    assert not repeated, sorted(repeated)[:5]
    return paths


def _index_files(cwd, pattern):
    return sorted(Path(cwd).glob(pattern))


@pytest.mark.mpi
@CAPS
@FILTERS
@pytest.mark.parametrize("tree_name", ["golden", "bigdir"])
def test_walkstat_index_is_closed_once_and_complete(walk, golden_tree, big_dir_tree, tmp_path, monkeypatch,
                                                    tree_name, filters, cap):
    root = golden_tree.root if tree_name == "golden" else big_dir_tree
    args = ["--index", *filters] + (["--batch-files", "10"] if tree_name == "bigdir" else [])
    if cap:
        monkeypatch.setenv("HFU_INDEX_CACHE_MAX", cap)
    r = walk.run("walkstat", [root], cwd=tmp_path, args=args)
    assert r.ok, r.report()
    files = _index_files(tmp_path, "index-r*.tsv")
    assert all(f.name[len("index-r"):-4].isdigit() for f in files), files
    paths = _checked(files)
    expected = set(find(root, preds=PREDS.get(filters[0] if filters else None, [])))
    assert paths == expected
    assert len(paths) == r.total_count()
    if filters == ["--mtime-older", "3y"]:                        # every directory is recent: P only
        assert all(r.rec == "P" for f in files for r in _records(f)[1] if r.type == "d")
        assert (len(files) > 0) == (len(expected) > 0)            # no file from a rank that counted nothing
    else:
        assert files
    if not filters:                                               # the checker's find is unfiltered
        assert index_mod.main([str(f) for f in files] + ["--find"]) == 0


@pytest.mark.mpi
def test_walkstat_index_takes_the_summary_prefix_and_odd_names(walk, golden_tree, non_utf8_tree, tmp_path):
    r = walk.run("walkstat", [golden_tree.root, non_utf8_tree.root], cwd=tmp_path,
                 args=["--index", "--summary-prefix", "pfx"])
    assert r.ok, r.report()
    files = _index_files(tmp_path, "index-pfx-r*.tsv")
    assert files and not _index_files(tmp_path, "index-r*.tsv")
    header, _ = read_index(files[0])
    assert header.roots == {str(golden_tree.root), str(non_utf8_tree.root)}
    paths = _checked(files)
    assert paths == set(find(golden_tree.root)) | set(find(non_utf8_tree.root))
    for f in files:
        f.read_bytes().decode("utf-8")                            # strict: the odd names are escaped


@pytest.mark.mpi
def test_no_index_without_the_option(walk, golden_tree, private_golden_tree, tmp_path):
    r = walk.run("walkstat", [golden_tree.root], cwd=tmp_path / "stat")
    assert r.ok, r.report()
    r = walk.run("walktar", [private_golden_tree.root], cwd=tmp_path / "tar")
    assert r.ok, r.report()
    assert not list((tmp_path / "stat").glob("index-*")) and not list((tmp_path / "tar").glob("*.idx"))


def _members(tar_path):
    """{member name: (type, size, offset, offset_data)} of a tar."""
    with tarfile.open(tar_path) as tf:
        return {m.name: (m.type, m.size, m.offset, m.offset_data) for m in tf.getmembers()}


def _check_tar_indexes(cwd, suffix=".idx"):
    """Each <tar>.idx (suffix) beside its tar lists exactly the members, with the reader's
    offsets, and size bytes at offset_data are the file; the E paths across every index."""
    tars = sorted(Path(cwd).glob("output-r*.tar"))
    assert tars and [Path(f"{t}{suffix}") for t in tars] == _index_files(cwd, "output-r*.tar.idx*")
    for t in tars:
        members = _members(t)
        header, recs = _records(f"{t}{suffix}")
        assert header.tool == "walktar"
        es = [r for r in recs if r.rec == "E"]
        assert {r.path.lstrip("/") for r in es} == set(members), t
        assert all(r.offset is None and r.offset_data is None for r in recs if r.rec == "P"), t
        with open(t, "rb") as raw:
            for r in es:
                mtype, msize, offset, offset_data = members[r.path.lstrip("/")]
                assert (r.offset, r.offset_data) == (offset, offset_data), (t, r)
                if mtype == tarfile.REGTYPE:
                    assert r.type == "f" and r.size == msize, (t, r)
                    raw.seek(r.offset_data)
                    assert raw.read(r.size) == Path(r.path).read_bytes(), (t, r)
    return _checked(_index_files(cwd, "*.idx*"))


@pytest.mark.mpi
@CAPS
@FILTERS
@pytest.mark.parametrize("read_ahead", ["16", "0"])
def test_walktar_indexes_match_their_tars(walk, private_golden_tree, tmp_path, monkeypatch, read_ahead, filters, cap):
    tree = private_golden_tree
    if cap:
        monkeypatch.setenv("HFU_INDEX_CACHE_MAX", cap)
    r = walk.run("walktar", [tree.root], cwd=tmp_path, args=["--index", "--read-ahead", read_ahead, *filters])
    assert r.ok, r.report()
    paths = _check_tar_indexes(tmp_path)
    dirs = set(find(tree.root, preds=["-type", "d"], mindepth=0))
    files = set(find(tree.root, preds=["!", "-type", "d", *PREDS.get(filters[0] if filters else None, [])]))
    assert paths == dirs | files
    assert index_mod.main([str(f) for f in _index_files(tmp_path, "*.idx")] + (["--find"] if not filters else [])) == 0

    # the found atime: every file's record carries the atime the fixture set, not the read's.
    # --read-ahead 0 reads through tar.add(), without O_NOATIME: on Linux the first name of
    # a hard link is read before the second name's lstat, which then sees the read's atime
    found = {r.path: r.atime for f in _index_files(tmp_path, "output-r*-b*.tar.idx") for r in _records(f)[1]
             if r.rec == "E"}
    assert set(found) == files
    checked = files if read_ahead != "0" else {p for p in files if os.lstat(p).st_nlink == 1}
    assert len(files) - len(checked) in (0, 2)
    assert {p: found[p] for p in checked} == {p: tree.atimes[p] for p in checked}


@pytest.mark.mpi
@CAPS
def test_walktar_indexes_of_a_big_directory_spread_over_ranks(walk, big_dir_tree, tmp_path, monkeypatch, cap):
    if cap:
        monkeypatch.setenv("HFU_INDEX_CACHE_MAX", cap)
    r = walk.run("walktar", [big_dir_tree], cwd=tmp_path, args=["--index", "--batch-files", "10"])
    assert r.ok, r.report()
    paths = _check_tar_indexes(tmp_path)
    assert paths == set(find(big_dir_tree, mindepth=0))
    assert len(_index_files(tmp_path, "output-r*-b*.tar.idx")) > 3
    # every batch index holding files of the big directory starts with the root as P and
    # has the big directory as P before the first of its files (which batch holds what,
    # and whether a small directory's file came first in it, depends on the ranks' timing)
    big = str(big_dir_tree / "big")
    with_big = []
    for f in _index_files(tmp_path, "output-r*-b*.tar.idx"):
        recs = _records(f)[1]
        firsts = [i for i, x in enumerate(recs) if x.rec == "E" and os.path.dirname(x.path) == big]
        if firsts:
            with_big.append((recs, firsts[0]))
    assert len(with_big) > 3
    for recs, first in with_big:
        assert (recs[0].rec, recs[0].path) == ("P", str(big_dir_tree))
        assert any(x.rec == "P" and x.path == big for x in recs[:first])


@pytest.mark.mpi
def test_walktar_index_records_a_fifo_and_links_from_their_lstat(walk, private_golden_tree, tmp_path):
    tree = private_golden_tree
    r = walk.run("walktar", [tree.root], cwd=tmp_path, args=["--index"])
    assert r.ok, r.report()
    recs = [x for f in _index_files(tmp_path, "output-r*-b*.tar.idx") for x in _records(f)[1] if x.rec == "E"]
    by_name = {os.path.relpath(x.path, tree.root): x for x in recs}
    assert by_name["special/fifo"].type == "p"
    assert by_name["links/dangling"].type == "l" and by_name["links/dir-link"].type == "l"
    a, b = by_name["special/hard-a.dat"], by_name["special/hard-b.dat"]
    assert a.ino == b.ino and a.nlink == b.nlink == 2 and a.size == b.size == 3 * 1024 * 1024
    assert all(x.mode == os.lstat(x.path).st_mode & 0o7777 for x in recs)


# ---------------------------------------------------------------------------
# --index=gz under MPI
# ---------------------------------------------------------------------------

@pytest.mark.mpi
@CAPS
@pytest.mark.parametrize("tree_name", ["golden", "bigdir"])
def test_walkstat_gz_index_is_closed_once_and_complete(walk, golden_tree, big_dir_tree, tmp_path, cap, monkeypatch,
                                                       tree_name):
    root = golden_tree.root if tree_name == "golden" else big_dir_tree
    if cap:
        monkeypatch.setenv("HFU_INDEX_CACHE_MAX", cap)
    args = ["--index=gz"] + (["--batch-files", "10"] if tree_name == "bigdir" else [])
    r = walk.run("walkstat", [root], cwd=tmp_path, args=args)
    assert r.ok, r.report()
    files = _index_files(tmp_path, "index-r*")
    assert files and all(f.name.endswith(".tsv.gz") and f.read_bytes()[:2] == index_mod.GZIP_MAGIC for f in files)
    paths = _checked(files)
    assert paths == set(find(root)) and len(paths) == r.total_count()
    assert index_mod.main([str(f) for f in files] + ["--find"]) == 0


@pytest.mark.mpi
@CAPS
@pytest.mark.parametrize("tree_name", ["golden", "bigdir"])
def test_walktar_gz_indexes_match_their_tars(walk, private_golden_tree, big_dir_tree, tmp_path, monkeypatch, cap,
                                             tree_name):
    root = private_golden_tree.root if tree_name == "golden" else big_dir_tree
    if cap:
        monkeypatch.setenv("HFU_INDEX_CACHE_MAX", cap)
    args = ["--index=gz"] + (["--batch-files", "10"] if tree_name == "bigdir" else [])
    r = walk.run("walktar", [root], cwd=tmp_path, args=args)
    assert r.ok, r.report()
    assert not _index_files(tmp_path, "*.idx")
    paths = _check_tar_indexes(tmp_path, ".idx.gz")
    assert paths == set(find(root, mindepth=0))
    assert index_mod.main([str(f) for f in _index_files(tmp_path, "*.idx.gz")] + ["--find"]) == 0
