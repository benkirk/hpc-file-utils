"""The end-of-walk reports, written by rank 0 from the combined WalkStats (no MPI).

  summary-<prefix>             totals, type counts, users, groups, the largest files and
                               directories (published by the scans as <dir>/.usage/summary.txt)
  userlists/<prefix>/<user>    the same lists for each user above the thresholds
                               (published as <dir>/.usage/users/)

Plain text, one entry per line: paths go through printable(), so a name with a newline
or undecodable bytes still takes one line of valid UTF-8. Column order (size, items,
last access, path) is what scans/sort_results.sh relies on.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from hpc_file_utils.walk.common.formatting import (format_date, format_number, format_size,
                                                   printable)
from hpc_file_utils.walk.common.stats import sorted_counts, sorted_ids

LIST_LENGTH = 250     # entries printed per list (the heaps hold --heap-size)

SEP = '#' + '-' * 79

EXPLAINER = '''\
# In the lists that follow
#   - For directories:
#     - The sizes shown include direct contents within the directory only,
#       and are not inclusive of any subdirectories (NOT recursive).
#     - The date shown is the most recent access time of any file content within
#       the directory (again, NOT recursive), or '-' if it holds no files.
#   - For files, the date shown corresponds to last access time.'''

DIR_LISTS = (
    ('nbytes_dirs',      'Largest Directories'),
    ('nbytes_dirs_3yrs', 'Largest Directories, last access [3-5yrs)'),
    ('nbytes_dirs_5yrs', 'Largest Directories, last access [5-7yrs)'),
    ('nbytes_dirs_7yrs', 'Largest Directories, last access [7yrs+)'),
    ('nitems_dirs',      'Bloated Directories (file count)'),
)
FILE_LISTS = (
    ('nbytes_files',     'Largest Files'),
)

DIR_ROW = '{:>11}  {:>12}  {:<11}  {}'
FILE_ROW = '{:>11}  {:<11}  {}'


def _header(title):
    return ['', SEP, '# ' + title, SEP]


def _columns(row, *names):
    """A '#' comment line naming the columns of 'row'."""
    line = row.format(*names)
    return '#' + line[1:] if line.startswith(' ') else '# ' + line


def dir_lines(heap, title, count=LIST_LENGTH):
    if heap.empty(): return []
    lines = _header(title + ':') + [_columns(DIR_ROW, 'size', 'items', 'last access', 'path')]
    for _, de in heap.top(count):
        lines.append(DIR_ROW.format(format_size(de.nbytes), format_number(de.nitems),
                                    format_date(de.max_atime), printable(de.path.rstrip('/')) + '/'))
    return lines


def file_lines(heap, title, count=LIST_LENGTH):
    if heap.empty(): return []
    lines = _header(title + ':') + [_columns(FILE_ROW, 'size', 'last access', 'path')]
    for _, fe in heap.top(count):
        lines.append(FILE_ROW.format(format_size(fe.nbytes), format_date(fe.atime), printable(fe.path)))
    return lines


def above_threshold(counts, options):
    return counts.nitems >= options.threshold_count or counts.nbytes >= options.threshold_size


def id_lines(ids, label, options):
    rows = [c for c in sorted_ids(ids) if above_threshold(c, options)]
    width = max([12] + [len(printable(str(c.name))) for c in rows])
    row = '{:>%d}  {:>11}  {:>12}' % width
    lines = _header(label + ':') + [_columns(row, label.lower().rstrip('s'), 'size', 'items')]
    for c in rows:
        lines.append(row.format(printable(str(c.name)), format_size(c.nbytes), format_number(c.nitems)))
    return lines


def scanned_line(dirs):
    dirs = [d.rstrip('/') or '/' for d in dirs]
    if len(dirs) == 1:
        return 'Scanned:     {}'.format(printable(dirs[0]))
    try:
        parent = os.path.commonpath([os.path.abspath(d) for d in dirs])
    except ValueError:
        parent = None
    return 'Scanned:     {} directories{}'.format(format_number(len(dirs)),
                                                  ' under ' + printable(parent) if parent else '')


def summary_text(stats, options, now=None):
    now = now or datetime.now()
    lines = [SEP, '# Summary as of {}'.format(now.isoformat(sep=' ', timespec='minutes')), SEP,
             scanned_line(options.dirs),
             'Total Count: {} items'.format(format_number(stats.num_items)),
             'Total Size:  {}'.format(format_size(stats.total_size))]
    if stats.num_errors:
        lines.append('Scan Errors: {} entries could not be scanned, so these totals are incomplete'.format(
            format_number(stats.num_errors)))
    lines.append('Type Counts:')
    for k, v in sorted_counts(stats.st_modes):
        lines.append('{:>8} : {:>12}'.format(k, format_number(v)))

    lines += id_lines(stats.uids, 'Users', options)
    lines += id_lines(stats.gids, 'Groups', options)
    lines += ['', SEP, EXPLAINER, SEP]
    for name, title in DIR_LISTS:
        lines += dir_lines(stats.heaps[name], title)
    for name, title in FILE_LISTS:
        lines += file_lines(stats.heaps[name], title)
    return '\n'.join(lines) + '\n'


def userlist_text(stats, counts, options, now=None):
    """One user's lists; counts: that user's UIDCounts."""
    now = now or datetime.now()
    uid, name = counts.id, printable(str(counts.name))
    lines = [SEP, '# Summary as of {}'.format(now.isoformat(sep=' ', timespec='minutes')), SEP,
             'User:        {}'.format(name),
             'Total Count: {} items'.format(format_number(counts.nitems)),
             'Total Size:  {}'.format(format_size(counts.nbytes)),
             '', SEP, EXPLAINER, SEP]
    for heap_name, title in DIR_LISTS:
        heap = stats.user_heaps[heap_name].get(uid)
        if heap is not None:
            lines += dir_lines(heap, '{} - {}'.format(name, title))
    for heap_name, title in FILE_LISTS:
        heap = stats.user_heaps[heap_name].get(uid)
        if heap is not None:
            lines += file_lines(heap, '{} - {}'.format(name, title))
    return '\n'.join(lines) + '\n'


def rank_lines(stats):
    """--verbose: each rank's own share of the walk."""
    hosts = max([4] + [len(r.host) for r in stats.ranks])
    row = '{:>6}  {:<%d}  {:>12}  {:>12}  {:>11}  {:>9}  {:>9}' % hosts
    lines = [SEP, _columns(row, 'rank', 'host', 'dirs', 'files', 'size', 'waited s', 'max stack')]
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


def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def write_reports(stats, options, now=None):
    """Write summary-<prefix> (and echo it), userlists/ and, if asked, the spreadsheet."""
    now = now or datetime.now()

    text = summary_text(stats, options, now)
    _write('summary-{}'.format(options.summary_prefix or 'output'), text)
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode('utf-8'))   # whatever the locale's encoding
    sys.stdout.buffer.flush()

    for counts in sorted_ids(stats.uids):
        if above_threshold(counts, options):
            # a name is a path component here: keep it to one
            fname = printable(str(counts.name)).replace('/', '_')
            _write(os.path.join('userlists', options.summary_prefix or '', fname),
                   userlist_text(stats, counts, options, now))

    if options.spreadsheet:
        from hpc_file_utils.walk.stat.spreadsheet import write_spreadsheet
        print('\n--> Writing summary to {}'.format(options.spreadsheet))
        write_spreadsheet(stats, options)
