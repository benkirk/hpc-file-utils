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

import os, sys

from hpc_file_utils.walk.common import worker as base
from hpc_file_utils.walk.common.stats import DirTotals
from hpc_file_utils.walk.common.worker import BaseWorker



################################################################################
class StatWorker(BaseWorker):

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
                st.count(di.path, statinfo, subdirs, select is not None)

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
            st.count(path, statinfo, totals, select is not None)

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
