"""FindWorker: walkfind's worker. Lists the entries that match, like find.

  scan     a directory: one scandir (names and d_type, no stat). Every entry scanned counts
           as walked. --name is tested on the name: a non-match is never stat'ed. A matched
           directory is lstat'ed here (as walkstat's scan stats subdirectories) and listed if
           the selecting filters pass; with --prune-matches it is not walked, otherwise it is,
           like every other directory. Matched non-directories go into find batches of
           --batch-files, FindBatch(dirname, names), which a 'share' can hand to idle ranks.
  find     a FindBatch: lstat each name, apply the selecting filters, and list it.

A listed entry goes into this rank's list, walkfind-r{rank}.list in the working directory
(one printable() line each, or raw bytes and a NUL with --print0), and with --index into
its index, and is counted by type and bytes (st_size) in stats.counters. The walk's
progress line counts entries walked and the matches' bytes.

A find batch creates nothing, so the termination argument holds (manager.py).
"""

import os
import re
import stat
from typing import NamedTuple

from hpc_file_utils.walk.common.filters import TYPES
from hpc_file_utils.walk.common.formatting import printable
from hpc_file_utils.walk.common.index import IndexWriter, rank_index_name
from hpc_file_utils.walk.common.worker import BaseWorker
from hpc_file_utils.walk.find.tool import LIST_FILE

TYPE_LETTERS = {mode: letter for letter, mode in TYPES.items()}


class FindBatch(NamedTuple):
    """One directory's non-directory names that matched --name, still to lstat."""
    dirname : str
    names   : list


class FindWorker(BaseWorker):

    def __init__(self):
        super().__init__()
        o = self.options
        self.name_match = re.compile(o.name_regex).match if o.name_regex else None
        self.prune = o.prune_matches
        self.print0 = o.print0
        self.list = self.rank_file(LIST_FILE, binary=o.print0)
        self.index = None
        if o.index:
            self.index = IndexWriter(rank_index_name(o.summary_prefix, self.rank), 'walkfind', o.dirs, how=o.index)
            self.files.append(self.index)

    def process_directory(self, dirname):
        if self.vanished(dirname):
            return

        st = self.stats
        match = self.name_match
        nbatch = self.options.batch_files
        st.num_dirs += 1
        names = []
        try:
            for di in self.entries(dirname):
                st.num_items += 1
                matched = match is None or match(di.name)
                if di.is_dir(follow_symlinks=False):      # d_type: no stat
                    if matched:
                        self.found_dir(di)
                        if self.prune:
                            st.counters['pruned'] += 1
                            continue
                    self.push_dir(di.path)
                elif matched:
                    names.append(di.name)
                    if len(names) >= nbatch:
                        self.local_batches.append(FindBatch(dirname, names))
                        names = []
        except Exception as error:
            self.error('scan', error)

        if names:
            self.local_batches.append(FindBatch(dirname, names))

    def found_dir(self, di):
        """A directory that matched --name: its lstat, then found()."""
        try:
            statinfo = di.stat(follow_symlinks=False)
        except OSError as error:
            self.error('stat', error)
            return
        self.found(di.path, statinfo)

    def process_batch(self, batch):
        """lstat a FindBatch's names, ours or shared, and list the ones the filters select."""
        join = os.path.join
        for name in self.ticked(batch.names):
            path = join(batch.dirname, name)
            try:
                statinfo = os.lstat(path)
            except OSError as error:
                self.error('stat', error)
                continue
            self.found(path, statinfo)

    def found(self, path, statinfo):
        """An entry that matched --name (or every entry, without it): listed and counted if
        the selecting filters pass."""
        st = self.stats
        if self.select is not None and not self.select(statinfo):
            st.num_unmatched += 1
            return
        c = st.counters
        c['found'] += 1
        c['found_' + TYPE_LETTERS.get(stat.S_IFMT(statinfo.st_mode), '?')] += 1
        c['found_bytes'] += statinfo.st_size
        st.total_size += statinfo.st_size
        if self.print0:
            self.list.write(os.fsencode(path) + b'\0')
        else:
            self.list.write(printable(path) + '\n')
        if self.index is not None:
            self.index.add(path, statinfo)
