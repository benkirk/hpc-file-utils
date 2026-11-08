"""walksync's summary, from the counters the ranks summed (no MPI)."""

from hpc_file_utils.walk.common.formatting import format_number, format_size


def sync_lines(stats, elapsed, options):
    """The lines printed after a walksync run."""
    c = stats.counters
    n = lambda k: format_number(c[k])
    lines = ['--> Sync: {} new, {} changed, {} unchanged; {} only in the destination (kept)'.format(
        n('new'), n('changed'), n('unchanged'), n('extraneous'))]
    if stats.num_excluded or stats.num_unmatched:
        lines.append('--> Not compared: {} excluded by name (--exclude), {} not selected by the filters'.format(
            format_number(stats.num_excluded), format_number(stats.num_unmatched)))
    if options.dry_run:
        lines.append('--> Dry run: would copy {} entries, {}; listed (N new, C changed, X only in the '
                     'destination) in walksync-dryrun-r*.txt'.format(
                         format_number(c['new'] + c['changed']), format_size(c['bytes_to_copy'])))
        return lines
    rate = c['copied_bytes'] / elapsed if elapsed > 0 else 0
    lines.append('--> Copied {} entries, {} ({}/s) in {} rsync batches ({:.1f} rsync-seconds over {:.1f} s); '
                 '{} failed, {} with vanished source files'.format(
                     n('copied_files'), format_size(c['copied_bytes']), format_size(rate), n('rsync_batches'),
                     c['rsync_seconds'], elapsed, n('rsync_failures'), n('rsync_vanished')))
    missing = ''.join(', {} (this rsync has none)'.format(m) for m in getattr(options, 'rsync_missing', []))
    lines.append('--> Not done here: deletions, hard links, directory modes and times{}; finish with '
                 'rsync -aHAX --delete {}/ {}/'.format(missing, options.dirs[0], options.dest))
    return lines
