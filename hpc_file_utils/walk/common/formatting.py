"""Text formatting shared by the progress line and the reports (no MPI)."""

import os
import re
from datetime import datetime

import humanfriendly


def format_size(val):
    return humanfriendly.format_size(val, binary=True)


def format_number(val):
    return humanfriendly.format_number(val)


def format_timespan(val):
    return humanfriendly.format_timespan(val)


def format_date(timestamp):
    """YYYY-MM-DD (local time); '-' for a directory with no files (its max time is -1)."""
    if timestamp is None or timestamp < 0:
        return '-'
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')


_CONTROL_RE = re.compile('[\x00-\x1f\x7f-\x9f]')
_ESCAPES = {'\n': '\\n', '\t': '\\t', '\r': '\\r'}


def _escape(m):
    c = m.group()
    return _ESCAPES.get(c) or f'\\x{ord(c):02x}'


def printable(path):
    """A path as one line of valid UTF-8 text, for the reports.

    os.scandir() hands back undecodable bytes as surrogates, which a UTF-8 file cannot
    hold: they are shown as \\xNN of the original bytes. Control characters (a newline
    in a name would split a list line in two) are shown as \\n, \\t, \\r or \\xNN.
    """
    if isinstance(path, bytes):
        text = path.decode('utf-8', 'backslashreplace')
    else:
        try:
            path.encode('utf-8')
            text = path
        except UnicodeEncodeError:
            text = os.fsencode(path).decode('utf-8', 'backslashreplace')
    return _CONTROL_RE.sub(_escape, text)
