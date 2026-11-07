"""walkstat --spreadsheet: the summary's lists as an .xlsx workbook (pandas + openpyxl, no MPI).

Three sheets, '<prefix> - Directories', '<prefix> - Files' and '<prefix> - Users & Groups',
added to (or replaced in) an existing workbook, so several scans can share one file.
"""

import os

from hpc_file_utils.walk.common.formatting import format_size, printable
from hpc_file_utils.walk.common.stats import sorted_ids


def write_spreadsheet(stats, options):
    import pandas as pd

    path = options.spreadsheet
    heap_size = options.heap_size
    sheet_prefix = '{} - '.format(options.summary_prefix) if options.summary_prefix else ''
    mode = 'a' if os.path.exists(path) else 'w'
    if_sheet_exists = None if 'w' == mode else 'replace'

    with pd.ExcelWriter(path, mode=mode, if_sheet_exists=if_sheet_exists) as writer:
        # directories: the largest and the most crowded
        entries = [de for _, de in stats.heaps['nitems_dirs'].top(heap_size)]
        entries += [de for _, de in stats.heaps['nbytes_dirs'].top(heap_size)]
        entries = sorted(set(entries), key=lambda x: (-x.nitems, x.path))
        df = pd.DataFrame(entries, columns=['path', 'nbytes', 'nitems', 'max_mtime', 'max_ctime', 'max_atime'])
        df['path'] = df['path'].map(printable)   # openpyxl refuses control characters
        for ts in ['max_mtime', 'max_ctime', 'max_atime']: df[ts] = pd.to_datetime(df[ts], unit='s')
        df['size'] = df['nbytes'].map(format_size)
        # drop ctime: it is likely to confuse
        df = df[['path', 'nitems', 'size', 'nbytes', 'max_mtime', 'max_atime']]
        df.to_excel(writer, sheet_name=sheet_prefix + 'Directories', index=False)

        # files
        entries = sorted({fe for _, fe in stats.heaps['nbytes_files'].top(heap_size)},
                         key=lambda x: (-x.nbytes, x.path))
        df = pd.DataFrame(entries, columns=['path', 'nbytes', 'mtime', 'ctime', 'atime'])
        df['path'] = df['path'].map(printable)
        for ts in ['mtime', 'ctime', 'atime']: df[ts] = pd.to_datetime(df[ts], unit='s')
        df['size'] = df['nbytes'].map(format_size)
        df = df[['path', 'size', 'nbytes', 'mtime', 'atime']]
        df.to_excel(writer, sheet_name=sheet_prefix + 'Files', index=False)

        # users, then groups, on one sheet
        rows = [{'username': c.name, 'groupname': None, 'size': format_size(c.nbytes),
                 'nbytes': c.nbytes, 'nitems': c.nitems} for c in sorted_ids(stats.uids)]
        rows += [{'username': None, 'groupname': c.name, 'size': format_size(c.nbytes),
                  'nbytes': c.nbytes, 'nitems': c.nitems} for c in sorted_ids(stats.gids)]
        df = pd.DataFrame(rows, columns=['username', 'groupname', 'size', 'nbytes', 'nitems'])
        df.to_excel(writer, sheet_name=sheet_prefix + 'Users & Groups', index=False)

    _format_workbook(path)


def _format_workbook(path):
    """Fixed-width font, column widths and number formats; hide the raw byte counts."""
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font

    wb = load_workbook(path)
    for ws in wb.worksheets:
        ws.freeze_panes = 'A2'
        for column in ws.columns:
            label = str(column[0].value or '')
            max_length = 0
            for cell in column:
                max_length = max(max_length, len(str(cell.value)) if cell.value is not None else 0)
                cell.font = Font(name='Courier')
                if 'size' in label:
                    cell.alignment = Alignment(horizontal='right')
                if 'nitems' in label:
                    cell.number_format = '#,###'
                if 'time' in label:
                    cell.number_format = 'yyyy-mm-dd h:mm:ss'

            letter = column[0].column_letter
            width = (max_length + 2) * 1.2
            ws.column_dimensions[letter].width = width
            if 'time' in label:
                ws.column_dimensions[letter].width = 20
            elif 'path' in label:
                ws.column_dimensions[letter].width = min(width, 140)
            elif 'nbytes' in label:
                ws.column_dimensions[letter].hidden = True

            # column label formatting
            column[0].font = Font(bold=True, size=14)
            column[0].alignment = Alignment(horizontal='center')
    wb.save(path)
