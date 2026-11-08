#!/usr/bin/env python3

"""SyncWorker: walksync's worker (phase 6). Copies what is new or changed from the source
(--dirs SRC) to --dest DEST with batched rsync --files-from, never deleting anything.

Three kinds of work, the first the Manager's directories, the others batches (phase 5),
which a 'share' can hand to idle ranks (see manager.py):

  scan     a source directory: one scandir of it (names and d_type: no stat for files,
           as walkstat's scan) and one scandir of the matching destination directory,
           the lookup for the whole directory. Subdirectories are walked; one the
           destination lacks (or has as a non-directory) goes on the copy list itself, so
           empty directories are created. The other names go into compare batches of
           --batch-files, ('cmp', reldir, [(name, dest kind)]), and destination names the
           source lacks are counted as extraneous (listed with --dry-run, never deleted).
           --dry-run lists to walksync-dryrun-r{rank}.txt in the working directory, one
           file per rank: lines from many ranks on one stdout get spliced together.
  compare  ('cmp', ...): lstat each source name (and the destination's, if it has
           one), and needs_copy() (compare.py). What must be copied goes into the
           open copy batch, which carries over between directories, so changed files
           from many small directories make one rsync; it closes at --batch-files names
           or --batch-size bytes.
  copy     ('copy', relpaths, nbytes): one
           rsync -aAX --numeric-ids --chmod=Du+w --files-from=- --from0 [--rsync-opts] SRC/ DEST/
           With --files-from rsync does not recurse, so a directory listed is created
           (with u+w: other ranks' batches write into it) but not filled from here.

A compare batch creates copy batches; they stay on this rank's list and are processed
before its next 'ready' (manager.py, point 1), so the termination argument holds.

What is left to a final serial rsync -aHAX --delete SRC/ DEST/: deletions, hard links
(copied here as separate files) and the directories' real modes and times.
"""

import os
import stat
import subprocess
import sys
import tempfile
import time

from hpc_file_utils.walk.common import worker as base
from hpc_file_utils.walk.common.options import RSYNC_OPTS
from hpc_file_utils.walk.common.worker import BaseWorker
from hpc_file_utils.walk.sync.compare import NEW, TYPE, extraneous, needs_copy, show

RSYNC_POLL_SECONDS = 0.5   # while an rsync runs, look for a 'share' and send progress this often
RSYNC_VANISHED = 24        # rsync's "some source files vanished": a warning, not a failure
STDERR_TAIL = 4000         # bytes of a failed rsync's stderr to print
DRYRUN_FILE = 'walksync-dryrun-r{:03d}.txt'


