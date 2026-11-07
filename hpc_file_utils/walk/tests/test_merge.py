"""WalkStats.merge() (the reduction operator) and the report sort orders (no MPI)."""

import pickle

from hpc_file_utils.walk.common.stats import (DirEntry, FileEntry, GIDCounts, UIDCounts, WalkStats,
                                              sorted_counts, sorted_ids)


def ids(cls, **named):
    """{id: counts} with fixed names: name -> (id, nbytes, nitems)."""
    out = {}
    for name, (i, nbytes, nitems) in named.items():
        c = cls()
        c.id, c.name, c.nbytes, c.nitems = i, name, nbytes, nitems
        out[i] = c
    return out


def test_sorted_counts_largest_first_ties_by_key():
    assert sorted_counts({"reg": 3, "dir": 5, "link": 3, "fifo": 1}) == \
        [("dir", 5), ("link", 3), ("reg", 3), ("fifo", 1)]


def test_sorted_ids_bytes_then_items_then_name():
    u = ids(UIDCounts, carol=(3, 100, 1), alice=(1, 100, 5), bob=(2, 100, 5), dave=(4, 900, 1))
    assert [c.name for c in sorted_ids(u)] == ["dave", "alice", "bob", "carol"]


def rank_stats(rank):
    s = WalkStats(heap_size=3)
    s.num_items, s.total_size, s.num_errors, s.num_files, s.num_dirs = 10 + rank, 1000 * rank, rank % 2, 7, 3
    s.st_modes.update({"reg": 7, "dir": 3})
    s.uids.update(ids(UIDCounts, **{f"u{rank % 2}": (rank % 2, 100, 10)}))
    s.gids.update(ids(GIDCounts, g=(0, 100, 10)))
    for i in range(4):
        d = DirEntry(f"/r{rank}/d{i}", 10 * i + rank, i, 0.0, 0.0, 0.0)
        s.heaps["nbytes_dirs"].add((d.nbytes, d))
        f = FileEntry(f"/r{rank}/f{i}", 5 * i + rank, 0.0, 0.0, 0.0)
        s.heaps["nbytes_files"].add((f.nbytes, f))
        s.user_heaps["nbytes_files"][rank % 2].add((f.nbytes, f))
    return s


def test_merge_sums_counts_and_keeps_top_n():
    total = WalkStats(heap_size=3)
    for r in range(1, 5):
        total = WalkStats.merged(total, rank_stats(r))
    assert total.num_items == sum(10 + r for r in range(1, 5))
    assert total.total_size == 1000 * (1 + 2 + 3 + 4)
    assert total.num_errors == 2
    assert dict(total.st_modes) == {"reg": 28, "dir": 12}
    assert {c.name: (c.nbytes, c.nitems) for c in total.uids.values()} == {"u0": (200, 20), "u1": (200, 20)}
    assert total.gids[0].nbytes == 400
    assert [k for k, _ in total.heaps["nbytes_dirs"].top()] == [34, 33, 32]
    assert [e.path for _, e in total.user_heaps["nbytes_files"][1].top()] == ["/r3/f3", "/r1/f3", "/r3/f2"]


def test_merge_order_does_not_matter_and_survives_pickling():
    def combine(order):
        total = WalkStats(heap_size=3)
        for r in order:
            # the reduction pickles partial results between ranks
            total = WalkStats.merged(pickle.loads(pickle.dumps(total)), rank_stats(r))
        return (total.num_items, sorted_counts(total.st_modes),
                [(c.name, c.nbytes) for c in sorted_ids(total.uids)],
                {n: h.top() for n, h in total.heaps.items()},
                {n: {u: h.top() for u, h in d.items()} for n, d in total.user_heaps.items()})

    assert combine([1, 2, 3, 4]) == combine([4, 2, 1, 3])


def test_new_heaps_use_the_heap_size():
    s = WalkStats(heap_size=7)
    assert s.heaps["nbytes_files"].maxsize == 7
    assert s.user_heaps["nbytes_dirs"][1234].maxsize == 7   # not MaxHeap's old default of 200
