Forked from https://github.com/benkirk/mpi4py_task_runner

## Layout

| | |
|---|---|
| `cli.py` | the `walkstat` and `walktar` console scripts: options on rank 0, Manager on rank 0, a tool's worker on the others, then the collectives and the reports |
| `common/` | the walk itself, shared by every tool: `manager.py` (its docstring is the protocol and the termination rule), `worker.py` (BaseWorker), `protocol.py` (tags, the progress payload), `mpiclass.py` (the end-of-walk collectives), `stats.py` (what a rank accumulates, and how two ranks' combine), `heaps.py`, `progress.py`, `formatting.py`, `options.py`, `filters.py` |
| `stat/` | walkstat: `worker.py` (StatWorker), `report.py` (summary and userlists), `spreadsheet.py` |
| `tar/` | walktar: `worker.py` (TarWorker) |

Only `manager.py`, `worker.py`, `mpiclass.py` and `cli.py` touch MPI, and they import mpi4py when
called rather than when imported, so the rest unit-tests without it.

Outputs (in the working directory, written by rank 0):
- `summary-<prefix>` (`summary-output` without `--summary-prefix`), also echoed to stdout. The
  scans publish it as `<dir>/.usage/summary.txt`.
- `userlists/<prefix>/<user>`, one per user above `--threshold-size`/`--threshold-count`,
  published as `<dir>/.usage/users/`.
- `--spreadsheet FILE.xlsx`: three sheets per prefix (four when filtered), added to (or
  replaced in) FILE.
- walktar: `output-r{rank}-b{n}.tar` (one per batch) and `output-r{rank}-dirs.tar`, written
  by the workers (see [File batches](#file-batches-phase-5)).

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

The scanning rank turns the files it selects into **batches**: a batch closes after
`--batch-files` files [1000] or `--batch-size` bytes of `st_size` [16GiB], whichever comes
first. A file bigger than `--batch-size` makes a batch on its own. Batches are units of work,
just as directories are, so one huge directory no longer ties up one rank for hours.
- **Where batches go:**
  - The scanning rank keeps its batches and archives them itself, batches before
    directories. The open batch carries over from one directory to the next, so small
    directories combine.
  - A batch leaves its rank only in the answer to the Manager's `share`: the oldest half of
    the rank's closed batches, rounded up, plus the oldest half of its stack.
  - The Manager hands out one batch per `execute`, and hands batches out before directories.
  - The termination argument covers batches (see `manager.py`).
- **walkstat batches too**, but its batches are names still to stat (below).
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

## Behaviour changes since the csg-utils import

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
    is the same on campaign GPFS (37.5 ms per ~5 MiB file of the GDEX VIT zarr).
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

  - Big files are unchanged within the noise (408–430 s for every setting): a big file's
    time is its transfer, not the start of its read.
  - With read-ahead, 31 streams read small files from scratch at 3.6 GiB/s. Phase 5's
    2.4–2.9 GiB/s "Lustre read cap" was partly this latency.
  - These runs used PALS's default binding, which pins each rank to one CPU (`taskset`
    showed ranks on CPUs 0, 1, 2, ...), so a rank's reader threads share that CPU.
