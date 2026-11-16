"""walkrm's summary, on rank 0 after the walk and the rmdir pass (no MPI)."""

from hpc_file_utils.walk.common.formatting import format_number
from hpc_file_utils.walk.rm.tool import DRYRUN_FILES


def rm_lines(stats, options, walk_seconds, rmdir_seconds):
    """The lines printed after a walkrm run; the first is the one scripts read."""
    c = stats.counters
    n = format_number
    ndirs = stats.num_dirs - (len(options.dirs) if options.keep_root else 0)
    if options.dry_run:
        chmod = f" ({n(c['chmod'])} would need u+rwx first)" if c['chmod'] else ''
        lines = [f"--> Dry run: would remove {n(c['removed'])} files, links and specials and {n(ndirs)} directories"
                 f"{chmod}; listed in {DRYRUN_FILES}"]
    else:
        lines = [f"--> Removed {n(c['removed'])} files, links and specials and {n(c['dirs_removed'])} directories "
                 f"({n(c['chmod'])} made u+rwx first); walk {walk_seconds:.1f} s, directories {rmdir_seconds:.1f} s"]
    if c['vanished']:
        lines.append(f"--> {n(c['vanished'])} entries vanished before their removal (removed by someone else)")
    if c['cross_device']:
        lines.append(f"--> {n(c['cross_device'])} directories on another filesystem left alone")
    if stats.num_errors:
        lines.append(f'--> {n(stats.num_errors)} errors: not everything was removed (see stderr)')
    if options.keep_root:
        lines.append(f"--> Kept (emptied): {' '.join(options.dirs)}")
    return lines