################################################################################
class SyncWorker(BaseWorker):

    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self):
        BaseWorker.__init__(self)
        o = self.options
        self.src = o.dirs[0]                  # normalized by options.py: no trailing '/'
        self.dest = o.dest
        self.src_prefix = len(self.src.rstrip('/')) + 1
        self.dry_run = o.dry_run
        self.cmd = [o.rsync, *RSYNC_OPTS, '--files-from=-', '--from0', *o.rsync_opts,
                    self.src.rstrip('/') + '/', self.dest.rstrip('/') + '/']
        self.copied_bytes = 0                 # in rsync batches that finished (progress_extra)
        self.copied_files = 0
        self.nrsync = 0
        self.dryrun_file = None               # walksync-dryrun-r{rank}.txt, opened at the first line
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def rel(self, path):
        """A source path relative to the source root ('' for the root)."""
        return '' if path == self.src else path[self.src_prefix:]



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def error(self, what, error):
        self.stats.num_errors += 1
        print('[{:3d}] Cannot {}: {}'.format(self.rank, what, error), file=sys.stderr)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
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



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def want_copy(self, rel, size, reason, lines):
        """An entry that must be copied: count it, then list it (--dry-run, into 'lines')
        or put it in the open copy batch."""
        c = self.stats.counters
        c['new' if reason == NEW else 'changed'] += 1
        c['bytes_to_copy'] += size
        if self.dry_run:
            lines.append('{} {}\n'.format('N' if reason == NEW else 'C', show(rel)))
        else:
            self.add_to_batch(rel, size)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def emit(self, lines):
        """--dry-run's lines, into this rank's own list."""
        if lines:
            if self.dryrun_file is None:
                self.dryrun_file = open(DRYRUN_FILE.format(self.rank), 'w', encoding='utf-8')
            self.dryrun_file.write(''.join(lines))
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_directory(self, dirname):

        # last-minute check for a race condition:
        if not os.path.exists(dirname):
            print('[{:3d}] directory \'{}\' vanished'.format(self.rank, dirname), file=sys.stderr)
            return

        st = self.stats
        exclude = self.exclude
        nbatch = self.options.batch_files
        join = os.path.join

        st.num_dirs += 1
        reldir = self.rel(dirname)
        dest = self.dest_kinds(join(self.dest, reldir) if reldir else self.dest)

        src_names = []            # every name not excluded, for the extraneous count
        names = []                # non-directories, for the compare batches
        lines = []                # --dry-run
        try:
            with os.scandir(dirname) as it:
                for nscanned, di in enumerate(it, 1):

                    # progress, and a 'share' from the Manager, every so many entries scanned
                    if 0 == nscanned % base.PROGRESS_INCREMENT:
                        self.report_progress()
                    if 0 == nscanned % base.SHARE_CHECK_INCREMENT:
                        self.check_share()

                    # --exclude: by name, before any stat; never compared, copied or descended
                    if exclude is not None and exclude(di.name):
                        st.num_excluded += 1
                        continue

                    name = di.name
                    src_names.append(name)
                    kind = dest.get(name)        # None: missing; True: a directory; False: other

                    if not di.is_dir(follow_symlinks=False):      # d_type: no stat
                        names.append((name, kind))
                        if len(names) >= nbatch:
                            self.local_batches.append(('cmp', reldir, names))
                            names = []
                        continue

                    # a directory: walked whatever the selecting filters say, and created
                    # if the destination lacks it
                    st.num_items += 1
                    self.local_dirs.append(di.path)
                    if len(self.local_dirs) >= self.options.local_queue_max: self.ship_overflow()
                    if kind is True:
                        st.counters['unchanged'] += 1
                    else:
                        self.want_copy(join(reldir, name), 0, NEW if kind is None else TYPE, lines)

        except Exception as error:
            self.error('scan', error)

        if names:
            self.local_batches.append(('cmp', reldir, names))

        extra = extraneous(src_names, dest, exclude)
        st.counters['extraneous'] += len(extra)
        if self.dry_run:
            lines += ['X {}\n'.format(show(join(reldir, n))) for n in extra]
        self.emit(lines)
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_batch(self, batch):
        """A compare batch or a copy batch, ours or shared."""
        if batch[0] == 'cmp':
            self.compare_batch(batch[1], batch[2])
        else:
            self.copy_batch(batch[1], batch[2])
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def close_batch(self):
        """The open copy batch, if it holds anything, onto our list, tagged 'copy'."""
        if self.batch:
            self.local_batches.append(('copy', self.batch, self.batch_bytes))
            self.batch, self.batch_bytes = [], 0
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def compare_batch(self, reldir, names):
        """lstat a batch of one directory's names on both sides, and queue the ones to copy."""
        st = self.stats
        c = st.counters
        select = self.select
        join = os.path.join
        srcdir = join(self.src, reldir) if reldir else self.src
        destdir = join(self.dest, reldir) if reldir else self.dest
        share_every = base.SHARE_CHECK_INCREMENT
        lines = []
        for i, (name, kind) in enumerate(names, 1):
            if 0 == i % share_every:
                self.check_share()
                self.report_progress(only_if_due=True)

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
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def copy_batch(self, paths, nbytes):
        """One rsync for a batch of relative paths. While it runs we look for a 'share'
        and send our counts, so a long copy does not keep our queued work from idle ranks."""
        c = self.stats.counters
        t0 = time.monotonic()
        with tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
            except OSError as error:
                c['rsync_failures'] += 1
                self.error('run rsync', error)
                return
            try:
                proc.stdin.write(b'\0'.join(os.fsencode(p) for p in paths) + b'\0')
                proc.stdin.close()
            except BrokenPipeError:
                pass                  # rsync quit early: its exit status and stderr say why
            while True:
                try:
                    rc = proc.wait(timeout=RSYNC_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    self.check_share()
                    self.report_progress(only_if_due=True)

            self.nrsync += 1
            c['rsync_batches'] += 1
            c['rsync_seconds'] += time.monotonic() - t0
            if rc == RSYNC_VANISHED:
                c['rsync_vanished'] += 1
                print('[{:3d}] rsync: some source files vanished (exit {})'.format(self.rank, rc), file=sys.stderr)
            elif rc != 0:
                c['rsync_failures'] += 1
                err.seek(0, os.SEEK_END)
                err.seek(max(0, err.tell() - STDERR_TAIL))
                tail = err.read().decode(errors='replace').rstrip()
                self.error('copy a batch of {} entries: rsync exit {}'.format(len(paths), rc),
                           '\n' + tail if tail else '(no output)')
                return

        self.copied_bytes += nbytes
        self.copied_files += len(paths)
        self.stats.num_files += len(paths)      # each rank's share of the copying (--verbose)
        c['copied_files'] += len(paths)
        c['copied_bytes'] += nbytes
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def progress_extra(self):
        """(bytes, entries, rsync batches) copied so far, for the progress line's copy
        rate; None with --dry-run, which copies nothing."""
        if self.dry_run:
            return None
        return (self.copied_bytes, self.copied_files, self.nrsync)



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def run(self):
        super().run()
        if self.dryrun_file is not None:
            self.dryrun_file.close()
            self.dryrun_file = None
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def process_file(self, filename, statinfo):
        # not called: SyncWorker has its own scan (process_directory above)
        return
