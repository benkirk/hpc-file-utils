#!/usr/bin/env python3

"""TarWorker: walktar's worker. One tar file per batch of files, plus one per rank for
the directories it walks (phase 5; see manager.py for batches and the protocol).

  output-r{rank}-b{n}.tar   one batch (selected files only), archived by 'rank', its n-th
  output-r{rank}-dirs.tar   every directory 'rank' walked, roots included (mode, owner, times)

Restore: extract the batch tars in any order (or in parallel), then the -dirs tars, which
set the directories' modes and times after their files are in place (a read-only
directory would otherwise refuse files from another tar).

Read-ahead (phase 5.5): a small file costs one storage round trip, ~35 ms on campaign GPFS
and on scratch, whatever its size up to a few MiB; one stream waiting on each in turn made
~26 files/s. --read-ahead reader threads open and read the batch's next files while this
thread writes the current one into the tar, in order. tarfile's own gettarinfo() and
addfile() build and write each member, so the members are exactly tar.add()'s.
"""

import collections, io, os, stat, sys
import tarfile
from concurrent.futures import ThreadPoolExecutor

from hpc_file_utils.walk.common.worker import BaseWorker

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
class TarWorker(BaseWorker):


    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self):
        BaseWorker.__init__(self)

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
            self.stats.num_errors += 1
            print('[{:3d}] Cannot add to tar file: {}'.format(self.rank, error), file=sys.stderr)
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
            self.stats.num_errors += 1
            print('[{:3d}] Cannot add to tar file: {}'.format(self.rank, error), file=sys.stderr)
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
                self.stats.num_errors += 1
                print('[{:3d}] Cannot add to tar file: {}'.format(self.rank, error), file=sys.stderr)
                return False



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def read_ahead(self, paths):
        """Yield (path, Future of open_member(path)) in order, keeping --read-ahead files
        in the reader threads ahead of the one yielded; (path, None) with --read-ahead 0.
        Files opened but never archived (an error broke the loop) are closed."""
        depth = self.options.read_ahead
        if depth == 0:
            for path in paths:
                yield path, None
            return
        if self.readers is None:
            self.readers = ThreadPoolExecutor(max_workers=depth, thread_name_prefix='reader')
        pending, todo = collections.deque(), iter(paths)
        try:
            for path in todo:
                pending.append((path, self.readers.submit(open_member, path)))
                if len(pending) > depth:
                    yield pending.popleft()
            while pending:
                yield pending.popleft()
        finally:
            for _, future in pending:
                if future.cancel() or future.exception() is not None:
                    continue
                member = future.result()
                if member is not None:
                    member[0].close()



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
        """One batch, ours or shared, into its own tar file, its files read ahead by the
        reader threads. Between files we look for a 'share' (an iprobe costs microseconds, a
        file a read) and send our counts when due."""
        paths, nbytes = batch
        name = 'output-r{:03d}-b{:05d}.tar'.format(self.rank, self.ntars)
        self.ntars += 1
        with tarfile.open(name, 'w', format=tarfile.PAX_FORMAT, copybufsize=COPY_BUFSIZE) as tar:
            self.batch_tar = tar
            for path, opened in self.read_ahead(paths):
                if self.add_member(tar, path, opened):
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
        if self.readers is not None:
            self.readers.shutdown()
            self.readers = None
        return
