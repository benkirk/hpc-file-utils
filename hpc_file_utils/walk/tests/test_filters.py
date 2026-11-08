"""The phase 4 filters: parsing, validation and the compiled callables (no MPI)."""

import grp
import os
import pickle
import pwd
import stat
import types
from datetime import datetime

import pytest

from hpc_file_utils.walk.common.filters import parse_when
from hpc_file_utils.walk.common.options import parse_options

NOW = 2_000_000_000.0
DAY = 86400
ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


def filters(tmp_path, *args):
    return parse_options("walkstat", ["--dirs", str(tmp_path), *args]).filters


def entry(mode=stat.S_IFREG, size=0, uid=0, gid=0, mtime=NOW, atime=NOW, ctime=NOW):
    return types.SimpleNamespace(st_mode=mode | 0o644, st_size=size, st_uid=uid, st_gid=gid,
                                 st_mtime=mtime, st_atime=atime, st_ctime=ctime)


def test_no_filters_is_none(tmp_path):
    assert filters(tmp_path) is None


@pytest.mark.parametrize("when, seconds", [("30d", 30 * DAY), ("7", 7 * DAY), ("12h", 12 * 3600),
                                           ("90m", 90 * 60), ("45s", 45), ("1.5w", 10.5 * DAY),
                                           ("3y", 3 * 365 * DAY)])
def test_ages(when, seconds):
    assert parse_when(when, NOW) == NOW - seconds


@pytest.mark.parametrize("when, dt", [("2020-01-01", datetime(2020, 1, 1)),
                                      ("2020-01-01T12:30", datetime(2020, 1, 1, 12, 30)),
                                      ("2020-01-01T12:30:15", datetime(2020, 1, 1, 12, 30, 15))])
def test_dates_are_local(when, dt):
    assert parse_when(when, NOW) == dt.timestamp()


@pytest.mark.parametrize("when", ["yesterday", "30x", "-30d", "2020-13-01", "2020-01-01T00:00+00:00", ""])
def test_bad_when(when):
    with pytest.raises(ValueError):
        parse_when(when, NOW)


def test_time_window(tmp_path):
    f = filters(tmp_path, "--mtime-older", "3y", "--mtime-newer", "5y")
    newer, older = f.times["mtime"]
    assert older - newer == pytest.approx(2 * 365 * DAY, abs=5)
    keep = f.predicate()
    assert keep(entry(mtime=older - DAY))
    assert not keep(entry(mtime=older + DAY))     # too recent
    assert not keep(entry(mtime=newer - DAY))     # too old
    assert "--mtime-older 3y (before " in f.describe()


@pytest.mark.parametrize("field", ["mtime", "atime", "ctime"])
def test_each_time_field(tmp_path, field):
    keep = filters(tmp_path, f"--{field}-older", "2000-01-01").predicate()
    old = datetime(1999, 1, 1).timestamp()
    assert keep(entry(**{field: old}))
    assert not keep(entry(**{field: NOW}))


def test_users_and_groups(tmp_path):
    f = filters(tmp_path, "--user", f"{ME},987654", "--user", "0", "--group", MY_GROUP)
    assert f.uids == {os.getuid(), 987654, 0}
    assert f.gids == {os.getgid()}
    keep = f.predicate()
    assert keep(entry(uid=987654, gid=os.getgid()))
    assert not keep(entry(uid=987654, gid=os.getgid() + 1))
    assert not keep(entry(uid=1, gid=os.getgid()))


def test_sizes_are_inclusive(tmp_path):
    f = filters(tmp_path, "--min-size", "1MB", "--max-size", "2MB")
    assert (f.min_size, f.max_size) == (1024 ** 2, 2 * 1024 ** 2)
    keep = f.predicate()
    assert [keep(entry(size=n)) for n in (1024 ** 2 - 1, 1024 ** 2, 2 * 1024 ** 2, 2 * 1024 ** 2 + 1)] \
        == [False, True, True, False]


def test_types(tmp_path):
    keep = filters(tmp_path, "--type", "f,l").predicate()
    assert keep(entry(stat.S_IFREG)) and keep(entry(stat.S_IFLNK))
    assert not keep(entry(stat.S_IFDIR)) and not keep(entry(stat.S_IFIFO))


def test_kinds_and_together(tmp_path):
    keep = filters(tmp_path, "--type", "f", "--min-size", "10", "--user", "5").predicate()
    assert keep(entry(size=10, uid=5))
    assert not keep(entry(size=9, uid=5))
    assert not keep(entry(size=10, uid=6))
    assert not keep(entry(stat.S_IFDIR, size=10, uid=5))


def test_exclude_matches_names(tmp_path):
    f = filters(tmp_path, "--exclude", "COLD_STORAGE*", "--exclude", "*.tmp")
    assert f.predicate() is None and not f.selects    # pruning only
    excluded = f.exclude_matcher()
    assert excluded("COLD_STORAGE") and excluded("COLD_STORAGE_2019")
    assert excluded("x.tmp") and excluded(".hidden.tmp")    # like find, '*' matches a leading dot
    assert not excluded("cold_storage")                     # case-sensitive
    assert not excluded("my_COLD_STORAGE") and not excluded("x.tmp.gz")
    assert excluded("new\nline.tmp")
    assert f.describe() == "--exclude 'COLD_STORAGE*'  --exclude '*.tmp'"


def test_filters_pickle(tmp_path):
    """The options reach the workers by bcast; the callables are built on each rank."""
    f = filters(tmp_path, "--exclude", "COLD*", "--atime-older", "1y", "--user", ME, "--type", "f")
    g = pickle.loads(pickle.dumps(f))
    assert g == f
    old = entry(uid=os.getuid(), atime=0.0)
    assert g.predicate()(old) and not g.predicate()(entry(uid=os.getuid()))
    assert g.exclude_matcher()("COLD_STORAGE")


@pytest.mark.parametrize("args", [["--exclude", "a/b"], ["--exclude", ""], ["--mtime-older", "soon"],
                                  ["--atime-newer", "2020-02-30"], ["--user", "no-such-user-hfu"],
                                  ["--group", "no-such-group-hfu"], ["--user", "a,,b"],
                                  ["--min-size", "lots"], ["--min-size", "2MB", "--max-size", "1MB"],
                                  ["--type", "f,x"], ["--type", ""]])
def test_usage_errors_exit_2(tmp_path, args, capsys):
    with pytest.raises(SystemExit) as e:
        parse_options("walkstat", ["--dirs", str(tmp_path), *args])
    assert e.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_walktar_takes_the_filters(tmp_path):
    assert parse_options("walktar", ["--dirs", str(tmp_path), "--type", "f"]).filters.types == {stat.S_IFREG}
