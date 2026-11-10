"""walksync's summary, from the counters the ranks summed (no MPI)."""

from hpc_file_utils.walk.common.formatting import format_number, format_size


def sync_lines(stats, elapsed, options):
    """The lines printed after a walksync run."""
    c = stats.counters
    n = lambda k: format_number(c[k])
    lines = [f"--> Sync: {n('new')} new, {n('changed')} changed, {n('unchanged')} unchanged; "
             f"{n('extraneous')} only in the destination (kept)"]
    if stats.num_excluded or stats.num_unmatched:
        lines.append(f'--> Not compared: {format_number(stats.num_excluded)} excluded by name (--exclude), '
                     f'{format_number(stats.num_unmatched)} not selected by the filters')
    if options.dry_run:
        lines.append(f"--> Dry run: would copy {format_number(c['new'] + c['changed'])} entries, "
                     f"{format_size(c['bytes_to_copy'])}; listed (N new, C changed, X only in the "
                     'destination) in walksync-dryrun-r*.txt')
        return lines
    rate = c['copied_bytes'] / elapsed if elapsed > 0 else 0
    lines.append(f"--> Copied {n('copied_files')} entries, {format_size(c['copied_bytes'])} ({format_size(rate)}/s) "
                 f"in {n('rsync_batches')} rsync batches ({c['rsync_seconds']:.1f} rsync-seconds over {elapsed:.1f} s); "
                 f"{n('rsync_failures')} failed, {n('rsync_vanished')} with vanished source files")
    missing = ''.join(f', {m} (this rsync has none)' for m in options.rsync_missing)
    lines.append(f'--> Not done here: deletions, hard links, directory modes and times{missing}; finish with '
                 f'rsync -aHAX --delete {options.dirs[0]}/ {options.dest}/')
    return lines
