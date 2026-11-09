# Hand-off: the walk toolkit sweep, indexes, logging and old idioms (written 2026-11-09)

Phases 3–6 were additive. Each added a tool or a feature: local queues, filters, batches, walktar's
tars, walkstat's stat batches, walksync. Each copied the scaffolding it needed instead of
reshaping what was there. This note is the retrospective: what to consolidate now that the shape
is clear, two features that the consolidation makes easy, and the old idioms to retire. It is a
plan for a fresh session. **No code has changed yet.**

Based on PR #11 (walksync, phase 6) at `cd32aac`. Every `file:line` below is against that commit,
under `hpc_file_utils/walk/` unless a path says otherwise. The work can start once PR #11 merges,
or stack on it.

Read first:
- The module docstring of `common/manager.py`: the protocol and the termination argument.
- `hpc_file_utils/walk/README.md`: the "File batches (phase 5)" and "walksync (phase 6)" sections.
- [HANDOFF.md](HANDOFF.md), phases 5 and 6.

Contents:
1. Findings
2. Part A: the sweep (no behaviour change)
3. Part B: walktar onto stat batches (deferred: plan only)
4. Part C: optional indexes (new)
5. Part D: logging and console output
6. Part E: Pythonic cleanup, and what not to "modernize"
7. Deliberately left alone
8. Order, and how each step is checked

### Rules for all of it
- **One step, one commit.** The suite passes after every commit.
- **Golden output stays byte-identical** through Parts A, D and E: `HFU_UPDATE_GOLDEN` is never
  needed. The text that `scans/sort_results.sh`, the golden files and the tests' regexes parse is a
  contract: the summary, the userlists, the progress line and the `-->` lines.
- **The Manager's protocol and termination argument stay as they are.** Part B leans on them as
  they stand, and nothing here edits manager.py's docstring argument.
- **No new runtime dependency** without a line here saying why.

---

## 1. Findings

### 1.1 walkstat's accounting is written twice
- **Two copies of the same 40 lines.** `BaseWorker.process_directory` (`common/worker.py:45-177`)
  stats every entry and does walkstat's counting inline:
  - the min(size, blocks·512) item size
  - the uid and gid counts
  - the `st_modes` chain
  - the max times
  - the `nbytes_files` heaps

  `StatWorker.count` (`stat/worker.py:41-87`) is the same code. Since phase 5, walkstat has its own
  scan, so `BaseWorker.process_directory` serves only walktar.
- **The same data packed and unpacked twice.** `BaseWorker.record_directory` (`common/worker.py:182-190`)
  repacks seven loose locals into a `DirTotals` that `StatWorker` builds directly.

### 1.2 The scandir loop is written three times
`BaseWorker.process_directory`, `StatWorker.process_directory` (`stat/worker.py:111-178`) and
`SyncWorker.process_directory` (`sync/worker.py:136-202`) each open with the same prologue:
- the "vanished" check and its message (`common/worker.py:48`, `stat/worker.py:114`,
  `sync/worker.py:139`)
- `report_progress()` every `PROGRESS_INCREMENT` entries, and `check_share()` every
  `SHARE_CHECK_INCREMENT`
- `--exclude` by name, then `num_excluded += 1`
- `local_dirs.append(...)` and then the `local_queue_max` overflow check, at 5 sites
- the outer `except Exception` that counts and prints "Cannot scan"

The batch loops repeat the "every `SHARE_CHECK_INCREMENT` items: share, progress" tick
(`stat/worker.py:190-193`, `sync/worker.py:238-241`).

`error()` exists twice (`stat/worker.py:103-106`, `sync/worker.py:82-85`). `BaseWorker` and
`TarWorker.add` (`tar/worker.py:42-50`) count and print inline instead.

Only `BaseWorker` uses a bare `os.scandir()` without `with`, so its directory handle lives until
garbage collection.

