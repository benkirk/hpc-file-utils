"""IndexWriter: an index file a rank writes with --index (the sweep's Part C): one line of
metadata per entry, TSV, written by every rank in parallel with no coordination.

  walkstat   index-r{rank}.tsv (index-<prefix>-r{rank}.tsv with --summary-prefix): every
             entry the rank counted, at walk speed, like a policy-engine list or find -ls
  walktar    <tar>.idx beside each tar: every member with its offsets, so one member can
             be found or extracted without reading the whole tar
  walksync   index-r{rank}.tsv: every entry the rank's rsyncs copied (sync/worker.py),
             with dest= (and dry-run=yes for a dry run) in the header

The rule: parents before children, in every file. Before any record, every ancestor
directory up to its --dirs root has a record earlier in the *same* file, so a reader never
meets a child before its parent and each file imports on its own, in any order or in
parallel (cat index-*.tsv keeps the property). Two kinds of record, the first column:
  E  an entry: counted (walkstat) or archived (walktar) here, and exactly once across the
     whole set of files, by the guarantees the reports and the tars rely on
  P  a parent: a structural record that keeps the file closed, repeated across files (and
     batches) as needed, one lstat each. An importer uses it for the hierarchy and skips
     it for totals. The --dirs roots and the directories that fail a selecting filter
     appear only as P in walkstat's index (count() never sees them); walktar archives
     every directory it walks in a dirs tar, so there they are E, with offsets.

The written cache: a writer remembers the directories it has recorded (E or P), and before
each record walks up from the entry's parent to the first one remembered (or the root),
writing the missing ancestors top-down as P. Only a cache: a forgotten parent costs one
repeated P line, a false positive (a parent believed written that was not) would break the
rule, so it holds the paths themselves (no hashing, no Bloom filter), evicts the least
recently used past WRITTEN_CACHE_MAX and is forgotten when the file closes. Walks are
depth-first and a directory's batches are processed close together, so a small cache
catches nearly every repeat; without the cap walkstat's one file per rank would keep every
directory the rank ever touched. HFU_INDEX_CACHE_MAX in the environment sets the cap (the
tests set it to 1, so every record's parents are re-derived and the P repeats happen).

Format: a versioned header, then one record per line (the column set is fixed at v1, for
every tool: docs/HANDOFF-sweep.md 4.8):
  # hfu-index v1<TAB>tool=walkstat<TAB>columns=rec,type,...,path<TAB>root=<root>[<TAB>root=...][<TAB>key=value...]
  rec type size alloc mode uid gid nlink ino atime mtime ctime [offset offset_data] path
type is one of f d l p s b c ('?' otherwise), alloc st_blocks*512, mode the permission
bits in octal, the times integer epochs; nlink and ino let an importer find the hard
links. walktar adds offset and offset_data, the member's header and data positions in its
tar. A P record of a directory that vanished has '-' in every stat field, and walktar's P
records in both offsets. The path is last, as walked, so sort and cut work; it is
encoded reversibly by encode_path() (formatting.printable() is lossy): '\\' as '\\\\',
newline, tab and return as '\\n' '\\t' '\\r', other control characters and undecodable bytes
as '\\xNN'. decode_path() reverses it. read_index() parses a file, and the checks below
(closure, exactly once, completeness against find) run as
  python -m hpc_file_utils.walk.common.index [--find] index-*.tsv

--index=gz writes the same records through gzip, '.gz' on the name (index-r001.tsv.gz,
output-r001-b00003.tar.idx.gz): one gzip stream per file, at level 6 (gzip's own default;
Python's gzip.open() defaults to 9, ~4x the CPU for ~4% less). The readers below take
either, by the file's first two bytes, whatever its name; so does zcat, and cat of .gz
files is still one valid gzip file, closed under ancestry as before. A rank killed mid-file
leaves a gzip stream without its end, which a reader refuses (EOFError) where a plain file
would end in a partial line.
"""

import gzip
import os
import re
import stat
import subprocess
import sys
from collections import OrderedDict
from typing import NamedTuple

from hpc_file_utils.walk.common.worker import RankFile

