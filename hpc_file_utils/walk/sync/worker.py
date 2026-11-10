"""SyncWorker: walksync's worker (phase 6). Copies what is new or changed from the source
(--dirs SRC) to --dest DEST with batched rsync --files-from, never deleting anything.

Three kinds of work, the first the Manager's directories, the others batches (phase 5),
which a 'share' can hand to idle ranks (see manager.py):

  scan     a source directory: one scandir of it (names and d_type: no stat for files,
           as walkstat's scan) and one scandir of the matching destination directory,
           the lookup for the whole directory. Subdirectories are walked; one the
           destination lacks (or has as a non-directory) goes on the copy list itself, so
           empty directories are created. The other names go into compare batches of
           --batch-files, CompareBatch(reldir, [(name, dest kind)]), and destination names
           the source lacks are counted as extraneous (listed with --dry-run, never deleted).
           --dry-run lists to walksync-dryrun-r{rank}.txt in the working directory, one
           file per rank: lines from many ranks on one stdout get spliced together.
  compare  a CompareBatch: lstat each source name (and the destination's, if it has
           one), and needs_copy() (compare.py). What must be copied goes into the
           open copy batch, which carries over between directories, so changed files
           from many small directories make one rsync; it closes at --batch-files names
           or --batch-size bytes.
  copy     a FileBatch(relpaths, nbytes): --rsyncs K of
           rsync -aAX --numeric-ids --chmod=Du+w --files-from=- --from0 [--rsync-opts] SRC/ DEST/
           side by side, each over a K-th of the batch (rsync.py; -A / -X dropped, with a
           warning, if this rsync was built without them). With --files-from rsync does
           not recurse, so a directory listed is created (with u+w: other ranks' batches
           write into it) but not filled from here.

A compare batch creates copy batches; they stay on this rank's list and are processed
before its next 'ready' (manager.py, point 1), so the termination argument holds.

What is left to a final serial rsync -aHAX --delete SRC/ DEST/: deletions, hard links
(copied here as separate files) and the directories' real modes and times.
"""

import os
import stat
import sys
import time
from typing import NamedTuple

from hpc_file_utils.walk.common.formatting import printable
from hpc_file_utils.walk.common.worker import BaseWorker
from hpc_file_utils.walk.sync.compare import NEW, TYPE, extraneous, needs_copy
from hpc_file_utils.walk.sync.rsync import RSYNC_VANISHED, Rsync, rsync_command, split

DRYRUN_FILE = 'walksync-dryrun-r{:03d}.txt'


class CompareBatch(NamedTuple):
    """One source directory's non-directory names still to compare with the destination's."""
    reldir : str    # the directory, relative to the source root ('' for the root)
    names  : list   # (name, kind): kind is None (the destination lacks it), True (a directory there) or False