### 1.3 Batches are untyped tuples of three shapes
- walktar: `(paths, nbytes)`
- walkstat: `(dirname, names, tail)`
- walksync: `('cmp', reldir, names)` and `('copy', paths, nbytes)`, tagged by a string.
  `SyncWorker.close_batch` (`sync/worker.py:218-223`) overrides the base only to add the tag, and
  `process_batch` dispatches on `batch[0]`.

### 1.4 No "tool" abstraction
- **`app ==` branches.** There are five `app == 'walksync'` branches in `cli.py` (lines 83, 89,
  102, 125, 130) and five `appname` branches in `common/options.py` (lines 78, 80, 98, 124, 131).
- **Tool settings carried on `args`.** These are `written_verb` and `written_units`
  (`common/options.py:154`) and `rsync_missing`. They are read back with
  `getattr(options, ..., default)` in `common/manager.py:169,334-336` and `sync/report.py:25`.
- **Code in the wrong module:**
  - The rsync capability code (`RSYNC_OPTS`, `parse_rsync_capabilities`, `rsync_capabilities`,
    `rsync_base_opts`, `common/options.py:20-48`) is walksync's, but lives in `common/`.
  - The rsync command line is built in `SyncWorker.__init__` (`sync/worker.py:64-65`).
  - `balance_lines` and `rank_lines` (`stat/report.py:169-197`) describe any walk, but live in
    walkstat's report and are imported from there by `cli.report()`.
- **Two names for the worker's tool hook.** `make_worker()` (`cli.py:13-23`) is a fourth
  `if app ==` chain.

### 1.5 Per-rank output files, opened lazily, three ways
`TarWorker.dirs_tar` (`tar/worker.py:60-62`, closed in `run()`), `SyncWorker.dryrun_file`
(`sync/worker.py:125-131`, closed in `run()`), and glade_hsm's `LazyWriter`
(`tools/glade_hsm/libexec/glade_hsm_scan_filelist.py:12`). The index (Part C) would be a fourth.

### 1.6 Smaller duplicates and leftovers
- **Duplicated code:**
  - `sync/compare.py:57-61` `show()` re-implements `common/formatting.py:44-59` `printable()`,
    which escapes every control character, not only `\n` and `\t`.
  - `SECONDS_IN_YEAR` is defined in `common/stats.py:63` and `common/filters.py:33`. The comment
    there says "as common/worker.py", which is stale.
  - The `parse_size` / `InvalidSize` → parser-error wrapper appears three times:
    `common/options.py:99-105`, `common/options.py:113-117` and `common/filters.py:163-168`.
  - In `WalkStats.merge`, the `partial_dirs` loop (`common/stats.py:177-181`) duplicates
    `add_partial_dir` (`:184-189`), and the uid/gid "add or assign" (`:162-167`) is the same
    pattern again.
- **Manager:**
  - `ndispatches` and `nexecute` (`common/manager.py:112,126`) are incremented together in
    `dispatch()`.
  - `nexecute`'s comment ("answered with a directory") is wrong: batches count too.
- **Leftovers:**
  - `for bs in [ 512 ]:` and the unused `item_nblocks` (`common/worker.py:114-116`).
  - `#print(...)` commented out (`common/worker.py:63`).
  - Shebangs on modules that are not scripts.
  - An empty `StatWorker.__init__`.
  - "Not called" `process_file` stubs (`stat/worker.py:219`, `sync/worker.py:361`).
  - `MPIClass.i_am_root` and `MPIClass.dirs` (`common/mpiclass.py:22,24`), never read.
  - `MPIClass.collect` reads `getattr(self, 'wait_time', 0.0)` (`common/mpiclass.py:43`). On rank 0
    that picks up the Manager's own `wait_time`, which means its recv wait, not a worker's wait for
    work.
- **Stale text:**
  - The filters' help and docstrings say "counted (walkstat) or archived (walktar)", with no
    walksync.
  - `Manager.update_progress` says the payload is "(items, bytes, extra)".
  - `add_to_batch` says "(walktar)".
  - The test conftest header and `console_script()` say "walkstat/walktar".

---

## 2. Part A: the sweep (no behaviour change)

