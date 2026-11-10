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
from hpc_file_utils.walk.common.report import SEP, columns as _columns
from hpc_file_utils.walk.common.stats import sorted_counts, sorted_ids

LIST_LENGTH = 250     # entries printed per list (the heaps hold --heap-size)

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


def dir_lines(heap, title, count=LIST_LENGTH):
    if heap.empty():
        return []
    lines = _header(title + ':') + [_columns(DIR_ROW, 'size', 'items', 'last access', 'path')]
    for _, de in heap.top(count):
        lines.append(DIR_ROW.format(format_size(de.nbytes), format_number(de.nitems),
                                    format_date(de.max_atime), printable(de.path.rstrip('/')) + '/'))
    return lines


def file_lines(heap, title, count=LIST_LENGTH):
    if heap.empty():
        return []
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


def filter_items(stats, options, per_user=False):
    """(label, text) pairs naming the active filters (phase 4), or [] for an unfiltered walk,
    so a filtered report is never mistaken for a full one. The counts are the whole walk's,
    so a userlist gets only the filters."""
    filters = getattr(options, 'filters', None)
    if filters is None:
        return []
    items = [('Filters', filters.describe())]
    if per_user:
        return items
    if filters.exclude:
        items.append(('Excluded', '{} entries (never stat\'ed or descended)'.format(format_number(stats.num_excluded))))
    if filters.selects:
        items.append(('Unmatched', '{} entries scanned but not counted'.format(format_number(stats.num_unmatched))))
    return items


def filter_lines(stats, options, per_user=False):
    return ['{:<13}{}'.format(label + ':', text) for label, text in filter_items(stats, options, per_user)]


def summary_text(stats, options, now=None):
    now = now or datetime.now()
    lines = [SEP, '# Summary as of {}'.format(now.isoformat(sep=' ', timespec='minutes')), SEP,
             scanned_line(options.dirs)]
    lines += filter_lines(stats, options)
    lines += ['Total Count: {} items'.format(format_number(stats.num_items)),
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
             'User:        {}'.format(name)]
    lines += filter_lines(stats, options, per_user=True)
    lines += ['Total Count: {} items'.format(format_number(counts.nitems)),
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
