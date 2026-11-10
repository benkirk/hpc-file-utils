"""What a walk accumulates, per rank, and how two ranks' accumulators combine (no MPI).

WalkStats.merge() is associative and commutative (sums, and top-N heaps whose ties
break on the path), so the job combines its ranks' stats with one tree reduction
(MPIClass.collect()) and the result does not depend on which rank saw what.
"""

import grp
import pwd
import stat
import time
from collections import Counter, defaultdict
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


class DirTotals:
    """A directory's own (non-recursive) totals, or part of them: walkstat's stat batches
    (phase 5) can split a big directory's entries over several ranks, whose parts add up.
    Max times are over the non-directories only; -1 while there are none."""
    __slots__ = ('nitems', 'nbytes', 'max_mtime', 'max_ctime', 'max_atime', 'uids')

    def __init__(self):
        self.nitems = 0
        self.nbytes = 0
        self.max_mtime = self.max_ctime = self.max_atime = -1
        self.uids = set()

    def add(self, other):
        self.nitems += other.nitems
        self.nbytes += other.nbytes
        self.max_mtime = max(self.max_mtime, other.max_mtime)
        self.max_ctime = max(self.max_ctime, other.max_ctime)
        self.max_atime = max(self.max_atime, other.max_atime)
        self.uids |= other.uids
        return self

    def __getstate__(self):
        return tuple(getattr(self, k) for k in self.__slots__)

    def __setstate__(self, state):
        for k, v in zip(self.__slots__, state):
            setattr(self, k, v)


SECONDS_IN_YEAR = 60*60*24*365
THREE_YEARS = 3*SECONDS_IN_YEAR
FIVE_YEARS  = 5*SECONDS_IN_YEAR
SEVEN_YEARS = 7*SECONDS_IN_YEAR

NOW_TIMESTAMP = time.time()


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
            return f'uid-{uid}'

class GIDCounts(IDCounts):
    @staticmethod
    def _id_to_name(gid):
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return f'gid-{gid}'


# heaps of (key, DirEntry) or (key, FileEntry), largest key first
DIR_HEAPS = ('nbytes_dirs', 'nbytes_dirs_3yrs', 'nbytes_dirs_5yrs', 'nbytes_dirs_7yrs', 'nitems_dirs')
FILE_HEAPS = ('nbytes_files',)
HEAPS = DIR_HEAPS + FILE_HEAPS

# an entry's type bits (S_IFMT) -> its name in the summary's type counts (directories apart)
TYPE_NAMES = {stat.S_IFREG: 'reg', stat.S_IFLNK: 'link', stat.S_IFBLK: 'block', stat.S_IFCHR: 'char',
              stat.S_IFIFO: 'fifo', stat.S_IFSOCK: 'sock'}


class RankInfo(NamedTuple):
    """One rank's own counts, for --verbose."""
    rank      : int
    host      : str
    num_dirs  : int
    num_files : int
    nbytes    : int
    wait_time : float   # waiting for the Manager to answer 'ready' with work
    max_local_dirs : int  # high-water mark of its local stack


def _add_into(mine, theirs):
    """Add the dict 'theirs' into 'mine': a key both hold is add()ed, a new one taken."""
    for k, v in theirs.items():
        if k in mine:
            mine[k].add(v)
        else:
            mine[k] = v


