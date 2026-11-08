#!/usr/bin/env python3

"""StatWorker: walkstat's worker, with stat batches (phase 5).

The scan of a directory reads its names and types from scandir (d_type: no stat on
GPFS or Lustre) and stats only the subdirectories, which it must count and walk. The
other names go into batches of --batch-files, (dirname, names, tail), and the stats
happen in process_batch(): on this rank, or on an idle one the Manager's 'share' hands
the batch to (see manager.py). A 1M-entry directory is no longer one rank's hour of
stats while the others wait.

A directory's own totals (its line in the directory lists) are assembled from its
parts: the scan's subdirectories, carried by the directory's last batch as 'tail', and
each batch's files.
- A directory that fits in one batch: that batch has it all, and whichever rank
  processes it lists the directory (tail is (subdir totals, True)).
- A directory split over several batches: each part goes into WalkStats.partial_dirs,
  the parts add up in the reduction, and rank 0 lists the directory after it
  (WalkStats.finish_directories()). Only big directories go there.
"""

import os, stat, sys

from hpc_file_utils.walk.common import worker as base
from hpc_file_utils.walk.common.stats import DirTotals, FileEntry
from hpc_file_utils.walk.common.worker import BaseWorker



################################################################################
class StatWorker(BaseWorker):

    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self):
        BaseWorker.__init__(self)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def count(self, path, statinfo, totals):
        """Count one entry that passed the filters (walkstat's accounting), into the
        walk's totals and its directory's 'totals'."""
        st = self.stats
        fmode = statinfo.st_mode

        # about statinfo.st_size: we can't always trust it. for sparse files, or files
        # migrated to tape via HSM, the size is not what is occupied on disk.
        item_size = min(statinfo.st_size, statinfo.st_blocks * 512)

        st.num_items += 1
        st.total_size += item_size
        totals.nitems += 1
        totals.nbytes += item_size
        totals.uids.add(statinfo.st_uid)

        u = st.uids[statinfo.st_uid]
        u.set_id(statinfo.st_uid)
        u.nitems += 1
        u.nbytes += item_size

        g = st.gids[statinfo.st_gid]
        g.set_id(statinfo.st_gid)
        g.nitems += 1
        g.nbytes += item_size

        if stat.S_ISDIR(fmode):
            if self.select is not None: st.st_modes['dir'] += 1
            return

        st.num_files += 1
        if   stat.S_ISREG(fmode):  st.st_modes['reg']   += 1
        elif stat.S_ISLNK(fmode):  st.st_modes['link']  += 1
        elif stat.S_ISBLK(fmode):  st.st_modes['block'] += 1
        elif stat.S_ISCHR(fmode):  st.st_modes['char']  += 1
        elif stat.S_ISFIFO(fmode): st.st_modes['fifo']  += 1
        elif stat.S_ISSOCK(fmode): st.st_modes['sock']  += 1

        # the *maximum* mtime/ctime/atime of the directory's contents (not the dir itself)
        totals.max_mtime = max(totals.max_mtime, statinfo.st_mtime)
        totals.max_ctime = max(totals.max_ctime, statinfo.st_ctime)
        totals.max_atime = max(totals.max_atime, statinfo.st_atime)

        fe = FileEntry(path, item_size, statinfo.st_mtime, statinfo.st_ctime, statinfo.st_atime)
        st.heaps['nbytes_files'].add((item_size, fe))
        st.user_heaps['nbytes_files'][statinfo.st_uid].add((item_size, fe))
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def list_directory(self, dirname, totals):
        """A directory's complete totals, into the lists (none, with a selecting filter,
        if nothing in it matched)."""
        if self.select is None or totals.nitems:
            self.stats.record_directory(dirname, totals, self.options.threshold_count,
                                        self.options.threshold_size)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def error(self, what, error):
        self.stats.num_errors += 1
        print('[{:3d}] Cannot {}: {}'.format(self.rank, what, error), file=sys.stderr)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_directory(self, dirname):

        # last-minute check for a race condition:
        if not os.path.exists(dirname):
            print('[{:3d}] directory \'{}\' vanished'.format(self.rank, dirname), file=sys.stderr)
            return

        st = self.stats
        exclude = self.exclude
        select = self.select
        nbatch = self.options.batch_files

        st.num_dirs += 1
        # with a selecting filter, directories are counted as entries that match (in count()),
        # as find does; without one, every directory walked, the roots included
        if select is None: st.st_modes['dir'] += 1

        subdirs = DirTotals()     # this directory's subdirectories, stat'ed here
        names = []                # its other entries, for the stat batches
        split = False             # has a batch of it closed already?
        try:
            for nscanned, di in enumerate(os.scandir(dirname), 1):

                # progress, and a 'share' from the Manager, every so many entries scanned
                if 0 == nscanned % base.PROGRESS_INCREMENT:
                    self.report_progress()
                if 0 == nscanned % base.SHARE_CHECK_INCREMENT:
                    self.check_share()

                # --exclude: by name, before any stat; never counted or descended
                if exclude is not None and exclude(di.name):
                    st.num_excluded += 1
                    continue

                if not di.is_dir(follow_symlinks=False):      # d_type: no stat
                    names.append(di.name)
                    if len(names) >= nbatch:
                        self.local_batches.append((dirname, names, None))
                        names, split = [], True
                    continue

                try:
                    statinfo = di.stat(follow_symlinks=False)
                except OSError as error:
                    self.error('stat', error)
                    continue

                # a directory that fails a selecting filter is still walked, for the
                # entries inside that pass
                self.local_dirs.append(di.path)
                if len(self.local_dirs) >= self.options.local_queue_max: self.ship_overflow()
                if select is not None and not select(statinfo):
                    st.num_unmatched += 1
                    continue
                self.count(di.path, statinfo, subdirs)

        except Exception as error:
            self.error('scan', error)

        # the directory's last batch carries its subdirectories' totals; with no names
        # left, they are listed (or kept as a part) here
        if names:
            self.local_batches.append((dirname, names, (subdirs, not split)))
        elif split:
            st.add_partial_dir(dirname, subdirs)
        else:
            self.list_directory(dirname, subdirs)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_batch(self, batch):
        """Stat one batch of a directory's names, ours or shared, and count them."""
        dirname, names, tail = batch
        st = self.stats
        select = self.select
        share_every = base.SHARE_CHECK_INCREMENT
        totals = DirTotals()
        for i, name in enumerate(names, 1):
            if 0 == i % share_every:
                self.check_share()
                self.report_progress(only_if_due=True)
            path = os.path.join(dirname, name)
            try:
                statinfo = os.lstat(path)
            except OSError as error:
                self.error('stat', error)
                continue
            if select is not None and not select(statinfo):
                st.num_unmatched += 1
                continue
            self.count(path, statinfo, totals)

        if tail is None:                        # one part of a split directory
            st.add_partial_dir(dirname, totals)
            return
        subdirs, whole = tail
        totals.add(subdirs)
        if whole:
            self.list_directory(dirname, totals)
        else:                                   # the last part of a split directory
            st.add_partial_dir(dirname, totals)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_file(self, filename, statinfo):
        # not called: StatWorker has its own scan (process_directory above)
        return