Six commits. "Done" for the whole part:
- `grep -n "app ==" hpc_file_utils/walk` finds nothing.
- The non-test package is smaller than today's ≈3,350 lines.
- A fourth tool would be one `tool.py` and one `worker.py`, with no edits to `cli.py` or
  `options.py`.

### A1. Duplicates and leftovers
- `sync/worker.py` uses `printable()`, and `show()` goes. The dry-run lists change only for names
  holding control characters other than `\n` and `\t`, which are now escaped too.
- One `SECONDS_IN_YEAR`, in `common/stats.py`, which `common/filters.py` imports.
- One `parse_size_option(text, option)` in `common/filters.py` that raises `ValueError`.
  `parse_options()` already turns `ValueError` into a parser error.
- `_add_into(mine, theirs)` in `common/stats.py` for the uids, gids and `partial_dirs`.
  `add_partial_dir` uses it.
- Drop `nexecute`; `print_counters()` uses `ndispatches`.
- Drop the leftovers listed in 1.6 and fix the stale text.
- Tests: no new ones. The existing suite covers these.

### A2. One accounting routine
- `WalkStats.count(path, statinfo, totals, count_dirs)` in `common/stats.py` is `StatWorker.count`
  moved, pure, with no MPI and no `self.options`. The `elif S_IS*` chain becomes a module-level
  `S_IFMT -> name` dict.
