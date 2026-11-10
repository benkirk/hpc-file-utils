"""WalkStats.count(): the walk's accounting for one entry (pure, no MPI; sweep A2)."""

import stat
from types import SimpleNamespace

from hpc_file_utils.walk.common.stats import DirTotals, WalkStats

KINDS = {"reg": stat.S_IFREG, "link": stat.S_IFLNK, "block": stat.S_IFBLK, "char": stat.S_IFCHR,
         "fifo": stat.S_IFIFO, "sock": stat.S_IFSOCK}


def entry(kind=stat.S_IFREG, size=4096, blocks=None, uid=1000, gid=1000, mtime=100.0, ctime=200.0, atime=300.0):
    """A stat result; blocks default to what the size occupies."""
    blocks = -(-size // 512) if blocks is None else blocks
    return SimpleNamespace(st_mode=kind | 0o644, st_size=size, st_blocks=blocks, st_uid=uid, st_gid=gid,
                           st_mtime=mtime, st_ctime=ctime, st_atime=atime)


def test_type_counts():
    st, totals = WalkStats(heap_size=5), DirTotals()
    for name, kind in KINDS.items():
        st.count("/t/" + name, entry(kind), totals, False)
    assert dict(st.st_modes) == {name: 1 for name in KINDS}
    assert (st.num_items, st.num_files, totals.nitems) == (6, 6, 6)
    assert st.gids[1000].nitems == 6


def test_a_directory_counts_as_an_item_and_as_a_dir_only_when_asked():
    st, totals = WalkStats(heap_size=5), DirTotals()
    st.count("/t/d", entry(stat.S_IFDIR), totals, False)        # no selecting filter: the scan counts it
    assert dict(st.st_modes) == {} and st.num_files == 0
    assert (st.num_items, totals.nitems, totals.uids) == (1, 1, {1000})
    assert st.uids[1000].nitems == 1
    st.count("/t/e", entry(stat.S_IFDIR), totals, True)         # a selecting filter: an entry that matched
    assert dict(st.st_modes) == {"dir": 1}
    assert not st.heaps["nbytes_files"]                         # directories never go in the file lists


def test_heaps_keep_the_largest_files_overall_and_per_user():
    st, totals = WalkStats(heap_size=2), DirTotals()
    for i, (size, uid) in enumerate([(1 << 20, 1), (3 << 20, 2), (2 << 20, 1), (4 << 20, 2)]):
        st.count("/t/f{}".format(i), entry(size=size, uid=uid), totals, False)
    assert [e.path for _, e in st.heaps["nbytes_files"].top()] == ["/t/f3", "/t/f1"]
    assert [e.path for _, e in st.user_heaps["nbytes_files"][1].top()] == ["/t/f2", "/t/f0"]
    assert [e.path for _, e in st.user_heaps["nbytes_files"][2].top()] == ["/t/f3", "/t/f1"]
    assert (st.uids[1].nbytes, st.uids[2].nitems) == (3 << 20, 2)
    assert (st.total_size, totals.nbytes) == (10 << 20, 10 << 20)


def test_max_times_are_over_the_files_only():
    st, totals = WalkStats(heap_size=5), DirTotals()
    st.count("/t/d", entry(stat.S_IFDIR, mtime=900, ctime=900, atime=900), totals, True)
    assert (totals.max_mtime, totals.max_ctime, totals.max_atime) == (-1, -1, -1)
    st.count("/t/a", entry(mtime=10, ctime=20, atime=30), totals, True)
    st.count("/t/b", entry(mtime=5, ctime=25, atime=15), totals, True)
    assert (totals.max_mtime, totals.max_ctime, totals.max_atime) == (10, 25, 30)


def test_size_is_the_smaller_of_st_size_and_the_blocks():
    st, totals = WalkStats(heap_size=5), DirTotals()
    st.count("/t/sparse", entry(size=1 << 30, blocks=8), totals, False)   # sparse, or migrated to tape
    st.count("/t/small", entry(size=10, blocks=8), totals, False)         # a whole block for 10 bytes
    assert (st.total_size, totals.nbytes) == (4096 + 10, 4096 + 10)
    assert [e.nbytes for _, e in st.heaps["nbytes_files"].top()] == [4096, 10]
