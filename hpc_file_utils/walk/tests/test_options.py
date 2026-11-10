"""Option parsing (no MPI; the CLI runs it on rank 0 only)."""

import pytest

from hpc_file_utils.walk.common.options import parse_options
from hpc_file_utils.walk.sync.rsync import choose_rsync, parse_rsync_capabilities, rsync_base_opts
from hpc_file_utils.walk.tools import TOOLS


def test_defaults(tmp_path):
    a = parse_options("walkstat", ["--dirs", str(tmp_path)])
    assert a.dirs == [str(tmp_path)]
    assert (a.progress, a.heap_size, a.threshold_count) == (5, 500, 10000)
    assert (a.local_queue_max, a.dirs_per_dispatch) == (1000, 8)
    assert not hasattr(a, "max_dirs_per_send")       # replaced in phase 3.5
    assert a.threshold_size == 10 * 1024 * 1024      # '10MB', binary
    assert not a.verbose and not a.fail_on_errors
    assert a.spreadsheet is None and a.summary_prefix is None
    assert not hasattr(a, "errors")                  # -e/--errors was never used: removed
    assert not hasattr(a, "tar_queue_depth")


def test_values(tmp_path):
    a = parse_options("walkstat", ["--dirs", str(tmp_path), str(tmp_path), "--progress", "0",
                                   "--threshold-size", "0", "--heap-size", "3", "-v"])
    assert len(a.dirs) == 2
    assert a.progress == float("inf")                # <= 0 disables
    assert a.threshold_size == 0
    assert a.heap_size == 3 and a.verbose
    assert parse_options("walkstat", ["-d", str(tmp_path), "--threshold-size", "2GB"]).threshold_size == 2 * 1024 ** 3


def test_walktar_extras(tmp_path):
    a = parse_options("walktar", ["--dirs", str(tmp_path)])
    assert (a.batch_files, a.batch_size, a.read_ahead) == (1000, 16 * 1024 ** 3, 16)
    assert not hasattr(a, "tar_queue_depth")         # the tar thread is gone (phase 5)
    a = parse_options("walktar", ["--dirs", str(tmp_path), "--batch-files", "7", "--batch-size", "2MB"])
    assert (a.batch_files, a.batch_size) == (7, 2 * 1024 ** 2)
    assert parse_options("walktar", ["--dirs", str(tmp_path), "--read-ahead", "0"]).read_ahead == 0
    assert parse_options("walkstat", ["--dirs", str(tmp_path)]).batch_files == 1000   # stat batches
    for opt in (["--batch-size", "1GB"], ["--tar-queue-depth", "5"], ["--read-ahead", "4"]):
        with pytest.raises(SystemExit):
            parse_options("walkstat", ["--dirs", str(tmp_path), *opt])


@pytest.mark.parametrize("args", [["--batch-files", "0"], ["--batch-size", "lots"], ["--batch-size", "0"],
                                  ["--tar-queue-depth", "5"], ["--read-ahead", "-1"]])
def test_walktar_usage_errors(tmp_path, args, capsys):
    with pytest.raises(SystemExit) as e:
        parse_options("walktar", ["--dirs", str(tmp_path)] + args)
    assert e.value.code == 2


@pytest.mark.parametrize("args", [["--local-queue-max", "1"], ["--dirs-per-dispatch", "0"], ["--heap-size", "0"],
                                  ["--batch-files", "0"],
                                  ["--threshold-size", "lots"], ["--no-such-option"],
                                  ["--summary", "summary.xlsx"]])   # no abbreviations
def test_usage_errors_exit_2(tmp_path, args, capsys):
    with pytest.raises(SystemExit) as e:
        parse_options("walkstat", ["--dirs", str(tmp_path)] + args)
    assert e.value.code == 2


def test_dirs_required(capsys):
    with pytest.raises(SystemExit) as e:
        parse_options("walkstat", [])
    assert e.value.code == 2


def test_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError, match="no such directory"):
        parse_options("walkstat", ["--dirs", str(tmp_path / "nope")])


def test_walksync_options(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir(), dest.mkdir()
    a = parse_options("walksync", ["--dirs", f"{src}/", "--dest", f"{dest}/", "--rsync-opts", "--inplace -v"])
    assert a.dirs == [str(src)] and a.dest == str(dest)          # normalized: no trailing '/'
    assert (a.batch_files, a.batch_size) == (1000, 16 * 1024 ** 3)
    assert a.rsync_opts == ["--inplace", "-v"] and not a.dry_run
    assert a.app == "walksync" and a.rsyncs == 8
    assert TOOLS["walksync"].writes == ("copied", "rsync batches") and TOOLS["walkstat"].writes is None
    assert parse_options("walksync", ["--dirs", str(src), "--dest", str(dest), "--rsyncs", "3"]).rsyncs == 3
    a = parse_options("walksync", ["--dirs", str(src), "--dest", str(dest), "-n", "--mtime-newer", "1y",
                                   "--exclude", "COLD_STORAGE*", "--batch-size", "1GiB"])
    assert a.dry_run and a.filters.selects and a.batch_size == 1024 ** 3
    a = parse_options("walksync", ["--dirs", str(src), "--dest", str(dest), "--min-size", "1MiB", "--max-size", "2GiB"])
    assert (a.filters.min_size, a.filters.max_size) == (1024 ** 2, 2 * 1024 ** 3)


@pytest.mark.parametrize("args", [
    [],                                               # no --dest
    ["--dest", "{src}"],                              # the source itself
    ["--dest", "{src}/inner"],                        # inside the source
    ["--dest", "{dest}", "--rsync", "no-such-rsync-here"],
    ["--dest", "{dest}", "--rsync-opts", "'unbalanced"],
    ["--dest", "{dest}", "--rsyncs", "0"],
])
def test_walksync_usage_errors(tmp_path, args, capsys):
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "inner").mkdir(parents=True), dest.mkdir()
    args = [x.format(src=src, dest=dest) for x in args]
    with pytest.raises(SystemExit) as e:
        parse_options("walksync", ["--dirs", str(src)] + args)
    assert e.value.code == 2