- `StatWorker.list_directory` moves to `BaseWorker`.
- `BaseWorker.process_directory` (walktar's scan) builds a `DirTotals`, calls `stats.count(...)`
  per entry and `self.list_directory(...)` at the end. `BaseWorker.record_directory` goes.
- Check the two copies line by line before deleting one. They agree today:
  - the uid is added for directories too
  - `st_modes['dir']` is counted only with a selecting filter
  - the `nbytes_files` heaps
- New unit tests for `count()`, in `tests/test_merge.py` or a new `tests/test_stats.py`:
  - type counts
  - the directory and select rule
  - heaps and per-user heaps
  - max times over non-directories only
  - min(size, blocks·512)

### A3. Worker scaffolding in `BaseWorker`
- `error(what, err)`: counts `num_errors` and prints, in one copy.
  - Stat and Sync drop theirs.
  - Base's "Cannot scan" becomes `error('scan', e)`, and Tar's becomes `error('add to tar file', e)`.
  - The messages stay identical (Part D changes them, separately).
- `vanished(dirname)`: True after printing today's message. It replaces 3 copies.
- `push_dir(path)`: append, then `ship_overflow()` past `--local-queue-max`. It replaces 5 sites.
- `entries(dirname)`: a generator over `with os.scandir(dirname)` that does the progress tick, the
  share tick and the `--exclude` count, and yields the entries that are left. All three scans use
  it, and the callers keep their own `try/except → self.error('scan', e)`.
- `ticked(items)`: share and progress every `SHARE_CHECK_INCREMENT` items, for
  `StatWorker.process_batch` and `SyncWorker.compare_batch`. walktar keeps its per-file check
  (`tar/worker.py:90-91`): a file read costs far more than an `iprobe`.
- `rank_info()`: a `RankInfo` for `MPIClass.collect`, replacing the `getattr`s. Workers report
  their `wait_time` and `max_local_dirs`, and the Manager reports zeros.
- `RankFile(pattern)`: a per-rank or per-batch file opened at its first write and closed by
  `run()`. walksync's dry-run file uses it. walktar's dirs tar does not, since `tarfile` keeps its
  own handle, but follows the same open-at-first-use and close-in-`run()` shape. Part C's index
  uses it too.
- Delete the "not called" `process_file` stubs. `BaseWorker.process_file` stays as the hook walktar
  uses, until Part B.
- **Performance check** (a generator now sits in the hot loop):
  - Run walkstat `-n 4` on a synthetic 1M-entry directory and on the wide tree, before and after,
    best of 3.
  - Expect noise: a generator step costs tens of ns, a `scandir` entry far more.
  - If it costs more than noise, inline the loop in `StatWorker` only, and say so here.

### A4. Typed batches
- In `common/worker.py`, a `FileBatch(paths, nbytes)` NamedTuple. `close_batch()` creates it, for
  walktar's tars and walksync's copies.
- `StatBatch(dirname, names, tail)` in `stat/worker.py`, and `CompareBatch(reldir, names)` in
  `sync/worker.py`.
- `SyncWorker.process_batch` dispatches with `isinstance`, and its `close_batch` override goes.
- Module-level NamedTuples pickle. The Manager still treats batches as opaque.
- Tests: `test_worker.py` builds batches by hand in a few places; update those.

### A5. Tool classes
- **`common/tool.py`:** `class Tool`. No MPI, cheap to import. It has:
  - `name` and `description`
  - `writes`: `None`, or `(verb, units)`: `('wrote', 'archives')` or `('copied', 'rsync batches')`
  - `batches_bytes`: adds `--batch-size`
  - `add_arguments(parser)` and `check(parser, args)`
  - `banner(args) -> lines`
  - `report(manager, stats)`
  - `exit_status(total_errors, options)`: walksync is always 1 on errors; the others give 2 with
    `--fail-on-errors`
  - `make_worker()`: imports the worker class lazily
- **One subclass per tool:** `stat/tool.py` `StatTool`, `tar/tool.py` `TarTool` and
  `sync/tool.py` `SyncTool`. The registry `TOOLS = {name: Tool}` lives in `walk/tools.py`.
- **`parse_options(appname, argv)` keeps its signature**, which the tests call. It looks up the
  tool, calls its hooks and sets `args.app = appname`.
- **The Manager** reads `TOOLS[options.app].writes` instead of `getattr(options, 'written_verb')`.
- **`cli.py` loses every `app ==`.** The banner, report and exit status come from the tool, and
  `make_worker` is the registry lookup.
- **`sync/rsync.py`:**
  - `RSYNC_OPTS`, `parse_rsync_capabilities`, `rsync_capabilities` and `rsync_base_opts`, from
    `common/options.py`
  - `rsync_command(options)`, from `SyncWorker.__init__`
  - possibly the run-and-poll loop of `copy_batch` (`sync/worker.py:291-336`), if it reads better
    as a function that takes a "while waiting" callback
- **`common/report.py`:** `SEP`, `_columns`, `rank_lines` and `balance_lines`, from
  `stat/report.py`, which imports them back.
- **Tests and docs:** update the imports in `tests/test_options.py` and `tests/test_walksync.py`,
  and the "Layout" section of `walk/README.md`.

### A6. Test helpers (`tests/conftest.py`)
- `WalkResult.ints(regex)`: asserts that `regex` matches stdout and returns the groups as ints. It
  replaces `_int` and the search-assert-map pattern in `test_walksync.py`, `test_batches.py` and
  `test_load_balance.py`.
- `_unlock(root)`: the chmod-back that the `golden_tree` and `private_golden_tree` fixtures each
  copy.

---

## 3. Part B: walktar onto stat batches (deferred: plan only)

Decided during the sweep's planning: write it up, but don't do it in the sweep.

- **What.** walktar becomes `StatWorker` plus tar batches:
  - The scan is walkstat's (d_type, with stats only for subdirectories).
  - Processing a `StatBatch` stats and counts each name as walkstat does, and puts the selected
    files in the open `FileBatch`. That is walksync's pattern of compare batches closing copy
    batches.
  - manager.py's termination argument already covers it: a batch may create batches only on the
    rank that processes it, which keeps them and processes them before its next `ready`.
  - The `-dirs` tar is still written at scan time, by the rank that walks the directory.
- **Then delete** `BaseWorker.process_directory` and the `process_file` hook: every tool shares one
  scan.
- **Bonus.** The batch already holds each file's lstat. `tar.addfile(tarinfo_from(statinfo),
  fileobj)` saves tarfile's second lstat in `gettarinfo()`, and Part C's walktar index costs no
  extra system calls.
- **Risks and measurements:**
  - Stats move from the scanning rank to whichever rank takes the batch.
  - walktar's golden summary must not change.
  - Re-measure on derecho against the phase 5 walktar numbers (same filesets, 1 node × 32 ranks)
    before merging. Record the numbers in `walk/README.md`'s phase 5 section.

---

## 4. Part C: optional indexes (new)

One line of metadata per entry, written by every rank in parallel with no coordination. Off by
default in both tools.

- **`walkstat --index`:** a parallel metadata dump, one file per rank: `index-r{rank:03d}.tsv`, or
  `index-<prefix>-r001.tsv` with `--summary-prefix`. These are like a GPFS policy-engine list or
  `find -ls`, made at walk speed.
- **`walktar --index`:** one index per tar, written beside it: `output-r001-b00003.tar.idx` and
  `output-r001-dirs.tar.idx`. It lists the members and their offsets, so one member can be found
  or extracted without reading the whole tar.
- **What gets listed:** what the tool counted (walkstat) or archived (walktar). `--exclude`d
  entries never appear. With selecting filters, only selected entries appear: the same rule as the
  reports and the tars.

### 4.1 The hard rule: parents before children, in every file
- **Each index file is closed under ancestry.** Before any record, every ancestor directory up to
  its `--dirs` root has a record earlier in the *same* file. A reader never meets a child before
  its parent, and each file can be imported on its own, and in parallel.
- **Concatenation keeps the property.** `cat index-*.tsv` is closed too. So are concatenated gzip
  members, if the index is ever compressed.

### 4.2 Two kinds of record (the first column), so repeats don't double-count
- **`E`, an entry.** Counted or archived here, and **exactly once across the whole set of index
  files**, by the same exactly-once guarantees the reports and the tars rely on.
- **`P`, a parent.** A structural record that keeps the file closed. It is repeated across files
  (and batches) as needed. An importer uses it for the hierarchy and skips it for totals.
- **`P`-only directories.** The `--dirs` roots, and directories that fail a selecting filter but
  hold selected entries, appear only as `P`.

### 4.3 How a writer keeps the rule
- **A "written" cache per file.** Each index file remembers directory paths it has written, as
  `E` or `P`.
- **Before each record,** walk up from its parent to the first path in the cache (or the root)
  and write the missing ancestors top-down as `P`, with one `lstat` each. A directory that
  vanished gets a `P` with `-` fields.
- **The cache is bounded, because it is only a cache.** The two kinds of mistake are not
  symmetric:
  - A *false positive* (believing a parent was written when it was not) breaks the rule. The cache
    must never produce one, so no Bloom filter and no hashing of paths.
  - A *false negative* (forgetting a parent that was written) only costs a repeated `P` line.
    So entries may be evicted freely.

  Keep it as a recently-used dict (`dict` keeps insertion order; drop the oldest past a cap, say
  100k paths, which is ~20 MB), reset when the file closes. Walks are depth-first and a directory's
  batches are processed close together in time, so a small cache catches nearly all the repeats.
  Without a cap, walkstat's one-file-per-rank index would keep every directory the rank ever
  touched: on a 10M-directory fileset over 32 ranks that is ~300k paths per rank, ~60 MB per rank
  and ~2 GB per node, and it grows with the tree. Walktar's cache is bounded anyway, since its
  index rolls with each tar.
- **Cost:**
  - walkstat's stat batches are all one directory: one check per batch.
  - A walktar batch spans directories: one check whenever the parent changes.
  - A cache miss costs one chain of `P` lines, at most the directory's depth.
- **Tests (added to 4.7):** the closure checker must pass with the cap set to 1, which is the
  worst case for repeats and the only way to exercise eviction on a small tree.

### 4.4 Format
- **Header.** TSV, with a versioned header line:
  `# hfu-index v1  tool=walkstat  root=<root>  columns=<names>`.
