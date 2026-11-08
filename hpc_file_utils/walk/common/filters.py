"""Filters for the walk (phase 4): find-like, compiled once on rank 0 (no MPI).

Two kinds, which behave differently:

  pruning    --exclude GLOB      tested on an entry's name, before its stat: a match is never
                                 stat'ed, counted, descended or archived (find's
                                 '-name GLOB -prune'). The --dirs roots are never tested.
  selecting  --{m,a,c}time-{older,newer} WHEN, --user, --group, --min-size, --max-size,
             --type              tested on the stat already fetched (no extra system calls),
                                 on every entry, directories included. They decide what is
                                 counted (walkstat) or archived (walktar's files), never what
                                 is descended: a directory that fails can hold entries that pass.

Kinds AND together; the values of one repeatable kind (--user a,b --user c) OR together.

compile_filters() resolves everything on rank 0 (names to ids, ages and dates to absolute
epochs, sizes to bytes, globs to one regex) into a picklable Filters, which reaches the
workers inside the options by bcast. Each rank then builds its callables once from it
(exclude_matcher(), predicate()); with no filter the options carry None, and the hot loop
pays only an 'is None' test per entry.
"""

import fnmatch
import grp
import pwd
import re
import shlex
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime

SECONDS_IN_YEAR = 60*60*24*365   # as common/worker.py
AGE_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 7*86400, 'y': SECONDS_IN_YEAR}
AGE_RE = re.compile(r'^(\d+(?:\.\d+)?)([smhdwy]?)$')

TYPES = {'f': stat.S_IFREG, 'd': stat.S_IFDIR, 'l': stat.S_IFLNK, 'p': stat.S_IFIFO,
         's': stat.S_IFSOCK, 'b': stat.S_IFBLK, 'c': stat.S_IFCHR}

TIME_FIELDS = ('mtime', 'atime', 'ctime')

WHEN_HELP = ('an age, N[smhdwy] (a bare N is days; y = 365 days), or a local date, '
             'YYYY-MM-DD[THH:MM[:SS]]')


@dataclass
class Filters:
    """The active filters, resolved; plain data, so it pickles (closures do not)."""
    exclude: list = field(default_factory=list)   # globs, as given
    exclude_regex: str = None                     # their union, for re.match on a name
    times: dict = field(default_factory=dict)     # 'mtime' -> [newer_than, older_than] epochs (None: open)
    uids: frozenset = None
    gids: frozenset = None
    min_size: int = None
    max_size: int = None
    types: frozenset = None                       # S_IFMT values
    described: list = field(default_factory=list) # one item per option, for the reports

    @property
    def selects(self):
        """True if any selecting filter is active."""
        return bool(self.times) or any(v is not None for v in
                                       (self.uids, self.gids, self.min_size, self.max_size, self.types))

    def describe(self):
        """The active filters, the way they were given (plus the resolved cutoffs)."""
        return '  '.join(self.described)

    def exclude_matcher(self):
        """name -> truthy if excluded, or None without --exclude."""
        return re.compile(self.exclude_regex).match if self.exclude_regex else None

    def predicate(self):
        """statinfo -> bool, True for an entry that is counted; None without a selecting filter."""
        tests = []
        for name, (newer, older) in self.times.items():
            lo = float('-inf') if newer is None else newer
            hi = float('inf') if older is None else older
            attr = 'st_' + name
            tests.append(lambda s, a=attr, lo=lo, hi=hi: lo < getattr(s, a) < hi)
        if self.uids is not None:
            tests.append(lambda s, ids=self.uids: s.st_uid in ids)
        if self.gids is not None:
            tests.append(lambda s, ids=self.gids: s.st_gid in ids)
        if self.min_size is not None or self.max_size is not None:
            lo = 0 if self.min_size is None else self.min_size
            hi = float('inf') if self.max_size is None else self.max_size
            tests.append(lambda s, lo=lo, hi=hi: lo <= s.st_size <= hi)
        if self.types is not None:
            tests.append(lambda s, t=self.types: stat.S_IFMT(s.st_mode) in t)

        if not tests:
            return None
        if len(tests) == 1:
            return tests[0]
        tests = tuple(tests)

        def select(s):
            for t in tests:
                if not t(s):
                    return False
            return True
        return select