class SyncWorker(BaseWorker):

    def __init__(self):
        super().__init__()
        o = self.options
        self.src = o.dirs[0]                  # normalized by options.py: no trailing '/'
        self.dest = o.dest
        self.src_prefix = len(self.src.rstrip('/')) + 1
        self.dry_run = o.dry_run
        self.cmd = rsync_command(o)
        self.rsyncs = o.rsyncs                # rsyncs side by side over one copy batch
        self.copied_bytes = 0                 # in rsync batches that finished (progress_extra)
        self.copied_files = 0
        self.nrsync = 0
        self.dryrun = self.rank_file(DRYRUN_FILE)   # --dry-run's list, a file at its first line

    def rel(self, path):
        """A source path relative to the source root ('' for the root)."""
        return '' if path == self.src else path[self.src_prefix:]

    def dest_kinds(self, destdir):
        """One scandir of a destination directory: name -> True for a directory, False
        for anything else. Empty if the destination has no such directory yet."""
        kinds = {}
        try:
            with os.scandir(destdir) as it:
                for de in it:
                    try:
                        kinds[de.name] = de.is_dir(follow_symlinks=False)
                    except OSError:
                        kinds[de.name] = False
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError as error:
            self.error('scan destination', error)
        return kinds

    def want_copy(self, rel, size, reason, lines):
        """An entry that must be copied: count it, then list it (--dry-run, into 'lines')
        or put it in the open copy batch."""
        c = self.stats.counters
        c['new' if reason == NEW else 'changed'] += 1
        c['bytes_to_copy'] += size
        if self.dry_run:
            lines.append(f"{'N' if reason == NEW else 'C'} {printable(rel)}\n")
        else:
            self.add_to_batch(rel, size)

    def emit(self, lines):
        """--dry-run's lines, into this rank's own list."""
        if lines:
            self.dryrun.write(''.join(lines))

    def process_directory(self, dirname):
        if self.vanished(dirname):
            return

        st = self.stats
        nbatch = self.options.batch_files
        join = os.path.join

        st.num_dirs += 1
        reldir = self.rel(dirname)
        dest = self.dest_kinds(join(self.dest, reldir) if reldir else self.dest)

        src_names = []            # every name not excluded, for the extraneous count
        names = []                # non-directories, for the compare batches
        lines = []                # --dry-run
        try:
            # --exclude (in entries()): never compared, copied or descended
            for di in self.entries(dirname):
                name = di.name
                src_names.append(name)
                kind = dest.get(name)        # None: missing; True: a directory; False: other

                if not di.is_dir(follow_symlinks=False):      # d_type: no stat
                    names.append((name, kind))
                    if len(names) >= nbatch:
                        self.local_batches.append(CompareBatch(reldir, names))
                        names = []
                    continue

                # a directory: walked whatever the selecting filters say, and created
                # if the destination lacks it
                st.num_items += 1
                self.push_dir(di.path)
                if kind is True:
                    st.counters['unchanged'] += 1
                else:
                    self.want_copy(join(reldir, name), 0, NEW if kind is None else TYPE, lines)

        except Exception as error:
            self.error('scan', error)

        if names:
            self.local_batches.append(CompareBatch(reldir, names))

        extra = extraneous(src_names, dest, self.exclude)
        st.counters['extraneous'] += len(extra)
        if self.dry_run:
            lines += [f'X {printable(join(reldir, n))}\n' for n in extra]
        self.emit(lines)

    def process_batch(self, batch):
        """A CompareBatch, or a FileBatch to copy, ours or shared."""
        if isinstance(batch, CompareBatch):
            self.compare_batch(batch.reldir, batch.names)
        else:
            self.copy_batch(batch.paths, batch.nbytes)

    def compare_batch(self, reldir, names):
        """lstat a batch of one directory's names on both sides, and queue the ones to copy."""
        st = self.stats
        c = st.counters
        select = self.select
        join = os.path.join
        srcdir = join(self.src, reldir) if reldir else self.src
        destdir = join(self.dest, reldir) if reldir else self.dest
        lines = []
        for name, kind in self.ticked(names):
            spath = join(srcdir, name)
            try:
                s = os.lstat(spath)
            except OSError as error:
                self.error('stat', error)
                continue

            # selecting filters choose which files are copied (directories are always walked)
            if select is not None and not select(s):
                st.num_unmatched += 1
                continue

            st.num_items += 1
            st.total_size += s.st_size

            if kind is None:
                reason = NEW
            elif kind is True:
                reason = TYPE
            else:
                dpath = join(destdir, name)
                try:
                    d = os.lstat(dpath)
                except FileNotFoundError:
                    d = None
                except OSError as error:
                    self.error('stat', error)
                    continue
                slink = dlink = None
                if d is not None and stat.S_ISLNK(s.st_mode) and stat.S_ISLNK(d.st_mode):
                    try:
                        slink, dlink = os.readlink(spath), os.readlink(dpath)
                    except OSError as error:
                        self.error('readlink', error)
                        continue
                reason = needs_copy(s, d, slink, dlink)

            if reason is None:
                c['unchanged'] += 1
                continue
            self.want_copy(join(reldir, name), s.st_size if stat.S_ISREG(s.st_mode) else 0, reason, lines)

        self.emit(lines)

    def start_rsync(self, paths):
        """One rsync over a part of a copy batch, started (in_flight()'s start)."""
        return Rsync(self.cmd, paths)

    def copy_batch(self, paths, nbytes):
        """One copy batch of relative paths: --rsyncs rsyncs side by side, each over its
        part of the batch, waited for in turn. While they run we look for a 'share' and
        send our counts, so a long copy does not keep our queued work from idle ranks.
        The batch is one unit: it fails if any of its rsyncs does."""
        c = self.stats.counters
        t0 = time.monotonic()
        parts = split(paths, self.rsyncs)
        failed = vanished = False
        try:
            for part, rsync in self.in_flight(parts, self.start_rsync, len(parts), Rsync.kill):
                rc = rsync.wait(self.tick)
                if rc == RSYNC_VANISHED:
                    vanished = True
                    print(f'[{self.rank:3d}] rsync: some source files vanished (exit {rc})', file=sys.stderr)
                elif rc != 0:
                    failed = True
                    tail = rsync.tail()
                    self.error(f'copy a batch of {len(part)} entries: rsync exit {rc}',
                               '\n' + tail if tail else '(no output)')
                rsync.close()
        except OSError as error:              # rsync would not start
            c['rsync_failures'] += 1
            self.error('run rsync', error)
            return

        self.nrsync += 1
        c['rsync_batches'] += 1
        c['rsync_seconds'] += time.monotonic() - t0
        if vanished:
            c['rsync_vanished'] += 1
        if failed:
            c['rsync_failures'] += 1
            return

        self.copied_bytes += nbytes
        self.copied_files += len(paths)
        self.stats.num_files += len(paths)      # each rank's share of the copying (--verbose)
        c['copied_files'] += len(paths)
        c['copied_bytes'] += nbytes

    def progress_extra(self):
        """(bytes, entries, rsync batches) copied so far, for the progress line's copy
        rate; None with --dry-run, which copies nothing."""
        if self.dry_run:
            return None
        return (self.copied_bytes, self.copied_files, self.nrsync)
