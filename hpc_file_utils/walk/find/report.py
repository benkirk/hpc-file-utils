"""walkfind's summary and --output, on rank 0 after the walk (no MPI)."""

import os
import shutil

from hpc_file_utils.walk.common.formatting import format_number, format_size

TYPE_ORDER = 'fdlpsbc?'


def find_lines(stats, options, list_file):
    """The lines printed after a walkfind run. The first is the one scripts read:
    '--> Find: walked N entries below the roots ...; matched M ...'."""
    c = stats.counters
    n = format_number
    types = ', '.join(f'{t} {n(c["found_" + t])}' for t in TYPE_ORDER if c['found_' + t])
    lines = [f"--> Find: walked {n(stats.num_items)} entries below the roots ({n(stats.num_dirs)} directories "
             f"scanned); matched {n(c['found'])}{f' ({types})' if types else ''}, {format_size(c['found_bytes'])} "
             f"({n(c['found_bytes'])} bytes)"]
    if stats.num_excluded or c['pruned']:
        lines.append(f"--> Not walked: {n(stats.num_excluded)} excluded by name (--exclude), "
                     f"{n(c['pruned'])} matching directories not descended (--prune-matches)")
    if stats.num_unmatched:
        lines.append(f'--> Not listed: {n(stats.num_unmatched)} matched by name but not selected by the filters')
    where = options.output or list_file.replace('{:03d}', '*') + ', one per worker'
    lines.append(f"--> Listed in {where}{' (NUL-terminated)' if options.print0 else ''}")
    return lines


def join_lists(output, names):
    """--output: the per-rank lists that exist, in order, into output (via output.tmp, so a
    reader never sees half of it), then removed. output exists, empty, if none do."""
    tmp = output + '.tmp'
    present = [name for name in names if os.path.exists(name)]
    with open(tmp, 'wb') as out:
        for name in present:
            with open(name, 'rb') as f:
                shutil.copyfileobj(f, out)
    os.replace(tmp, output)
    for name in present:
        os.unlink(name)