VERSION = 1
HEADER = f'# hfu-index v{VERSION}'
STAT_COLUMNS = ('type', 'size', 'alloc', 'mode', 'uid', 'gid', 'nlink', 'ino', 'atime', 'mtime', 'ctime')
OFFSET_COLUMNS = ('offset', 'offset_data')
WRITTEN_CACHE_MAX = int(os.environ.get('HFU_INDEX_CACHE_MAX', 100_000))
FORMATS = ('tsv', 'gz')          # --index's values: --index alone is tsv
GZIP_SUFFIX = '.gz'
GZIP_LEVEL = 6
GZIP_MAGIC = b'\x1f\x8b'
RANK_INDEX = 'index-r{:03d}.tsv'              # walkstat's and walksync's per-rank index
PREFIXED_RANK_INDEX = 'index-{}-r{:03d}.tsv'  # ... with --summary-prefix

TYPE_LETTERS = {stat.S_IFREG: 'f', stat.S_IFDIR: 'd', stat.S_IFLNK: 'l', stat.S_IFIFO: 'p',
                stat.S_IFSOCK: 's', stat.S_IFBLK: 'b', stat.S_IFCHR: 'c'}

# the characters encode_path() escapes: backslash, C0 controls and DEL, and the surrogates
# os.fsdecode() makes of undecodable bytes
_ESCAPE_RE = re.compile('[\\\\\x00-\x1f\x7f\udc80-\udcff]')
_ESCAPES = {'\\': '\\\\', '\n': '\\n', '\t': '\\t', '\r': '\\r'}
_UNESCAPE_RE = re.compile(rb'\\(x[0-9a-fA-F]{2}|[\\ntr])')
_UNESCAPES = {b'\\': b'\\', b'n': b'\n', b't': b'\t', b'r': b'\r'}


def _escape(m):
    c = m.group()
    return _ESCAPES.get(c) or ''.join(f'\\x{b:02x}' for b in c.encode('utf-8', 'surrogateescape'))


def add_index_argument(parser, help):
    """The --index option of a tool: --index (plain TSV) or --index=gz (the same through
    gzip); options.index is None without it, else 'tsv' or 'gz'."""
    parser.add_argument('--index', nargs='?', const='tsv', default=None, choices=FORMATS, metavar='gz',
                        help=help + '. --index=gz writes the same through gzip, ".gz" on each name')


def rank_index_name(prefix, rank):
    """A per-rank index's name (walkstat, walksync), before any '.gz': index-r{rank}.tsv,
    or index-<prefix>-r{rank}.tsv with --summary-prefix."""
    return PREFIXED_RANK_INDEX.format(prefix, rank) if prefix else RANK_INDEX.format(rank)


class IndexStat(NamedTuple):
    """What a record needs of an lstat, named as os.stat_result's fields: small to carry in
    a batch that a share may pickle to another rank (walksync's copy batches)."""
    st_mode     : int
    st_size     : int
    st_blocks   : int
    st_uid      : int
    st_gid      : int
    st_nlink    : int
    st_ino      : int
    st_atime_ns : int
    st_mtime_ns : int
    st_ctime_ns : int


def index_stat(s):
    """An lstat result as an IndexStat."""
    return IndexStat(s.st_mode, s.st_size, s.st_blocks, s.st_uid, s.st_gid, s.st_nlink, s.st_ino,
                     s.st_atime_ns, s.st_mtime_ns, s.st_ctime_ns)


def encode_path(path):
    """A path as one line of valid UTF-8 text without tabs, reversibly (decode_path())."""
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    return _ESCAPE_RE.sub(_escape, path)


def _unescape(m):
    s = m.group(1)
    return _UNESCAPES.get(s) or bytes([int(s[1:], 16)])


def decode_path(text):
    """The path encode_path() wrote, as os.scandir() gives it (undecodable bytes as surrogates)."""
    return os.fsdecode(_UNESCAPE_RE.sub(_unescape, text.encode('utf-8', 'surrogateescape')))


