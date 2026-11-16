# Walk tools: changelog and measurement log

What changed in `hpc_file_utils/walk/` since the import from csg-utils, sprint by sprint, with
the measurements each change was judged by. This is the dated record. It is kept as written:
an entry describes the code as of its sprint, and later entries supersede earlier ones.

- For how the tools behave now, read the [walk README](../hpc_file_utils/walk/README.md).
- For what the measurements say about GLADE, read [GLADE_PERFORMANCE.md](GLADE_PERFORMANCE.md),
  which collects them by topic.

The sprints, in order: the import and phases 1 to 3 (packaging, race-free termination),
phase 3.5 (local queues and sharing), phase 4 (filters), phases 5 and 5.5 (batches,
read-ahead), phase 6 (walksync), the sweep (Parts A to E: refactor, walktar onto stat batches,
indexes, logging), perf-1 (CPU binding, `--stat-threads`, multi-node walktar), indexes-2
(`--index=gz`, walksync `--index`, the import contract), tools-find-rm (walkfind, walkrm,
in-flight bytes, exit statuses) and tail-fixes (sharing the last directory, walking before
copying). "4F", "4D" and "3A" are directory pairs of the CGD migration (`sync/pairs.conf`);
VIT, `manyfiles`, `bigdir` and `.Radiosonde` are benchmark trees. `hfu-bench/` paths name
benchmark scripts kept outside this repository.

## Open

- **Tests that assert a timing outcome can fail on a loaded machine.**
  `test_walkstat_stats_a_big_directory_on_every_rank` and
  `test_every_rank_gets_work_through_sharing` require that more than one rank gets work
  through a `share`. A share reply needs the scanning rank to tick while others are idle; with
  more test workers than cores, the scanning rank can finish before an idle rank's request
  arrives. Both pass alone and in `make test-pbs` (noted in indexes-2; seen again on a
  4-core container).
- **One unexplained failure of the termination-race test** (perf-1): a single local run in
  which 370 of 500 race cases failed in 162 s, too fast for the usual timeouts. Not
  reproduced.
- **walksync's costliest-first copy order is not yet measured at scale**
  ([walk-tail-4F.md](walk-tail-4F.md), recommendation 3).

## Since the csg-utils import


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
  [docs/walk-load-balancing.md](walk-load-balancing.md).
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
  - **Measured on derecho** ([docs/phase3.5-results.md](phase3.5-results.md)), with
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

