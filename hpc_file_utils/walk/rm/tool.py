"""walkrm: remove directory trees in parallel, with guard rails (common/tool.py)."""

import logging
import os

from hpc_file_utils.walk.common.tool import Tool

log = logging.getLogger(__name__)

DRYRUN_FILE = 'walkrm-dryrun-r{:03d}.txt'        # each worker's --dry-run list, in the working directory
DRYRUN_FILES = DRYRUN_FILE.replace('{:03d}', '*')


def strictly_under(path, root):
    """True if path is below root (both normalized, absolute), not root itself."""
    return path != root and path.startswith(root.rstrip('/') + '/')


class RmTool(Tool):

    name = 'walkrm'
    description = ('Remove directory trees in parallel (MPI): files, links and specials unlinked in batches '
                   'during the walk, then the directories, deepest first, after it. Every target must be '
                   'strictly under an --allowed-root; symlinks are never followed and no filesystem boundary '
                   'is crossed.')
    examples = ('mpiexec -n 32 walkrm --allowed-root /glade/derecho/scratch/$USER --dirs /glade/derecho/scratch/$USER/old-run --dry-run',
                'mpiexec -n 32 walkrm --allowed-root /glade/derecho/scratch/$USER --dirs /glade/derecho/scratch/$USER/old-run')
    dirs_help = 'The directories to remove, each with everything below it'
    batch_help = 'a directory\'s files are unlinked a batch at a time'
    reports = False
    indexes = False
    selects = None           # whole trees only: a filter is refused
    errors_exit = 1          # something was left (or would not be removed)

    def add_arguments(self, parser):
        parser.add_argument('--allowed-root', action='append', required=True, metavar='DIR', help='Required (repeatable): every --dirs target must be strictly below one of these (after resolving symlinks); a root itself is refused')
        parser.add_argument('-n', '--dry-run', action='store_true', help='Remove nothing: count, and list what would be removed, one file per rank: ' + DRYRUN_FILES)
        parser.add_argument('--keep-root', action='store_true', help='Empty each target but keep the target directory itself')

    def check(self, parser, args):
        """The guard rails, all before any rank touches anything: no filters; allowed roots
        that exist; targets that are real directories (not symlinks, not '/'), strictly
        below an allowed root after resolving symlinks, none inside another. The walk uses
        the targets' real paths; each target's st_dev is the one the walk stays on."""
        if args.filters is not None:
            parser.error(f'walkrm removes whole trees: it takes no filters ({args.filters.describe()})')
        roots = []
        for r in args.allowed_root:
            real = os.path.realpath(r)
            if real == '/':
                parser.error(f"--allowed-root cannot be '/': {r}")
            if not os.path.isdir(real):
                raise FileNotFoundError(f'--allowed-root: no such directory: {r}')
            roots.append(real)
        targets = []
        for d in args.dirs:
            given = d.rstrip('/') or '/'
            if os.path.islink(given):
                parser.error(f'{d}: a symlink; walkrm removes only real directories (give its target, if that is meant)')
            real = os.path.realpath(given)
            if real == '/':
                parser.error("walkrm will not remove '/'")
            if not any(strictly_under(real, root) for root in roots):
                parser.error(f"{d} ({real}) is not strictly below an --allowed-root ({' '.join(roots)})")
            if real in targets:
                parser.error(f'{d} ({real}) is given twice')
            targets.append(real)
        for a in targets:
            for b in targets:
                if strictly_under(b, a):
                    parser.error(f'{b} is inside {a}: give the outer one only')
        args.dirs_given = list(args.dirs)
        args.dirs = targets
        args.allowed_roots = roots
        args.target_devs = {t: os.lstat(t).st_dev for t in targets}

    def banner(self, args):
        verb = 'Would remove' if args.dry_run else 'Removing'
        lines = []
        for given, real in zip(args.dirs_given, args.dirs):
            shown = given if os.path.normpath(given) == real else f'{given} ({real})'
            lines.append(f"{verb} {shown}{' (its contents; --keep-root)' if args.keep_root else ''}")
        lines.append(f"Allowed roots: {' '.join(args.allowed_roots)}")
        if args.dry_run:
            lines.append(f'Dry run: nothing is removed; the lists are {DRYRUN_FILES}')
        return lines

    def finish(self, walker):
        """The rmdir pass, on every rank once the walk is over (every file is unlinked by
        then): level by level, deepest first, each rank removes the directories it scanned
        at that depth, then a barrier. A directory's children are deeper, so they are gone
        (or failed, which leaves it non-empty: an error) before its turn. Then rank 0 checks
        the targets: gone, or empty with --keep-root."""
        from hpc_file_utils.walk.rm.worker import rmdir_pass
        MPI, comm, o = walker.MPI, walker.comm, walker.options
        scanned = getattr(walker, 'scanned', {})      # the Manager scanned nothing
        lo = comm.allreduce(min(scanned, default=1 << 30), op=MPI.MIN)
        hi = comm.allreduce(max(scanned, default=-1), op=MPI.MAX)
        t0 = MPI.Wtime()
        keep = frozenset(o.dirs) if o.keep_root else frozenset()
        for depth in range(hi, lo - 1, -1):
            if not o.dry_run and depth in scanned:
                rmdir_pass(walker, scanned[depth], keep)
            comm.Barrier()
        if walker.rank == 0:
            walker.rmdir_seconds = MPI.Wtime() - t0      # for the report
            if not o.dry_run:
                self.check_targets(walker)

    @staticmethod
    def check_targets(walker):
        """Rank 0, after the rmdir pass: each target gone (or empty, with --keep-root); a
        failure is an error, so the exit status says so."""
        o = walker.options
        for target in o.dirs:
            if o.keep_root:
                try:
                    with os.scandir(target) as it:
                        left = next(it, None)
                except OSError as error:
                    left = error
                if left is not None:
                    walker.count_error()
                    log.error('%s is not empty after the run (%s)', target, getattr(left, 'name', left))
            elif os.path.lexists(target):
                walker.count_error()
                log.error('%s still exists after the run', target)

    def report(self, manager, stats):
        from hpc_file_utils.walk.rm.report import rm_lines
        print('\n'.join(rm_lines(stats, manager.options, manager.walk_elapsed, manager.rmdir_seconds)), flush=True)

    def make_worker(self):
        from hpc_file_utils.walk.rm.worker import RmWorker
        return RmWorker()