- **Columns.**
  - `rec`: `E` or `P`
  - `type`: `f d l p s b c`
  - `size`: st_size
  - `alloc`: st_blocks·512
  - `mode` (octal), `uid`, `gid`, `nlink` and `ino`
  - `atime`, `mtime` and `ctime`: integer epochs
  - `path`, last, as in the reports, so `sort` and `cut` work
  - walktar adds `offset` and `offset_data` (from `TarInfo`) before `path`, with `-` on `P` records
- **Hard links.** `nlink` and `ino` let an importer de-duplicate them.
- **Paths are absolute, as walked, and encoded reversibly.**
  - `printable()` is lossy, for reading. The index has to be parsed back.
  - Escape `\` as `\\`, `\n` `\t` `\r` as themselves, other control characters and undecodable bytes
    as `\xNN`.
  - Ship `decode_path()` next to the encoder.

### 4.5 Import contract (a parser for hpc-usage-queries `fs_scans/parsers`)
- **The interface already fits.** `FilesystemParser.parse_line()` returns a `ParsedEntry`, mapped
  as:
  - path
  - size
  - allocated = `alloc`
  - uid, gid
  - is_dir = (`type == 'd'`)
  - atime
  - inode = `ino`
- **Detection.** `can_parse()` keys on the `# hfu-index v1` header, or on the file names
  `index-*-r*.tsv` and `*.tar.idx`.
