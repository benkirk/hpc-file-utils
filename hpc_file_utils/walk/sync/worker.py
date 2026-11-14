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
  copy     a CopyBatch(relpaths, nbytes, ...): --rsyncs K of
           rsync -aAX --numeric-ids --chmod=Du+w --files-from=- --from0 [--rsync-opts] SRC/ DEST/
           side by side, each over a K-th of the batch (rsync.py; -A / -X dropped, with a
           warning, if this rsync was built without them). With --files-from rsync does
           not recurse, so a directory listed is created (with u+w: other ranks' batches
           write into it) but not filled from here.

A compare batch creates copy batches; they stay on this rank's list and are processed
before its next 'ready' (manager.py, point 1), so the termination argument holds.

--index (indexes-2, step B; common/index.py): a per-rank record of what was copied,
index-r{rank}.tsv (index-<prefix>-r{rank}.tsv with --summary-prefix, .gz with --index=gz),
walkstat's format with tool=walksync, root= the source and dest= the destination in the
header. One E record for each entry an rsync copied, from the source lstat the compare
took (a new directory's from one lstat at the scan, taken only with --index): the copy
batch carries those stats (a CopyBatch, so a share hands them over with the paths), and
the records are written by the rank that ran the rsync, after it returned. A part whose
rsync failed records nothing; one that exited 24 (vanished source files) records the
entries still present. An entry compared and found unchanged is not recorded: the index
is what this run copied, not a listing (walkstat --index is that). With --dry-run the
index is the one the run would write, recorded at the compare, and the header says
dry-run=yes.

In-flight bytes (tools-find-rm, step 0): a copy batch's bytes are credited when every
rsync of it has returned, so a batch of one 900 GiB file used to show nothing for an
hour, and on 4F (16 nodes) the progress line said 36.7 GiB/s while df said ~86 GiB/s.
A batch now carries its big entries (st_size >= INFLIGHT_MIN), and while it runs the
progress line also counts what is already written of each: with --inplace the
destination file itself, otherwise rsync's temp file .<name>.XXXXXX beside it (found by
one scandir of the destination directory, then lstat'ed), each capped at its size. A big
file whose part's rsync returned counts in full; the whole batch's bytes replace all of
that in one step when the batch ends, and a failed batch drops it: nothing counts twice.
rsync options that move the temp file elsewhere (-T/--temp-dir, --partial-dir,
--delay-updates) turn the in-flight count off (rsync.inflight_mode()).

What is left to a final serial rsync -aHAX --delete SRC/ DEST/: deletions, hard links
(copied here as separate files) and the directories' real modes and times.
"""

import logging
import os
import stat
import time
from typing import NamedTuple

from hpc_file_utils.walk.common.formatting import printable
from hpc_file_utils.walk.common.index import IndexWriter, encode_path, index_stat, rank_index_name
from hpc_file_utils.walk.common.worker import BaseWorker
from hpc_file_utils.walk.sync.compare import NEW, TYPE, extraneous, needs_copy
from hpc_file_utils.walk.sync.rsync import RSYNC_VANISHED, Rsync, rsync_command, split

log = logging.getLogger(__name__)

DRYRUN_FILE = 'walksync-dryrun-r{:03d}.txt'

INFLIGHT_MIN = 256 * 1024 * 1024   # entries at least this big are followed while their rsync runs
INFLIGHT_CACHE_SECONDS = 1.0       # the in-flight total is re-measured at most this often
TEMP_LOOK_MAX_SECONDS = 60.0       # a temp file not found yet is looked for again after 1, 2, 4 ... s, up to this
TEMP_SUFFIX_LEN = 6                # rsync's temp file: .<name>.XXXXXX


class CompareBatch(NamedTuple):
    """One source directory's non-directory names still to compare with the destination's."""
    reldir : str    # the directory, relative to the source root ('' for the root)
    names  : list   # (name, kind): kind is None (the destination lacks it), True (a directory there) or False


class CopyBatch(NamedTuple):
    """A closed copy batch: its paths and st_size total, as a FileBatch; with --index each
    entry's source lstat (an IndexStat, or None for one that vanished before its stat), for
    the records; and its big entries, for the in-flight bytes (the module docstring)."""
    paths  : list   # relative to the source root
    nbytes : int
    stats  : list   # None without --index
    big    : tuple  # ((index in paths, st_size), ...) of the entries >= INFLIGHT_MIN


class InFlight:
    """A big entry of the copy batch now running: where its bytes land, and what we know."""
    __slots__ = ('part', 'destdir', 'name', 'size', 'temp', 'done', 'next_look', 'look_wait')

    def __init__(self, part, destdir, name, size):
        self.part, self.destdir, self.name, self.size = part, destdir, name, size
        self.temp = None          # rsync's temp file's name, once found
        self.done = False         # its part's rsync returned (or its temp file was renamed into place)
        self.next_look = 0.0      # when to look for the temp file again (monotonic)
        self.look_wait = 1.0


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
        self.index = self.open_index()
        self.batch_stats = [] if self.index is not None else None   # the open batch's stats (--index)
        self.init_inflight(o.inflight)

    def init_inflight(self, mode):
        """The in-flight bytes' state. mode: 'inplace', 'temp' or None (not counted:
        rsync.inflight_mode())."""
        self.inflight_mode = mode
        self.batch_big = []                   # the open batch's big entries: (index in batch, st_size)
        self.inflight = []                    # InFlight, one per big entry of the batch being copied
        self.inflight_time = float('-inf')    # when inflight_value was measured (monotonic)
        self.inflight_value = 0

    def open_index(self):
        """--index: this rank's record of what it copied (the module docstring), opened at
        its first record and closed by run(); None without."""
        o = self.options
        if not o.index:
            return None
        fields = [('dest', encode_path(self.dest))] + ([('dry-run', 'yes')] if self.dry_run else [])
        index = IndexWriter(rank_index_name(o.summary_prefix, self.rank), 'walksync', [self.src],
                            how=o.index, fields=fields)
        self.files.append(index)
        return index

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

    def want_copy(self, rel, size, reason, lines, statinfo=None):
        """An entry that must be copied: count it, then list it (--dry-run, into 'lines';
        recorded in the index now) or put it in the open copy batch. statinfo is its
        source lstat, for the index (None without --index, or if it vanished)."""
        c = self.stats.counters
        c['new' if reason == NEW else 'changed'] += 1
        c['bytes_to_copy'] += size
        if self.dry_run:
            lines.append(f"{'N' if reason == NEW else 'C'} {printable(rel)}\n")
            if self.index is not None and statinfo is not None:
                self.index.add(os.path.join(self.src, rel), statinfo)
        else:
            if self.batch_stats is not None:
                self.batch_stats.append(None if statinfo is None else index_stat(statinfo))
            if size >= INFLIGHT_MIN:
                self.batch_big.append((len(self.batch), size))
            self.add_to_batch(rel, size)

    def close_batch(self):
        """BaseWorker's, as a CopyBatch: the stats (--index) and the big entries go with the paths."""
        if self.batch:
            self.local_batches.append(CopyBatch(self.batch, self.batch_bytes, self.batch_stats,
                                                tuple(self.batch_big)))
            self.batch, self.batch_bytes, self.batch_big = [], 0, []
            if self.batch_stats is not None:
                self.batch_stats = []

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
                    self.want_copy(join(reldir, name), 0, NEW if kind is None else TYPE, lines,
                                   self.dir_stat(di))

        except Exception as error:
            self.error('scan', error)

        if names:
            self.local_batches.append(CompareBatch(reldir, names))

        extra = extraneous(src_names, dest, self.exclude)
        st.counters['extraneous'] += len(extra)
        if self.dry_run:
            lines += [f'X {printable(join(reldir, n))}\n' for n in extra]
        self.emit(lines)

    def dir_stat(self, di):
        """--index: a new directory's lstat, for its record (one call per directory to
        copy; the scan stats no directory otherwise). None without --index, or if it
        vanished: the copy goes ahead, as without --index, and rsync reports it."""
        if self.index is None:
            return None
        try:
            return di.stat(follow_symlinks=False)
        except OSError:
            return None

    def process_batch(self, batch):
        """A CompareBatch, or a CopyBatch to copy, ours or shared."""
        if isinstance(batch, CompareBatch):
            self.compare_batch(batch.reldir, batch.names)
        else:
            self.copy_batch(batch.paths, batch.nbytes, batch.stats, batch.big)

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
            self.want_copy(join(reldir, name), s.st_size if stat.S_ISREG(s.st_mode) else 0, reason, lines, s)

        self.emit(lines)

    def start_rsync(self, paths):
        """One rsync over a part of a copy batch, started (in_flight()'s start)."""
        return Rsync(self.cmd, paths)

    def copy_batch(self, paths, nbytes, stats=None, big=()):
        """One copy batch of relative paths: --rsyncs rsyncs side by side, each over its
        part of the batch, waited for in turn. While they run we look for a 'share' and
        send our counts (with the big entries' in-flight bytes), so a long copy does not
        keep our queued work from idle ranks. The batch is one unit: it fails if any of
        its rsyncs does. stats (--index): the paths' source lstats, recorded per part as
        its rsync returns (record_copied()). big: CopyBatch.big."""
        try:
            self.run_copy_batch(paths, nbytes, stats, big)
        finally:
            self.clear_inflight()             # credited (or not) by now: never counted twice

    def run_copy_batch(self, paths, nbytes, stats, big):
        c = self.stats.counters
        t0 = time.monotonic()
        parts = split(paths, self.rsyncs)
        stat_parts = split(stats, self.rsyncs) if stats is not None else None   # the same cuts
        self.start_inflight(paths, parts, big)
        failed = vanished = False
        try:
            for i, (part, rsync) in enumerate(self.in_flight(parts, self.start_rsync, len(parts), Rsync.kill)):
                rc = rsync.wait(self.tick)
                if rc in (0, RSYNC_VANISHED):
                    self.part_done(i)
                if stat_parts is not None and rc in (0, RSYNC_VANISHED):
                    self.record_copied(part, stat_parts[i], rc == RSYNC_VANISHED)
                if rc == RSYNC_VANISHED:
                    vanished = True
                    log.warning('rsync: some source files vanished (exit %d)', rc)
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

        # the batch's bytes replace its in-flight bytes in one step (no tick() between)
        self.clear_inflight()
        self.copied_bytes += nbytes
        self.copied_files += len(paths)
        self.stats.num_files += len(paths)      # each rank's share of the copying (--verbose)
        c['copied_files'] += len(paths)
        c['copied_bytes'] += nbytes

    def record_copied(self, part, stats, vanished=False):
        """--index: E records for the entries of a part its rsync copied, from the compare's
        stats. After exit 24 (vanished), only the entries still in the source."""
        index, join, src = self.index, os.path.join, self.src
        for rel, s in zip(part, stats):
            if s is None:
                continue
            path = join(src, rel)
            if vanished and not os.path.lexists(path):
                continue
            index.add(path, s)

    def start_inflight(self, paths, parts, big):
        """An InFlight for each big entry of the batch about to run, in its part
        (split()'s consecutive cuts)."""
        self.inflight = []
        self.inflight_time = float('-inf')
        if not big or self.inflight_mode is None:
            return
        ends, end = [], 0
        for part in parts:
            end += len(part)
            ends.append(end)
        join, dirname, basename = os.path.join, os.path.dirname, os.path.basename
        for i, size in big:
            part = next(k for k, e in enumerate(ends) if i < e)
            rel = paths[i]
            self.inflight.append(InFlight(part, join(self.dest, dirname(rel)), basename(rel), size))

    def part_done(self, part):
        """A part's rsync returned 0 or 24: its big entries are written, in full."""
        for f in self.inflight:
            if f.part == part:
                f.done = True
        self.inflight_time = float('-inf')

    def clear_inflight(self):
        self.inflight = []
        self.inflight_time = float('-inf')
        self.inflight_value = 0

    def inflight_bytes(self):
        """What is written so far of the running batch's big entries (the module
        docstring), re-measured at most every INFLIGHT_CACHE_SECONDS: a share reply
        carries our counts too, and must not cost a round of lstats each time."""
        if not self.inflight:
            return 0
        now = time.monotonic()
        if now - self.inflight_time >= INFLIGHT_CACHE_SECONDS:
            self.inflight_value = sum(self.written_of(f, now) for f in self.inflight)
            self.inflight_time = now
        return self.inflight_value

    def written_of(self, f, now):
        """Bytes written so far of one big entry, at most its size."""
        if f.done:
            return f.size
        if self.inflight_mode == 'inplace':
            try:
                return min(os.lstat(os.path.join(f.destdir, f.name)).st_size, f.size)
            except OSError:
                return 0
        if f.temp is None:
            if now < f.next_look:
                return 0
            f.temp = find_temp(f.destdir, f.name)
            if f.temp is None:          # not created yet: look again later, less and less often
                f.next_look = now + f.look_wait
                f.look_wait = min(2 * f.look_wait, TEMP_LOOK_MAX_SECONDS)
                return 0
        try:
            return min(os.lstat(os.path.join(f.destdir, f.temp)).st_size, f.size)
        except FileNotFoundError:       # renamed into place: written in full
            f.done = True
            return f.size
        except OSError:
            return 0

    def progress_extra(self):
        """(bytes, entries, rsync batches) copied so far, the bytes with the running
        batch's in-flight bytes, for the progress line's copy rate; None with --dry-run,
        which copies nothing."""
        if self.dry_run:
            return None
        return (self.copied_bytes + self.inflight_bytes(), self.copied_files, self.nrsync)


def find_temp(destdir, name):
    """The name of rsync's temp file for 'name' in destdir, .<name>.XXXXXX, or None; if
    several match (one left by a killed run), the most recently modified."""
    prefix = f'.{name}.'
    want = len(prefix) + TEMP_SUFFIX_LEN
    best, best_mtime = None, None
    try:
        with os.scandir(destdir) as it:
            for de in it:
                if len(de.name) == want and de.name.startswith(prefix):
                    try:
                        mtime = de.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    if best is None or mtime > best_mtime:
                        best, best_mtime = de.name, mtime
    except OSError:
        return None
    return best
