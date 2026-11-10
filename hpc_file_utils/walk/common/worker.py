"""BaseWorker: scans the directories the Manager hands it (see manager.py for the protocol).

The scaffolding every tool's worker shares: entries() is the scan of one directory with
the progress and 'share' checks and --exclude; ticked() and tick() are the same checks
inside a batch; in_flight() keeps I/O started ahead of its use (walktar's reader threads,
walksync's rsyncs); error() and vanished() count and print; push_dir() feeds the local
stack; a RankFile is a per-rank output opened at its first write and closed by run().
"""

import collections
import logging
import os
from typing import NamedTuple

from hpc_file_utils.walk.common.mpiclass import MPIClass
from hpc_file_utils.walk.common.protocol import Tag

log = logging.getLogger(__name__)

PROGRESS_INCREMENT = 20000   # entries of one directory between progress messages
SHARE_CHECK_INCREMENT = 1000 # entries of one directory (or items of one batch) between looks for a 'share'
WORKER_UPDATE_SECONDS = 10   # most seconds between a worker's progress messages, whatever --progress
                             # (that is the screen's interval): the Manager's progress line and its
                             # choice of whom to ask for a 'share' use the stack lengths they carry


class FileBatch(NamedTuple):
    """A closed batch of selected files (close_batch()): walktar writes it as one tar,
    walksync copies it with one rsync. Batches are module-level NamedTuples so they
    pickle in a share_reply; the Manager treats every kind as one opaque unit of work."""
    paths  : list   # the files' paths (walktar) or paths relative to the source root (walksync)
    nbytes : int    # their st_size total


class RankFile:
    """A text file one rank writes (walksync's dry-run list): opened at its first write, so
    a rank that writes nothing leaves no file; closed by the worker's run(), which knows
    the ones made with BaseWorker.rank_file()."""

    def __init__(self, name):
        self.name = name
        self.file = None

    def write(self, text):
        if self.file is None:
            self.file = open(self.name, 'w', encoding='utf-8')
        self.file.write(text)

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


