"""RmWorker: walkrm's worker (tools-find-rm, step 2). Removes trees in two phases.

  scan     a directory: opened with O_NOFOLLOW (a directory swapped for a symlink since its
           parent's scan is refused, not followed), checked to be on its target's st_dev,
           then one scandir of the open directory (names and d_type). Each subdirectory is
           lstat'ed: one on another st_dev is an error and left alone (never crossed); one
           without u+rwx is given it first if it is ours (chmod), else it is an error and
           left. Subdirectories are walked; the other names (files, links, specials) go into
           unlink batches of --batch-files, RmBatch(dirname, names, dev, ino), which a
           'share' hands to idle ranks, so a 741K-entry directory's unlinks spread over
           every rank. The rank records the directory under its depth, for the rmdir pass.
  unlink   an RmBatch: the directory opened again with O_NOFOLLOW and checked to be the
           one scanned (st_dev and st_ino), then unlink(name, dir_fd) for each name: a
           symlink is removed, never followed.
  rmdir    after the walk (RmTool.finish(), every rank): depth by depth, deepest first,
           each rank rmdirs the directories it scanned at that depth (rmdir(name,
           dir_fd) of the parent opened with O_NOFOLLOW), a barrier per level. Children
           are always deeper, and every unlink is done once the walk has ended, so no
           message is needed and the walk's termination argument is untouched: an unlink
           batch creates nothing.

--dry-run lists every path that would be removed (directories included, the targets too
unless --keep-root) in walkrm-dryrun-r{rank}.txt, and removes and chmods nothing.

Not guarded: a directory higher up the path swapped for a symlink between a scan and the
open of a directory below it (only the last component is opened with O_NOFOLLOW).
"""

import logging
import os
import stat
from typing import NamedTuple

from hpc_file_utils.walk.common.formatting import printable
from hpc_file_utils.walk.common.worker import BaseWorker
from hpc_file_utils.walk.rm.tool import DRYRUN_FILE

log = logging.getLogger(__name__)

OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
USER_RWX = stat.S_IRWXU


class RmBatch(NamedTuple):
    """One scanned directory's non-directory names, to unlink."""
    dirname : str
    names   : list
    dev     : int    # the directory's st_dev and st_ino at its scan: the unlinks happen in it or not at all
    ino     : int


