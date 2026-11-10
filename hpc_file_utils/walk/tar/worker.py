"""TarWorker: walktar's worker. One tar file per batch of files, plus one per rank for
the directories it walks (phase 5; see manager.py for batches and the protocol).

  output-r{rank}-b{n}.tar   one batch (selected files only), archived by 'rank', its n-th
  output-r{rank}-dirs.tar   every directory 'rank' walked, roots included (mode, owner, times)

Restore: extract the batch tars in any order (or in parallel), then the -dirs tars, which
set the directories' modes and times after their files are in place (a read-only
directory would otherwise refuse files from another tar).

Two levels of batches (the sweep's Part B): the scan is walkstat's (stat/worker.py), so
the scanning rank stats only the subdirectories and cuts the other names into
StatBatches by count. The rank that processes a StatBatch stats, filters and counts
each name, and puts the selected files in its open FileBatch, which closes at
--batch-files files or --batch-size bytes of real st_size and is written as one tar,
here or on an idle rank. Before this, the scanning rank stat'ed every file itself: on
one huge campaign GPFS directory it made ~1,000 entries/s while the other ranks waited
(phase 5.5). The StatBatch's FileBatches are created only on the rank processing it
and processed before its next 'ready', so the Manager's termination argument holds.

Read-ahead (phase 5.5): a small file costs one storage round trip, ~35 ms on campaign GPFS
and on scratch, whatever its size up to a few MiB; one stream waiting on each in turn made
~26 files/s. --read-ahead reader threads open and read the batch's next files while this
thread writes the current one into the tar, in order. tarfile's own gettarinfo() and
addfile() build and write each member, so the members are exactly tar.add()'s.
"""

import io, os, stat
import tarfile
from concurrent.futures import ThreadPoolExecutor

from hpc_file_utils.walk.stat.worker import StatBatch, StatWorker

# tarfile copies a member's data in chunks of copybufsize bytes, 16 KiB by default. Bigger
# reads and writes cost fewer calls; 16 MiB was 2-5% faster on derecho scratch (phase 5).
COPY_BUFSIZE = 16 * 1024 * 1024

# A reader thread reads a regular file whole when it is at most this big; a bigger one it
# only opens, and the tar copies it in COPY_BUFSIZE chunks. Bounds the bytes held per rank
# at --read-ahead x READ_WHOLE_MAX (16 x 16 MiB).
READ_WHOLE_MAX = COPY_BUFSIZE



def _open_nofollow(path, flags):
    """open()'s opener: never follow a symlink (the entry was a regular file at its lstat)."""
    return os.open(path, flags | os.O_NOFOLLOW)



def open_member(path):
    """Run by a reader thread: (file, data) for a regular file, data its whole contents
    (READ_WHOLE_MAX at most, else None and the file is left at its start), or None for
    any other entry, which tar.add() archives as before (a fifo is never opened)."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        return None
    f = open(path, 'rb', opener=_open_nofollow)
    try:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            f.close()
            return None
        data = f.read(st.st_size) if st.st_size <= READ_WHOLE_MAX else None
        return f, data
    except BaseException:
        f.close()
        raise


################################################################################
class TarWorker(StatWorker):


    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self):
        StatWorker.__init__(self)

        self.dirs_tar = None          # output-r{rank}-dirs.tar, opened at our first directory
        self.ntars = 0                # batch tars written
        self.nwritten_files = 0       # files archived into them
        self.nwritten_closed = 0      # bytes in the batch tars closed so far
        self.batch_tar = None         # the batch tar being written, for progress_extra()
        self.readers = None           # --read-ahead reader threads, started at our first batch
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def add(self, tar, path):
        """Add one entry (never its contents, for a directory); count a failure."""
        try:
            tar.add(path, recursive=False)
            return True
        except Exception as error:
            self.error('add to tar file', error)
            return False



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def add_member(self, tar, path, opened):
        """Add one batch entry; opened is open_member(path)'s Future, or None (--read-ahead
        0) for plain tar.add(), phase 5's path. Counts a failure, as add() does."""
        if opened is None:
            return self.add(tar, path)
        try:
            member = opened.result()
        except Exception as error:
            self.error('add to tar file', error)
            return False
        if member is None:                      # not a regular file
            return self.add(tar, path)
        f, data = member
        with f:
            try:
                # the members are tar.add()'s: same gettarinfo (from an fstat, here), and
                # tarfile keeps the hard links in order
                tarinfo = tar.gettarinfo(arcname=path, fileobj=f)
                if data is not None:
                    # the file changed size since the reader's fstat: copy what tar.add()
                    # would (tarinfo.size bytes), but fail before writing a short member
                    if len(data) < tarinfo.size:
                        data += f.read(tarinfo.size - len(data))
                    if len(data) < tarinfo.size:
                        raise tarfile.ReadError('unexpected end of data: {}'.format(path))
                    tar.addfile(tarinfo, io.BytesIO(memoryview(data)[:tarinfo.size]))
                else:
                    tar.addfile(tarinfo, f)
                return True
            except Exception as error:
                self.error('add to tar file', error)
                return False



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def read_ahead(self, paths):
        """(path, Future of open_member(path)) in order, keeping --read-ahead files in the
        reader threads ahead of the one yielded (in_flight()); (path, None) with
        --read-ahead 0. Files opened but never archived (an error broke the loop) are closed."""
        depth = self.options.read_ahead
        if depth and self.readers is None:
            self.readers = ThreadPoolExecutor(max_workers=depth, thread_name_prefix='reader')
        return self.in_flight(paths, self.open_ahead, depth, self.discard_member)



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def open_ahead(self, path):
        """Start a reader on a file: its open_member() Future."""
        return self.readers.submit(open_member, path)



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    @staticmethod
    def discard_member(future):
        """A reader's Future whose file will never be archived: close the file."""
        if future.cancel() or future.exception() is not None:
            return
        member = future.result()
        if member is not None:
            member[0].close()



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_directory(self, dirname):
        """walkstat's scan (the subdirectories stat'ed here, the other names into
        StatBatches), after the directory itself goes into our dirs tar."""

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
    def process_file(self, path, statinfo):
        """A stat batch's entry, selected and counted: into the open batch (closed past
        --batch-files files or --batch-size bytes of st_size). Not a directory: one that
        became a directory since the scan is counted, as walkstat counts it, but belongs
        in no tar (it was never walked)."""
        if not stat.S_ISDIR(statinfo.st_mode):
            self.add_to_batch(path, statinfo.st_size)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_batch(self, batch):
        """A StatBatch (the stats, which fill our open FileBatch) or a FileBatch (one tar),
        ours or shared."""
        if isinstance(batch, StatBatch):
            super().process_batch(batch)
        else:
            self.tar_batch(batch.paths)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def tar_batch(self, paths):
        """One FileBatch's paths into their own tar file, read ahead by the reader threads.
        Between files we look for a 'share' (an iprobe costs microseconds, a file a read)
        and send our counts when due."""
        name = 'output-r{:03d}-b{:05d}.tar'.format(self.rank, self.ntars)
        self.ntars += 1
        with tarfile.open(name, 'w', format=tarfile.PAX_FORMAT, copybufsize=COPY_BUFSIZE) as tar:
            self.batch_tar = tar
            for path, opened in self.read_ahead(paths):
                if self.add_member(tar, path, opened):
                    self.nwritten_files += 1
                self.tick()
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
        if self.readers is not None:
            self.readers.shutdown()
            self.readers = None
        return
