#!/usr/bin/env python3

"""TarWorker: walktar's worker. One tar file per batch of files, plus one per rank for
the directories it walks (phase 5; see manager.py for batches and the protocol).

  output-r{rank}-b{n}.tar   one batch (selected files only), archived by 'rank', its n-th
  output-r{rank}-dirs.tar   every directory 'rank' walked, roots included (mode, owner, times)

Restore: extract the batch tars in any order (or in parallel), then the -dirs tars, which
set the directories' modes and times after their files are in place (a read-only
directory would otherwise refuse files from another tar).
"""

import os, sys
import tarfile

from hpc_file_utils.walk.common.worker import BaseWorker

# tarfile copies a member's data in chunks of copybufsize bytes, 16 KiB by default. Bigger
# reads and writes cost fewer calls; 16 MiB was 2-5% faster on derecho scratch (phase 5).
COPY_BUFSIZE = 16 * 1024 * 1024


################################################################################
class TarWorker(BaseWorker):


    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self):
        BaseWorker.__init__(self)

        self.dirs_tar = None          # output-r{rank}-dirs.tar, opened at our first directory
        self.ntars = 0                # batch tars written
        self.nwritten_files = 0       # files archived into them
        self.nwritten_closed = 0      # bytes in the batch tars closed so far
        self.batch_tar = None         # the batch tar being written, for progress_extra()
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def add(self, tar, path):
        """Add one entry (never its contents, for a directory); count a failure."""
        try:
            tar.add(path, recursive=False)
            return True
        except Exception as error:
            self.stats.num_errors += 1
            print('[{:3d}] Cannot add to tar file: {}'.format(self.rank, error), file=sys.stderr)
            return False



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_directory(self, dirname):

        # last-minute check for a race condition:
        if os.path.exists(dirname):
            # the directory object itself, for its mode, owner and times, in our own dirs tar
            if self.dirs_tar is None:
                self.dirs_tar = tarfile.open('output-r{:03d}-dirs.tar'.format(self.rank), 'w',
                                             format=tarfile.PAX_FORMAT)
            self.add(self.dirs_tar, dirname)

        super().process_directory(dirname)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_file(self, filename, statinfo):
        # a selected file: into the open batch (closed past --batch-files / --batch-size)
        self.add_to_batch(filename, statinfo.st_size)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_batch(self, batch):
        """One batch, ours or shared, into its own tar file. Between files we look for a
        'share' (an iprobe costs microseconds, a file a read) and send our counts when due."""
        paths, nbytes = batch
        name = 'output-r{:03d}-b{:05d}.tar'.format(self.rank, self.ntars)
        self.ntars += 1
        with tarfile.open(name, 'w', format=tarfile.PAX_FORMAT, copybufsize=COPY_BUFSIZE) as tar:
            self.batch_tar = tar
            for path in paths:
                if self.add(tar, path):
                    self.nwritten_files += 1
                self.check_share()
                self.report_progress(only_if_due=True)
            self.batch_tar = None
        self.nwritten_closed += os.path.getsize(name)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def progress_extra(self):
        """(bytes, files, archives) written into batch tars so far: the progress line's
        write rate. The dirs tar (headers only) is left out, so at the end these are
        exactly the batch tars' sizes."""
        nbytes = self.nwritten_closed
        if self.batch_tar is not None:
            nbytes += self.batch_tar.offset
        return (nbytes, self.nwritten_files, self.ntars)



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):

        super().run()

        # finish the dirs tar (its end-of-archive blocks); interpreter shutdown does not
        # reliably flush it
        if self.dirs_tar is not None:
            self.dirs_tar.close()
            self.dirs_tar = None
        return