- **Filters** (see [Filtering](../hpc_file_utils/walk/README.md#filters) in the walk README): `--exclude` prunes by name;
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

- **walktar archives file batches** (see [File batches](../hpc_file_utils/walk/README.md#walktar)
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
  - Hence the reader threads (`--read-ahead`, [above](../hpc_file_utils/walk/README.md#walktar)), not a faster `tar.add()`.
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

- **walktar's scan is walkstat's** ([File batches](../hpc_file_utils/walk/README.md#walktar) in the walk README): the scanning rank stats
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

- **`--index`** on walkstat and walktar ([Indexes](../hpc_file_utils/walk/README.md#indexes) in the walk README), off by
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
  - **Lustre honors `O_NOATIME`**: three sampled files' atimes were the same before and after
    a full walktar read of the tree (the owner's run); the index's atimes are the found ones.
  - The 1M-entry index is 121 MiB; a per-rank file holds one `P` for the root and its share
    of the `E` records, since every rank's batches came from one directory.
  - `du` right after a write under-reports on Lustre (16 KiB for 200,000 lines), so the
    sizes above are the million run's, taken a minute later; nothing stayed on scratch.

### perf-1, step A (CPU binding and `--read-ahead`)

- **The question** (`docs/GLADE_PERFORMANCE.md` 7): should walktar size `--read-ahead` from
  the CPUs a rank has, or should the launcher bind by default? **The launcher binds**
  ([CPU binding](../hpc_file_utils/walk/README.md#cpu-binding) in the walk README): the scan scripts' `mpiexec` lines carry
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
  was `depth 2`, `depth 4`, `depth 8`, `none`, default. Times are the Manager's walk: this
  job's wall times came from a 10 s poll in the bench and run up to 10 s long (fixed
  since). Round 1's walls predate that poll and sit ~3 s above their walks, so its walks
  are used too. The `bigdir` controls agree, so the storage was comparable:

  | binding | `bigdir` walk, round 1 → 2 | `manyfiles` walk, round 1 → 2 | `manyfiles` CPU, round 2 |
  |---|---|---|---|
  | PALS's default (1) | 410.2 → 409.5 s | 59.4 → 54.2 s | 42.8 s |
  | `depth 2` (2) | 400.0 → 407.6 s | 36.9 → **49.1** s | 32.8 s |
  | `depth 4` (4) | 400.7 → 423.4 s | 40.7 → 37.8 s | 40.3 s |
  | `depth 8` (8) | 409.5 → 415.8 s | 37.8 → 40.6 s | 38.5 s |
  | `none` (256) | 401.2 → 418.3 s | 41.8 → 39.9 s | 44.5 s |

  - **The default's one-CPU ceiling reproduced**, and 4 CPUs or more lifted it again by
    1.35–1.45×, as in round 1.
  - **`depth 2` gained only 1.1× this time** (49.1 s against 36.9 in round 1), its workers
    busy 54% of the wall, so waiting rather than short of CPU. It ran first in the job.
    Two rounds cannot say which `depth 2` result is the outlier.
  - The recommendation stands on the campaign rows, where `depth 2` was best or equal in
    both rounds. On scratch's small files the evidence is now split between 2 and 4 CPUs
    per rank; a third round with `depth 2` not first would settle it.

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
  - **Measured since** (indexes-2; job 8144846, 8 × 32, `--depth 2`, `--read-ahead` 16,
    `TOOL=mainD`, `main` at ff27254; `benchbind.pbs` with its new `BATCH_SIZES`, rotated
    per round; the csgteam array's 8 subjobs ran throughout). "Walk" is the Manager's
    loop, "tail" is how long the first idle worker waited for the end, and the rate is
    the last progress line's average. A batch of VIT closes at `--batch-files` 1,000
    (~4 GiB) long before the default 16 GiB, so the default row is 1,000-file batches.

    | `--batch-size` | walk | rate | tail | tars |
    |---|---|---|---|---|
    | 16 GiB, the default (1,000 files) | 51.4, 56.0 s | 25.1, 23.1 GiB/s | 14.6, 23.7 s | 334 |
    | 2 GiB (~500 files) | **43.6, 44.5 s** | **29.6, 29.0 GiB/s** | 14.1, 15.3 s | 668 |
    | 1 GiB (~250 files) | 54.6, 43.5 s | 23.7, 29.7 GiB/s | 10.4, 6.0 s | 1,334 |

    - **2 GiB gives the predicted ~30 GiB/s, in both rounds**: the walk ~20% shorter than
      the default's, 29–30 GiB/s against 23–25.
    - **1 GiB shrinks the tail most** (6–10 s), but its first round was as slow as the
      default. Both rounds ran beside the production scans, and two rounds cannot say
      whether that was campaign noise or the 4× tar count.
    - **Smaller batches mean fewer share requests** (24,000–31,000 at 1 GiB against
      69,000–82,000 at the default): the busy ranks have batches to give, so idle ones
      ask less often.
    - Not a default change: this is one tree at one node count, and a smaller batch
      doubles or quadruples the tars, which a one-node run does not need. For a
      VIT-shaped tree at 8 nodes, `--batch-size 2GiB` is the measured choice.
    - The last 1 GiB run's walk finished (its summary was written), but the job's
      walltime killed it while its tars were being deleted; they were deleted by hand,
      and csg holds nothing. At 8 nodes a run's cycle is ~6 minutes, the walk ~1 of
      them: the per-run stats and deleting 1.3 TiB of tars on csg are the rest.
  - **12 and 16 nodes** (indexes-2; jobs 8145461 and 8145462, chained, the same settings,
    `--progress 10`, 1 GiB and 2 GiB batches, two rounds each). "Peak" is the highest
    10-second write rate on the progress lines.

    | nodes | `--batch-size` | walk | rate | peak | tail |
    |---|---|---|---|---|---|
    | 8 | 2 GiB | 43.6, 44.5 s | 29.6, 29.0 GiB/s | – | 14.1, 15.3 s |
    | 12 | 1 GiB | 42.5, 32.9 s | 30.3, 39.2 GiB/s | 46, 56 GiB/s | 12.4, 4.7 s |
    | 12 | 2 GiB | 43.3, 31.8 s | 29.8, 40.5 GiB/s | 79, 77 GiB/s | 26.8, 15.3 s |
    | 16 | 1 GiB | 26.0, 26.8 s | 49.7, 48.2 GiB/s | 66, 67 GiB/s | 8.4, 9.6 s |
    | 16 | 2 GiB | 27.2, 26.2 s | 47.3, 49.2 GiB/s | 107, 109 GiB/s | 12.1, 11.2 s |

    - **The rate keeps climbing with nodes**: ~49 GiB/s on average at 16 nodes, 1.7× the
      8-node rate, and 10-second peaks of 107–109 GiB/s. csg and GDEX had not flattened
      by 16 nodes, with the production scans running beside them.
    - **VIT is too small to say more.** At 16 nodes the walk is ~26 s, a third to two
      fifths of it the tail, with 1.3–2.7 batches per worker; the average is set by
      start-up and the tail, not by the filesystems. A sustained number at this scale
      needs a tree several times VIT's size (a separate decision: each run would write
      ~5 TiB to csg).
    - **1 GiB against 2 GiB made no difference to the walk** at 12 or 16 nodes. 1 GiB had
      the shorter tails and 2 GiB the higher peaks, and they canceled. At 12 nodes the
      round mattered more than the size (both ~43 s in round 1, ~32 s in round 2), the
      campaign's load moving between the rounds.
    - Per node: ~3.7 GiB/s at 8 nodes with 2 GiB batches, 2.5–3.4 at 12, ~3.0 at 16.
  - **Worker CPU per rank halves per doubling**, as the work per rank does; the Manager's
    CPU is its wall (it spins in the receive).

### indexes-2, step A (`--index=gz`)

- **`--index=gz`** on walkstat and walktar ([Indexes](../hpc_file_utils/walk/README.md#indexes) in the walk README):
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
  index format ([Indexes](../hpc_file_utils/walk/README.md#indexes) in the walk README), `tool=walksync` with `root=`
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

- **The column set stays fixed at v1** for every tool (`docs/history/HANDOFF-sweep.md` 4.8 has
  why). gzip answers the size question that trimming would have, every column comes from
  the `lstat` already taken, and one format means one parser. A different set would be
  `# hfu-index v2`.
- **The import contract** ([Indexes](../hpc_file_utils/walk/README.md#indexes), "Importing") now says
  what steps A and B add for a parser:
  - gzip files, told apart by their first two bytes;
  - the per-rank names, whose old pattern `index-*-r*.tsv` missed the unprefixed
    `index-r001.tsv`, each with an optional `.gz`;
  - `tool=walksync`, the header's other fields to ignore (`dest=`, `dry-run=`), and a
    `#` line starting each file's records in a `cat` of several;
  - and that walksync's `E` records are one run's copies, not a tree's usage.
- No code changed beyond `common/index.py`'s docstring.

### walksync at 4–16 nodes (indexes-2, measured)

- **The question:** does walksync scale with nodes the way walktar does? It was measured on
  GDEX `exchange/.Radiosonde` copied onto scratch: 584,851 files of ~80 KB in 292
  directories, 43.9 GiB. Jobs 8148321–8148324, one per node count, chained. Settings:
  32 ranks per node, `--cpu-bind depth --depth 4`, `--rsyncs 8`, `--batch-files 250`
  (~5 batches per worker even at 16 nodes), `main` at ff27254. Each of two rounds copied
  into a fresh destination, deleted after it; the csgteam array's 8 subjobs ran
  throughout. A single-directory tree such as VIT would measure only that directory's
  create rate (~450–520 files/s, the sweep's "K rsyncs"), whatever the node count.

  | nodes | walk | files/s | data rate | time per copy batch | tail | worker CPU (with rsyncs) |
  |---|---|---|---|---|---|---|
  | 1 (the sweep, 1,000-file batches) | 285 s | 2,050 | – | – | – | – |
  | 4 | 65.2, 65.5 s | 8,970, 8,930 | 0.67 GiB/s | 2.5 s | 3.4, 2.2 s | 14.9, 14.7 s |
  | 8 | 46.1, 43.4 s | 12,680, 13,490 | 0.95–1.01 GiB/s | 3.6 s | 4.0, 3.4 s | 12.9, 10.2 s |
  | 12 | 33.2, 33.4 s | 17,620, 17,510 | 1.31 GiB/s | 3.9 s | 2.6, 2.9 s | 8.7, 8.4 s |
  | 16 | 31.6, 32.9 s | 18,490, 17,780 | 1.33–1.39 GiB/s | 5.3 s | 3.4, 4.4 s | 7.6, 8.1 s |

  - **walksync scales to ~12 nodes and then flattens at ~18,000 files/s.** 8 nodes give
    1.45× 4 nodes, 12 give 1.35× 8, and 16 give 1.03× 12. Every run copied all 584,851
    entries with no failed batch and no error.
  - **It is a shared ceiling, not the walk.** The tails stay at 2–4 s (the 250-file batches
    did their job), and worker CPU falls with nodes. But each copy batch takes longer as
    nodes are added, 2.5 s at 4 nodes and 5.3 s at 16: the same files cost more when more
    run at once. The bytes are trivial (1.4 GiB/s); the per-file metadata is not. Each
    file is a create, a write, a rename, and the times, mode and xattrs set on scratch,
    plus an open and a read of a small file on campaign. Scratch has 4 metadata servers
    (MDTs), and the destination's directories spread over them. These runs do not say
    which side sets the ceiling. A copy from scratch to scratch, or walktar reading the
    same tree to csg, would separate them.
  - **Deleting a copy is the slow part of a bench**: 470–590 s per copy, ~1,000–1,250
    unlinks/s with 32 `rm` in parallel on one node, against the 18,000 creates/s that
    made it. A parallel remover on the walk's machinery would cut that, if it matters.

### tools-find-rm and tail-fixes

These two sprints wrote their notes into the tool sections of the walk README rather than
here. What they added:

- **walkfind** and **walkrm**, the fourth and fifth tools.
- **walksync's in-flight bytes**: big entries count in the progress line while `rsync` writes
  them. Before, a batch of one 900 GiB file showed nothing for an hour; on 4F (16 nodes) the
  line said 36.7 GiB/s while the destination's `df` grew at about 86 GiB/s.
- **Exit statuses under PALS**: only rank 0 exits non-zero, and last, after every rank has
  finalized MPI and a grace period (`HFU_EXIT_GRACE`). When every rank exited 1 at once, PALS
  terminated the ranks still finishing and the job showed 143.
- **A share takes the last directory** (the oldest half of a rank's stack, rounded up), and
  **walksync walks before it copies**. On 4F the walk, which `dfind` did in 66 s, had taken
  10.4 h behind the copies. The investigation and the measurements after the fix are in
  [walk-tail-4F.md](walk-tail-4F.md).
- **walkrm measured** (Derecho scratch, 2 nodes × 32): a mock of 4D's `cpl` with 741,000 files
  in one directory (917,204 entries) went in 144 s (137.7 s walk, 0.1 s rmdir pass) against
  `drm`'s 154 s on its twin. The names were scanned in 12 s and the 741 unlink batches spread
  over the ranks; then both tools ran at the directory's rate of about 6,000 unlinks/s.

### Costliest copies first, and the documentation pass

- **walksync copies its costliest batches first** ([walk-tail-4F.md](walk-tail-4F.md),
  recommendation 3). A copy batch's cost is `max(largest file, bytes ÷ --rsyncs)`, the bytes its
  longest `rsync` stream moves. A rank picks its costliest copy batch, a share takes compare
  batches and then the costliest copies, and the Manager hands out copy batches costliest
  first, after everything else. The other tools' order is unchanged. Checked by
  `tests/test_pick_order.py` and by a small end-to-end run; not yet measured at scale.
- **`common/filters.py` imports `argparse`.** Without it the package failed to import on
  Python 3.10 to 3.13; only 3.14's deferred annotations hid that.
- **`--help` shows each tool what it uses.** `walkfind` and `walkrm` no longer list the report
  options, `walkrm` no longer lists the filters it refuses, and `--fail-on-errors` is listed
  only by `walkstat` and `walktar`. The hidden options still parse, so no command line breaks.
  Each `--help` ends with examples.
- **The deploy smoke test covers all five tools.**
- **Documentation**: the walk README became a user guide and reference; this changelog moved
  out of it; the sprint hand-offs moved to [history/](history/).

