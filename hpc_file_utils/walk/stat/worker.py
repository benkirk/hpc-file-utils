"""StatWorker: walkstat's worker, with stat batches (phase 5); the scan every tool's
worker builds on (walktar's TarWorker is a StatWorker whose stat batches fill tar batches).

The scan of a directory reads its names and types from scandir (d_type: no stat on
GPFS or Lustre) and stats only the subdirectories, which it must count and walk. The
other names go into batches of --batch-files, StatBatch(dirname, names, tail), and the
stats happen in process_batch(): on this rank, or on an idle one the Manager's 'share'
hands the batch to (see manager.py). A 1M-entry directory is no longer one rank's hour
of stats while the others wait. process_batch() hands each entry it counts to
process_file(), the hook a tool fills (walktar: into the open FileBatch, sized by the
real st_size).

A directory's own totals (its line in the directory lists) are assembled from its
parts: the scan's subdirectories, carried by the directory's last batch as 'tail', and
each batch's files.
- A directory that fits in one batch: that batch has it all, and whichever rank
  processes it lists the directory (tail is (subdir totals, True)).
- A directory split over several batches: each part goes into WalkStats.partial_dirs,
  the parts add up in the reduction, and rank 0 lists the directory after it
  (WalkStats.finish_directories()). Only big directories go there.
"""

import os
from typing import NamedTuple

from hpc_file_utils.walk.common.index import IndexWriter
from hpc_file_utils.walk.common.stats import DirTotals
from hpc_file_utils.walk.common.worker import BaseWorker

INDEX_FILE = 'index-r{:03d}.tsv'
PREFIXED_INDEX_FILE = 'index-{}-r{:03d}.tsv'


class StatBatch(NamedTuple):
    """One directory's names still to stat."""
    dirname : str
    names   : list
    tail    : object   # None for a part of a split directory; else (subdirs: DirTotals, whole: bool),
                       # the directory's last batch, which lists it (whole) or adds the last part


class StatWorker(BaseWorker):

    index = None   # --index: this rank's IndexWriter (common/index.py); None without

    def __init__(self):
        super().__init__()
        self.index = self.open_index()

    def open_index(self):
        """--index: this rank's index file, index-r{rank}.tsv (index-<prefix>-r{rank}.tsv
        with --summary-prefix) in the working directory, opened at its first record and
        closed by run(); None without. Every entry count() counts goes in it, next to the
        count: the scan's subdirectories here, the stat batches' entries in process_file().
        (walktar's indexes are its tars', tar/worker.py.)"""
        o = self.options
        if not o.index:
            return None
        name = (PREFIXED_INDEX_FILE.format(o.summary_prefix, self.rank) if o.summary_prefix
                else INDEX_FILE.format(self.rank))
        index = IndexWriter(name, 'walkstat', o.dirs)
        self.files.append(index)
        return index

    def process_directory(self, dirname):
        if self.vanished(dirname):
            return

        st = self.stats
        select = self.select
        index = self.index
        nbatch = self.options.batch_files

        st.num_dirs += 1
        # with a selecting filter, directories are counted as entries that match (in count()),
        # as find does; without one, every directory walked, the roots included
        if select is None:
            st.st_modes['dir'] += 1

        subdirs = DirTotals()     # this directory's subdirectories, stat'ed here
        names = []                # its other entries, for the stat batches
        split = False             # has a batch of it closed already?
        try:
            for di in self.entries(dirname):

                if not di.is_dir(follow_symlinks=False):      # d_type: no stat
                    names.append(di.name)
                    if len(names) >= nbatch:
                        self.local_batches.append(StatBatch(dirname, names, None))
                        names, split = [], True
                    continue

                try:
                    statinfo = di.stat(follow_symlinks=False)
                except OSError as error:
                    self.error('stat', error)
                    continue

                # a directory that fails a selecting filter is still walked, for the
                # entries inside that pass
                self.push_dir(di.path)
                if select is not None and not select(statinfo):
                    st.num_unmatched += 1
                    continue
                st.count(di.path, statinfo, subdirs, select is not None)
                if index is not None:
                    index.add(di.path, statinfo)

        except Exception as error:
            self.error('scan', error)

        # the directory's last batch carries its subdirectories' totals; with no names
        # left, they are listed (or kept as a part) here
        if names:
            self.local_batches.append(StatBatch(dirname, names, (subdirs, not split)))
        elif split:
            st.add_partial_dir(dirname, subdirs)
        else:
            self.list_directory(dirname, subdirs)

    def process_batch(self, batch):
        """Stat one StatBatch of a directory's names, ours or shared, count them, and
        hand each one counted to process_file()."""
        dirname, names, tail = batch
        st = self.stats
        select = self.select
        count_dirs = select is not None
        process_file = self.process_file
        totals = DirTotals()
        for path, statinfo in self.lstats(dirname, self.ticked(names)):
            if isinstance(statinfo, OSError):
                self.error('stat', statinfo)
                continue
            if select is not None and not select(statinfo):
                st.num_unmatched += 1
                continue
            st.count(path, statinfo, totals, count_dirs)
            process_file(path, statinfo)

        if tail is None:                        # one part of a split directory
            st.add_partial_dir(dirname, totals)
            return
        subdirs, whole = tail
        totals.add(subdirs)
        if whole:
            self.list_directory(dirname, totals)
        else:                                   # the last part of a split directory
            st.add_partial_dir(dirname, totals)

    def lstats(self, dirname, names):
        """(path, lstat result) for a stat batch's names, in order, an OSError in place of
        the result for a name that could not be stat'ed: one call at a time here (walktar
        overlaps them in its reader threads)."""
        join = os.path.join
        for name in names:
            path = join(dirname, name)
            try:
                yield path, os.lstat(path)
            except OSError as error:
                yield path, error

    def process_file(self, path, statinfo):
        """A stat batch's entry, just stat'ed and counted: the hook a tool fills (walktar
        puts a non-directory in its open batch); walkstat records it in its index, with
        --index. Only the batches' names come here, never the scan's subdirectories."""
        if self.index is not None:
            self.index.add(path, statinfo)