- **The catch.** `importers/pass1.py` collects directories into a dict by path, so repeated `P`
  records are harmless there. But `importers/pass2a.py` counts every directory record into its
  parent's `nr_dirs`, so a `P` record must not reach it as an entry. Two ways to handle that:
  - a `structural: bool` field on `ParsedEntry`, which pass2a skips
  - a parser mode that drops `P` records after pass1

  That choice belongs to hpc-usage-queries. This doc fixes only the hfu side.

### 4.6 Where it plugs in (after Part A)
- **`common/index.py`:** `IndexWriter(RankFile)`, which owns the per-file set and the `P` logic,
  with `add_stat(path, statinfo, kind)` and `add_tarinfo(tarinfo, kind)`.
- **walkstat** calls it next to `stats.count(...)`: for subdirectories at scan time, and for files
  in stat batches.
- **walktar** calls it after each successful `tar.add`, where the `TarInfo` is `tar.members[-1]`.
  After Part B it calls it from the stat already in hand. A batch's index opens and closes with its
  tar, and the dirs index with the dirs tar.
- **Order:** C works today for walktar, through `TarInfo`. It is cheaper after Part B, but does not
  depend on it.

### 4.7 Tests
- **Closure.** A checker asserts that each record's parent appeared earlier in the same file. Run
  it on:
  - walkstat and walktar outputs of the golden tree
  - the big-directory tree with `--batch-files 10`, so batches move between ranks and the `P`
    repeats really happen
  - both trees unfiltered, and with `--mtime-newer`, for the filter holes
- **Exactly once.** Across all files, no `E` path appears twice.
- **Completeness.** The union of walkstat's `E` records equals `find`'s listing (reuse
  `test_filtering.find()`), including under filters.
- **walktar.** Each index matches its tar's `getmembers()`, offsets included. Reading `size` bytes
  at `offset_data` returns the file's bytes.
- **Encoding.** `decode_path(encode(p)) == p` for the golden tree's odd names and the non-UTF-8
  tree.
- **Eviction.** The closure and exactly-once checks pass again with the written-cache cap set to
  1 (a test-only knob), so every record's parents are re-derived and repeated `P` lines are
  exercised on a small tree.
- **Off by default.** Without `--index`, no index files are written.

### 4.8 Open questions
- gzip the index (`--index=gz`)?
- Give walksync an `--index` too, as a per-rank list of what it copied?
- Is atime worth carrying? Reading through the tree for walktar changes it anyway.
- A configurable column set, or a fixed v1?

---

## 5. Part D: logging and console output

- **Today:** 11 `print(..., file=sys.stderr)` with a hand-built `'[{:3d}] '` rank prefix, 8
  `print()` + `sys.stdout.flush()` pairs, and `--verbose` as an `if` around extra prints. Worker
  diagnostics and rank 0's results share `print`.