def test_walksync_one_source_and_an_existing_dest(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    with pytest.raises(SystemExit):
        parse_options("walksync", ["--dirs", str(src), str(src), "--dest", str(tmp_path)])
    with pytest.raises(FileNotFoundError):
        parse_options("walksync", ["--dirs", str(src), "--dest", str(dest)])
    dest.mkdir()
    with pytest.raises(SystemExit):     # the source inside the destination
        parse_options("walksync", ["--dirs", str(src), "--dest", str(tmp_path)])


RSYNC_327 = """rsync  version 3.2.7  protocol version 31
Capabilities:
    64-bit files, 64-bit inums, 64-bit timestamps, 64-bit long ints,
    hardlink-symlinks, IPv6, atimes, batchfiles, inplace, append, ACLs,
    xattrs, optional secluded-args, iconv, prealloc, stop-at, no crtimes
"""


def test_rsync_capabilities():
    assert parse_rsync_capabilities(RSYNC_327) == (True, True)
    assert parse_rsync_capabilities(RSYNC_327.replace("ACLs", "no ACLs")) == (False, True)
    assert parse_rsync_capabilities(RSYNC_327.replace("xattrs,", "no xattrs,")) == (True, False)
    assert parse_rsync_capabilities("something else entirely") == (True, True)
    assert rsync_base_opts(True, True)[0] == "-aAX"
    assert rsync_base_opts(False, True) == ["-aX", "--numeric-ids", "--chmod=Du+w"]
    assert rsync_base_opts(False, False)[0] == "-a"


def test_walksync_without_acls_drops_A(tmp_path):
    """An rsync built without ACLs: -A is dropped and the run says ACLs are not copied."""
    fake = tmp_path / "rsync"
    fake.write_text("#!/bin/sh\necho 'rsync version 9  Capabilities: no ACLs, xattrs'\n")
    fake.chmod(0o755)
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir(), dest.mkdir()
    a = parse_options("walksync", ["--dirs", str(src), "--dest", str(dest), "--rsync", str(fake)])
    assert a.rsync_base[0] == "-aX" and a.rsync_missing == ["ACLs"]


def _fake_rsync(path, capabilities):
    """A stand-in rsync whose --version prints the given Capabilities text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho 'rsync  version 9  protocol version 31'\necho 'Capabilities:'\necho '    {capabilities}'\n")
    path.chmod(0o755)
    return str(path)


def test_walksync_prefers_an_rsync_with_acls(tmp_path, monkeypatch):
    """rsync on PATH without ACLs (derecho's conda env): an alternative that lists ACLs and
    xattrs is chosen and the reason given; one that lists them only vaguely is not."""
    _fake_rsync(tmp_path / "bin" / "rsync", "symlinks, no ACLs, xattrs")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    full = _fake_rsync(tmp_path / "usr" / "rsync", "symlinks, ACLs, xattrs")
    vague = tmp_path / "mac" / "rsync"
    vague.parent.mkdir()
    vague.write_text("#!/bin/sh\necho 'openrsync: protocol version 29'\n")
    vague.chmod(0o755)

    path, acls, xattrs, note = choose_rsync(alternatives=(str(vague), full))
    assert (path, acls, xattrs) == (full, True, True)
    assert note.startswith(f"using {full}") and "has no ACLs" in note

    path, acls, xattrs, note = choose_rsync(alternatives=(str(vague),))     # no capable alternative
    assert (path, acls, xattrs, note) == (str(tmp_path / "bin" / "rsync"), False, True, "")

    path, acls, xattrs, note = choose_rsync("rsync", alternatives=(full,))  # an explicit --rsync
    assert (path, acls, note) == (str(tmp_path / "bin" / "rsync"), False, "")


def test_walksync_keeps_a_capable_rsync_on_path(tmp_path, monkeypatch):
    on_path = _fake_rsync(tmp_path / "bin" / "rsync", "symlinks, ACLs, xattrs")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    other = _fake_rsync(tmp_path / "usr" / "rsync", "symlinks, ACLs, xattrs")
    assert choose_rsync(alternatives=(other,)) == (on_path, True, True, "")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))                  # none on PATH
    assert choose_rsync(alternatives=(other,))[0] == other