def add_filter_options(parser):
    g = parser.add_argument_group('filters', 'find-like; different kinds AND together. '
                                  '--exclude prunes (never stat\'ed or descended); the others select what is '
                                  'counted (walkstat) or archived (walktar\'s files), and are tested on every '
                                  'entry, directories included, without stopping descent.')
    g.add_argument('--exclude', action='append', metavar='GLOB',
                   help='skip entries whose name matches GLOB (fnmatch, case-sensitive; repeatable): '
                        'never stat\'ed, counted, descended or archived, like find\'s -name GLOB -prune')
    for f in TIME_FIELDS:
        g.add_argument('--{}-older'.format(f), metavar='WHEN',
                       help='only entries whose {} is before WHEN: {}'.format(f, WHEN_HELP))
        g.add_argument('--{}-newer'.format(f), metavar='WHEN',
                       help='only entries whose {} is after WHEN'.format(f))
    g.add_argument('--user', action='append', metavar='U[,U...]',
                   help='only entries owned by these users (names, or uids; repeatable)')
    g.add_argument('--group', action='append', metavar='G[,G...]',
                   help='only entries in these groups (names, or gids; repeatable)')
    g.add_argument('--min-size', metavar='SIZE', help='only entries of at least SIZE bytes (st_size; e.g. 10MB, binary)')
    g.add_argument('--max-size', metavar='SIZE', help='only entries of at most SIZE bytes (st_size)')
    g.add_argument('--type', metavar='T[,T...]',
                   help='only entries of these types: f (regular file), d (directory), l (symlink), '
                        'p (fifo), s (socket), b (block device), c (character device)')
    return g


def parse_when(text, now):
    """An age or a date (see WHEN_HELP) as an absolute epoch; ValueError if neither."""
    m = AGE_RE.match(text)
    if m:
        return now - float(m.group(1)) * AGE_UNITS[m.group(2) or 'd']
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError('not an age (N[smhdwy]) or a date (YYYY-MM-DD[THH:MM[:SS]]): {!r}'.format(text))
    if when.tzinfo is not None:
        raise ValueError('give a local date, without a time zone: {!r}'.format(text))
    return when.timestamp()


def _ids(values, lookup, kind):
    """--user/--group values (comma lists, repeatable) as ids: a name first, then a number,
    as find does."""
    ids = set()
    for value in values:
        for item in value.split(','):
            item = item.strip()
            if not item:
                raise ValueError('empty {} in {!r}'.format(kind, value))
            try:
                ids.add(lookup(item))
            except KeyError:
                if not item.isdigit():
                    raise ValueError('no such {}: {}'.format(kind, item))
                ids.add(int(item))
    return frozenset(ids)


def _size(text, option):
    from humanfriendly import InvalidSize, parse_size
    try:
        return parse_size(text, binary=True)
    except InvalidSize as e:
        raise ValueError('{}: {}'.format(option, e))


def _stamp(epoch):
    return datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M')


def compile_filters(args, now=None):
    """The Filters for parsed args, or None if no filter is given.

    Raises ValueError with a usage message; parse_options() turns it into a parser error.
    """
    now = time.time() if now is None else now
    f = Filters()

    if args.exclude:
        for glob in args.exclude:
            if not glob or '/' in glob:
                raise ValueError('--exclude matches names, so GLOB cannot be empty or hold a \'/\': {!r}'.format(glob))
        f.exclude = list(args.exclude)
        f.exclude_regex = '|'.join(fnmatch.translate(g) for g in f.exclude)
        f.described += ['--exclude {}'.format(shlex.quote(g)) for g in f.exclude]

    for name in TIME_FIELDS:
        newer_text = getattr(args, name + '_newer')
        older_text = getattr(args, name + '_older')
        if newer_text is None and older_text is None:
            continue
        newer = older = None
        if older_text is not None:
            older = parse_when(older_text, now)
            f.described.append('--{}-older {} (before {})'.format(name, older_text, _stamp(older)))
        if newer_text is not None:
            newer = parse_when(newer_text, now)
            f.described.append('--{}-newer {} (after {})'.format(name, newer_text, _stamp(newer)))
        f.times[name] = [newer, older]

    if args.user:
        f.uids = _ids(args.user, lambda n: pwd.getpwnam(n).pw_uid, 'user')
        f.described.append('--user {}'.format(','.join(args.user)))
    if args.group:
        f.gids = _ids(args.group, lambda n: grp.getgrnam(n).gr_gid, 'group')
        f.described.append('--group {}'.format(','.join(args.group)))

    if args.min_size is not None:
        f.min_size = _size(args.min_size, '--min-size')
        f.described.append('--min-size {}'.format(args.min_size))
    if args.max_size is not None:
        f.max_size = _size(args.max_size, '--max-size')
        f.described.append('--max-size {}'.format(args.max_size))
    if f.min_size is not None and f.max_size is not None and f.min_size > f.max_size:
        raise ValueError('--min-size is larger than --max-size')

    if args.type is not None:
        letters = [t.strip() for t in args.type.split(',')]
        bad = [t for t in letters if t not in TYPES]
        if bad:
            raise ValueError('--type: unknown type {!r} (use {})'.format(','.join(bad), ','.join(TYPES)))
        f.types = frozenset(TYPES[t] for t in letters)
        f.described.append('--type {}'.format(','.join(letters)))

    return f if f.described else None