### 5.1 Recommendation: stdlib `logging`, for diagnostics only
- **Setup.** Configure it in `cli.main()` right after MPI init: one `StreamHandler(sys.stderr)` per
  rank, formatted `[%(rank)3d] %(levelname)s %(message)s`, with the rank put on each record by a
  `logging.Filter`.
- **Levels:**
  - `WARNING`: a directory vanished, rsync exit 24
  - `ERROR`: "Cannot scan / stat / readlink / add to tar file / copy a batch"
  - `INFO`: the Manager's end-of-walk counters
  - `DEBUG`: `--verbose` detail

  `--verbose` sets the level. A `--log-level` option is optional.
- **Why here:**
  - `StreamHandler.emit` writes each line and its newline in a single `write()`, so lines from
    different ranks splice less under mpiexec. PR #11 lost dry-run lines to splicing on stdout.
  - Levels and `-q`/`-v` come for free.
  - The prefix lives in one place.
- **`BaseWorker.error()` (A3) stays the one place that counts `num_errors` and logs.** Logging is
  never the counter.
- **Tests:**
  - `capfd` unit tests for the format.
  - Update the MPI tests' stderr asserts that match the old `[  1] Cannot` text.

### 5.2 Results stay `print(..., flush=True)` on rank 0
The banner, the progress line, the summary and reports, and the `-->` lines are the parsed
contract (see the rules above). Part D changes only how they are flushed.

### 5.3 rich: not now
- **Production output is a pipe into PBS job logs, not a TTY.** rich falls back to plain text
  there, which leaves a dependency for nothing. Its progress bars and colours are noise, or escape
  codes, in a log.
- **The reports and the progress line are parsed:** by the golden tests, `scans/sort_results.sh`
  and the PR #11 tests.
- **Every rank would import it.**
- **Where it could fit later:**
  - `rich.logging.RichHandler` on rank 0, only when `sys.stderr.isatty()` (interactive runs on a
    login node)
  - a `rich.table` for the `--verbose` per-rank table, under the same guard

  Both would match hpc-usage-queries' `console.print`. Revisit if interactive use grows.

---

## 6. Part E: Pythonic cleanup, and what not to "modernize"

Counts are from the non-test package at `cd32aac`. Mechanical commits go in a new
`.git-blame-ignore-revs`, so `git blame` still lands on the phase commits.

### E1. Mechanical (one pass each, no logic change)
- **Bare returns.** 54 bare `return` at the end of a function or `__init__`: delete them.
- **Banners.** 56 `#~~~~` banners between methods and the `####` rulers before classes: delete
  them. Collapse the runs of three blank lines to PEP 8's two (top level) and one (methods).
- **Yoda conditions.** 18 of them: `0 == rank`, `'walksync' == appname`, `0 >= args.progress`,
  `10 < max_atime`, `1 == len(...)`, `0 == n % N`. Write the variable first.
- **f-strings.** 110 `str.format` calls against 5 f-strings, with `requires-python >= 3.10`:
  convert them. Leave the module-level row templates (`DIR_ROW`, `FILE_ROW` and the `%`-built
  widths): they have to stay format strings.
- **Flushing.** `print(...)` then `sys.stdout.flush()` becomes `print(..., flush=True)`.
- **Constructors.** 5 `Base.__init__(self)` calls become `super().__init__()`.
- **Imports.** `import os, sys, stat` becomes one import per line.
- **Small layout fixes.** `class MaxHeap():` loses its parens. Single-line bodies
  (`if heap.empty(): return []`, `for ts in [...]: df[ts] = ...`) go on their own lines.

### E2. Dead or outgrown code
- **Unused attributes.** `MPIClass.i_am_root` and `MPIClass.dirs` (A1 may already have removed them).
- **The `humanfriendly` fallback.** The optional-import fallback in `common/formatting.py:7-25`
  is dead: `humanfriendly` is a required dependency, and `options` and `filters` import
  `parse_size` unconditionally. The fallback could only print output that differs from the golden
  files.
