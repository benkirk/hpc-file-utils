#!/usr/bin/env python3

"""BaseWorker: scans the directories the Manager hands it (see manager.py for the protocol)."""

from hpc_file_utils.walk.common.mpiclass import MPIClass
from hpc_file_utils.walk.common.stats import DirEntry, FileEntry
import os, sys, stat, time

PROGRESS_INCREMENT = 20000   # entries of one directory between progress messages
SHARE_CHECK_INCREMENT = 1000 # entries of one directory between looks for a 'share'
WORKER_UPDATE_SECONDS = 10   # most seconds between a worker's progress messages, whatever --progress
                             # (that is the screen's interval): the Manager's progress line and its
                             # choice of whom to ask for a 'share' use the stack lengths they carry

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

        self.queue = None             # derived tools' work queue (walktar's tar thread)
        self.local_dirs = []          # directories still to walk, a stack (phase 3.5)
        self.max_local_dirs = 0       # its high-water mark
        self.wait_time = 0.0          # waiting for the Manager to answer 'ready' with work
        self.last_progress_time = self.MPI.Wtime()
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_directory(self, dirname):

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
                # and look for a 'share' from the Manager, so a huge directory does not
                # keep the subdirectories we have already queued from the idle ranks
                if 0 == thisdir_nitems%SHARE_CHECK_INCREMENT:
                    self.check_share()

                # decode file type
                fmode = statinfo.st_mode

                if stat.S_ISDIR(fmode):
                    self.local_dirs.append(pathname)
                    if len(self.local_dirs) >= self.options.local_queue_max: self.ship_overflow()
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
            # (None if not: uid 0 is a valid owner, and root runs the scans)
            single_owner = thisdir_uids.pop() if 1 == len(thisdir_uids) else None

            # granular directory tracking by timestamp thresholds.
            # check against thresholds to avoid tracking users that only have
            # tiny residual data
            if thisdir_nitems >= self.options.threshold_count or thisdir_nbytes >= self.options.threshold_size:

                if single_owner is not None:
                    user_heaps['nitems_dirs'][single_owner].add((thisdir_nitems, de))
                    user_heaps['nbytes_dirs'][single_owner].add((thisdir_nbytes, de))

                # ignore UNIX epoch (atime ~0 seconds) in timeframe accounting
                if 10 < thisdir_max_atime:
                    if   SEVEN_YEARS < (NOW_TIMESTAMP - thisdir_max_atime):
                        heaps['nbytes_dirs_7yrs'].add((thisdir_nbytes, de))
                        if single_owner is not None: user_heaps['nbytes_dirs_7yrs'][single_owner].add((thisdir_nbytes, de))

                    elif FIVE_YEARS  < (NOW_TIMESTAMP - thisdir_max_atime):
                        heaps['nbytes_dirs_5yrs'].add((thisdir_nbytes, de))
                        if single_owner is not None: user_heaps['nbytes_dirs_5yrs'][single_owner].add((thisdir_nbytes, de))

                    elif THREE_YEARS < (NOW_TIMESTAMP - thisdir_max_atime):
                        heaps['nbytes_dirs_3yrs'].add((thisdir_nbytes, de))
                        if single_owner is not None: user_heaps['nbytes_dirs_3yrs'][single_owner].add((thisdir_nbytes, de))

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
        """(items, bytes, queued, extra), carried by every message to the Manager (protocol.py)."""
        return (self.stats.num_items, self.stats.total_size, len(self.local_dirs), self.progress_extra())



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def oldest_half(self):
        """Remove and return the oldest half of the local stack.

        The oldest entries are the shallowest, so the ones most likely to hold big
        subtrees: one shipment carries real work, and we keep walking depth-first.
        """
        n = len(self.local_dirs) // 2
        give, self.local_dirs = self.local_dirs[:n], self.local_dirs[n:]
        return give



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def ship_overflow(self):
        """The local stack is full: send its oldest half to the Manager."""
        self.max_local_dirs = max(self.max_local_dirs, len(self.local_dirs))
        give = self.oldest_half()
        self.comm.send((give, self.progress_payload()), dest=0, tag=self.tags['dir_reply'])
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def check_share(self):
        """Answer a 'share' from the Manager, if one is waiting, with the oldest half of
        the local stack (which may be nothing)."""
        if self.comm.iprobe(source=0, tag=self.tags['share']):
            self.comm.recv(source=0, tag=self.tags['share'])
            give = self.oldest_half()
            self.comm.send((give, self.progress_payload()), dest=0, tag=self.tags['share_reply'])
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def report_progress(self, only_if_due=False):
        """Send our counts. With only_if_due, only when --progress seconds (at most
        WORKER_UPDATE_SECONDS) have passed since the last time: a worker walking many small
        directories from its own stack otherwise sends nothing between PROGRESS_INCREMENT
        entries."""
        now = self.MPI.Wtime()
        interval = min(self.options.progress, WORKER_UPDATE_SECONDS)
        if only_if_due and (now - self.last_progress_time) < interval:
            return
        self.last_progress_time = now
        self.comm.send(self.progress_payload(), dest=0, tag=self.tags['progress'])
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def drain_local_dirs(self):
        """Walk the local stack, newest directory first, until it is empty."""
        while self.local_dirs:
            self.check_share()
            self.max_local_dirs = max(self.max_local_dirs, len(self.local_dirs))
            self.process_directory(self.local_dirs.pop())
            self.report_progress(only_if_due=True)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):
        MPI = self.MPI
        self.comm.Barrier()
        status = MPI.Status()
        while True:
            assert not self.local_dirs

            # our stack is empty: ask the Manager for work, and wait for its answer.
            # A 'share' that arrives meanwhile is ignored: we have nothing to give, and
            # the Manager forgets the request when it receives our 'ready'.
            t0 = MPI.Wtime()
            self.comm.send(self.progress_payload(), dest=0, tag=self.tags['ready'])
            while True:
                msg = self.comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
                if status.Get_tag() != self.tags['share']:
                    break

            if status.Get_tag() == self.tags['terminate']:
                assert msg is None
                break

            assert status.Get_tag() == self.tags['execute']
            self.wait_time += MPI.Wtime() - t0   # only waits that got work: dispatch latency
            self.local_dirs.extend(msg)
            self.drain_local_dirs()

        return
