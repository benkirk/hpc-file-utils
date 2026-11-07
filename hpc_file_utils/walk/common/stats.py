"""What a walk accumulates, per rank, and how two ranks' accumulators combine (no MPI).

WalkStats.merge() is associative and commutative (sums, and top-N heaps whose ties
break on the path), so the job combines its ranks' stats with one tree reduction
(MPIClass.collect()) and the result does not depend on which rank saw what.
"""

import grp
import pwd
from collections import defaultdict
from functools import partial
from typing import NamedTuple

from hpc_file_utils.walk.common.heaps import MaxHeap


class FileEntry(NamedTuple):
    path   : str
    nbytes : int
    mtime  : float
    ctime  : float
    atime  : float

class DirEntry(NamedTuple):
    path      : str
    nbytes    : int
    nitems    : int
    max_mtime : float   # of the files directly inside; -1 if there are none
    max_ctime : float
    max_atime : float


class IDCounts:
    """Bytes and entries owned by one uid or gid."""
    def __init__(self):
        self.id = None
        self.name = None
        self.nbytes = 0
        self.nitems = 0

    def set_id(self, id_in):
        if id_in != self.id:
            self.id = id_in
            self.name = self._id_to_name(id_in)

    def add(self, other):
        self.nbytes += other.nbytes
        self.nitems += other.nitems

    def to_dict(self):
        return {'name': self.name, 'nbytes': self.nbytes, 'nitems': self.nitems}

class UIDCounts(IDCounts):
    @staticmethod
    def _id_to_name(uid):
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return 'uid-{}'.format(uid)

class GIDCounts(IDCounts):
    @staticmethod
    def _id_to_name(gid):
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return 'gid-{}'.format(gid)


# heaps of (key, DirEntry) or (key, FileEntry), largest key first
DIR_HEAPS = ('nbytes_dirs', 'nbytes_dirs_3yrs', 'nbytes_dirs_5yrs', 'nbytes_dirs_7yrs', 'nitems_dirs')
FILE_HEAPS = ('nbytes_files',)
HEAPS = DIR_HEAPS + FILE_HEAPS


class RankInfo(NamedTuple):
    """One rank's own counts, for --verbose."""
    rank      : int
    host      : str
    num_dirs  : int
    num_files : int
    nbytes    : int


class WalkStats:
    """One rank's accumulators (or, after merge(), several ranks')."""

    def __init__(self, heap_size):
        self.heap_size = heap_size
        self.num_files = 0
        self.num_dirs = 0
        self.num_items = 0
        self.total_size = 0
        self.num_errors = 0   # entries that could not be scanned (or archived)
        self.st_modes = defaultdict(int)
        self.uids = defaultdict(UIDCounts)
        self.gids = defaultdict(GIDCounts)
        new_heap = partial(MaxHeap, heap_size)   # picklable, unlike a lambda
        self.heaps = {name: new_heap() for name in HEAPS}
        self.user_heaps = {name: defaultdict(new_heap) for name in HEAPS}   # name -> uid -> heap
        self.ranks = []       # RankInfo, one per rank merged in

    def merge(self, other):
        """Add 'other' into this one (in place); returns self."""
        self.num_files += other.num_files
        self.num_dirs += other.num_dirs
        self.num_items += other.num_items
        self.total_size += other.total_size
        self.num_errors += other.num_errors
        for k, v in other.st_modes.items():
            self.st_modes[k] += v
        for mine, theirs in ((self.uids, other.uids), (self.gids, other.gids)):
            for k, v in theirs.items():
                if k in mine:
                    mine[k].add(v)
                else:
                    mine[k] = v
        for name, heap in other.heaps.items():
            self.heaps[name].merge(heap)
        for name, per_user in other.user_heaps.items():
            for uid, heap in per_user.items():
                if heap.empty():
                    continue
                self.user_heaps[name][uid].merge(heap)
        self.ranks += other.ranks
        return self

    @staticmethod
    def merged(a, b):
        """The reduction operator for comm.reduce()."""
        return a.merge(b)


def sorted_counts(counts):
    """(key, count) pairs, largest count first; ties by key."""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def sorted_ids(ids):
    """IDCounts, largest nbytes first; ties by item count, then name and id."""
    return sorted(ids.values(), key=lambda c: (-c.nbytes, -c.nitems, str(c.name), c.id))
