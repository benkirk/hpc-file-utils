"""walktar's members (phase 5.5): the reader threads open and read a batch's files ahead,
and TarWorker.add_member() writes them; the tar must hold exactly what tar.add() wrote.
In-process, no MPI."""

import concurrent.futures
import os
import tarfile
import types

import pytest

from hpc_file_utils.walk.common.stats import WalkStats
from hpc_file_utils.walk.common.worker import FileBatch
from hpc_file_utils.walk.tar import worker as tar_worker
from hpc_file_utils.walk.tar.worker import TarWorker

FIELDS = ("name", "type", "mode", "uid", "gid", "uname", "gname", "mtime", "size", "linkname",
          "pax_headers", "devmajor", "devminor")


@pytest.fixture
def entries(tmp_path):
    """A batch's paths, every kind a batch can hold, hard links in order."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "empty.dat").write_bytes(b"")
    (src / "small.dat").write_bytes(b"x" * 100)
    (src / "big.dat").write_bytes(os.urandom(300_000))
    (src / "hard-a.dat").write_bytes(b"linked")
    os.link(src / "hard-a.dat", src / "hard-b.dat")
    os.symlink("small.dat", src / "link-to-file")
    os.symlink("nowhere", src / "dangling")
    (src / "sub").mkdir()
    os.symlink("sub", src / "link-to-dir")
    os.mkfifo(src / "fifo")
    os.chmod(src / "small.dat", 0o640)
    os.utime(src / "big.dat", (1_000_000_000, 1_000_000_000.5))
    names = ["empty.dat", "small.dat", "big.dat", "hard-a.dat", "link-to-file", "dangling",
             "hard-b.dat", "link-to-dir", "fifo"]
    return [str(src / n) for n in names]


def _tar_worker(read_ahead):
    w = object.__new__(TarWorker)   # skip MPIClass.__init__: no MPI here
    w.rank, w.ntars, w.nwritten_files, w.nwritten_closed = 1, 0, 0, 0
    w.batch_tar, w.dirs_tar, w.readers = None, None, None
    w.options = types.SimpleNamespace(read_ahead=read_ahead)
    w.stats = WalkStats(heap_size=10)
    w.check_share = w.report_progress = lambda *a, **k: None
    return w


def _members(path):
    with tarfile.open(path) as tf:
        out = []
        for m in tf.getmembers():
            data = tf.extractfile(m).read() if m.isreg() else None
            out.append((tuple(getattr(m, f) for f in FIELDS), data))
    return out


def _reference(paths, out):
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar:
        for p in paths:
            tar.add(p, recursive=False)
    return _members(out)


@pytest.mark.parametrize("whole_max", [tar_worker.READ_WHOLE_MAX, 1000], ids=["whole", "stream"])
@pytest.mark.parametrize("read_ahead", [0, 1, 4])
def test_members_are_tar_adds(entries, tmp_path, monkeypatch, read_ahead, whole_max):
    expected = _reference(entries, tmp_path / "ref.tar")
    monkeypatch.setattr(tar_worker, "READ_WHOLE_MAX", whole_max)
    monkeypatch.chdir(tmp_path)
    w = _tar_worker(read_ahead)
    w.process_batch(FileBatch(entries, 0))
    got = _members(tmp_path / "output-r001-b00000.tar")
    assert got == expected
    assert [m[0][1] for m in got].count(tarfile.LNKTYPE) == 1        # hard-b links to hard-a
    assert (w.nwritten_files, w.stats.num_errors) == (len(entries), 0)
    assert w.nwritten_closed == os.path.getsize(tmp_path / "output-r001-b00000.tar")
    if w.readers is not None:
        w.readers.shutdown()


@pytest.mark.parametrize("read_ahead", [0, 4])
def test_a_vanished_file_costs_one_error(entries, tmp_path, monkeypatch, read_ahead, caplog):
    os.unlink(entries[1])                                   # between the scan and the tar
    monkeypatch.chdir(tmp_path)
    w = _tar_worker(read_ahead)
    w.process_batch(FileBatch(entries, 0))
    names = [m[0][0] for m in _members(tmp_path / "output-r001-b00000.tar")]
    assert (w.nwritten_files, w.stats.num_errors) == (len(entries) - 1, 1)
    assert entries[1].lstrip("/") not in names and len(names) == len(entries) - 1
    assert "Cannot add to tar file" in caplog.text
    if w.readers is not None:
        w.readers.shutdown()


def test_readers_never_open_a_fifo_or_follow_a_symlink(entries):
    by_name = {os.path.basename(p): p for p in entries}
    for name in ("fifo", "link-to-file", "dangling", "link-to-dir"):
        assert tar_worker.open_member(by_name[name]) is None        # tar.add() archives these
    f, data = tar_worker.open_member(by_name["small.dat"])
    with f:
        assert data == b"x" * 100


def test_a_file_that_grew_after_its_read_is_archived_as_tar_add_would(entries, tmp_path, monkeypatch):
    path = entries[1]
    opened = concurrent.futures.Future()
    opened.set_result(tar_worker.open_member(path))         # read: 100 bytes
    with open(path, "ab") as f:
        f.write(b"y" * 50)                                  # then it grows
    monkeypatch.chdir(tmp_path)
    w = _tar_worker(1)
    with tarfile.open("out.tar", "w", format=tarfile.PAX_FORMAT) as tar:
        assert w.add_member(tar, path, opened)
    expected = _reference([path], tmp_path / "ref.tar")
    assert _members(tmp_path / "out.tar") == expected
    assert expected[0][1] == b"x" * 100 + b"y" * 50


def test_unarchived_read_ahead_files_are_closed(entries, monkeypatch):
    opened = []

    def recording(path):
        member = real(path)
        if member is not None:
            opened.append(member[0])
        return member

    real = tar_worker.open_member
    monkeypatch.setattr(tar_worker, "open_member", recording)
    w = _tar_worker(4)
    regular = [p for p in entries if p.endswith(".dat")]
    gen = w.read_ahead(regular)
    path, future = next(gen)                                # the next 4 are being read ahead
    future.result()[0].close()                              # (as add_member would)
    gen.close()                                             # an error broke the loop
    w.readers.shutdown()
    assert len(opened) > 1 and all(f.closed for f in opened)
