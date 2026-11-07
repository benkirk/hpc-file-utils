"""A fixed-size heap that keeps the N largest elements (no MPI).

Elements are (key, entry) tuples, e.g. (nbytes, DirEntry). Ties on the key fall back to
comparing the entries, whose first field is the path, so which elements survive the
cut-off does not depend on the order they were added (or on which rank saw them).
"""

# https://stackoverflow.com/questions/30443150/maintain-a-fixed-size-heap-python
from heapq import heapify, heappush, heappushpop, nlargest


class MaxHeap():

    def __init__(self, top_n):
        self.h = []
        self.maxsize = top_n
        heapify(self.h)

    def add(self, element):
        if len(self.h) < self.maxsize:
            heappush(self.h, element)
        else:
            heappushpop(self.h, element)

    def add_all(self, elements):
        for e in elements:
            self.add(e)

    def merge(self, other):
        """Keep the N largest of both heaps (in place); returns self."""
        self.add_all(other.h)
        return self

    def top(self, count=None):
        """The largest 'count' elements (all, if None), largest first."""
        if not count: count = len(self.h)
        return nlargest(count, self.h)

    def get_list(self):
        return self.h

    def empty(self):
        return 0 == len(self.h)

    def __len__(self):
        return len(self.h)
