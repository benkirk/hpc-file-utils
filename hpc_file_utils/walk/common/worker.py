#!/usr/bin/env python3

"""BaseWorker: scans the directories the Manager hands it (see manager.py for the protocol)."""

from hpc_file_utils.walk.common.mpiclass import MPIClass
from hpc_file_utils.walk.common.stats import DirEntry, FileEntry
import os, sys, stat, time

PROGRESS_INCREMENT = 20000

SECONDS_IN_YEAR = 60*60*24*365
THREE_YEARS = 3*SECONDS_IN_YEAR
FIVE_YEARS  = 5*SECONDS_IN_YEAR
SEVEN_YEARS = 7*SECONDS_IN_YEAR

NOW_TIMESTAMP = time.time()



################################################################################
class BaseWorker(MPIClass):

    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self):
        MPIClass.__init__(self)

        self.queue = None
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_directory(self, dirname):

        self.dirs = []

        # last-minute check for a race condition:
        if not os.path.exists(dirname):
            print('[{:3d}] directory \'{}\' vanished'.format(self.rank, dirname), file=sys.stderr)
            return

        st = self.stats
        heaps = st.heaps
        user_heaps = st.user_heaps

        st.num_dirs += 1
        st.st_modes['dir'] += 1

        #print('[{:3d}](d) {}'.format(self.rank, dirname))

        try:
            thisdir_nitems = 0
            thisdir_nbytes = 0
            thisdir_max_mtime = -1
            thisdir_max_ctime = -1
            thisdir_max_atime = -1

            thisdir_uids = set()

            dirdepth = dirname.count(os.path.sep)

            for di in os.scandir(dirname):

                pathname = di.path

                statinfo = di.stat(follow_symlinks=False)

                # about statinfo.st_size: we can't always trust it.
                # for sparse files, or files migrated to tape via HSM,
                # the size is not what is occupied on disk.
                # a better metric is (number of blocks * fs_block_size);
                # however the block size is variable across filesystems / GPFS file sets
                # for regular, uncompressed, unmigrated files; size <= (n_blocks*fs_block_size)
                item_size = statinfo.st_size
                item_nblocks = statinfo.st_blocks
                for bs in [ 512 ]: item_size = min(statinfo.st_size, statinfo.st_blocks*bs)

                thisdir_nitems += 1
                thisdir_nbytes += item_size
                st.num_items += 1
                st.total_size += item_size

                thisdir_uids.add(statinfo.st_uid)

                u = st.uids[statinfo.st_uid]
                u.set_id(statinfo.st_uid)
                u.nitems += 1
                u.nbytes += item_size

                g = st.gids[statinfo.st_gid]
                g.set_id(statinfo.st_gid)
                g.nitems += 1
                g.nbytes += item_size

                # send a progress update periodically
                # (in production we have many directories with 1M+ files, this ensures some progress is
                # detected and reported by Manager even for huge dirs)
                if 0 == thisdir_nitems%PROGRESS_INCREMENT:
                    self.report_progress()

                # decode file type
                fmode = statinfo.st_mode

                if stat.S_ISDIR(fmode):
                    self.dirs.append(pathname)
                    if len(self.dirs) >= self.options.max_dirs_per_send: self.send_my_dirlist()
                else:
                    st.num_files += 1

                    if   stat.S_ISREG(fmode):  st.st_modes['reg']   += 1
                    elif stat.S_ISLNK(fmode):  st.st_modes['link']  += 1
                    elif stat.S_ISBLK(fmode):  st.st_modes['block'] += 1
                    elif stat.S_ISCHR(fmode):  st.st_modes['char']  += 1
                    elif stat.S_ISFIFO(fmode): st.st_modes['fifo']  += 1
                    elif stat.S_ISSOCK(fmode): st.st_modes['sock']  += 1

                    # track the *maximum mtime/ctime/atime for this directories contents (not the dir itself though)
                    thisdir_max_mtime = max(thisdir_max_mtime, statinfo.st_mtime)
                    thisdir_max_ctime = max(thisdir_max_ctime, statinfo.st_ctime)
                    thisdir_max_atime = max(thisdir_max_atime, statinfo.st_atime)

                    fe = FileEntry(pathname, item_size, statinfo.st_mtime, statinfo.st_ctime, statinfo.st_atime)

                    # track the size & count of this file in our top heap
                    heaps['nbytes_files'].add((item_size, fe))
                    user_heaps['nbytes_files'][statinfo.st_uid].add((item_size, fe))

                    # --------------------------------------------------------------------------
                    # additional file processing - simply a placeholder stub for derived classes
                    # to do additional work on this file.
                    self.process_file(pathname, statinfo)
                    #------------------------------------


            # track the size & count of this directory in our top heaps
            de = DirEntry(dirname, thisdir_nbytes, thisdir_nitems, thisdir_max_mtime, thisdir_max_ctime, thisdir_max_atime)

            heaps['nitems_dirs'].add((thisdir_nitems, de))
            heaps['nbytes_dirs'].add((thisdir_nbytes, de))

            # are the contents owned by a single user id?  If so, track per-user info as well
            single_owner = thisdir_uids.pop() if 1 == len(thisdir_uids) else False

            # granular directory tracking by timestamp thresholds.
            # check against thresholds to avoid tracking users that only have
            # tiny residual data
            if thisdir_nitems >= self.options.threshold_count or thisdir_nbytes >= self.options.threshold_size:

                if single_owner:
                    user_heaps['nitems_dirs'][single_owner].add((thisdir_nitems, de))
                    user_heaps['nbytes_dirs'][single_owner].add((thisdir_nbytes, de))

                # ignore UNIX epoch (atime ~0 seconds) in timeframe accounting
                if 10 < thisdir_max_atime:
                    if   SEVEN_YEARS < (NOW_TIMESTAMP - thisdir_max_atime):
                        heaps['nbytes_dirs_7yrs'].add((thisdir_nbytes, de))
                        if single_owner: user_heaps['nbytes_dirs_7yrs'][single_owner].add((thisdir_nbytes, de))

                    elif FIVE_YEARS  < (NOW_TIMESTAMP - thisdir_max_atime):
                        heaps['nbytes_dirs_5yrs'].add((thisdir_nbytes, de))
                        if single_owner: user_heaps['nbytes_dirs_5yrs'][single_owner].add((thisdir_nbytes, de))

                    elif THREE_YEARS < (NOW_TIMESTAMP - thisdir_max_atime):
                        heaps['nbytes_dirs_3yrs'].add((thisdir_nbytes, de))
                        if single_owner: user_heaps['nbytes_dirs_3yrs'][single_owner].add((thisdir_nbytes, de))

        except Exception as error:
            st.num_errors += 1
            print('[{:3d}] Cannot scan: {}'.format(self.rank, error), file=sys.stderr)

        # if present, wait for our work queue to drain.
        # note that since each rank is wholly responsible for a given directory,
        # and our queue is populated by item count, we have not accounted yet for
        # different item sizes and their impact on queue processing time.  By
        # waiting here for our queue to drain we can more effectively load balance,
        # rather than filling up the queue with items that may take a long time to
        # process later.
        if self.queue: self.queue.join()

        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_file(self, filename, statinfo):
        print('Not implemented!')
        raise NotImplementedError
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def progress_extra(self):
        """Derived tools' own progress info for the Manager (e.g. walksync: bytes copied)."""
        return None



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def progress_payload(self):
        """(items, bytes, extra), carried by every message to the Manager (protocol.py)."""
        return (self.stats.num_items, self.stats.total_size, self.progress_extra())



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def send_my_dirlist(self):
        # the subdirectories found so far, with our progress, in a single message
        if self.dirs:
            self.maxnumdirs = max(self.maxnumdirs, len(self.dirs))
            self.comm.send((self.dirs, self.progress_payload()), dest=0, tag=self.tags['dir_reply'])
            self.dirs = []
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def report_progress(self):
        self.comm.send(self.progress_payload(), dest=0, tag=self.tags['progress'])
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):
        from mpi4py import MPI
        self.comm.Barrier()
        status = MPI.Status()
        while True:

            # send our dir list to manager (if any)
            self.send_my_dirlist()

            # # receive instructions from Manager
            next_dir = self.comm.sendrecv(self.progress_payload(),
                                          dest=0,  sendtag=self.tags['ready'],
                                          source=0, recvtag=MPI.ANY_TAG,
                                          status=status)

            if status.Get_tag() == self.tags['terminate']:
                assert (next_dir == None)
                break

            if next_dir:
                assert (status.Get_tag() == self.tags['execute'])
                self.process_directory(next_dir)

        #print('[{:3d}] *** Finished, maximum # of dirs at once: {}'.format(self.rank,
        #                                                                   format_number(self.maxnumdirs)))
        return
