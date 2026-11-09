"""BaseWorker: scans the directories the Manager hands it (see manager.py for the protocol)."""

from hpc_file_utils.walk.common.mpiclass import MPIClass
from hpc_file_utils.walk.common.stats import DirTotals, FileEntry
import os, sys, stat

PROGRESS_INCREMENT = 20000   # entries of one directory between progress messages
SHARE_CHECK_INCREMENT = 1000 # entries of one directory between looks for a 'share'
WORKER_UPDATE_SECONDS = 10   # most seconds between a worker's progress messages, whatever --progress
                             # (that is the screen's interval): the Manager's progress line and its
                             # choice of whom to ask for a 'share' use the stack lengths they carry



################################################################################
class BaseWorker(MPIClass):

    exclude = None   # --exclude: name -> truthy if pruned (filters.py); None without it
    select = None    # selecting filters: statinfo -> bool; None without any

    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self):
        MPIClass.__init__(self)

        self.local_dirs = []          # directories still to walk, a stack (phase 3.5)
        self.local_batches = []       # closed batches still to process, (paths, nbytes) (phase 5)
        self.batch = []               # the open batch: paths of selected files, across directories
        self.batch_bytes = 0          # ... and their st_size total
        self.max_local_dirs = 0       # its high-water mark
        self.wait_time = 0.0          # waiting for the Manager to answer 'ready' with work
        self.last_progress_time = self.MPI.Wtime()

        # the filters rank 0 compiled, as this rank's callables (None: no cost in the loop)
        filters = getattr(self.options, 'filters', None)
        if filters is not None:
            self.exclude = filters.exclude_matcher()
            self.select = filters.predicate()
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
        exclude = self.exclude
        select = self.select

        st.num_dirs += 1
        # with a selecting filter, directories are counted as entries that match (below), as find
        # does; without one, every directory walked, the roots included, as before phase 4
        if select is None: st.st_modes['dir'] += 1

        try:
            thisdir_nitems = 0
            thisdir_nbytes = 0
            thisdir_max_mtime = -1
            thisdir_max_ctime = -1
            thisdir_max_atime = -1

            thisdir_uids = set()

            dirdepth = dirname.count(os.path.sep)

            for nscanned, di in enumerate(os.scandir(dirname), 1):

                # send a progress update periodically
                # (in production we have many directories with 1M+ files, this ensures some progress is
                # detected and reported by Manager even for huge dirs)
                if 0 == nscanned%PROGRESS_INCREMENT:
                    self.report_progress()
                # and look for a 'share' from the Manager, so a huge directory does not
                # keep the subdirectories we have already queued from the idle ranks.
                # Both count entries scanned, not counted: a filter may match few of them.
                if 0 == nscanned%SHARE_CHECK_INCREMENT:
                    self.check_share()

                # --exclude: by name, before the stat; never counted or descended
                if exclude is not None and exclude(di.name):
                    st.num_excluded += 1
                    continue

                pathname = di.path

                statinfo = di.stat(follow_symlinks=False)
                fmode = statinfo.st_mode

                # selecting filters: an entry that fails is not counted (nor archived),
                # but a directory that fails is still walked, for the entries inside that pass
                if select is not None and not select(statinfo):
                    st.num_unmatched += 1
                    if stat.S_ISDIR(fmode):
                        self.local_dirs.append(pathname)
                        if len(self.local_dirs) >= self.options.local_queue_max: self.ship_overflow()
                    continue

                # about statinfo.st_size: we can't always trust it. for sparse files, or files
                # migrated to tape via HSM, the size is not what is occupied on disk.
                item_size = min(statinfo.st_size, statinfo.st_blocks * 512)

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

                # decode file type
                if stat.S_ISDIR(fmode):
                    if select is not None: st.st_modes['dir'] += 1
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


            # with a selecting filter, a directory holding no matching entries goes in no list
            if select is None or thisdir_nitems:
                self.record_directory(dirname, thisdir_nitems, thisdir_nbytes, thisdir_max_mtime,
                                      thisdir_max_ctime, thisdir_max_atime, thisdir_uids)

        except Exception as error:
            st.num_errors += 1
            print('[{:3d}] Cannot scan: {}'.format(self.rank, error), file=sys.stderr)

        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def record_directory(self, dirname, thisdir_nitems, thisdir_nbytes, thisdir_max_mtime,
                         thisdir_max_ctime, thisdir_max_atime, thisdir_uids):
        """A walked directory's own (non-recursive) totals, into the directory lists."""
        totals = DirTotals()
        totals.nitems, totals.nbytes = thisdir_nitems, thisdir_nbytes
        totals.max_mtime, totals.max_ctime, totals.max_atime = thisdir_max_mtime, thisdir_max_ctime, thisdir_max_atime
        totals.uids = thisdir_uids
        self.stats.record_directory(dirname, totals, self.options.threshold_count, self.options.threshold_size)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def add_to_batch(self, path, size):
        """Tools with a process_batch() (walktar, walksync): put a selected file in the open batch.

        Past --batch-files files or --batch-size bytes the batch closes onto our list of
        batches to process, which a 'share' can hand to idle ranks. The open batch carries
        over between directories, so small directories combine. (walkstat makes its
        own batches, of names still to stat: see stat/worker.py.)
        """
        self.batch.append(path)
        self.batch_bytes += size
        if len(self.batch) >= self.options.batch_files or self.batch_bytes >= self.options.batch_size:
            self.close_batch()
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def close_batch(self):
        """Move the open batch, if it holds anything, onto our list of batches."""
        if self.batch:
            self.local_batches.append((self.batch, self.batch_bytes))
            self.batch, self.batch_bytes = [], 0
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_batch(self, batch):
        """Tools that call add_to_batch(): process one (paths, nbytes) batch, ours or shared."""
        raise NotImplementedError



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def progress_extra(self):
        """Derived tools' own progress info for the Manager (e.g. walksync: bytes copied)."""
        return None



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def progress_payload(self):
        """(items, bytes, queued, extra), carried by every message to the Manager (protocol.py)."""
        return (self.stats.num_items, self.stats.total_size, len(self.local_dirs) + len(self.local_batches),
                self.progress_extra())



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
    def oldest_batches(self):
        """Remove and return the oldest half of our closed batches, rounded up: a rank
        busy with one batch gives away the only other it holds."""
        n = len(self.local_batches) - len(self.local_batches) // 2
        give, self.local_batches = self.local_batches[:n], self.local_batches[n:]
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
        the local stack and of our closed batches (either may be nothing). Never the open
        batch: it is still filling."""
        if self.comm.iprobe(source=0, tag=self.tags['share']):
            self.comm.recv(source=0, tag=self.tags['share'])
            dirs = self.oldest_half()
            batches = self.oldest_batches()
            self.comm.send((dirs, batches, self.progress_payload()), dest=0, tag=self.tags['share_reply'])
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
    def drain_local_work(self):
        """Process our batches and walk our stack, batches first, until both are empty;
        then close the open batch (the last, partial one) and process it too.

        The 'share' check comes before each pick, since it may give away what we would
        have picked. On return we hold nothing: no directory, no batch, open or closed.
        """
        while True:
            self.check_share()
            if self.local_batches:
                self.process_batch(self.local_batches.pop())
            elif self.local_dirs:
                self.max_local_dirs = max(self.max_local_dirs, len(self.local_dirs))
                self.process_directory(self.local_dirs.pop())
            else:
                self.close_batch()
                if not self.local_batches:
                    return
                continue
            self.report_progress(only_if_due=True)



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):
        MPI = self.MPI
        self.comm.Barrier()
        status = MPI.Status()
        while True:
            assert not self.local_dirs and not self.local_batches and not self.batch

            # we hold nothing (manager.py, point 1): ask the Manager for work, and wait for its answer.
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
            dirs, batches = msg
            self.local_dirs.extend(dirs)
            self.local_batches.extend(batches)
            self.drain_local_work()

        return