class BaseWorker(MPIClass):

    exclude = None   # --exclude: name -> truthy if pruned (filters.py); None without it
    select = None    # selecting filters: statinfo -> bool; None without any

    def __init__(self):
        super().__init__()

        self.local_dirs = []          # directories still to walk, a stack (phase 3.5)
        self.local_batches = []       # closed batches still to process (phase 5): FileBatch, or the tool's own kind
        self.batch = []               # the open batch: paths of selected files, across directories
        self.batch_bytes = 0          # ... and their st_size total
        self.max_local_dirs = 0       # its high-water mark
        self.wait_time = 0.0          # waiting for the Manager to answer 'ready' with work
        self.last_progress_time = self.MPI.Wtime()
        self.files = []               # RankFiles (rank_file()), closed by run()

        # the filters rank 0 compiled, as this rank's callables (None: no cost in the loop)
        filters = self.options.filters
        if filters is not None:
            self.exclude = filters.exclude_matcher()
            self.select = filters.predicate()

    def rank_info(self):
        return MPIClass.rank_info(self)._replace(wait_time=self.wait_time, max_local_dirs=self.max_local_dirs)

    def rank_file(self, pattern):
        """A RankFile named pattern.format(rank) (e.g. 'walksync-dryrun-r{:03d}.txt'), in the
        working directory, closed by run()."""
        f = RankFile(pattern.format(self.rank))
        self.files.append(f)
        return f

    def error(self, what, error):
        """Count what could not be done (an entry scanned, stat'ed, archived or copied) and
        say so on stderr: '[rank] ERROR Cannot <what>: <error>' (common/log.py)."""
        self.stats.num_errors += 1
        log.error('Cannot %s: %s', what, error)

    def vanished(self, dirname):
        """True, after saying so, if a directory we were handed is gone: a last-minute
        check for a race with whoever is changing the tree."""
        if os.path.exists(dirname):
            return False
        log.warning("directory '%s' vanished", dirname)
        return True

    def push_dir(self, path):
        """A subdirectory onto the local stack; past --local-queue-max, the oldest half
        goes to the Manager."""
        self.local_dirs.append(path)
        if len(self.local_dirs) >= self.options.local_queue_max:
            self.ship_overflow()

    def entries(self, dirname):
        """One scandir of a directory: its entries, less those --exclude prunes by name
        (counted; never stat'ed, counted or descended). Progress goes out every
        PROGRESS_INCREMENT entries scanned and a 'share' is looked for every
        SHARE_CHECK_INCREMENT: a huge directory must still report, and must not keep the
        subdirectories already queued here from the idle ranks. Both count entries scanned,
        not kept: a filter may match few of them. The caller catches the errors
        (error('scan', ...)): the directory's partial totals are still listed."""
        st = self.stats
        exclude = self.exclude
        with os.scandir(dirname) as it:
            for nscanned, di in enumerate(it, 1):
                if nscanned % PROGRESS_INCREMENT == 0:
                    self.report_progress()
                if nscanned % SHARE_CHECK_INCREMENT == 0:
                    self.check_share()
                if exclude is not None and exclude(di.name):
                    st.num_excluded += 1
                    continue
                yield di

    def tick(self):
        """Between units of work that may take a while (a batch's files, a poll of a
        running rsync): answer a 'share' if one is waiting, and send our counts when due."""
        self.check_share()
        self.report_progress(only_if_due=True)

    def ticked(self, items):
        """The items of a batch, with tick() before every SHARE_CHECK_INCREMENT-th one: a stat
        or a compare costs a system call, so every item would be too often (walktar ticks
        after every file: a file costs a read)."""
        for i, item in enumerate(items, 1):
            if i % SHARE_CHECK_INCREMENT == 0:
                self.tick()
            yield item

    def in_flight(self, items, start, depth, discard=None):
        """Yield (item, handle) in order, keeping up to 'depth' items started ahead of the
        one yielded: handle = start(item) begins the item's I/O (a reader thread's Future,
        an rsync process) and the caller waits on each handle in turn, ticking between
        them. With depth 0 nothing is started: (item, None). Handles started but never
        yielded (an error ended the caller's loop) go to discard(handle)."""
        if depth == 0:
            for item in items:
                yield item, None
            return
        pending = collections.deque()
        try:
            for item in items:
                pending.append((item, start(item)))
                if len(pending) > depth:
                    yield pending.popleft()
            while pending:
                yield pending.popleft()
        finally:
            if discard is not None:
                for _, handle in pending:
                    discard(handle)

    def process_directory(self, dirname):
        """Scan one directory, ours or the Manager's: count it, push its subdirectories
        (push_dir()) and queue its other names as the tool's batches. Every tool's scan
        is walkstat's (stat/worker.py) or built on it; walksync's is its own."""
        raise NotImplementedError

    def list_directory(self, dirname, totals):
        """A walked directory's complete totals, into the directory lists (none, with a
        selecting filter, if nothing in it matched)."""
        if self.select is None or totals.nitems:
            self.stats.record_directory(dirname, totals, self.options.threshold_count,
                                        self.options.threshold_size)

    def add_to_batch(self, path, size):
        """Tools that write (walktar, walksync): put a selected file in the open batch.

        Past --batch-files files or --batch-size bytes the batch closes onto our list of
        batches to process, which a 'share' can hand to idle ranks. The open batch carries
        over between directories and between the stat or compare batches that fill it,
        so small directories combine. (The scan's own batches, of names still to stat or
        compare, are the tool's kind: see stat/worker.py and sync/worker.py.)
        """
        self.batch.append(path)
        self.batch_bytes += size
        if len(self.batch) >= self.options.batch_files or self.batch_bytes >= self.options.batch_size:
            self.close_batch()

    def close_batch(self):
        """Move the open batch, if it holds anything, onto our list of batches as a FileBatch."""
        if self.batch:
            self.local_batches.append(FileBatch(self.batch, self.batch_bytes))
            self.batch, self.batch_bytes = [], 0

    def process_batch(self, batch):
        """Process one batch, ours or shared: a FileBatch from add_to_batch(), or the tool's own kind."""
        raise NotImplementedError

    def progress_extra(self):
        """Derived tools' own progress info for the Manager (e.g. walksync: bytes copied)."""
        return None

    def progress_payload(self):
        """(items, bytes, queued, extra), carried by every message to the Manager (protocol.py)."""
        return (self.stats.num_items, self.stats.total_size, len(self.local_dirs) + len(self.local_batches),
                self.progress_extra())

    def oldest_half(self):
        """Remove and return the oldest half of the local stack.

        The oldest entries are the shallowest, so the ones most likely to hold big
        subtrees: one shipment carries real work, and we keep walking depth-first.
        """
        n = len(self.local_dirs) // 2
        give, self.local_dirs = self.local_dirs[:n], self.local_dirs[n:]
        return give

    def oldest_batches(self):
        """Remove and return the oldest half of our closed batches, rounded up: a rank
        busy with one batch gives away the only other it holds."""
        n = len(self.local_batches) - len(self.local_batches) // 2
        give, self.local_batches = self.local_batches[:n], self.local_batches[n:]
        return give

    def ship_overflow(self):
        """The local stack is full: send its oldest half to the Manager."""
        self.max_local_dirs = max(self.max_local_dirs, len(self.local_dirs))
        give = self.oldest_half()
        self.comm.send((give, self.progress_payload()), dest=0, tag=Tag.DIR_REPLY)

    def check_share(self):
        """Answer a 'share' from the Manager, if one is waiting, with the oldest half of
        the local stack and of our closed batches (either may be nothing). Never the open
        batch: it is still filling."""
        if self.comm.iprobe(source=0, tag=Tag.SHARE):
            self.comm.recv(source=0, tag=Tag.SHARE)
            dirs = self.oldest_half()
            batches = self.oldest_batches()
            self.comm.send((dirs, batches, self.progress_payload()), dest=0, tag=Tag.SHARE_REPLY)

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
        self.comm.send(self.progress_payload(), dest=0, tag=Tag.PROGRESS)

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
            self.comm.send(self.progress_payload(), dest=0, tag=Tag.READY)
            while True:
                msg = self.comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
                if status.Get_tag() != Tag.SHARE:
                    break

            if status.Get_tag() == Tag.TERMINATE:
                assert msg is None
                break

            assert status.Get_tag() == Tag.EXECUTE
            self.wait_time += MPI.Wtime() - t0   # only waits that got work: dispatch latency
            dirs, batches = msg
            self.local_dirs.extend(dirs)
            self.local_batches.extend(batches)
            self.drain_local_work()

        for f in self.files:
            f.close()
