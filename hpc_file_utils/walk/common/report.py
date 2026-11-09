"""The lines every tool prints after its walk, from the combined WalkStats (no MPI): how
the directories were spread over the ranks and how long they waited, and --verbose's
per-rank table. walkstat's own reports are in stat/report.py."""

from hpc_file_utils.walk.common.formatting import format_number, format_size

SEP = '#' + '-' * 79


def columns(row, *names):
    """A '#' comment line naming the columns of the row template 'row'."""
    line = row.format(*names)
    return '#' + line[1:] if line.startswith(' ') else '# ' + line


def rank_lines(stats):
    """--verbose: each rank's own share of the walk."""
    hosts = max([4] + [len(r.host) for r in stats.ranks])
    row = '{:>6}  {:<%d}  {:>12}  {:>12}  {:>11}  {:>9}  {:>9}' % hosts
    lines = [SEP, columns(row, 'rank', 'host', 'dirs', 'files', 'size', 'waited s', 'max stack')]
    for r in stats.ranks:
        if r.rank == 0: continue    # the Manager scans nothing
        lines.append(row.format(r.rank, r.host, format_number(r.num_dirs), format_number(r.num_files),
                                format_size(r.nbytes), '{:.2f}'.format(r.wait_time),
                                format_number(r.max_local_dirs)))
    return lines


def balance_lines(stats, walk_elapsed):
    """How evenly the directories were spread over the workers, and how long they waited
    for the Manager (docs/walk-load-balancing.md, section 1.4)."""
    workers = [r for r in stats.ranks if r.rank != 0]
    if not workers:
        return []
    lo = min(workers, key=lambda r: (r.num_dirs, r.rank))
    hi = max(workers, key=lambda r: (r.num_dirs, -r.rank))
    mean = sum(r.num_dirs for r in workers) // len(workers)
    rank_seconds = walk_elapsed * len(workers)
    waited = sum(r.wait_time for r in workers)
    return ['--> Directories per rank: min {} (rank {}) / mean {} / max {} (rank {}); local queue high-water mark: {}'.format(
                format_number(lo.num_dirs), lo.rank, format_number(mean), format_number(hi.num_dirs), hi.rank,
                format_number(max(r.max_local_dirs for r in workers))),
            '--> Workers waited for the Manager {:.1%} of the walk ({:,.1f} of {:,.1f} rank-seconds)'.format(
                waited / rank_seconds if rank_seconds else 0.0, waited, rank_seconds)]
