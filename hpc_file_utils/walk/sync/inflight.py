"""walksync's in-flight bytes: what rsync has written so far of the batch now running, for
the progress line (no MPI).

A copy batch's bytes are credited when every
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
"""

import os
import time

INFLIGHT_MIN = 256 * 1024 * 1024   # entries at least this big are followed while their rsync runs
INFLIGHT_CACHE_SECONDS = 1.0       # the in-flight total is re-measured at most this often
TEMP_LOOK_MAX_SECONDS = 60.0       # a temp file not found yet is looked for again after 1, 2, 4 ... s, up to this
TEMP_SUFFIX_LEN = 6                # rsync's temp file: .<name>.XXXXXX


class InFlight:
    """A big entry of the copy batch now running: where its bytes land, and what we know."""
    __slots__ = ('part', 'destdir', 'name', 'size', 'temp', 'done', 'next_look', 'look_wait')

    def __init__(self, part, destdir, name, size):
        self.part, self.destdir, self.name, self.size = part, destdir, name, size
        self.temp = None          # rsync's temp file's name, once found
        self.done = False         # its part's rsync returned (or its temp file was renamed into place)
        self.next_look = 0.0      # when to look for the temp file again (monotonic)
        self.look_wait = 1.0


class InFlightBytes:
    """SyncWorker's part that follows the big entries of the copy batch it is running."""

    def init_inflight(self, mode):
        """The in-flight bytes' state. mode: 'inplace', 'temp' or None (not counted:
        rsync.inflight_mode())."""
        self.inflight_mode = mode
        self.batch_big = []                   # the open batch's big entries: (index in batch, st_size)
        self.inflight = []                    # InFlight, one per big entry of the batch being copied
        self.inflight_time = float('-inf')    # when inflight_value was measured (monotonic)
        self.inflight_value = 0

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