class IndexWriter(RankFile):
    """One index file (the module docstring): add() writes an entry's E record after the P
    records of the ancestors the file lacks. Opened at its first record, as a RankFile, or
    by open() for a file that must exist even empty (walktar's, beside its tar); close()
    forgets the written cache. how is --index's value: 'gz' writes name + '.gz' through
    gzip, anything else name as plain text. fields are more (key, value) pairs for the
    header, after the roots (walksync's dest= and dry-run=)."""

    def __init__(self, name, tool, roots, offsets=False, cache_max=None, how='tsv', fields=()):
        self.gz = how == 'gz'
        super().__init__(name + GZIP_SUFFIX if self.gz else name)
        self.fields = tuple(fields)
        self.tool = tool
        # as the walk names them: os.path.join() gives root/name even for a root given as 'root/'
        self.roots = frozenset(r.rstrip('/') or '/' for r in roots)
        self.offsets = offsets
        self.cache_max = WRITTEN_CACHE_MAX if cache_max is None else cache_max
        self.written = OrderedDict()   # directory paths recorded in this file, least recently used first
        self.columns = ('rec',) + STAT_COLUMNS + (OFFSET_COLUMNS if offsets else ()) + ('path',)

    def open(self):
        """Open the file and write its header, unless done already (write() does it at the
        first record)."""
        if self.file is None:
            self.file = (gzip.open(self.name, 'wt', encoding='utf-8', compresslevel=GZIP_LEVEL) if self.gz
                         else open(self.name, 'w', encoding='utf-8'))
            roots = ''.join(f'\troot={encode_path(r)}' for r in sorted(self.roots))
            fields = ''.join(f'\t{k}={v}' for k, v in self.fields)
            self.file.write(f'{HEADER}\ttool={self.tool}\tcolumns={",".join(self.columns)}{roots}{fields}\n')

    def write(self, text):
        self.open()
        self.file.write(text)

    def close(self):
        super().close()
        self.written.clear()

    def add(self, path, statinfo, offsets=None):
        """Record one entry, counted or archived here: its E line, after the P lines of any
        ancestor this file lacks. statinfo is its lstat, the found one (before any read);
        offsets is walktar's (offset, offset_data) of its member. Any object with
        os.stat_result's field names will do (an IndexStat)."""
        if path not in self.roots:
            parent = os.path.dirname(path)
            if parent in self.written:
                self.written.move_to_end(parent)
            else:
                self.parents(parent)
        self.record('E', path, statinfo, offsets)
        if stat.S_ISDIR(statinfo.st_mode):
            self.remember(path)

    def parents(self, parent):
        """P records, top-down, for 'parent' and its ancestors up to the first one this file
        holds or the --dirs root, one lstat each ('-' fields for one that vanished)."""
        chain = []
        d = parent
        while d not in self.written:
            chain.append(d)
            if d in self.roots:
                break
            up = os.path.dirname(d)
            if not up or up == d:    # the top: a path that was not walked from a root
                break
            d = up
        for d in reversed(chain):
            try:
                statinfo = os.lstat(d)
            except OSError:
                statinfo = None
            self.record('P', d, statinfo, None)
            self.remember(d)

    def remember(self, path):
        """A directory recorded: into the written cache, the least recently used one out past the cap."""
        written = self.written
        written[path] = None
        written.move_to_end(path)
        if len(written) > self.cache_max:
            written.popitem(last=False)

    def record(self, rec, path, statinfo, offsets):
        if statinfo is None:                     # a vanished directory's P
            fields = 'd\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-'
        else:
            m = statinfo.st_mode
            fields = (f'{TYPE_LETTERS.get(stat.S_IFMT(m), "?")}\t{statinfo.st_size}\t{statinfo.st_blocks * 512}\t'
                      f'{m & 0o7777:04o}\t{statinfo.st_uid}\t{statinfo.st_gid}\t{statinfo.st_nlink}\t{statinfo.st_ino}\t'
                      f'{statinfo.st_atime_ns // 1_000_000_000}\t{statinfo.st_mtime_ns // 1_000_000_000}\t'
                      f'{statinfo.st_ctime_ns // 1_000_000_000}')
        if self.offsets:
            o = '-\t-' if offsets is None else f'{offsets[0]}\t{offsets[1]}'
            self.write(f'{rec}\t{fields}\t{o}\t{encode_path(path)}\n')
        else:
            self.write(f'{rec}\t{fields}\t{encode_path(path)}\n')


# ---------------------------------------------------------------------------
# reading and checking
# ---------------------------------------------------------------------------

class Record(NamedTuple):
    """One index line, parsed: ints (None for '-'), mode from its octal, the path decoded."""
    rec    : str
    type   : str
    size   : int | None
    alloc  : int | None
    mode   : int | None
    uid    : int | None
    gid    : int | None
    nlink  : int | None
    ino    : int | None
    atime  : int | None
    mtime  : int | None
    ctime  : int | None
    path   : str
    offset      : int | None = None   # walktar only
    offset_data : int | None = None


class Header(NamedTuple):
    tool    : str
    columns : tuple
    roots   : frozenset
    fields  : dict = {}   # the header's other key=value fields, as written (walksync: dest, dry-run)


def _int(s):
    return None if s == '-' else int(s)


def _mode(s):
    return None if s == '-' else int(s, 8)


_PARSERS = {'mode': _mode, 'path': decode_path, 'rec': str, 'type': str}


