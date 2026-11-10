"""MaxHeap: the fixed-size top-N lists (no MPI)."""

import random

from hpc_file_utils.walk.common.heaps import MaxHeap
from hpc_file_utils.walk.common.stats import DirEntry


def de(path, nbytes):
    return DirEntry(path, nbytes, 1, 0.0, 0.0, 0.0)


def test_keeps_the_n_largest_largest_first():
    h = MaxHeap(3)
    for n in [5, 1, 9, 3, 7, 2]:
        h.add((n, de(f"/d{n}", n)))
    assert [k for k, _ in h.top()] == [9, 7, 5]
    assert [k for k, _ in h.top(2)] == [9, 7]
    assert len(h) == 3 and h
    assert not MaxHeap(3)


def test_ties_at_the_cutoff_break_on_path_whatever_the_order():
    entries = [(1, de(f"/p{i:02d}", 1)) for i in range(20)]
    kept = set()
    for seed in range(10):
        random.Random(seed).shuffle(entries)
        h = MaxHeap(5)
        h.add_all(entries)
        kept.add(tuple(e.path for _, e in h.top()))
    assert kept == {("/p19", "/p18", "/p17", "/p16", "/p15")}


def test_merge_is_the_top_n_of_the_union():
    a, b = MaxHeap(4), MaxHeap(4)
    a.add_all((n, de(f"/a{n}", n)) for n in [1, 8, 3, 6])
    b.add_all((n, de(f"/b{n}", n)) for n in [7, 2, 9, 5])
    assert a.merge(b) is a
    assert [k for k, _ in a.top()] == [9, 8, 7, 6]
    assert len(a) == 4


def test_merge_order_does_not_matter():
    parts = []
    for r in range(4):
        h = MaxHeap(6)
        h.add_all((n % 7, de(f"/r{r}/{n}", n % 7)) for n in range(r, 40, 3))
        parts.append(h)

    def combined(order):
        out = MaxHeap(6)
        for i in order:
            out.merge(parts[i])
        return out.top()

    assert combined([0, 1, 2, 3]) == combined([3, 1, 0, 2])
