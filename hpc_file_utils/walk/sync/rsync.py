"""walksync's rsync (no MPI): which one runs and with what options, the command for a
copy batch, and one rsync as a process to start, poll and finish.

Several rsyncs run side by side over one copy batch (--rsyncs K, split() cutting the
batch K ways): a cold file costs one storage round trip, ~40 ms per 1 MiB file on
derecho's scratch, whatever rsync does with it, and K rsyncs overlap those round trips as
walktar's reader threads do (docs/HANDOFF-sweep.md 0.4).
"""

import os
import re
import shutil
import subprocess
import tempfile

# walksync's rsync for each copy batch; --rsync-opts are appended. Du+w: batches on
# different ranks create the same parent directories, and a read-only source's would
# refuse the files of the next batch.
RSYNC_OPTS = ['-aAX', '--numeric-ids', '--chmod=Du+w']

# Where walksync looks when rsync on PATH lacks ACLs or xattrs. On derecho, config_env.sh puts the
# env's conda-forge rsync (no ACLs) first on PATH, while /usr/bin/rsync (3.2.7) has both.
RSYNC_ALTERNATIVES = ('/usr/bin/rsync',)

RSYNC_POLL_SECONDS = 0.5   # while an rsync runs, look for a 'share' and send progress this often
RSYNC_VANISHED = 24        # rsync's "some source files vanished": a warning, not a failure
STDERR_TAIL = 4000         # bytes of a failed rsync's stderr to print


def parse_rsync_capabilities(version_text):
    """(acls, xattrs) from 'rsync --version': False only for an explicit 'no ACLs' /
    'no xattrs' in its Capabilities list (conda-forge's Linux rsync has no ACLs); an
    unfamiliar format counts as supported, and rsync itself will say if it is not."""
    acls = not re.search(r'\bno ACLs\b', version_text)
    xattrs = not re.search(r'\bno xattrs\b', version_text)
    return acls, xattrs


def rsync_version(rsync):
    """'rsync --version' of the rsync at path 'rsync', or '' if it would not run."""
    try:
        return subprocess.run([rsync, '--version'], capture_output=True, text=True, errors='replace',
                              timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def rsync_capabilities(rsync):
    """(acls, xattrs) the rsync at path 'rsync' was built with."""
    return parse_rsync_capabilities(rsync_version(rsync))


def choose_rsync(requested=None, alternatives=RSYNC_ALTERNATIVES):
    """(path, acls, xattrs, note): the rsync walksync runs.

    An explicit --rsync is used as given. Otherwise rsync on PATH, unless it lacks ACLs or
    xattrs and an alternative's --version explicitly lists both. Explicitly: macOS's
    /usr/bin/rsync (openrsync) prints no Capabilities list, which parse_rsync_capabilities()
    would count as supported. note says why an alternative was chosen; path is None if
    no rsync was found."""
    path = shutil.which(requested or 'rsync')
    acls, xattrs = rsync_capabilities(path) if path else (False, False)
    if requested is not None or (acls and xattrs):
        return path, acls, xattrs, ''
    for alt in alternatives:
        alt_path = shutil.which(alt)
        if not alt_path or (path and os.path.realpath(alt_path) == os.path.realpath(path)):
            continue
        text = rsync_version(alt_path)
        if (re.search(r'\bACLs\b', text) and re.search(r'\bxattrs\b', text)
                and parse_rsync_capabilities(text) == (True, True)):
            lacks = ' and '.join(name for name, ok in (('ACLs', acls), ('xattrs', xattrs)) if not ok)
            why = f'rsync on PATH ({path}) has no {lacks}' if path else 'no rsync on PATH'
            return alt_path, True, True, f'using {alt_path}, which has ACLs and xattrs: {why}'
    return path, acls, xattrs, ''


def rsync_base_opts(acls, xattrs):
    """RSYNC_OPTS without -A / -X where rsync lacks them."""
    flags = '-a' + ('A' if acls else '') + ('X' if xattrs else '')
    return [flags] + RSYNC_OPTS[1:]


def rsync_command(options):
    """The rsync for a copy batch, which reads its list from stdin:
    rsync <base opts> --files-from=- --from0 <--rsync-opts> SRC/ DEST/"""
    return [options.rsync, *options.rsync_base, '--files-from=-', '--from0', *options.rsync_opts,
            options.dirs[0].rstrip('/') + '/', options.dest.rstrip('/') + '/']


def split(paths, k):
    """paths cut into k consecutive lists of near-equal length (fewer when there are fewer
    paths than k; one when k is 1; none for no paths), in order."""
    if not paths:
        return []
    k = max(1, min(k, len(paths)))
    size, extra = divmod(len(paths), k)
    parts, start = [], 0
    for i in range(k):
        end = start + size + (1 if i < extra else 0)
        parts.append(paths[start:end])
        start = end
    return parts


class Rsync:
    """One rsync over a list of paths, started at once (the list goes down its stdin,
    \\0-separated); wait() for it, tail() its stderr if it failed, then close()."""

    def __init__(self, cmd, paths):
        self.err = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.err)
        except OSError:
            self.err.close()
            raise
        try:
            self.proc.stdin.write(b'\0'.join(os.fsencode(p) for p in paths) + b'\0')
            self.proc.stdin.close()
        except BrokenPipeError:
            pass                  # rsync quit early: its exit status and stderr say why

    def wait(self, tick):
        """rsync's exit status, once it is done; tick() every RSYNC_POLL_SECONDS meanwhile
        (the worker answers a 'share' and sends progress there)."""
        while True:
            try:
                return self.proc.wait(timeout=RSYNC_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                tick()

    def tail(self):
        """The last STDERR_TAIL bytes of rsync's stderr, for the error message."""
        err = self.err
        err.seek(0, os.SEEK_END)
        err.seek(max(0, err.tell() - STDERR_TAIL))
        return err.read().decode(errors='replace').rstrip()

    def close(self):
        self.err.close()

    def kill(self):
        """Started but never waited for (its batch was abandoned): stop it."""
        self.proc.kill()
        self.proc.wait()
        self.err.close()