def read_header(line):
    """The Header of an index file's first line (ValueError if it is not one)."""
    parts = line.rstrip('\n').split('\t')
    if parts[0] != HEADER:
        raise ValueError(f'not an hfu-index v{VERSION} file: {line[:40]!r}')
    fields = [p.split('=', 1) for p in parts[1:]]
    tool = next(v for k, v in fields if k == 'tool')
    columns = tuple(next(v for k, v in fields if k == 'columns').split(','))
    roots = frozenset(decode_path(v) for k, v in fields if k == 'root')
    others = {k: v for k, v in fields if k not in ('tool', 'columns', 'root')}
    return Header(tool, columns, roots, others)


def open_index(name):
    """An index file opened for reading as text: through gzip if it starts with gzip's
    magic (--index=gz), plain otherwise, whatever its name."""
    with open(name, 'rb') as f:
        gz = f.read(2) == GZIP_MAGIC
    return gzip.open(name, 'rt', encoding='utf-8') if gz else open(name, encoding='utf-8')


def read_index(name):
    """(header, records) of one index file, plain or gzip: the Header and a generator of its Records."""
    f = open_index(name)
    header = read_header(f.readline())
    parsers = [_PARSERS.get(c, _int) for c in header.columns]

    def records():
        with f:
            for line in f:
                values = line.rstrip('\n').split('\t')
                yield Record(**{c: p(v) for c, p, v in zip(header.columns, parsers, values)})
    return header, records()


def closure_errors(name):
    """The records of one file whose parent (up to a --dirs root) has no record earlier in
    it, as messages; [] if the file is closed under ancestry. Also a record that is
    neither E nor P."""
    header, records = read_index(name)
    seen = set()
    errors = []
    for n, r in enumerate(records, 2):
        if r.rec not in ('E', 'P'):
            errors.append(f'{name}:{n}: record kind {r.rec!r}')
        if r.path not in header.roots and os.path.dirname(r.path) not in seen:
            errors.append(f'{name}:{n}: {r.rec} {r.path!r} before its parent')
        if r.type == 'd':
            seen.add(r.path)
    return errors


def entries(names):
    """The E paths of these files, and the ones recorded more than once across them."""
    paths, repeated = set(), set()
    for name in names:
        _, records = read_index(name)
        for r in records:
            if r.rec == 'E':
                if r.path in paths:
                    repeated.add(r.path)
                paths.add(r.path)
    return paths, repeated


def find(root, mindepth):
    """find root -mindepth N -print0: every path below it (and the root itself with 0)."""
    out = subprocess.run(['find', root, '-mindepth', str(mindepth), '-print0'],
                         capture_output=True, check=True).stdout
    return {os.fsdecode(p) for p in out.split(b'\0') if p}


def main(argv=None):
    """Check index files: closure per file, E exactly once across them, and with --find the
    E set against find's listing of the header's roots (walkstat: below the roots, as
    Total Count; walktar: the roots too, as the tars). Exit 1 on any failure."""
    argv = sys.argv[1:] if argv is None else argv
    check_find = '--find' in argv
    names = [a for a in argv if a != '--find']
    failed = False
    tool, roots = None, set()
    ne = np = 0
    for name in names:
        header, records = read_index(name)
        tool, roots = header.tool, roots | header.roots
        counts = {'E': 0, 'P': 0}
        for r in records:
            counts[r.rec] = counts.get(r.rec, 0) + 1
        ne, np = ne + counts['E'], np + counts['P']
        errors = closure_errors(name)
        print(f'{name}: {counts["E"]} E, {counts["P"]} P, {"closed" if not errors else f"{len(errors)} NOT CLOSED"}')
        for e in errors[:10]:
            print('  ' + e)
        failed |= bool(errors)
    paths, repeated = entries(names)
    print(f'{len(names)} files: {ne} E records, {np} P records, {len(paths)} distinct E paths, '
          f'{len(repeated)} repeated')
    failed |= bool(repeated)
    for p in sorted(repeated)[:10]:
        print(f'  repeated: {p!r}')
    if check_find and names:
        expected = set()
        for root in sorted(roots):
            expected |= find(root, 0 if tool == 'walktar' else 1)
        missing, extra = expected - paths, paths - expected
        print(f'find: {len(expected)} entries, {len(missing)} missing from the index, {len(extra)} extra')
        for p in sorted(missing)[:10]:
            print(f'  missing: {p!r}')
        for p in sorted(extra)[:10]:
            print(f'  extra: {p!r}')
        failed |= bool(missing or extra)
    print('FAILED' if failed else 'OK')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
