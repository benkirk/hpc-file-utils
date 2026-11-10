Forked from https://github.com/benkirk/mpi4py_task_runner

## Layout

| | |
|---|---|
| `cli.py` | the `walkstat`, `walktar` and `walksync` console scripts: options on rank 0, Manager on rank 0, a tool's worker on the others, then the collectives and the reports; everything tool-specific comes from the tool's `Tool` |
| `tools.py` | `TOOLS`, the tools by name |
| `common/` | the walk itself, shared by every tool: `manager.py` (its docstring is the protocol and the termination rule), `worker.py` (BaseWorker: the scan, the batch and in-flight scaffolding, `FileBatch`, `RankFile`), `tool.py` (`Tool`: what a tool adds — options, banner, report, exit status, worker), `protocol.py` (tags, the progress payload), `mpiclass.py` (the end-of-walk collectives), `stats.py` (what a rank accumulates, `count()`, and how two ranks' combine), `report.py` (the balance and per-rank lines), `heaps.py`, `progress.py`, `formatting.py`, `options.py`, `filters.py` |
| `stat/` | walkstat: `tool.py`, `worker.py` (StatWorker, `StatBatch`), `report.py` (summary and userlists), `spreadsheet.py` |
| `tar/` | walktar: `tool.py`, `worker.py` (TarWorker) |
| `sync/` | walksync: `tool.py`, `worker.py` (SyncWorker, `CompareBatch`), `compare.py` (the copy decision, pure), `rsync.py` (which rsync, its command, one rsync as a process), `report.py` |

What these tools measured about GLADE itself (read latency and parallelism, stat costs, the
directory create ceiling, CPU binding, how to measure) is collected in
[docs/GLADE_PERFORMANCE.md](../../docs/GLADE_PERFORMANCE.md); the phase notes below are the
sources.

Only `manager.py`, `worker.py`, `mpiclass.py` and `cli.py` touch MPI, and they import mpi4py when
called rather than when imported, so the rest unit-tests without it. A fourth tool is one
`<tool>/tool.py` and one `<tool>/worker.py`, added to `TOOLS`.

Outputs (in the working directory, written by rank 0):
- `summary-<prefix>` (`summary-output` without `--summary-prefix`), also echoed to stdout. The
  scans publish it as `<dir>/.usage/summary.txt`.
- `userlists/<prefix>/<user>`, one per user above `--threshold-size`/`--threshold-count`,
  published as `<dir>/.usage/users/`.
- `--spreadsheet FILE.xlsx`: three sheets per prefix (four when filtered), added to (or
  replaced in) FILE.
- walktar: `output-r{rank}-b{n}.tar` (one per batch) and `output-r{rank}-dirs.tar`, written
  by the workers (see [File batches](#file-batches-phase-5)).
- walksync: `walksync-dryrun-r{rank}.txt` with `--dry-run`, one per worker. No summary or
  userlists unless `--summary-prefix` or `--spreadsheet` is given.
- `--profile`: `profile-r{rank}.prof`, a cProfile of every rank's run (the Manager's loop,
  each worker's walk), for `python -m pstats`.
- `--index`: walkstat's `index-r{rank}.tsv` (`index-<prefix>-r{rank}.tsv` with
  `--summary-prefix`), one per worker; walktar's `<tar>.idx` beside each tar (see
  [Indexes](#indexes-the-sweeps-part-c)); walksync's `index-r{rank}.tsv`, a record of what
  the rank copied. `--index=gz` writes the same through gzip, with `.gz` on each name.

## Filtering (phase 4)

Find-like filters, in `common/filters.py`, shared by walkstat and walktar. There are two kinds:

- **Pruning: `--exclude GLOB`** (repeatable) matches an entry's *name* (fnmatch, case-sensitive;
  `*` matches a leading dot, as in find). A match is never stat'ed, counted, descended or
  archived: `find TREE -name GLOB -prune -o … -print`. The `--dirs` roots are never tested. A GLOB
  holding `/` is refused (no path patterns yet). Phase 6's walksync will reuse this option.
- **Selecting** decides what is counted (walkstat) or archived (walktar's files). It is tested on
  the stat already fetched, on every entry, directories included, and never stops descent: a
  directory that fails is still walked for the entries inside it that pass.

  | option | an entry is counted if | find |
  |---|---|---|
  | `--mtime-older WHEN`, `--mtime-newer WHEN` (also `--atime-*`, `--ctime-*`) | its time is before (older) or after (newer) WHEN. WHEN is an age, `N[smhdwy]` (a bare N is days, y = 365 days), or a local date, `YYYY-MM-DD[THH:MM[:SS]]`. Older and newer together give a window. | `-mtime +N`, `-newermt DATE` |
  | `--user U[,U…]`, `--group G[,G…]` (repeatable) | it is owned by one of them. Each is a name, or else a numeric id. | `-user`, `-group` |
  | `--min-size S`, `--max-size S` | `S_min <= st_size <= S_max`. Sizes as for `--threshold-size` (binary). | `-size +Nc`, `-size -Nc` |
  | `--type T[,T…]` | its type is one of `f d l p s b c` | `-type` |

Different kinds AND together; the values of one kind OR together. Rank 0 resolves everything
once (names to ids, ages and dates to absolute times, globs to one regex) and broadcasts it
with the options. Without a filter, the hot loop pays one `is None` test for each kind.

What a filter changes:
- **walkstat:** Total Count, Total Size, Type Counts, the Users and Groups tables and every
  list count only the matching entries. So `--user bob`'s totals are bob's row of the
  unfiltered Users table.
  - A directory's own figures (size, items, last access, single owner, the thresholds) cover
    its matching entries only. With a selecting filter, a directory that holds none is left
    out of the lists.
  - Type Counts then leave out the roots, so they sum to Total Count, which is the count of
    `find TREE -mindepth 1 <predicates>`.
  - The summary gets `Filters:`, `Excluded:` (entries pruned) and `Unmatched:` (entries
    scanned but not counted) lines after `Scanned:`. Each userlist gets the `Filters:` line,
    and the spreadsheet a `<prefix> - Filters` sheet. An unfiltered report is unchanged.
  - The progress line counts matching items.
- **walktar:** archives every directory it walks (keeping its mode, owner and ACLs), the roots
  included, plus exactly the selected non-directories. Nothing below an `--exclude` is
  archived.
- **Sizes:** `--min-size`/`--max-size` test `st_size`, the apparent size, as find does. The
  reports add up `min(st_size, st_blocks × 512)`, which is smaller for sparse or HSM-migrated
  files.

## File batches (phase 5)

### walktar

The files a walk selects go into **batches**: a batch closes after `--batch-files` files
[1000] or `--batch-size` bytes of `st_size` [16GiB], whichever comes first. A file bigger
than `--batch-size` makes a batch on its own. Batches are units of work, just as
directories are, so one huge directory no longer ties up one rank for hours.
- **Two levels (the sweep's Part B).** walktar's scan is walkstat's (below): the scanning
  rank stats only the subdirectories and cuts a directory's other names into *stat
  batches* of `--batch-files`, without a stat. The rank that processes a stat batch stats,
  filters and counts each name, and puts the selected files in its open *file batch*,
  which closes by the real `st_size` and is written as one tar. So `--batch-size` keeps
  its meaning, and the per-file stats run on every rank instead of the scanning one.
  Before Part B the scanning rank stat'ed every file itself; on one huge campaign GPFS
  directory it made ~1,000 entries/s while the other ranks waited (Phase 5.5, below).
- **Where batches go:**
  - A rank keeps the batches it makes and processes them itself, batches before
    directories: a stat batch's file batches go on the same list and are written before
    the next stat batch. The open file batch carries over from one stat batch and one
    directory to the next, so small directories combine.
  - A batch leaves its rank only in the answer to the Manager's `share`: the oldest half of
    the rank's closed batches, rounded up, plus the oldest half of its stack.
  - The Manager hands out one batch per `execute`, and hands batches out before directories.
  - The termination argument covers batches (see `manager.py`): a stat batch creates file
    batches only on the rank processing it, which processes them before its next `ready`.
- **walkstat's batches are the stat batches alone** (below).
- **Outputs**, in the working directory:
  - `output-r{rank}-b{n}.tar`: one batch, archived by `rank` (its `n`-th). It holds only
    selected files.
  - `output-r{rank}-dirs.tar`: every directory `rank` walked, the roots included, with its
    mode, owner and times.
  - These replace the per-rank `output-r{rank}-f{n}.tar`, which rolled over at 2 TiB.
- **Restoring:**
  1. Extract the batch tars, in any order or in parallel.
  2. Then extract the `-dirs` tars. This sets each directory's mode and times after its
     files are in place; a read-only directory would otherwise refuse files from another
     tar.
- **Hard links:** two names of one inode that land in different tars are stored as two full
  copies. They were already, across ranks.
- **Progress:** the progress line adds `| wrote S (R/s now, A/s avg), batches waiting B`.
  The end of the walk prints a `--> Batches:` line with the batches shared and handed out,
  and the files, bytes and tars written.
- **Each tar copies its members in 16 MiB chunks** (`copybufsize`; `tarfile`'s default is
  16 KiB). This was 2–5% faster on scratch.
- **The archiving runs on the worker's own thread**, file by file. Between files it answers a
  `share` and sends its counts when they are due. The tar thread and `--tar-queue-depth` are
  gone. The thread overlapped scanning a directory with archiving it, but only within that
  directory: the worker waited for its queue to drain after every directory.
- **Read-ahead (phase 5.5):** `--read-ahead` [16] reader threads per rank open and read the
  batch's next files while the worker writes the current one into the tar.
  - A reader reads a regular file whole when it is at most 16 MiB, so a rank holds at most
    about 16 × 16 MiB. A bigger file it only opens, and the tar copies it in 16 MiB chunks
    as before.
  - A reader lstats first and opens only regular files, with `O_NOFOLLOW`. Symlinks, fifos
    and devices still go through `tar.add()`.
  - The worker writes the files in batch order, through tarfile's own `gettarinfo()` and
    `addfile()`. The members (names, modes, owners, times, hard links) are `tar.add()`'s.
    `test_tar_members.py` checks this.
  - A file that shrinks between its stat and its read counts one error and is left out.
    `tar.add()` would have written its header and then failed partway, breaking the tar.
  - `--read-ahead 0` is phase 5's `tar.add()`, one file at a time.

### walkstat: stat batches

A stat costs about 6 ms per rank on campaign GPFS, so one rank used to take an hour over a
1M-entry directory while the others waited. Now the scanning rank reads a directory's names
and types from `scandir` (`d_type`, no stat). It stats only the subdirectories, which it must
count and walk. The other names go into batches of `--batch-files` [1000] per directory,
`(dirname, names, tail)`, and the stats happen wherever the batch is processed: on this rank,
or on an idle one the Manager's `share` hands it to. Batches move exactly as walktar's do.
- **The directory lists don't change.**
  - A directory that fits in one batch is listed, whole, by the rank that stats it. Its last
    batch carries the subdirectories' totals (`tail`).
  - A directory split over several batches adds each part to a table keyed by path
    (`WalkStats.partial_dirs`). The table merges in the reduction, and rank 0 lists those
    directories after it. Only big directories go there.
  - Tested by the golden summary and userlists, and by the find-oracle filter tests, with
    `--batch-files 1`, which splits every directory.
- **Filters:** `--exclude` still prunes by name in the scan. The selecting filters are
  applied after the stat, wherever that happens.
- **Errors:** a file that fails its stat (it vanished, or its directory can be listed but not
  entered) counts one error, and the walk goes on. Before, the first failure abandoned the
  rest of its directory.
- **Messages:** a batch of 1,000 names is about 20 KB. Pickling it costs about 30 µs, against
  ~6 s of stats.

## walksync (phase 6)

```
mpiexec -n 33 walksync --dirs SRC --dest DEST [--exclude 'COLD_STORAGE*'] [--dry-run]
rsync -aHAX --delete [--exclude 'COLD_STORAGE*'] SRC/ DEST/     # the final, authoritative pass
```

Copies every entry of SRC that DEST lacks, or has in a different version, with batched
`rsync -aAX --numeric-ids --chmod=Du+w --files-from=- --from0 SRC/ DEST/`, spread over the ranks
by the walk's batches. It **never deletes**. Deletions, hard links (copied as separate files),
and the directories' real modes and times are left to a final serial `rsync -aHAX --delete`. A
rerun with nothing changed copies nothing: it costs only the walk.

- **Scan** (one directory): one `scandir` of the source (names and `d_type`, no stat for
  files) and one `scandir` of the matching destination directory, the lookup for the whole
  directory. Subdirectories are walked. One the destination lacks goes on the copy list
  itself, so empty directories get created. The other names go into **compare batches** of
  `--batch-files`. Destination names the source lacks are counted as "only in the
  destination" and kept.
- **Compare batch:** lstat each source name (and the destination's, when the scan saw one),
  then `compare.needs_copy()`, rsync's quick check:
  - regular files: missing, a different type, size or whole-second mtime;
  - symlinks: a different target (not times: not every system can set a symlink's);
  - directories: only missing or a different type;
  - devices: a different `st_rdev`.

  Ownership, modes, ACLs and xattrs are not compared, which matches rsync's quick check.
  Like walkstat's stat batches, a big directory's compares spread over the ranks.
- **Copy batch:** what must be copied goes into the open copy batch. That batch carries over
  between directories, so changes spread over many small directories still make one rsync.
  It closes at `--batch-files` [1000] entries or `--batch-size` [16GiB] bytes and becomes one
  rsync, on this rank or on an idle one that takes it through a `share`. While rsync runs,
  the worker polls it every 0.5 s and still answers `share` requests and sends progress.
  `--chmod=Du+w` is needed for two reasons: ranks' batches create the same parent
  directories, and a read-only source's directories would refuse the next batch's files.
- **`--rsyncs K`** [8]: K rsyncs side by side over one copy batch, each over a K-th of its
  files, the batch still one unit of work (it fails if any of them does). A cold file costs
  one storage round trip whatever rsync does with it, and K rsyncs overlap those round trips
  as walktar's `--read-ahead` does; `docs/HANDOFF-sweep.md` 0.4 measured 8 as the sweet spot
  for one rank with one CPU (6× on 1 MiB files, 7× on 80 KB files). At 1 × 32 the gain is
  smaller, because the ranks share a ceiling that one rank never meets:
  - **The small-file create ceiling** (the sweep's "K rsyncs" step; `hfu-bench/createprobe.py`,
    32 processes creating 1,000 files each the way rsync's receiver does, onto scratch):
    **~1,250 files/s into one directory from derecho, ~1,000 from Casper, whatever the file
    size** (80 KB or 1 MiB alike), against 2,600–4,300 files/s into one directory per
    process and 1.9–3.1 ms per file for a single stream. The directory is the serialization
    point, not the bytes.
  - walksync into one directory reaches ~450–520 files/s at any K (manyfiles, 200,000 ×
    1 MiB, derecho 1 × 32: K = 1 451 s, K = 4 565 s, K = 8 385 s, K = 8 with
    `--cpu-bind depth` 450 s; Part A: 443 and 400 s; Casper 805 and 398 s): about 2.5× below
    the probe, which is rsync's extra directory work per file (its stat of the destination
    name, the temporary name and the rename, the xattr and ACL calls) sharing that one
    directory's rate. Across many directories K pays: `.Radiosonde` (584,851 × 80 KB in 292
    directories, campaign → scratch) went 892 → 405 → 342 s for K = 1, 4, 8, and 285 s
    (2,050 files/s) with `--cpu-bind depth --depth 4`, the rsyncs' CPU unpinned. K = 8 is
    the best or within noise of it on every tree and machine measured and never worse than
    1 (1.2× into one directory, 2.6× across many). Hence 8.
  - `--rsyncs 1` is phase 6's behaviour, one rsync per batch.
- **Which rsync:** `--rsync` if given. Otherwise rsync on PATH, unless it lacks ACLs or xattrs
  and `/usr/bin/rsync` explicitly lists both, in which case the banner says it uses
  `/usr/bin/rsync` and why.
  - On derecho this matters: `config_env.sh` puts the env's conda-forge rsync, which has no
    ACLs, first on PATH, while `/usr/bin/rsync` has both.
  - macOS's `/usr/bin/rsync` (openrsync) lists no Capabilities, so it is never chosen this way.
- **rsync without ACLs or xattrs:** rank 0 reads `rsync --version` once. If the Capabilities
  list says `no ACLs` or `no xattrs`, `-A`/`-X` are dropped, since rsync would refuse them.
  The banner warns, and the summary says the final serial rsync (with a capable rsync) must
  copy them.
- **Termination:** a compare batch creates copy batches. They stay on the rank that
  processed it, which handles them before its next `ready`, so the Manager's argument
  (manager.py, point 1) is unchanged.
- **Filters:** `--exclude` prunes by name (never compared, copied or descended; for
  `COLD_STORAGE*`). The time, `--user`, `--group`, `--type` and `--min-size`/`--max-size`
  filters choose which files are copied, a partial copy (the banner says so). Every directory
  is still walked and created. A file that later leaves the selection (it grew past
  `--max-size`, or was modified under `--mtime-older`) keeps its old copy in the destination.
- **Progress:** `| copied S (R/s now, A/s avg)`. Bytes count when a batch's rsync returns, so
  the rate is lumpy. **Summary:** new / changed / unchanged / only in the destination, the
  excluded and unselected counts, and the files, bytes, rsync batches and rsync-seconds
  copied. `--verbose`'s per-rank `files` column is what each rank copied.
- **`--dry-run`:** copies nothing. Each rank lists `N path` (new), `C path` (changed) and
  `X path` (only in the destination) in its own `walksync-dryrun-r{rank}.txt`. Lines from
  several ranks on one stdout got spliced together mid-line under mpiexec.
- **`--index`** (`--index=gz`): each rank's record of what it copied, `index-r{rank}.tsv`, in
  walkstat's index format ([Indexes](#indexes-the-sweeps-part-c)): one `E` per entry an
  rsync copied, from the source lstat the compare took. Unchanged entries are not listed.
  With `--dry-run` it lists what the run would copy.
- **Exit status:** 1 on any error: a failed stat or scan, or an rsync failure. rsync's exit
  24 (source files vanished) is counted and reported, but it is not an error.
- **Tested** (`tests/test_walksync.py`, as root and non-root):
  - the oracle `rsync -naxAX --delete -i --chmod=Du+w` lists only directory-time lines on the
    golden tree (read-only subtree with ACLs included), and a rerun copies nothing;
  - `--exclude COLD_STORAGE*`, a selecting filter, changed and extraneous destination
    entries, and `--dry-run` on the 2,000-subdirectory tree;
  - a big directory is copied by several ranks;
  - a failing rsync fails the run.

## CPU binding (perf-1, step A)

The launcher decides how many CPUs a rank may run on, and a rank's threads (walktar's
`--read-ahead` readers, walkstat's `--stat-threads`) share them. On derecho, PALS's default
binds each rank to **one** CPU (`os.sched_getaffinity(0)` is `{rank}`), and that one CPU was
the ceiling on small files on both filesystems: walktar's 16 readers lost a third of the
rate on scratch `manyfiles` and on the GDEX VIT zarr on campaign. Any wider binding gives
the whole gain, and the width past two cores makes no difference (the measurements are in
the perf-1 entry below), so:

```bash
mpiexec --cpu-bind depth --depth 2 walktar ...     # two cores per rank
```

is the line to launch with (the scan scripts' `mpiexec` lines carry it), and every run
prints the binding it got, from every rank's affinity mask, as

```
CPUs per rank: 2 (the launcher's binding; ...)
```

so a number in a log always says what binding it was measured under. `--depth 2` fits any
rank count up to the node's 128 cores; `--depth 4` fits 32 ranks exactly and `--depth 8`
spreads 32 ranks over the 256 hardware threads; `--cpu-bind none` leaves every rank free to
roam, which measured the same within the noise. walktar does not size `--read-ahead` from
the mask: with one CPU per rank fewer threads are slower still (phase 5.5), and with two or
more the default 16 is already past the point where more threads pay.

## Indexes (the sweep's Part C)

`--index` makes walkstat, walktar and walksync also write one line of metadata per entry,
TSV, every rank in parallel with no coordination (`common/index.py`). Off by default.

- **`--index=gz`** writes the same records through gzip, `.gz` on each name
  (`index-r001.tsv.gz`, `output-r001-b00003.tar.idx.gz`). Each file is one gzip stream
  at level 6, gzip's own default. Python's `gzip.open()` defaults to 9, which costs ~4×
  the CPU for ~4% less. The index is 7–12× smaller and the walk no slower (indexes-2,
  step A, below). `zcat` reads it, and `cat` of `.gz` files is still one valid gzip file.
  A rank killed mid-file leaves a stream without its end, which a reader refuses, where
  a plain file ends in a partial line.

- **walkstat:** `index-r{rank}.tsv` (`index-<prefix>-r{rank}.tsv` with `--summary-prefix`)
  in the working directory, every entry the rank counted: a parallel metadata dump at walk
  speed, like a GPFS policy-engine list or `find -ls`. A rank that counted nothing leaves
  no file.
- **walktar:** `<tar>.idx` beside each tar (`output-r001-b00003.tar.idx`,
  `output-r001-dirs.tar.idx`), every member with its offsets, so one member can be found or
  extracted without reading the whole tar. Made with the tar, even if it stays empty.
- **walksync:** `index-r{rank}.tsv` (`index-<prefix>-r{rank}.tsv`), every entry the
  rank's rsyncs copied. The header has `tool=walksync`, `root=` the source and `dest=` the
  destination. The rules:
  - **`E` is "copied here"**, recorded from the source lstat the compare took, by the rank
    that ran the rsync, after it returned. A new directory's stat is one `lstat` at the
    scan, taken only with `--index`. The copy batch carries the stats, so a shared batch
    records on the rank that copies it.
  - **A failed part records nothing.** A part whose rsync exited 24 (source files
    vanished) records only the entries still in the source.
  - **An entry compared and found unchanged is not recorded**, neither as `E` nor as `P`
    unless it is an ancestor of a copied entry. The index is what this run copied, not a
    listing of the tree: walkstat `--index` is that. A rerun that copies nothing writes
    no index. Entries only in the destination are never listed: they have no source stat.
  - **`--dry-run` writes the index the run would write**, recorded at the compare, and its
    header says `dry-run=yes`. Its records are the real run's, which the dry-run list
    (paths only) lacks: the sizes, owners and times of the pending copy.
- **What is listed:** what the tool counted, archived or copied. An `--exclude`d entry never
  appears; with a selecting filter only the selected entries appear, the same rule as the
  reports and the tars.

**Format.** A versioned header line, then one record per line, tab-separated, the path
last so `sort` and `cut` work:

```
# hfu-index v1	tool=walkstat	columns=rec,type,size,alloc,mode,uid,gid,nlink,ino,atime,mtime,ctime,path	root=/glade/campaign/x
P	d	4096	4096	0755	1001	100	3	1234	1700000000	1700000000	1700000000	/glade/campaign/x
E	f	1048576	1048576	0644	1001	100	1	5678	1699990000	1600000000	1600000000	/glade/campaign/x/a.dat
```

| column | |
|---|---|
| `rec` | `E` or `P` (below) |
| `type` | `f d l p s b c` (`?` otherwise) |
| `size`, `alloc` | `st_size`, `st_blocks × 512` |
| `mode` | the permission bits, octal |
| `uid`, `gid`, `nlink`, `ino` | `nlink` and `ino` let an importer find the hard links |
| `atime`, `mtime`, `ctime` | integer epochs |
| `offset`, `offset_data` | walktar only: the member's header and data positions in its tar; `-` on `P` records |
| `path` | as walked, encoded reversibly (below) |

- **Two kinds of record, so repeats never double-count.** `E` is an entry: counted
  (walkstat) or archived (walktar) here, and exactly once across the whole set of files, by
  the guarantees the reports and the tars rely on. `P` is a parent: a structural record,
  repeated across files and batches as needed (one `lstat` each), which an importer uses
  for the hierarchy and skips for totals. In walkstat's index the `--dirs` roots and the
  directories that fail a selecting filter appear only as `P` (they are not counted);
  walktar archives every directory it walks into a dirs tar, so there they are `E`, with
  their offsets. A `P` of a directory that vanished has `-` in every stat field.
- **Parents before children, in every file.** Before any record, every ancestor directory
  up to its `--dirs` root has a record earlier in the *same* file. So each file can be
  imported on its own, in any order or in parallel, and `cat index-*.tsv` keeps the
  property. The writer keeps a bounded cache of the directories it has recorded (100,000
  paths, least recently used out; `HFU_INDEX_CACHE_MAX` sets it, the tests to 1) and
  writes the missing ancestors of each record top-down as `P`. It is only a cache: a
  forgotten parent costs one repeated `P` line, and it can never claim a parent it did not
  write.
- **Paths** are encoded reversibly, unlike the reports' `printable()`: `\` as `\\`, newline,
  tab and return as `\n` `\t` `\r`, other control characters and undecodable bytes as
  `\xNN`. `index.decode_path()` reverses it, `index.read_index()` parses a file.
- **walktar's atime is the found one**, from the `lstat` taken before the member is read
  (the reader thread's, in `open_member()`; one before `tar.add()` for anything else and
  with `--read-ahead 0`), so the index does not depend on whether the mount updates atimes.
  The readers also open with `O_NOATIME`, which Linux allows the file's owner and root (a
  run as anyone else gets `EPERM` and retries without it), so a root walktar leaves the
  atimes as it found them; `--read-ahead 0` reads through `tar.add()`, which does not.
  `tarfile` records a member's offsets only when it reads a tar, so the writer derives
  them from the tar's position around each add.
- **Checking:** `python -m hpc_file_utils.walk.common.index [--find] FILES` reads plain or
  gzip files, told apart by their first two bytes. It checks each file for closure and the
  `E` paths for repeats across the files. With `--find` it compares the `E` set against
  `find` of the header's roots: below the roots for walkstat, as Total Count, and for
  walksync, which matches only a copy into an empty destination; the roots too for
  walktar, as the tars. The tests run the same checks on the golden tree and on a big
  directory whose batches move between ranks, unfiltered and filtered, with the cache cap
  at 1. They compare each `.idx` with its tar's `getmembers()` (offsets included, the
  bytes at `offset_data`), every file's atime with the one the fixture set, and
  walksync's `E` set with rsync's own itemized list of what it would copy.
- **Importing** (hpc-usage-queries `fs_scans/parsers`, a parser to write there). The
  contract:
  - **Opening.** A file is plain text or gzip (`--index=gz`). Tell them apart by the first
    two bytes, `1f 8b` for gzip, or by a `.gz` suffix. Read as UTF-8: the paths are
    escaped, so a strict decode never fails.
  - **Detection.** `can_parse()` keys on the first line starting `# hfu-index v1`, after
    any gunzip. Failing that, the names are `index-r*.tsv`, `index-*-r*.tsv` (with
    `--summary-prefix`) and `*.tar.idx`, each with an optional `.gz`. The earlier
    `index-*-r*.tsv` alone missed the unprefixed name.
  - **The header** is tab-separated `key=value` fields after `# hfu-index v1`: `tool=`
    (`walkstat`, `walktar` or `walksync`), `columns=` (the column names, in order), one
    `root=` per `--dirs` entry, and for walksync `dest=` and, for a dry run,
    `dry-run=yes`. Map columns by name, and ignore fields you do not know. Any line
    starting with `#` is a header: `cat` of several files has one per file, and each
    starts a new file's records.
  - **The column set is fixed at v1** (`docs/HANDOFF-sweep.md` 4.8): every column above,
    for every tool, plus walktar's two offsets. A different set would be a new version, so
    refuse a header that is not v1.
  - **Records.** `parse_line()` maps `path` (through `decode_path()`), `size`,
    `allocated = alloc`, `uid`, `gid`, `is_dir = (type == 'd')`, `atime` and
    `inode = ino` onto `ParsedEntry`. A `-` field is unknown: only a vanished directory's
    `P` has them.
  - **`P` records stay out of the totals.** Pass 1 collects directories by path, so
    repeats are harmless there. Pass 2a counts every directory record into its parent, so
    a `P` must not reach it as an entry.
  - **What a set of files means.** walkstat's `E` records are the tree's usage below its
    roots. walktar's are what its tars hold, the roots included. walksync's are only what
    one run copied, a subset of the tree, and a dry run's what it would copy: the stats
    are the source's real ones, but the totals are not the tree's usage. An importer of a
    usage database wants walkstat's indexes.

## Behaviour changes since the csg-utils import

- **Diagnostics go through `logging` (the sweep's Part D).** Every rank's warnings and
  errors reach stderr as `[rank] LEVEL message`, one write per line: `[  3] ERROR Cannot
  scan: ...`, `[  3] WARNING directory '...' vanished`, rsync's exit 24, and rank 0's
  startup errors (`[  0] ERROR no such directory ...`). `--verbose` also enables DEBUG
  records (none yet). The results (the banner, the progress line, the `-->` lines, the
  summary and the userlists) stay `print` on rank 0's stdout, byte-identical: the scans
  and the tests parse them. `BaseWorker.error()` is still the one place that counts errors.
- **Termination is race-free.** The Manager now treats a worker as idle only while it holds
  that worker's unanswered `ready`, and receives each worker's messages in order (see the
  docstring at the top of `manager.py`). Before, a walk could
  hang (a rank stuck in `MPI_Send`) or silently drop directories.
- **Errors end every rank.** A bad option or directory on rank 0 exits all ranks with rank 0's
  status (1 for a missing directory, 2 for a usage error) instead of hanging them. (Open MPI's
  mpiexec returns that status; Cray PALS's returns 143, as it SIGTERMs the remaining ranks
  once the first exits non-zero.) Any other
  uncaught exception, on any rank, calls `MPI_Abort`.
- **Errors are counted.** Directories that cannot be scanned are summed over all ranks. When
  there are any, the summary gets a `Scan Errors: N entries could not be scanned` line and
  stdout gets a final `--> N entries could not be scanned` line. `--fail-on-errors` makes the
  exit status 2 in that case (non-zero, 143, under PALS); the default stays 0.
- **Deterministic ordering.** Type, user and group lists break ties by name, so the summary no
  longer depends on which rank saw what first. (The size heaps already broke ties by path.)
- **`--max-dirs-per-send N`** [200]: subdirectories a worker collects before sending them to
  the Manager (replaced by `--local-queue-max` in phase 3.5).

### Phase 3

- **A package with console scripts.** `walkstat`/`walktar` replace the symlinks to `run.py` and
  its dispatch on `argv[0]`.
- **Progress payload.** Every worker message carries one `(items, bytes, extra)` tuple
  (`protocol.py`); `extra` is for derived tools' own counters. `dir_reply` is `(dirs, payload)`.
- **Collectives.** The ranks' stats combine in one tree reduction (`comm.reduce` with
  `WalkStats.merged`) onto rank 0. Before, each list, and each list of each user, was its own
  gather-and-broadcast to every rank. rank 0 warns if the reduced totals differ from what its
  progress payloads said.
- **Reports.**
  - Column headers, as `#` comment lines.
  - `-`, not `1969-12-31`, for a directory with no files.
  - Aligned user and group tables.
  - A `Scanned:` line.
  - Userlists start with `User:`, `Total Count:` and `Total Size:` lines.
  - Columns (size, items, last access, path) and section names are as before.
- **Paths are printable.** In every report and the spreadsheet, undecodable bytes in a name
  show as `\xNN` and control characters as `\n`, `\t`, `\r`, `\xNN`, so each entry is one line
  of valid UTF-8. Before, a non-UTF-8 name crashed the summary writer, and a newline split an
  entry over two lines.
- **Per-user lists use `--heap-size`.** Before they were 200 entries, whatever the option said.
  Every printed list is capped at 250 entries.
- **Removed `-e/--errors`.** It was never implemented; the scans redirect stderr.
- **No abbreviated options.** `--summary x.xlsx` used to be taken as `--summary-prefix x.xlsx`,
  so `all_cs.sh` and `test_scan_dir.sh` never wrote their spreadsheet; they now pass
  `--spreadsheet`.
- **One blocking receive per message.** The Manager receives with ANY_SOURCE + ANY_TAG
  instead of probing each worker in turn (phase 2's loop). The termination rule is unchanged:
  every message matches the one receive, so each worker's messages still arrive in order.
  Benchmarked on derecho against the deployed scanner, with the CGD-ppc (3.6M items) and
  CMIP6 (4.5M items) filesets:
  - **1 node, 96 ranks:** phase 2's probing loop was 5–8% faster than the deployed scanner.
  - **4 nodes, 384 ranks:** phase 2's loop was 17% slower. Its pass over 383 workers took
    2.6–44 ms, and the Manager almost never slept. The blocking receive keeps the Manager
    blocked in `recv` 98% of the time, at 7–9 µs per message. It was 5–7% faster than the
    probing loop. Against the deployed scanner it was 9% faster on CGD-ppc and 10% slower
    on CMIP6.
  - **Collectives and reports:** about 3 s, against 12–20 s for the deployed scanner.
- **`--verbose`** adds a per-rank table (rank, host, dirs, files, size) and the Manager's
  counters:
  - messages received, and the handling time per message
  - time blocked in `recv`
  - how long each `ready` was held before its `execute`, mean and longest (this includes
    waiting for work to arrive)
- **walktar closes its last tar file.** Its end-of-archive blocks were left to interpreter
  shutdown.
- Dropped the oldest-mtime/atime heaps, which were collected but never reported.

### Phase 3.5

- **Workers walk their own queue (phase 3.5).** A worker pushes the subdirectories it finds
  onto a local stack and walks them itself, depth-first, and asks the Manager for work only
  when that stack is empty. Directories reach the Manager only to fix imbalance: as
  *overflow* (the oldest half of a stack that grew past `--local-queue-max`) or as the
  answer to a *share* request, which the Manager sends to the busiest workers whenever it
  has idle workers and nothing to give them. The termination argument is unchanged (see
  `manager.py`); the design and its alternatives are in
  [docs/walk-load-balancing.md](../../docs/walk-load-balancing.md).
  - **`--local-queue-max N`** [1000]: subdirectories a worker keeps before it ships the
    oldest half to the Manager (replaces `--max-dirs-per-send`).
  - **`--dirs-per-dispatch K`** [8]: directories the Manager hands an idle worker at once.
  - The progress line shows `queued` (directories on the workers' local stacks), `idle`
    and `shares`. The end of the run reports where the Manager's directories came from,
    the directories walked per rank, and the fraction of the walk the workers spent
    waiting for the Manager to answer a `ready` with work. `--verbose` adds each worker's
    wait and stack high-water mark to the per-rank table. With the Manager mostly blocked
    in `recv`, a large waiting fraction means starvation (too little shared work), not a
    slow Manager.
  - The progress payload gains the stack length: `(items, bytes, queued, extra)`;
    `execute` carries a list of directories.
  - A worker sends its counts at least every 10 s (`WORKER_UPDATE_SECONDS` in
    `common/worker.py`), whatever `--progress` says. `--progress` is only how often the
    Manager prints its progress line. Before, workers walking their own stacks sent counts
    only every `--progress` seconds, so with the scans' 600 the progress line lagged.
  - **Measured on derecho** ([docs/phase3.5-results.md](../../docs/phase3.5-results.md)), with
    the CGD-ppc and CMIP6 filesets:
    - **1 node × 96:** 13–19% faster than phase 3 and 13–24% faster than the deployed
      scanner.
    - **4 nodes × 384:** 25–43% faster than phase 3 and 30–50% faster than the deployed
      scanner.
    - **The Manager:** blocked in `recv` 98–100% of the time at every rank count, handling
      5–21% of phase 3's messages.
    - **The ceiling is the filesystem client on each node.** On one node, 32 ranks walk
      faster than 96. On 4 nodes, 4 × 32 beats 4 × 96 by 21–44%.
    - **The defaults (`--local-queue-max` 1000, `--dirs-per-dispatch` 8) measured best.**
      Moving work between ranks sooner cuts the waiting, but costs more in lost locality
      than it saves.
- **Walks run as root keep uid 0's per-user directory lists.** A directory owned only by
  uid 0 was skipped, because the single-owner test was a truth test on the uid.

### Phase 4

- **Filters** (see [Filtering](#filtering-phase-4) above): `--exclude` prunes by name;
  `--{m,a,c}time-{older,newer}`, `--user`, `--group`, `--min-size`, `--max-size` and `--type`
  select. Without a filter, every report is unchanged.
- **The share and progress checks count entries scanned,** not entries counted, so a huge
  directory whose entries a filter mostly rejects still answers the Manager's `share`.
- **Measured on derecho** (CGD-ppc, 3.6M items, 5 rounds, mean of rounds 2–5, against `main`
  at `c971cd7`):

  | | wall s, new | wall s, main | items/s, new | items/s, main |
  |---|---|---|---|---|
  | 1 node × 32 ranks, unfiltered | 114.0 | 114.9 | 32,084 | 31,853 |
  | 4 nodes × 32 ranks, unfiltered | 38.1 | 37.9 | 101,322 | 103,215 |

  - Both differences are well inside the run-to-run noise (±10–15%).
  - Without filters, the summary and all 20 userlists are identical to `main`'s, apart from
    the timestamp line.
  - A filtered walk (`--atime-older 3y --exclude 'COLD_STORAGE*'`, counting 1.2M of 3.6M
    entries) took 114.0 s, the same as an unfiltered one. The walk is bound by the `stat`
    calls, and a filter adds none.

### Phase 5

- **walktar archives file batches** (see [File batches](#file-batches-phase-5)
  above): `--batch-files` and `--batch-size` replace `--tar-queue-depth`. There is one tar per
  batch, plus one `-dirs` tar per rank.
- **Protocol:**
  - `share_reply` carries `(dirs, batches, payload)`;
  - `execute` carries `(dirs, batches)`;
  - the payload's `queued` counts a worker's closed batches as well as its stack.
- **Measured on derecho**, 1 node × 32 ranks, on synthetic trees on scratch (Lustre), with the
  tars on scratch:

  | tree | `main` (one tar stream per directory) | phase 5, 1,000 files / 16 GiB per batch |
  |---|---|---|
  | `bigdir`: 1,024 × 1 GiB, one directory | 2,433 s, 0.42 GiB/s, 1 rank | 416–423 s, 2.4–2.5 GiB/s, 31 ranks (5.8×) |
  | `manyfiles`: 200,000 × 1 MiB, one directory | ~8,400 s (extrapolated), 0.024 GiB/s | 270–282 s, 0.7 GiB/s, 31 ranks (~30×) |

  - **Batch size:**
    - 100 GiB batches reached only 11 ranks on `bigdir` (1,077 s); 32 GiB took 480 s and 8 GiB
      436 s.
    - 5,000-file batches left `manyfiles`' busiest rank with 9.8 GiB against a mean of 6.3
      (423–466 s).
    - Hence the defaults of 1,000 files and 16 GiB.
  - **GPFS and Lustre, each way**, 1 × 32 at the defaults. The GDEX trees (read-only, on
    campaign GPFS) are `day_1`, 468 × 4.9 GiB, and the VIT zarr, 333k × ~4 MiB. "csg" is
    campaign GPFS (`/glade/campaign/cisl/csg`). "Cut off" runs stopped at 900 s and give a
    rate only; "no copybufsize" ran before the 16 MiB chunks.

    | tree, read → write | `main` | phase 5 |
    |---|---|---|
    | scratch `bigdir`, Lustre → Lustre | 2,433 s, 0.42 GiB/s | 408–423 s, 2.4–2.5 GiB/s |
    | scratch `manyfiles`, Lustre → Lustre | ~8,400 s, 0.024 GiB/s (24.5 files/s) | 260–282 s, 0.7 GiB/s |
    | GDEX `day_1`, GPFS → GPFS (csg) | 881 s, 2.59 GiB/s | 362 s, 6.31 GiB/s (no copybufsize) |
    | GDEX VIT zarr, GPFS → GPFS (csg) | cut off, 0.10 GiB/s (26 files/s) | 410 s, 3.15 GiB/s (no copybufsize) |
    | GDEX `day_1`, GPFS → Lustre (scratch) | cut off, 0.56 GiB/s | 421 s, 5.43 GiB/s |
    | GDEX VIT zarr, GPFS → Lustre | cut off, 0.09 GiB/s | ~460 s, 2.81 GiB/s |
    | scratch `bigdir`, Lustre → GPFS (csg) | cut off, 0.61 GiB/s | 359 s, 2.85 GiB/s |
    | scratch `manyfiles`, Lustre → GPFS | cut off, 0.03 GiB/s | 247 s, 0.79 GiB/s |

  - **Reading Lustre caps a node:** 31 streams reading scratch reach 2.4–2.9 GiB/s whatever
    they write to; reading GPFS, 5.4–6.3 GiB/s. Writing to Lustre costs a little (6.31 →
    5.43 GiB/s).
  - **One stream is slow whenever Lustre is at either end:** 0.42–0.61 GiB/s, against 2.59
    GiB/s GPFS → GPFS.
  - **Small files cost about 40 ms each per stream** on both filesystems (24.5–26 files/s).
    Phase 5.5 found where (below).
  - The node's NIC is 200 Gb/s (~25 GB/s), and `mktree` wrote 17 GB/s from one node in 1 MiB
    writes: walktar is far from the hardware.
  - **walkstat** (CGD-ppc, 1 × 32, 5 rounds, the mean of rounds 2–5): 105.3 s against
    `main`'s 102.7 s, best runs 102.6 against 102.1 s. This is within the noise; the walk
    itself is unchanged.

### Phase 5.5

- **Where a small file's ~40 ms went:** a profile of `tar.add()` on 300 `manyfiles` members
  (1 MiB each, derecho login node) gave 38.8 ms per file.
  - 35 ms of it is the source file's one `read()`: the storage's latency on a cold read. It
    is the same on campaign GPFS (37.5 ms per ~4 MiB file of the GDEX VIT zarr).
  - lstat, open, close and the tar header together cost about 1.2 ms. The owner and group
    name lookups cost 0.04 ms.
  - Reading in parallel scales almost linearly: 16 threads read 500 files/s on scratch and
    389 on GPFS, against 26–30 one at a time. `posix_fadvise(WILLNEED)` changed nothing on
    either filesystem.
  - Hence the reader threads (`--read-ahead`, [above](#walktar)), not a faster `tar.add()`.
- **Measured on derecho**, 1 node × 32 ranks, tars on scratch, two rounds each. Each
  `manyfiles` run follows a `bigdir` run, whose 2 TiB of I/O flush the caches: scratch's
  servers answer a re-read of a recently read file in ~5 ms instead of ~35.

  | `--read-ahead` | `bigdir`: 1,024 × 1 GiB | `manyfiles`: 200,000 × 1 MiB |
  |---|---|---|
  | phase 5 (`tar.add()`) | 416.3, 416.9 s (2.46 GiB/s) | 274.5, 268.1 s (0.72 GiB/s) |
  | 4 | 429.7, 408.2 s | 84.2, 81.2 s (2.37 GiB/s, 3.3×) |
  | **16 (default)** | 417.9, 427.9 s | **54.2, 53.4 s (3.64 GiB/s, 5.0×)** |
  | 32 | 424.1, 421.1 s | 57.4, 54.7 s |

  - Big files are unchanged within the noise: 408–430 s for every setting.
  - With read-ahead 16, 31 streams read small files from scratch at 3.6 GiB/s. Phase 5's 31
    streams reached 2.4–2.9 GiB/s reading scratch.
- **On campaign GPFS**, GDEX VIT zarr (333,110 files, ~4 MiB each, one directory) → csg, 1
  node × 32, two rounds each (two jobs):

  | `--read-ahead` | wall | write rate |
  |---|---|---|
  | phase 5 (`tar.add()`) | 422.7, 425.9 s | 3.03–3.06 GiB/s |
  | 4 | 380.3, 374.3 s | 3.40–3.45 GiB/s |
  | 8 | 366.6, 372.3 s | 3.47–3.52 GiB/s |
  | 16 (default) | 378.8, 360.6, 377.8, 380.7 s | 3.39–3.58 GiB/s |

  - Every setting is 10–15% faster than phase 5, and the thread count makes no difference.
  - **The scan sets the pace.** walktar's scanning rank stats every file before it batches
    it. Phase 5's scanner had 320k entries by its first progress line (30 s) and all 333k
    by its second (60 s). With read-ahead 4, 8 or 16 it made ~900–1,000 entries/s and was
    still scanning in the run's last 30 s. Meanwhile the other ranks were idle with one or
    two batches queued.
  - Why the scanner's stats slow down while the other ranks read was not determined.
- These runs used PALS's default binding, which pins each rank to one CPU (`taskset` showed
  ranks on CPUs 0, 1, 2, ...), so a rank's reader threads share that CPU.
- **Deferred** (the scan sweep; see the phase 5.5 PR):
  - walktar's scan as walkstat's: names from `scandir` without a stat, batches cut by
    count, each file stat'ed and filtered by the rank that archives it. `--batch-size` would
    then need the archiving rank to split a batch.
  - Or parallel stats in the scanning rank.
  - `--read-ahead` with `--cpu-bind depth`.
  - Per-filesystem defaults, if a later measurement needs them.
  - Why the scan slows on GPFS.

### Part B (the sweep: walktar onto stat batches)

- **walktar's scan is walkstat's** ([File batches](#walktar) above): the scanning rank stats
  only the subdirectories and cuts the other names into stat batches by count; the rank that
  processes a stat batch stats, filters and counts each name and closes file batches by the
  real `st_size`. `BaseWorker`'s inline scan is gone, so every tool walks the same way, and
  a file that vanishes between the scan and its stat costs one error instead of the rest of
  its directory. The tars, the members (`tar.add()`'s), the golden output and the Manager's
  protocol are unchanged.
- **Measured on derecho**, 1 node × 32 ranks, `--read-ahead` 16, PALS's default binding, two
  rounds each, against a side copy of `main` at 3ba9582 (Part A: phase 5.5's walktar). Each
  `manyfiles` run follows a `bigdir` run (the scratch cache, phase 5.5). "CPU" is user+sys
  per worker rank, mean over the 31, from GNU `time` around each rank; it includes the time
  an idle rank spends spinning in MPI's receive, so a rank that waits costs as much as one
  that works (the Manager's CPU is its wall time for that reason).

  | tree, read → write | `main` (3ba9582) | Part B |
  |---|---|---|
  | scratch `bigdir`: 1,024 × 1 GiB, Lustre → Lustre | 434.9, 424.0 s (2.35–2.42 GiB/s); CPU 174, 170 s | 447.1, 421.5 s (2.29–2.43 GiB/s); CPU 167, 168 s |
  | scratch `manyfiles`: 200,000 × 1 MiB, Lustre → Lustre | 61.4, 58.8 s (3.19–3.33 GiB/s); CPU 40, 42 s | 58.8, 58.5 s (3.32–3.34 GiB/s); CPU 41, 42 s |
  | GDEX VIT zarr: 333,110 × ~4 MiB, one directory, GPFS → GPFS (csg) | 380.1, 360.9 s (3.40–3.58 GiB/s); CPU 199, 219 s | 402.3, 404.4 s (3.19–3.21 GiB/s); CPU 107, 104 s |

  - **Scratch is unchanged**, within the noise, for big and small files alike.
  - **The GPFS case did not gain: the scan was not its ceiling.** Phase 5.5 read the scanning
    rank's ~1,000 entries/s as the pace-setter. With Part B the scan is over at once (at the
    first progress line, 30 s in, 139 stat batches were waiting and no rank was idle, where
    `main` showed 3–15 idle ranks at every line), yet both run at 3.2–3.6 GiB/s, about
    850 files/s: the same ceiling every `--read-ahead` setting met in phase 5.5. The
    scanner's rate matched the archive rate; it did not set it. What limits VIT from one
    node is the storage path for 4 MiB files, not the walk, so the lever there is more nodes
    (phase 5.5's question 5), not the scan.
  - **Part B evens the load**: `main`'s scanning rank archived 3.9–4.3 GiB against the others'
    ~42; the least any rank archives is now 31–35 GiB.
  - **The VIT rounds are 6–11% slower with Part B**, both rounds, so not noise. A
    `--profile` run (job 8129483, 14 intact worker profiles) says why: of a worker's ~400 s,
    97 s are in `lstat`, nearly all of it the stat batches' serial calls on the worker
    thread, about 9 ms each on campaign GPFS under the readers' load (an unloaded GPFS
    lstat costs ~1 ms, phase 5.5), with the rank's 16 readers idle meanwhile. So each rank
    read nothing for a quarter of its time, where `main` did those stats on one rank,
    overlapped with the other 30's reads. Hence the next entry, Part B's second commit. The
    csgteam `scan_dirs` array (8 subjobs of 1 × 128) was running throughout these campaign
    runs, as during Part A's.
  - **The halved CPU on VIT** (199–219 → 104–107 s per rank) is mostly `main`'s idle ranks
    spinning in MPI, not work saved.
  - **`--cpu-bind depth --depth 4`** (job 8128929, Part B, one round): `bigdir` 417.0 s, as
    before; **`manyfiles` 45.0 s against 58.5–58.8** (4.35 GiB/s), so PALS's one CPU per
    rank costs the 16 reader threads about 23% on small files, and nothing on big ones.
    Phase 5.5's question 2 is answered for scratch; the VIT row with the same binding is
    below.
- **Part B's second commit: the stat batch's lstats in the reader threads.** walktar's
  `lstats()` runs a stat batch's `os.lstat` calls `--read-ahead` at a time in the reader
  pool, yielded in order (the same `in_flight()` as the reads), so the counting, the
  batches and the tars are as before and `--read-ahead 0` is the serial path. walkstat's
  stat batches stay serial (it has no threads; a pool for them would be its own option,
  and the same ~6–9 ms per GPFS lstat says it would pay).
  - **VIT with `--cpu-bind depth --depth 4`** (job 8129484, Part B's first commit, the
    serial stat batches still in): **287.6 s, 4.49 GiB/s**, against 361–380 s for `main`
    and 402–404 s for the same code under PALS's default binding, under the same
    production-scan load. So on campaign GPFS too the ceiling was the one CPU per rank
    that the 16 reader threads shared, not the scan and not the storage path: phase 5.5's
    question 2, answered on both filesystems. Worker CPU 58 s per rank (the ranks spend
    their time in I/O, not spinning).
  - **VIT re-measured with the threaded lstats** (job 8129867, `main` 3ba9582 against this,
    two rounds, PALS's default binding, the production scans running): **`main` 375.5,
    387.2 s; this 400.8, 402.4 s.** The threaded lstats bought nothing: the same 4–7% gap
    as the first commit, and the same even load (every rank 38.6–43.1 GiB against `main`'s
    3.9–46.8). So the serial stat batch was not the cost either. What is left is the
    stats' CPU: under PALS's one CPU per rank, each rank's CPU is the bottleneck (the
    `--cpu-bind depth` row above), and Part B moved ~333k lstats, with their Python, from
    `main`'s scanning rank, whose CPU was otherwise idle, onto the 31 busy ones. The
    profile of this run (job 8129868, 31 worker profiles, 398.7 s) does not settle it:
    `lstat` fell from 97 to 24 s per rank, yet the generator that waits on the threaded
    lstats (`TarWorker.lstats`) shows 210 s cumulative, so the rank waited on the shared
    reader pool, not on the stat calls; its CPU is 110 s of 399, so it is not saturated
    either; and the files' own calls dominate (per rank, summed over threads: `read` 94 s,
    `write` 92, `close` 80, `open` 56: ~7 ms per close and ~5 per open on loaded GPFS). An
    open point for the multi-node work.
  - **VIT with `--cpu-bind depth --depth 4`, this code** (job 8131570): **268.7 s, 4.81
    GiB/s**, the fastest VIT run so far: against 287.6 s for Part B's first commit under the
    same binding (the threaded lstats pay 7% once the CPU is not the limit) and 361–387 s
    for `main` under PALS's default. So Part B plus the binding is 1.34× over `main` on the
    tree it was for, and the binding, not the walk, is what to fix in the launcher.

### Part C (the sweep: indexes)

- **`--index`** on walkstat and walktar ([Indexes](#indexes-the-sweeps-part-c) above), off by
  default; `O_NOATIME` in walktar's readers. Nothing else changed: the members are still
  `tar.add()`'s and the golden output never moved.
- **Smoke run on derecho** (job 8132015, `hfu-bench/benchC.pbs`; 1 node × 32 ranks, PALS's
  default binding, scratch trees, every timed run after a warm-up so all of them see the
  same caches, two rounds of each pair; the checkers of `common/index.py` on round 1's
  output: closure of every file, no `E` path twice across the files, the `E` set against
  `find`, and each `.idx` against its tar's `getmembers()` with offsets and a sample of the
  bytes at `offset_data`):

  | walk | without `--index` | with `--index` | index |
  |---|---|---|---|
  | walkstat `million`: 1,000,000 empty files, one directory | 1.88, 1.64 s | 1.76, 1.73 s | 121 MiB in 31 files, 1,000,000 `E` + 31 `P` (~127 bytes per record); checker 16 s |
  | walkstat `manyfiles`: 200,000 × 1 MiB, one directory | 0.56, 0.57 s | 0.59, 0.68 s | 200,000 `E` + 31 `P`; checker 3.4 s |
  | walktar `manyfiles` → scratch, `--read-ahead` 16 | 57.4, 51.8 s; CPU 40.5, 40.7 s per rank | 53.5, 53.1 s; CPU 41.1, 40.3 s | 201 `.idx` (200 batch, 1 dirs), 200,001 `E` + 200 `P`; checker 4.9 s, 201 tars matched |

  - **The index costs nothing measurable**: within the noise on every pair, in wall time and
    in CPU. A record is one dict lookup, one formatted line and a buffered write, next to a
    stat (or a read) that costs a storage round trip.
  - **Lustre honours `O_NOATIME`**: three sampled files' atimes were the same before and after
    a full walktar read of the tree (the owner's run); the index's atimes are the found ones.
  - The 1M-entry index is 121 MiB; a per-rank file holds one `P` for the root and its share
    of the `E` records, since every rank's batches came from one directory.
  - `du` right after a write under-reports on Lustre (16 KiB for 200,000 lines), so the
    sizes above are the million run's, taken a minute later; nothing stayed on scratch.

### perf-1, step A (CPU binding and `--read-ahead`)

- **The question** (`docs/GLADE_PERFORMANCE.md` 7): should walktar size `--read-ahead` from
  the CPUs a rank has, or should the launcher bind by default? **The launcher binds**
  ([CPU binding](#cpu-binding-perf-1-step-a) above): the scan scripts' `mpiexec` lines carry
  `--cpu-bind depth --depth 2`, every run prints `CPUs per rank: N`, and `--read-ahead`
  stays 16. Nothing in the walk changed.
- **What a rank gets**, from `os.sched_getaffinity(0)` on every rank (`hfu-bench/affinity.py`,
  and each bench rank's own `Cpus_allowed_list`), 32 ranks on one node (128 cores, 256
  hardware threads):

  | `mpiexec` | CPUs per rank | the masks |
  |---|---|---|
  | PALS's default | 1 | rank r on CPU r |
  | `--cpu-bind depth --depth 2` | 2 | `0-1`, `2-3`, … `62-63`: two cores each, half the node |
  | `--cpu-bind depth --depth 4` | 4 | `0-3` … `124-127`: every core |
  | `--cpu-bind depth --depth 8` | 8 | `0-7` … `248-255`: every hardware thread |
  | `--cpu-bind none` | 256 | one mask for all |

- **Measured on derecho** (jobs 8135797 and 8135798 on scratch; 8135799 and 8135800 on
  GDEX VIT → csg; 1 × 32, `--read-ahead` 16, two rounds, the bindings' order rotated
  between rounds, each `manyfiles` run after a `bigdir` run; the csgteam `scan_dirs` array
  (8 subjobs) ran throughout the campaign runs). Scratch's second round (and a re-run of
  it, job 8136917) met outside load on Lustre, `bigdir` at 1.1–1.7 GiB/s instead of 2.5:
  those numbers are marked "loaded" and compare only with each other. "CPU" is user+sys per
  worker rank, mean over the 31 (it includes MPI's spin while a rank waits).

  | binding (CPUs per rank) | scratch `bigdir`: 1,024 × 1 GiB | scratch `manyfiles`: 200,000 × 1 MiB | GDEX VIT zarr: 333,110 × ~4 MiB → csg |
  |---|---|---|---|
  | PALS's default (1) | 412.6 s | 62.3 s; CPU 36 s | 385.1, 442.3 s (3.35, 2.92 GiB/s); CPU 97, 105 s |
  | `depth 2` (2) | 403.8 s; loaded 483.1 s | **39.7** s; CPU 36 s; loaded 75.6 s | **275.9, 272.7 s** (4.68, 4.74 GiB/s); CPU 67, 71 s |
  | `depth 4` (4) | 404.3 s; loaded 901.2, 917.5 s | 43.5 s; CPU 42 s; loaded 82.9, 79.4 s | 276.3, 321.8 s (4.68, 4.02 GiB/s); CPU 50, 53 s |
  | `depth 8` (8) | 412.7 s; loaded 870.5 s | 40.5 s; CPU 41 s; loaded 77.1 s | 266.7, 325.7 s (4.84, 3.97 GiB/s); CPU 51, 54 s |
  | `none` (256) | 405.0 s | 44.6 s; CPU 47 s | 271.0, 341.7 s (4.77, 3.78 GiB/s); CPU 65, 71 s |

  - **One CPU per rank was the ceiling, and any wider binding lifts it the same**: on
    small files every binding from two cores up is within the noise of every other, 1.4–1.6×
    over the default on both filesystems (`manyfiles` 62 → 40–45 s, VIT 385–442 → 267–342 s
    with the campaign noisier in the second round); big files never move. The width past
    two cores buys nothing: the 16 readers of a rank are waiting on storage round trips, not
    competing for CPU, once they have two cores between them.
  - **The CPU per rank falls as the binding widens** on VIT (97–105 → 50–54 s at `depth 4`):
    less time spinning in MPI and in the kernel's scheduler, not less work; the two-core
    rows spend 67–71 s and finish no slower.
- **`--read-ahead` against the binding** (jobs 8136189, 8136190, 8136191, one round each,
  the same trees; the 16 rows are from the table above):

  | binding | `--read-ahead` | `manyfiles` | VIT → csg |
  |---|---|---|---|
  | `depth 2` | 8 | – | 275.5 s (4.69 GiB/s) |
  | `depth 2` | 16 | 39.7 s | 275.9, 272.7 s |
  | `depth 2` | 32 | loaded 58.8 s (`bigdir` 720 s) | 286.3 s (4.51 GiB/s) |
  | `depth 2` | 64 | loaded 57.5 s (`bigdir` 846 s) | – |
  | `depth 4` | 8 | 51.9 s (3.77 GiB/s) | 313.0 s (4.13 GiB/s) |
  | `depth 4` | 16 | 43.5 s | 276.3, 321.8 s |
  | `depth 4` | 32 | 39.1 s (5.00 GiB/s) | 334.4 s (3.86 GiB/s) |
  | `depth 4` | 64 | 43.7 s (4.48 GiB/s) | – |

  - **16 is where it stops paying.** 8 threads lose 20% on `manyfiles` (and 10% on VIT at
    `depth 4`); 32 and 64 are within the noise of 16 on scratch and no better on campaign.
    So walktar does not derive the thread count from the mask: with one CPU, 16 threads
    beat 4 by 1.6× (phase 5.5), with two or more they are already enough, and the mask
    cannot tell a rank what the storage can overlap.
- **Bench hygiene, learned here:** scratch was under outside load from the second round
  on (`bigdir` at 1.1–1.7 GiB/s, 617–918 s, where it reads 2.5 GiB/s otherwise), which
  pushed job 8135798 past its walltime in its last run (the 574 GiB of tars it left were
  deleted by hand) and made a re-run pointless (stopped). A `bigdir` time is the
  storage-state control for the `manyfiles` run after it: trust a small-file pair only
  when its `bigdir`s agree. Under the load the ranking held (`depth 2` 75.6 s, `depth 8`
  77.1, `depth 4` 79.4–82.9), which is as much as that round says.
- **The quiet-day repeat of scratch's second round** (indexes-2; job 8142408,
  `benchbind.pbs` with `ROUND_OFFSET=1` and `TOOL=mainD`, `main` at ff27254). The order
  was `depth 2`, `depth 4`, `depth 8`, `none`, default. The `bigdir` controls agree with
  round 1, so the storage was comparable:

  | binding | `bigdir`, round 1 → 2 | `manyfiles`, round 1 → 2 | `manyfiles` CPU, round 2 |
  |---|---|---|---|
  | PALS's default (1) | 412.6 → 421.7 s | 62.3 → 60.5 s | 42.8 s |
  | `depth 2` (2) | 403.8 → 412.4 s | 39.7 → **60.5** s | 32.8 s |
  | `depth 4` (4) | 404.3 → 432.0 s | 43.5 → 50.4 s | 40.3 s |
  | `depth 8` (8) | 412.7 → 422.3 s | 40.5 → 50.4 s | 38.5 s |
  | `none` (256) | 405.0 → 432.4 s | 44.6 → 50.4 s | 44.5 s |

  - **The default's one-CPU ceiling reproduced**, and 4 CPUs or more lifted it again, by
    1.2× this time instead of 1.4×.
  - **`depth 2` did not.** It matched the default at 60.5 s, with its workers busy only
    54% of the wall, so they were waiting rather than short of CPU. It ran first in the
    job, where round 1's first run was the default's. Two rounds cannot say which `depth 2`
    result is the outlier.
  - The recommendation stands on the campaign rows, where `depth 2` was best or equal in
    both rounds, but on scratch's small files the evidence is now split. A third round
    with `depth 2` not first would settle it.

### perf-1, step C (walkstat `--stat-threads`)

- **`--stat-threads K`** [0]: a stat batch's lstats K at a time in K threads, yielded in
  order through the same `in_flight()` as walktar's reads, so the counting, the directory
  lists and the golden output are as with the serial calls (0). walktar's stat batches
  already ran this way in its reader threads (Part B's second commit); `StatWorker` now
  owns the mechanism (`stat_depth()`, `stat_pool()`, `lstats()`), and `TarWorker` points
  them at `--read-ahead` and its readers. Tests: the golden summary and userlists with
  `--stat-threads 4` and with `--batch-files 3 --stat-threads 2`; `lstats()` keeps the
  order and yields an error in place at 0, 2 and 16 threads; the split-directory and
  vanished-file tests at 0 and 3 or 4 threads; the option's bounds.
- **Measured on derecho** (job 8138431; 1 × 32, PALS's default binding, `--verbose`, two
  rounds with the order rotated; the csgteam `scan_dirs` array, 8 subjobs, running
  throughout; GDEX read-only, walkstat opens no file):

  | tree | serial (0) | `--stat-threads 4` | `--stat-threads 16` |
  |---|---|---|---|
  | GDEX VIT zarr: 333,110 files, one directory (campaign) | 22.6, 14.6 s (15–23k items/s) | 20.7, 18.5 s | 18.8, 19.8 s |
  | GDEX `.Radiosonde`: 584,851 entries, 292 directories (campaign) | 31.6, 28.3 s (19–21k items/s) | 31.9, 31.1 s | 34.0, 34.6 s |
  | scratch `million`: 1,000,000 empty files, one directory | 66.2 (cold), 1.8 s | 2.2, 2.2 s | 2.2, 2.3 s |
  | scratch `manyfiles`: 200,000 × 1 MiB, one directory | 14.5 (cold), 0.8 s | 0.9, 0.9 s | 0.9, 0.7 s |

  - **On an unloaded campaign the threads buy nothing.** A stat cost 1.5–2 ms today (31
    ranks × ~600 stats/s each), and 4 or 16 in flight per rank moved the walk nowhere:
    15–23k entries/s is the node's GPFS client, the ceiling phase 3.5 found (32 ranks
    beat 96 on one node; CGD-ppc at 32k items/s at 1 × 32), and it is a throughput limit,
    not a latency one, so overlapping the round trips does not lift it. Scratch's
    sub-millisecond stats are the same within the noise once the first walk has warmed
    the metadata server (the cold first walk of each tree is the 66 and 14.5 s).
- **The loaded case is the one the pool was for** (job 8138503, `hfu-bench/benchstatload.pbs`:
  each round starts a walktar of VIT → csg on the same node, 32 ranks × 16 readers at
  3.3–3.5 GiB/s, and runs the three walkstats of `.Radiosonde`, 32 ranks unbound, one after
  another while it reads; the walktar's tars deleted after, csg empty; the scans running):

  | `.Radiosonde`, 584,851 entries | while the node reads campaign at 3–4 GiB/s | after the read ended |
  |---|---|---|
  | serial (0) | **343.3 s** (1,724 items/s: ~18 ms per lstat per rank) | 20.6 s |
  | `--stat-threads 4` | **194.7 s** (3,031 items/s) | 36.7 s |
  | `--stat-threads 16` | **64.6 s** (9,387 items/s) | 37.7 s |

  - **Under the readers' load a stat costs ~18 ms, and the pool gives it back**: 16 threads
    walk 5.3× faster than the serial batches, 4 threads 1.8×, where unloaded the three are
    within 2× of each other at the client's ceiling. The loaded stat is latency, which
    threads overlap; the unloaded one is throughput, which they cannot lift. That is the
    campaign case the scans meet when a walktar or a copy shares the node (or, from the
    Part B profile, when the node's own readers do), and the reason to keep the option.
  - **The default stays 0.** The scans run alone on their nodes, where the serial batches
    are at the ceiling already; `--stat-threads 16` is for a node that is also reading.
    (The `--cpu-bind none` walkstat ranks shared the node with the walktar's 64 bound
    cores; the unloaded rows of this job and of job 8138431 agree, so the sharing itself
    cost nothing.)

### perf-1, step B (multi-node walktar on campaign)

- **Phase 5.5's question 5:** does walktar's ~4.5 GiB/s per node add up across nodes on
  campaign? GDEX VIT zarr (333,110 × ~4 MiB, one directory) → csg at 1, 2 and 4 nodes × 32
  ranks, two cores per rank (step A), `--read-ahead` 16, the csgteam `scan_dirs` array
  (8 subjobs) running throughout. Each run's tars were deleted after it; csg holds nothing.
- **First, a hang.** The first two 2-node runs (jobs 8138513, 8139236) stopped at the
  2-minute progress line with 95–98% of the tree in the tars (the second: 327,110 of
  333,110 files) and every rank spinning in MPI's receive until the walltime or the
  bench's stall watchdog. The ranks' Python stacks (`kill -USR1 <pid>` now makes a rank
  write every thread's stack to stderr, `faulthandler` in `cli.py`) showed all 63 workers
  waiting for the answer to the `ready` they had sent and the Manager, whose last line
  said `idle=59, queued=3`, waiting for any message: four `ready`s that were sent were never
  matched by the Manager's receive. mpi4py receives a pickled message with a matched probe
  (`MPI_Mprobe` + `MPI_Mrecv`); with `mpi4py.rc.recv_mprobe = False` (`MPI_Probe` +
  `MPI_Recv`) the same run finished (job 8139309), and with the matched probe kept but the
  rendezvous threshold raised to 1 MiB (`FI_CXI_RDZV_THRESHOLD`) it hung again (job
  8139310). So **every pickled receive now goes through Probe + Recv** (`cli.py`, before
  `from mpi4py import MPI`; `HFU_RECV_MPROBE=1` restores the matched probe for
  experiments): one thread per rank talks to MPI, so the unmatched probe is safe. One node
  never leaves shared memory, which is why no single-node run ever saw this, and phase 4's
  multi-node walkstat shipped only small messages.
- **Measured** (jobs 8138512 and 8139452 at 1 node, 8139309 and 8139453 at 2, 8139454 at
  4, 8140120 at 8; "wall" is the job's, from `mpiexec` to exit, and "walk" the Manager's
  last progress line; "per node" is the walk's rate over the nodes; CPU is user+sys, the
  Manager's and the workers' mean per rank; the csgteam array's 8 subjobs ran throughout):

  | nodes × ranks | wall (walk) | write rate (walk) | per node | CPU: Manager; worker mean |
  |---|---|---|---|---|
  | 1 × 32, matched probe, two rounds | 324.8, 284.9 s | 3.98, 4.54 GiB/s | 4.0–4.5 GiB/s | 324, 285 s; 71, 68 s |
  | 1 × 32 | 280.6 s | 4.60 GiB/s | 4.6 GiB/s | 275 s; 69.5 s |
  | 2 × 32 | 150.5, 160.5 s | 8.58, 8.05 GiB/s | 4.0–4.3 GiB/s | 147, 155 s; 40.6, 40.4 s |
  | 4 × 32 | 90.3, 90.2 s (80.5, 82.6 s) | 14.3 GiB/s (16.1, 15.6) | 3.9–4.0 GiB/s | 81, 83 s; 23.5, 30.8 s |
  | 8 × 32 | 60.2, 60.1 s (55.3, 53.5 s) | 21.5 GiB/s (23.4, 24.1; 24.6 peak) | 2.9–3.0 GiB/s | 56, 54 s; 27.5, 26.8 s |

  - **The nodes add up.** Every node brings its ~4 GiB/s GPFS client to the sum: 4.6, 8.3,
    16 GiB/s for 1, 2, 4 nodes, with csg taking 16 GiB/s of writes without a sign of
    strain; 8 nodes reach 24 GiB/s (24.6 at the peak), 3 GiB/s per node, with the tail
    (below) now 40% of a 55 s walk: the per-node rate has not fallen, the walk has run
    out of batches to spread. Phase 5.5's question 5 is answered: the VIT-shaped case scales with
    nodes, and only with nodes.
  - **The Manager is still nowhere near its limit** (phase 5.5's question 4): at 4 nodes it
    handled 37,424 messages in 0.6 s of the 80 s walk (0.016 ms each), blocked in its
    receive 99.3% of the time. The share traffic is where the rank count shows: 36,253
    share requests for 607 batches handed out, 35,520 answered empty (4,236 and 4,170 at
    one node, 10,498 and 10,364 at two; 69,384 and 65,327 at 8 nodes, where the Manager
    is still 96.9% blocked and spends 1.7 s of 55 handling 70,538 messages): the
    ping-pong grows ~9× for 4× the ranks, and it costs each busy worker one probe and
    one small reply per request, a few hundred over the run, nothing measurable. Not
    worth a change at this scale.
  - **The tail is one batch.** At 4 nodes the last progress line before the end showed 6
    idle ranks with nothing queued: a 1,000-file batch of VIT is ~4 GiB, ~30 s for one
    rank, so the walk ends when the last ranks finish theirs, about 20 s after the first
    rank runs dry (the Manager's "first idle worker waited 21 s for the end", 14–16 s at
    1–2 nodes, 22–23 s of a 55 s walk at 8). A smaller `--batch-size` (1–2 GiB) would trim
    it where the tail matters: at 8 nodes it is the difference between 24 and ~30 GiB/s.
  - **Worker CPU per rank halves per doubling**, as the work per rank does; the Manager's
    CPU is its wall (it spins in the receive).

### indexes-2, step A (`--index=gz`)

- **`--index=gz`** on walkstat and walktar ([Indexes](#indexes-the-sweeps-part-c) above):
  the same records through gzip, one stream per file, `.gz` on each name. `--index` alone
  is the plain TSV, as before. `read_index()` and the checkers read either by the file's
  first two bytes, whatever its name. Golden output unchanged.
- **Measured on derecho** (job 8142389, `hfu-bench/benchD.pbs`): 1 node × 32 ranks,
  `--cpu-bind depth --depth 2`, scratch trees. A warm-up run comes first, then two rounds
  of no index, `--index` and `--index=gz`. The checkers ran on round 1's indexes: closure,
  no `E` path twice, the `E` set against `find`, and each walktar index against its
  tar's members, offsets and bytes. All passed, for both kinds. Sizes are apparent sizes
  (`stat`), since `du` under-reports on Lustre right after a write. CPU is the workers'
  mean user+sys per rank.

  | walk | no index | `--index` | `--index=gz` | index: plain → gz |
  |---|---|---|---|---|
  | walkstat `million` (1,000,000 empty files, one directory) | 2.27, 2.22 s; CPU 2.09, 2.09 s | 2.31, 2.27 s; CPU 2.18, 2.11 s | 2.38, 2.36 s; CPU 2.23, 2.20 s | 137.3 → 13.4 MiB (10.2×), 31 files |
  | walkstat `manyfiles` (200,000 × 1 MiB, one directory) | 0.75, 0.75 s; CPU 0.56, 0.56 s | 0.71, 0.76 s; CPU 0.58, 0.61 s | 0.74, 0.75 s; CPU 0.61, 0.63 s | 30.7 → 2.5 MiB (12.3×), 31 files |
  | walktar `manyfiles` → scratch | 41.7, 41.9 s; CPU 35.7, 34.8 s | 43.0, 41.4 s; CPU 36.0, 35.3 s | 41.7, 41.7 s; CPU 34.7, 35.5 s | 34.6 → 4.6 MiB (7.5×), 201 files |
  | walktar `million` → scratch (1,000 tars of headers) | 58.3, 64.3 s; CPU 17.6, 17.6 s | 57.1, 59.6 s; CPU 18.1, 18.4 s | 59.1, 65.9 s; CPU 18.2, 18.5 s | 151.5 → 19.9 MiB (7.6×), 1,001 files |

  - **gzip costs ~3 µs of CPU per record and nothing in wall time.** On `million` each
    rank writes ~32,000 records, and gzip adds ~0.1 s of CPU per rank over the plain
    index, in walkstat and in walktar alike. That is under the round-to-round noise of
    every wall time above. walktar `million`, all metadata and no data, is the index's
    worst case: the plain index adds 0.5–0.8 s of CPU per rank to 17.6 s, and the walls
    spread 57–66 s with or without it.
  - **The index shrinks 7–12×.** A walkstat record is ~144 bytes of text, mostly the path,
    which repeats from line to line. walktar's records compress less (7.5×), since its two
    offset columns change on every line.
  - **The checkers read gzip at about the same speed**: 16.4 s plain and 17.4 s gzip for
    walkstat's million records, 19.1 and 22.2 s for walktar's, 3.4 and 3.7 s for
    `manyfiles`.
  - The bench's tar check reads a few sampled files per tar without `O_NOATIME`, so those
    files' atimes move between runs. The tools' own reads do not.
  - Nothing stayed on scratch. A first submission (job 8142303) was stopped after its
    walkstat rows, because the bench passed the checkers unexpanded globs; its tars were
    deleted.

### indexes-2, step B (walksync `--index`)

- **`walksync --index`** (`--index=gz`): each rank's record of what it copied, in the
  index format ([Indexes](#indexes-the-sweeps-part-c) above), `tool=walksync` with `root=`
  and `dest=` in the header. `E` is an entry an rsync copied, recorded from the compare's
  source lstat after the rsync returned. Unchanged entries are not recorded, and a
  `--dry-run` writes the index the run would write, marked `dry-run=yes`. Without
  `--index` nothing changed: the copy batches are `FileBatch`es as before, and the golden
  output is the same.
- **What it costs:** one `lstat` per new directory (the scan stats no directory
  otherwise), and the copy batch carries an `IndexStat` per entry, ten integers, so a
  shared batch's message grows by ~100 bytes per entry. Next to an rsync per batch that
  is nothing, so no bench was run.
- **The oracle:** rsync's own dry run (`-nax[A][X] --chmod=Du+w --out-format='%i %n'`, no
  `--delete`), taken before each run. On a fresh destination, and after changes in one
  (a size, an mtime, a symlink's target, one name of a hard link, a removed subtree, a
  name with a newline, an extraneous file), the `E` set of the run's indexes, and of a
  dry run's, is exactly the set rsync says it would copy. Every record's fields equal the
  source's lstat.

### indexes-2, step C (the import contract, 4.8 closed)

- **The column set stays fixed at v1** for every tool (`docs/HANDOFF-sweep.md` 4.8 has
  why). gzip answers the size question that trimming would have, every column comes from
  the `lstat` already taken, and one format means one parser. A different set would be
  `# hfu-index v2`.
- **The import contract** ([Indexes](#indexes-the-sweeps-part-c), "Importing") now says
  what steps A and B add for a parser:
  - gzip files, told apart by their first two bytes;
  - the per-rank names, whose old pattern `index-*-r*.tsv` missed the unprefixed
    `index-r001.tsv`, each with an optional `.gz`;
  - `tool=walksync`, the header's other fields to ignore (`dest=`, `dry-run=`), and a
    `#` line starting each file's records in a `cat` of several;
  - and that walksync's `E` records are one run's copies, not a tree's usage.
- No code changed beyond `common/index.py`'s docstring.