class RmWorker(BaseWorker):

    def __init__(self):
        super().__init__()
        o = self.options
        self.dry_run = o.dry_run
        self.keep = frozenset(o.dirs) if o.keep_root else frozenset()
        self.uid = os.geteuid()
        self.targets = sorted(((t.rstrip('/') + '/', dev) for t, dev in o.target_devs.items()),
                              key=lambda x: -len(x[0]))
        self.target_devs = o.target_devs
        self.scanned = {}           # depth -> [directories this rank scanned], for the rmdir pass
        self.dryrun = self.rank_file(DRYRUN_FILE)

    def target_dev(self, dirname):
        """The st_dev of the target that holds dirname."""
        dev = self.target_devs.get(dirname)
        if dev is not None:
            return dev
        for prefix, dev in self.targets:
            if dirname.startswith(prefix):
                return dev
        raise RuntimeError(f'walkrm: {dirname} is under no target')

    def fix_mode(self, dirname, statinfo, chmod):
        """A directory we must scan and empty needs u+rwx. True if it has it (or got it:
        chmod(mode) does it, unless a dry run); False, after an error, if it is not ours."""
        if statinfo.st_mode & USER_RWX == USER_RWX:
            return True
        if statinfo.st_uid != self.uid:
            self.error('empty a directory', f'{dirname}: not owned by uid {self.uid} and not u+rwx '
                                            f'({stat.filemode(statinfo.st_mode)})')
            return False
        c = self.stats.counters
        if self.dry_run:
            c['chmod'] += 1                  # would be needed
            return True
        try:
            chmod(stat.S_IMODE(statinfo.st_mode) | USER_RWX)
        except OSError as error:
            self.error('chmod u+rwx', f'{dirname}: {error}')
            return False
        c['chmod'] += 1
        return True

    def process_directory(self, dirname):
        st = self.stats
        c = st.counters
        if dirname in self.target_devs:      # a target: its own mode, before the open needs it
            try:
                s = os.lstat(dirname)
            except OSError as error:
                self.error('stat', error)
                return
            if not self.fix_mode(dirname, s, lambda mode: os.chmod(dirname, mode)):
                return
        try:
            fd = os.open(dirname, OPEN_DIR)
        except FileNotFoundError:
            log.warning("directory '%s' vanished", dirname)
            return
        except OSError as error:
            self.error('open directory', f'{dirname}: {error}')
            return
        try:
            s = os.fstat(fd)
            if s.st_dev != self.target_dev(dirname):
                self.error('cross a filesystem boundary', f'{dirname}: st_dev {s.st_dev}, not its target\'s')
                return
            st.num_dirs += 1
            self.scanned.setdefault(dirname.count('/'), []).append(dirname)
            if self.dry_run and dirname not in self.keep:
                self.dryrun.write(printable(dirname) + '\n')
            self.scan(fd, dirname, s)
        finally:
            os.close(fd)

    def scan(self, fd, dirname, dirstat):
        st = self.stats
        c = st.counters
        join = os.path.join
        nbatch = self.options.batch_files
        names = []
        try:
            for di in self.entries(fd):          # scandir of the open directory: no path lookup
                st.num_items += 1
                if not di.is_dir(follow_symlinks=False):
                    names.append(di.name)
                    if len(names) >= nbatch:
                        self.local_batches.append(RmBatch(dirname, names, dirstat.st_dev, dirstat.st_ino))
                        names = []
                    continue
                sub = join(dirname, di.name)
                try:
                    s = di.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    self.error('stat', f'{sub}: {error}')
                    continue
                if s.st_dev != dirstat.st_dev:
                    c['cross_device'] += 1
                    self.error('cross a filesystem boundary', f'{sub}: another filesystem is mounted there; left alone')
                    continue
                name = di.name
                if self.fix_mode(sub, s, lambda mode: os.chmod(name, mode, dir_fd=fd)):
                    self.push_dir(sub)
        except Exception as error:
            self.error('scan', f'{dirname}: {error}')
        if names:
            self.local_batches.append(RmBatch(dirname, names, dirstat.st_dev, dirstat.st_ino))

    def process_batch(self, batch):
        """Unlink an RmBatch's names (ours or shared) in the directory scanned, or list them (--dry-run)."""
        c = self.stats.counters
        join = os.path.join
        if self.dry_run:
            self.dryrun.write(''.join(printable(join(batch.dirname, n)) + '\n' for n in batch.names))
            c['removed'] += len(batch.names)
            self.stats.num_files += len(batch.names)
            return
        try:
            fd = os.open(batch.dirname, OPEN_DIR)
        except OSError as error:
            self.error('open directory', f'{batch.dirname}: {error}')
            return
        try:
            s = os.fstat(fd)
            if (s.st_dev, s.st_ino) != (batch.dev, batch.ino):
                self.error('unlink in', f'{batch.dirname}: not the directory that was scanned')
                return
            removed = 0
            for name in self.ticked(batch.names):
                try:
                    os.unlink(name, dir_fd=fd)
                    removed += 1
                except FileNotFoundError:
                    c['vanished'] += 1
                except OSError as error:
                    self.error('remove', f'{join(batch.dirname, name)}: {error}')
            c['removed'] += removed
            self.stats.num_files += removed          # each rank's share of the unlinks (--verbose)
        finally:
            os.close(fd)


def rmdir_pass(walker, dirs, keep):
    """One level of the rmdir pass on this rank: rmdir each of dirs (but those in keep)
    through its parent, opened with O_NOFOLLOW (one open per parent, the dirs sorted)."""
    c = walker.stats.counters
    parent_fd, parent = None, None
    try:
        for path in sorted(dirs):
            if path in keep:
                continue
            head, name = os.path.split(path)
            if head != parent:
                if parent_fd is not None:
                    os.close(parent_fd)
                    parent_fd = None
                parent = head
                try:
                    parent_fd = os.open(head, OPEN_DIR)
                except OSError as error:
                    walker.error('open directory', f'{head}: {error}')
                    continue
            if parent_fd is None:
                walker.error('remove directory', f'{path}: its parent could not be opened')
                continue
            try:
                os.rmdir(name, dir_fd=parent_fd)
                c['dirs_removed'] += 1
            except FileNotFoundError:
                c['vanished'] += 1
            except OSError as error:
                walker.error('remove directory', f'{path}: {error}')
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