class WalkStats:
    """One rank's accumulators (or, after merge(), several ranks')."""

    def __init__(self, heap_size):
        self.heap_size = heap_size
        self.num_files = 0
        self.num_dirs = 0
        self.num_items = 0
        self.total_size = 0
        self.num_errors = 0   # entries that could not be scanned (or archived)
        self.num_excluded = 0   # entries pruned by --exclude (never stat'ed or descended)
        self.num_unmatched = 0  # entries stat'ed but not counted: they failed a selecting filter
        self.st_modes = defaultdict(int)
        self.uids = defaultdict(UIDCounts)
        self.gids = defaultdict(GIDCounts)
        new_heap = partial(MaxHeap, heap_size)   # picklable, unlike a lambda
        self.heaps = {name: new_heap() for name in HEAPS}
        self.user_heaps = {name: defaultdict(new_heap) for name in HEAPS}   # name -> uid -> heap
        self.ranks = []       # RankInfo, one per rank merged in
        # walkstat: directories whose entries were split over several stat batches, path ->
        # DirTotals; their parts are added up here, and listed by finish_directories()
        self.partial_dirs = {}
        # a tool's own totals (walksync: new, changed, copied bytes, rsync batches, ...),
        # summed by merge()
        self.counters = Counter()

    def count(self, path, statinfo, totals, count_dirs):
        """Count one entry that passed the filters: the walk's accounting, into these
        totals and into its directory's 'totals' (a DirTotals). Every tool's walk keeps
        it. count_dirs: whether a directory counts in the type counts here (with a
        selecting filter, directories are counted as entries that match, as find does;
        without one, the scan counts every directory it walks, the roots included)."""
        fmode = statinfo.st_mode

        # about statinfo.st_size: we can't always trust it. for sparse files, or files
        # migrated to tape via HSM, the size is not what is occupied on disk.
        item_size = min(statinfo.st_size, statinfo.st_blocks * 512)

        self.num_items += 1
        self.total_size += item_size
        totals.nitems += 1
        totals.nbytes += item_size
        totals.uids.add(statinfo.st_uid)

        u = self.uids[statinfo.st_uid]
        u.set_id(statinfo.st_uid)
        u.nitems += 1
        u.nbytes += item_size

        g = self.gids[statinfo.st_gid]
        g.set_id(statinfo.st_gid)
        g.nitems += 1
        g.nbytes += item_size

        if stat.S_ISDIR(fmode):
            if count_dirs:
                self.st_modes['dir'] += 1
            return

        self.num_files += 1
        name = TYPE_NAMES.get(stat.S_IFMT(fmode))
        if name is not None:
            self.st_modes[name] += 1

        # the *maximum* mtime/ctime/atime of the directory's contents (not the dir itself)
        totals.max_mtime = max(totals.max_mtime, statinfo.st_mtime)
        totals.max_ctime = max(totals.max_ctime, statinfo.st_ctime)
        totals.max_atime = max(totals.max_atime, statinfo.st_atime)

        fe = FileEntry(path, item_size, statinfo.st_mtime, statinfo.st_ctime, statinfo.st_atime)
        self.heaps['nbytes_files'].add((item_size, fe))
        self.user_heaps['nbytes_files'][statinfo.st_uid].add((item_size, fe))

    def merge(self, other):
        """Add 'other' into this one (in place); returns self."""
        self.num_files += other.num_files
        self.num_dirs += other.num_dirs
        self.num_items += other.num_items
        self.total_size += other.total_size
        self.num_errors += other.num_errors
        self.num_excluded += other.num_excluded
        self.num_unmatched += other.num_unmatched
        for k, v in other.st_modes.items():
            self.st_modes[k] += v
        _add_into(self.uids, other.uids)
        _add_into(self.gids, other.gids)
        for name, heap in other.heaps.items():
            self.heaps[name].merge(heap)
        for name, per_user in other.user_heaps.items():
            for uid, heap in per_user.items():
                if not heap:
                    continue
                self.user_heaps[name][uid].merge(heap)
        self.ranks += other.ranks
        self.counters.update(other.counters)
        _add_into(self.partial_dirs, other.partial_dirs)
        return self

    def add_partial_dir(self, path, totals):
        """Part of a directory's totals, from one stat batch; listed once all are merged."""
        _add_into(self.partial_dirs, {path: totals})

    def record_directory(self, path, totals, threshold_count, threshold_size):
        """A walked directory's own (non-recursive) totals, into the directory lists."""
        heaps = self.heaps
        user_heaps = self.user_heaps
        nitems, nbytes = totals.nitems, totals.nbytes
        max_atime = totals.max_atime

        # track the size & count of this directory in our top heaps
        de = DirEntry(path, nbytes, nitems, totals.max_mtime, totals.max_ctime, max_atime)
        heaps['nitems_dirs'].add((nitems, de))
        heaps['nbytes_dirs'].add((nbytes, de))

        # are the contents owned by a single user id?  If so, track per-user info as well
        # (None if not: uid 0 is a valid owner, and root runs the scans)
        single_owner = next(iter(totals.uids)) if len(totals.uids) == 1 else None

        # granular directory tracking by timestamp thresholds.
        # check against thresholds to avoid tracking users that only have
        # tiny residual data
        if nitems >= threshold_count or nbytes >= threshold_size:

            if single_owner is not None:
                user_heaps['nitems_dirs'][single_owner].add((nitems, de))
                user_heaps['nbytes_dirs'][single_owner].add((nbytes, de))

            # ignore UNIX epoch (atime ~0 seconds) in timeframe accounting
            if max_atime > 10:
                age = NOW_TIMESTAMP - max_atime
                name = ('nbytes_dirs_7yrs' if SEVEN_YEARS < age else
                        'nbytes_dirs_5yrs' if FIVE_YEARS < age else
                        'nbytes_dirs_3yrs' if THREE_YEARS < age else None)
                if name is not None:
                    heaps[name].add((nbytes, de))
                    if single_owner is not None:
                        user_heaps[name][single_owner].add((nbytes, de))

    def finish_directories(self, threshold_count, threshold_size, filtered):
        """After the reduction: list the directories that were split over stat batches.
        With a selecting filter, one that holds no matching entries goes in no list."""
        for path in sorted(self.partial_dirs):
            totals = self.partial_dirs[path]
            if not filtered or totals.nitems:
                self.record_directory(path, totals, threshold_count, threshold_size)
        self.partial_dirs = {}

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