- **`MaxHeap` crust.** It calls `heapify([])` on an empty list, has a `get_list()` getter, and has
  `empty()` alongside `__len__`. Use `__len__`/`__bool__` and drop the rest.

### E3. Structural idioms
- **`Tag`.** `TAGS` (`common/protocol.py:17`) becomes `class Tag(IntEnum)`. It is an `int` to
  mpi4py, and `Tag.READY` reads better than `self.tags['ready']`.
- **`ManagerCounters`.** The Manager's ~15 counter attributes (`common/manager.py:100-129`) become a
  dataclass, whose fields drive `print_counters()`.
- **`WalkStats`'s scalar counters** (`num_files` … `num_unmatched`) move into a dataclass part, and
  `merge()` sums them with `dataclasses.fields()` instead of 7 hand-written lines. Today a counter
  added but forgotten in `merge()` is a silent bug.
- **The defensive `getattr(options, ...)` and `getattr(self, ...)`** (9 sites) exist because the
  tests build `SimpleNamespace` options. A3 and A5 remove most of them. A test helper
  `make_options(app, **overrides)` that goes through `parse_options()` replaces the hand-built
  namespaces.
- **Type hints** on the `common/` public surface (`stats`, `filters`, `options`, `tool`, `index`),
  as documentation. No mypy gate.

### E4. Correctness nits found on the way
- **One "now" for every rank.** `stats.NOW_TIMESTAMP` (`common/stats.py:68`) is taken at import on
  every rank, but the filters' `now` comes from rank 0. The 3/5/7-year buckets should use one
  reference time: set `args.now` on rank 0, and let it reach every rank in the bcast.
- **Resolve names once, on rank 0.** `IDCounts.set_id()` resolves uid and gid names on every worker
  rank, which is a `getpwuid`/`getgrgid` call per id per rank (LDAP/sssd on derecho). Workers should
  carry ids only, and rank 0 resolves the names once at report time, with `functools.lru_cache`.
  Check that the golden userlists don't change.

### Guard rails: do not "modernize" these
- **Hot loops** (`entries()`, the `process_batch`es, `count()`): keep `str` paths, `os.path` and
  `os.scandir`. pathlib costs several times as much per entry.
- **Local-variable caching** (`st = self.stats`, `join = os.path.join`, `select = self.select`): it
  is deliberate in hot loops. Drop it only in code that runs once.
- **The `select is None` / `exclude is None` fast paths:** "no filter costs one `is None`" is a
  phase 4 promise.
- **NamedTuples for `FileEntry` and `DirEntry`, not dataclasses:** they go into heaps that compare
  tuples, and they pickle small.
- **Plain-text output** stays byte-identical. Every Part E commit runs the golden tests.

---

## 7. Deliberately left alone
- **`sync/split_pair.py` and `tools/glade_hsm/libexec/glade_hsm_scan_filelist.py`** keep their own
  `parse_size` / `human` / `LazyWriter`. They are standalone scripts that run without the package
  installed, so the duplication is on purpose.
- **`MPIClass.collect` calls walkstat's `finish_directories` for every tool.** It is a no-op for
  the others, and not worth a hook.
- **The Manager's protocol.** It is as the phase 3.5–6 hand-offs left it.

---

## 8. Order, and how each step is checked

A (sweep) → E1/E2 (mechanical, dead code) → D (logging) → E3/E4 → C (indexes) → B (walktar on stat
batches, after its derecho re-measure). Each part is its own PR, stacked on the one before.

- **Environment:** `source etc/config_env.sh`, or `pip install -e .[mpi,dev]` with conda-forge
  openmpi.
- **After every commit:** run `pytest hpc_file_utils/walk/tests`. These must stay green:
  - the golden summary and userlist tests
  - the walksync oracle and rerun tests
  - walktar's union-equals-tree tests
  - the termination-race tests
  - the `test_worker.py` unit tests
- **On derecho:**
  - `make test-pbs`
  - A3's performance check, at scale
  - Part B's re-measure
  - an index smoke run on a scratch fileset, imported with the hpc-usage-queries parser
