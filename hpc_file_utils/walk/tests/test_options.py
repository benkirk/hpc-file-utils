"""Option parsing (no MPI; the CLI runs it on rank 0 only)."""

import pytest

from hpc_file_utils.walk.common.options import parse_options


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
    assert (a.batch_files, a.batch_size) == (1000, 16 * 1024 ** 3)
    assert not hasattr(a, "tar_queue_depth")         # the tar thread is gone (phase 5)
    a = parse_options("walktar", ["--dirs", str(tmp_path), "--batch-files", "7", "--batch-size", "2MB"])
    assert (a.batch_files, a.batch_size) == (7, 2 * 1024 ** 2)
    assert parse_options("walkstat", ["--dirs", str(tmp_path)]).batch_files == 1000   # stat batches
    for opt in (["--batch-size", "1GB"], ["--tar-queue-depth", "5"]):
        with pytest.raises(SystemExit):
            parse_options("walkstat", ["--dirs", str(tmp_path), *opt])


@pytest.mark.parametrize("args", [["--batch-files", "0"], ["--batch-size", "lots"], ["--batch-size", "0"],
                                  ["--tar-queue-depth", "5"]])
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
