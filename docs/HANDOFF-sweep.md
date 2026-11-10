# Hand-off: the walk toolkit sweep, indexes, logging and old idioms (written 2026-11-09)

Phases 3–6 were additive. Each added a tool or a feature: local queues, filters, batches, walktar's
tars, walkstat's stat batches, walksync. Each copied the scaffolding it needed instead of
reshaping what was there. This note is the retrospective: what to consolidate now that the shape
is clear, two features that the consolidation makes easy, and the old idioms to retire. It is a
plan for a fresh session. **No code has changed yet.**

Part of PR #11 (walksync, phase 6), rebased onto main at `fe6daa1`, which holds phase 5.5 (PR #12,
walktar's reader threads). Every `file:line` below is against the rebased PR #11, under
`hpc_file_utils/walk/` unless a path says otherwise. Section 0 records what phase 5.5 and walksync's
derecho smoke run measured, and what they change in this plan; 0.4 (added after PR #11 merged) splits
walksync's per-file cost and decides for K rsyncs in flight per rank. The code work starts with Part A.

Read first:
- The module docstring of `common/manager.py`: the protocol and the termination argument.
- `hpc_file_utils/walk/README.md`: the "File batches (phase 5)" and "walksync (phase 6)" sections.
- [HANDOFF.md](HANDOFF.md), phases 5 and 6.

Contents:
0. What phase 5.5 and walksync's smoke run changed (re-evaluation, 2026-11-09)
1. Findings
2. Part A: the sweep (no behaviour change)
3. Part B: walktar onto stat batches (next after A2–A3)
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

## 0. What phase 5.5 and walksync's smoke run changed (re-evaluation, 2026-11-09)

### 0.1 What phase 5.5 measured (PR #12; the walk README's "Phase 5.5")
- **A small file's ~40 ms was the storage's latency, not Python.** In a profile of `tar.add()` on
  1 MiB files, 35 of 38.8 ms per file was the source's one `read()`. lstat, open, close and the
  header cost ~1.2 ms. The owner and group name lookups cost 0.04 ms. Campaign GPFS is the same.
- **`--read-ahead` [16] reader threads** read the batch's next files while the worker writes the
  current one. `manyfiles` (200k × 1 MiB, scratch) went from 271 to 54 s (5.0×, 1 × 32).
  `bigdir` (1 GiB files) is unchanged.
- **On campaign GPFS** (GDEX VIT zarr: 333k files in one directory → csg), every read-ahead
  setting (4, 8, 16) is only 10–15% faster.
  - walktar's scanning rank stats every file before batching it. With read-ahead running, its
    rate fell from phase 5's ~10,600 entries/s to ~900–1,000, and the other ranks waited for
    batches.
  - Why the stats slow down was not determined.
- **Bench hygiene, which any re-measure here needs:**
  - Scratch's servers answer a re-read of a recently read file in ~5 ms instead of ~35, so a
    big tree goes between repeat small-file runs.
  - PALS pins each rank to one CPU, which its reader threads share. `benchtar.pbs` takes
    `MPIEXEC_ARGS` to try `--cpu-bind depth`.

### 0.2 walksync's smoke run (PR #11, derecho, 2026-11-09)
1 node × 32, `--rsync /usr/bin/rsync` (3.2.7, with ACLs and xattrs), destinations on scratch:

| source | copy | rerun | rsync-seconds per file |
|---|---|---|---|
| GDEX `special_projects/earthmover_test`: 76,868 entries, 2.69 GiB, deep | 63 s, 103 batches | 2.5 s, nothing copied | 15.8 ms |
| GDEX `exchange/.Radiosonde`: 584,851 entries, 43.9 GiB, wide | 367 s, 617 batches | 23 s | 17.8 ms |
| scratch `tartree/manyfiles`: 200,000 × 1 MiB, one directory | 464 s, 200 batches | 9.6 s | 67 ms |

- **Correct:** the oracle (`rsync -naxAX --delete -i --chmod=Du+w`) found nothing but the
  destination root, which the script made and which the final rsync owns.
- **No-ACL warning:** with the env's conda-forge rsync (3.5.2, "no ACLs") the banner warned and
  `-A` was dropped. That rsync comes first on PATH after `config_env.sh`, so on derecho walksync
  copies no ACLs unless given `--rsync /usr/bin/rsync`. This is a PR #11 follow-up.
- **The ranks were busy, not waiting.** On `.Radiosonde`, rsync ran for 92% of the rank-seconds.
- **walksync pays a fixed cost per file inside each serial rsync,** the same shape as walktar
  before read-ahead.
  - On `manyfiles` that is 67 ms per file: 464 s, against walktar's 54 s with read-ahead on the
    same tree.
  - About 35 ms of that is the cold read (0.1). The rest is split in 0.4: about 8 ms to create,
    write and set a 1 MiB file on Lustre, under 1 ms of it for ACLs, xattrs and times.

### 0.3 What it changes here
- **Part B moves up,** from "deferred: plan only" to the next step after A2–A3, which make it
  small (section 8).
  - It is the measured fix for the GPFS case: the per-file stats move from the scanning rank into
    stat batches that every rank processes.
  - Its two levels, stat batches closing file batches sized by the real `st_size`, keep
    `--batch-size`. PR #12's deferred note had worried that a stat-free scan would lose it.
- **Part B's "bonus" is corrected (section 3).** Since PR #12 the worker builds each `TarInfo`
  with `gettarinfo(fileobj=)`, an fstat after the reader's read (0.018 ms). Saving that call is
  not a performance item.
- **Part C's walktar index gets the found atime for free (4.6).** Since PR #12, `open_member()`'s
  reader lstats each file before it opens and reads it. Returning that stat gives the pre-read
  atime with no extra call; only `--read-ahead 0` (`tar.add()`) still needs one.
- **A3's `error()` also absorbs `TarWorker.add_member`'s prints:** `tar/worker.py` now prints
  "Cannot add to tar file" in three places.
  - `TarWorker.readers` (the thread pool, started at the first batch and shut down in `run()`)
    has `RankFile`'s lifecycle but is not a file, so it stays as it is.
- **E4's "resolve names once on rank 0"** is tidiness, not speed: a lookup costs 0.04–0.06 ms on
  derecho (sssd's cache). The same holds for tarfile's own per-member lookups, so don't cache
  them for speed.
- **O_NOATIME** (phase 5.5 question 6: open each source with `O_NOATIME`, falling back on
  `EPERM`) belongs in `open_member()`, next to Part C. With both, a root walktar leaves atimes
  alone and still records the found one.
- **walksync's per-file cost is measured (0.4, 2026-11-09, no code change),** as question 1 did
  for walktar: one rsync over 1,000 files, timed and under `strace -c`; the same files over N
  rsyncs side by side; the read side alone against the destination side alone; and the same with
  the source or the destination on `/glade/campaign/flash_test/csg`, a small GPFS flash fileset,
  which takes the disk out of the per-file cost and leaves software (rsync, the GPFS client).
  Flash is a profiling aid only: the baseline must perform on the normal pools.
  - It is fileset `flash_test` (pool `system`, 699 TiB of flash disks) with a 600 TiB quota.
    Stay below one third of that: 200 TiB.
  - It is not in `gladequota`. Check its use with
    `/usr/lpp/mmfs/bin/mmlsquota --block-size auto -j flash_test csfs1`.
  - Remove everything after each run.

  N rsyncs scale as walktar's readers did, so the fix is several rsyncs in flight per rank:
  A5's `sync/rsync.py` run-and-poll loop is built for K rsyncs from the start (0.4 says how).
- **A common "in flight" piece for A3: the measurement points there (0.4).** walktar's readers and
  walksync's concurrent rsyncs are one idea: K units of I/O in flight per rank, with `share` and
  progress answered while waiting. One piece in `common/`, next to `entries()` and `ticked()`,
  beats two tool-specific mechanisms. What differs: walktar's units are files whose results are
  taken in order, walksync's are K processes awaited together.
- **A `--profile` option (A3):** each rank writes a cProfile file (`profile-r{rank}.prof`). It
  costs little when off. Phase 5.5's per-file split needed a separate script.
- **Still open from phase 5.5,** for after Part B:
  - question 2, the Lustre read side for big files, including `--cpu-bind depth`;
  - 3, LPT batch order;
  - 4, share ping-pong at 4 nodes;
  - 5, multi-node scaling on GDEX.

  Part B's re-measure may also answer why the scan slowed on GPFS.

### 0.4 walksync's per-file cost, measured (2026-11-09, no code change)

Job 8125066 on derecho (`dec1528`, one node, no MPI), `/usr/bin/rsync` 3.2.7 with walksync's exact
command (`-aAX --numeric-ids --chmod=Du+w --files-from --from0 SRC/ DEST/`) over 1,000-file
slices, a fresh slice for every cold measurement. Scratch's servers were flushed first by reading
the 1 TiB `bigdir`; the probe row says how cold each tree was. Every row ran pinned to one CPU with
`taskset`, as PALS pins a rank, except those marked free. Scripts in `hfu-bench/`: `syncprof.pbs`
(the job), `syncprof-stage.sh` (the flash slices, copied from a login node so the compute node's
GPFS client had never seen them), `syncprof-lib.sh` (the slices) and `syncprof-table.py` (these
tables from `hfu-bench/syncprof/8125066/results.tsv` and `strace-*.txt`). Nothing was left on
scratch, flash or csg.

Trees: `tartree/manyfiles` (1 MiB files, scratch, Lustre; slice k is its sorted names
[1000k, 1000k+1000)) and GDEX `exchange/.Radiosonde` (80 KB files, campaign GPFS; one station
directory per slice, its first 1,000 names; the 173 stations with at least 1,000 files qualify).

**Where a file's time goes** (ms per file; one rsync, or one process, pinned):

| | manyfiles, 1 MiB, scratch | .Radiosonde, 80 KB, campaign |
|---|---:|---:|
| coldness probe: 20 files, one `cat`, one after another | 56.9 | 20.2 |
| **one rsync, cold source → scratch** (the smoke run: 67 / 17.8) | **48.9** (16% CPU) | **20.0** (8% CPU) |
| the read side alone: one `cat` over the slice | 41.2 | 18.6 |
| the read side through rsync: cold source → `/dev/shm` | 51.9 | 19.6 |
| the write side alone: `/dev/shm` → scratch, `-aAX` | 8.6 (the first such run: 14.2) | 3.2 |
| … `-aA` / `-aX` / `-a` / `-rlpgoD` (no times) | 8.1 / 8.5 / 8.1 / 7.7 | 3.0 / 3.3 / 2.9 / 2.9 |
| rsync's own floor: `/dev/shm` → `/dev/shm` | 4.5 (99% CPU) | 0.5 (93% CPU) |
| source on GPFS flash (client cold) → scratch | 10.3 | 3.9 |
| `/dev/shm` → GPFS flash, `-aAX` / `-a` | 5.0 / 5.9 | 1.25 / 1.35 |
| 16 `cat`s at a time (`xargs -P 16`) | 2.9 | 1.3 |

- **The cold read is the cost,** as for walktar (0.1): 41 of 49 ms on scratch, 18.6 of 20 on
  campaign. The whole destination side (create, write, mode, owner, ACL, xattrs, times, rsync's
  stats of both sides) is ~8 ms per 1 MiB file on Lustre and ~3 ms per 80 KB file, and `-A`, `-X`
  and `-t` together are within the noise (≤ 0.9 ms). Dropping them would buy nothing.
- **It is the media, not the software.** With the source on the flash fileset the same rsync
  takes 10.3 and 3.9 ms; writing onto flash costs 0.5–1.4 ms over rsync's own floor. GPFS and
  Lustre add a few ms of metadata round trips (below); the disks add the rest.
- **strace** (`-f -w -c`: wall time in syscalls, summed over rsync's three processes; 50.9 / 21.9 s
  wall), ms per file: `read` 46.3 / 19.3 (the sender's file reads, and the receiver's pipe reads
  that wait on them); `pselect6` 91.5 / 37.2 (the processes waiting on one another, not on
  storage); the metadata calls together 4.6 / 5.0: `openat` 0.95 / 0.90, `newfstatat` 0.80 / 0.94,
  `close` 0.68 / 0.29, `rename` 0.59 / 0.61, `fchmod` 0.54 / 1.48, `utimensat` 0.37 / 0.29,
  `llistxattr` 0.34 / 0.20, `chmod` 0.30 / 0.25; `write` 2.07 / 0.37.
- **rsync's CPU is not free:** 7.7 ms of CPU per 1 MiB file (user 4.0, sys 3.7: three processes,
  two pipes, a whole-file checksum), 1.6 ms per 80 KB file. That caps the pinned scaling below.
- The 32-rank smoke run saw 67 ms per file where one rsync sees 49: its 31 streams at ~15 files/s
  each made ~460 files/s in all, far under what scratch gave walktar's 31 × 16 readers (3,700).
- One outlier, not pursued: a serial `cat` of the flash slice took 18 ms per 1 MiB file, nearly
  all system time (15.3 of 18.2 s), while rsync read a like slice from flash at 10.3.

**N rsyncs side by side** (one slice split N ways, all started at once; ms per file of the group,
and the CPU share of the wall):

| N | manyfiles, pinned to one CPU | manyfiles, free | .Radiosonde, pinned | .Radiosonde, free |
|---:|---:|---:|---:|---:|
| 1 | 49.7 (16%) | 45.6 (18%) | 17.7 (9%) | 20.4 (8%) |
| 2 | 24.2 (32%) | 23.5 (34%) | 9.3 (16%) | 9.7 (17%) |
| 4 | 12.0 (62%) | 10.8 (72%) | 5.1 (31%) | 4.7 (36%) |
| 8 | **8.1 (91%)** | 5.6 (141%) | **2.6 (60%)** | 3.0 (59%) |
| 16 | 8.3 (91%) | **3.1 (259%)** | 2.3 (74%) | **1.6 (123%)** |

- They scale as walktar's readers did: linear to N = 4 on both trees and both bindings. Pinned to
  one CPU, manyfiles saturates at N = 8 (6.1×), where that CPU is 91% busy with rsync's 7.7 ms per
  file; .Radiosonde reaches 6.7× at 8 and 7.9× at 16 (1.6 ms of CPU per file leaves room). With the
  CPUs free, 16 rsyncs reach 3.1 and 1.6 ms per file, 15× and 13×: the same rate as 16 parallel
  `cat`s (2.9 and 1.3), which is the storage's parallel read rate, as read-ahead 16 reached for
  walktar.

**Decision: yes, K rsyncs in flight per rank.**
- A5's `sync/rsync.py` gets the loop as a function: split the copy batch's paths into K lists,
  start K rsyncs at once (each `--files-from` one list, the same options), poll them together,
  answer `share` and progress between polls, and the batch is done when all K have exited. No
  protocol change: a batch stays one unit of work with one accounting, and a failing rsync fails
  the batch as today. `rsyncn()` in `syncprof.pbs` is exactly this shape.
- **Default K = 8**, an option (`--rsyncs K`; 1 is today's behaviour): 6× on 1 MiB files and 7× on
  80 KB files under PALS's one-CPU binding, with that CPU at ~90%. 4 is the safe floor (4×
  everywhere); 16 pays only with `--cpu-bind depth`. To be measured at 1 × 32 with Part B's
  benches: manyfiles should go from 464 s toward ~60 (32 × 125 files/s ≈ 4,000 files/s, walktar's
  read-ahead rate), .Radiosonde from 367 toward ~60.
  - **Measured at 1 × 32 in Part A (section 2, "`--rsyncs` at scale"): 1.1× on manyfiles, 2.4×
    on .Radiosonde.** The option exists with default 1; the default flips once the shared
    ceiling at scale is understood.
- The A3 common piece (0.3) is confirmed: one `common/` helper for "wait on K things in flight,
  answering `share` and progress meanwhile", with walktar's readers and walksync's K rsyncs as
  its two callers.
- Once K ≥ 8 on one CPU, rsync's CPU per file matters as much as latency: Part B's bench records
  user+sys per rank, and `--cpu-bind depth` gets a row there (phase 5.5 question 2 lists it).
- Bench hygiene confirmed: the probe row is the coldness check (scratch's warm re-read is ~5 ms,
  0.1); campaign GPFS has no cheap flush, and its probe at 20 ms was cold by itself.

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

`error()` exists twice (`stat/worker.py:103-106`, `sync/worker.py:82-85`). `BaseWorker`,
`TarWorker.add` and `TarWorker.add_member` (`tar/worker.py:81-128`, three copies) count and print
inline instead.

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
  102, 125, 130) and seven `appname` branches in `common/options.py` (lines 78, 80, 82, 100, 108, 128, 135; 80 and
  108 are walktar's `--read-ahead`).
- **Tool settings carried on `args`.** These are `written_verb` and `written_units`
  (`common/options.py:158`) and `rsync_missing`. They are read back with
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
`TarWorker.dirs_tar` (`tar/worker.py:166-170`, closed in `run()`), `SyncWorker.dryrun_file`
(`sync/worker.py:125-131`, closed in `run()`), and glade_hsm's `LazyWriter`
(`tools/glade_hsm/libexec/glade_hsm_scan_filelist.py:12`). The index (Part C) would be a fourth.

### 1.6 Smaller duplicates and leftovers
- **Duplicated code:**
  - `sync/compare.py:57-61` `show()` re-implements `common/formatting.py:44-59` `printable()`,
    which escapes every control character, not only `\n` and `\t`.
  - `SECONDS_IN_YEAR` is defined in `common/stats.py:63` and `common/filters.py:33`. The comment
    there says "as common/worker.py", which is stale.
  - The `parse_size` / `InvalidSize` → parser-error wrapper appears three times:
    `common/options.py:101-107`, `common/options.py:117-121` and `common/filters.py:163-168`.
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

### Part A, done (2026-11-09, branch `sweep-A`, one commit per step)
- `grep "app =="` finds nothing; a fourth tool is one `<tool>/tool.py` and one
  `<tool>/worker.py`, added to `walk/tools.py`. The golden output never changed
  (`HFU_UPDATE_GOLDEN` was never needed); `make test` 486 passed, `make test-pbs` on derecho
  326 passed, CI green.
- **Size:** by `wc -l` of the non-test package, main was 3,030 lines, A1–A4 brought it to
  3,000, and A5's `Tool` classes (seven small modules, docstrings included) took it to 3,181.
  The "smaller than today" target is E1's (54 bare returns, ~110 banner lines), not Part A's.
- **A3's performance check** (job 8126155, 1 × 32, `hfu-bench/benchA.pbs`): walkstat, main
  (`hfu-bench/mainA`, 5ad81a9) against the branch, three rotated rounds after a warm-up, best of
  three. A 1,000,000-empty-file directory (`tartree/million`, made by the job; delete it with
  `tartree`): main 1.874 s, branch 1.898 s. `tartree/manyfiles` (200,000 files): 0.587 against
  0.597 s. The warm-up itself (the directory just made, client cache cold) took 31.5 s. Both
  trees were cached for the timed rounds, which is the strictest case for a per-entry cost:
  the generator in the hot loop is within noise (≤ 2%), so `entries()` stays in `StatWorker`.
- **`--rsyncs` at scale** (the same job, 1 × 32, `/usr/bin/rsync`, copies onto scratch, the
  oracle clean, each copy deleted):

  | tree | K = 1 | K = 8 | gain | aggregate |
  |---|---:|---:|---:|---|
  | `manyfiles`: 200,000 × 1 MiB, one directory, scratch → scratch | 443 s | 400 s | 1.1× | 452 → 505 files/s |
  | `.Radiosonde`: 584,851 × 80 KB, 292 directories, campaign → scratch | 912 s | 382 s | 2.4× | 641 → 1,531 files/s |

  - 0.4's one-rank 6× does not carry to 31 ranks: something shared caps the copy. On one big
    directory the aggregate is ~500 files/s whatever K is (the smoke run's 31 × 1 rsync made
    460); on 292 directories K = 8 still gains 2.4×. A login-node probe (32 streams × 1,000
    create + rename + chmod on scratch) took 26.5 s into one directory and 14.1 s into 32, so
    the destination directory is part of it, not all of it (that probe's 1,200 files/s is above
    walksync's ~500: rsync's two xattr lists, the chown, the 1 MiB write and the utimes per
    file add up).
  - Campaign GPFS varies with the center's load: this K = 1 copy of `.Radiosonde` took 912 s
    where the morning's smoke run (0.2) took 367. During this job the production scans
    (csgteam's `scan_dirs` array, the old walkstat, 8 subjobs of 1 × 128 at once over campaign
    filesets) were running: eight nodes of stat traffic on GPFS's metadata. Only pairs from one
    job compare, and a bench on campaign should say what else was running (`qstat -u csgteam`).
  - So the "walksync's K rsyncs" step of section 8 is not just the default flip: first find
    the ceiling at 1 × 32 (a create + write probe in one directory against many, 80 KB against
    1 MiB files, K = 1/4/8; and `--cpu-bind depth` for the 8 × 31 rsyncs' CPU), then set the
    default. It shares Part B's bench runs, as planned.
- **The same on Casper** (the same scripts and slices; Casper mounts the same filesystems
  over its own network, and ran these on a 34-core node held by a sleep job, `crhtc98`, with
  the test copy's `conda-env-casper`: mpi4py on the host's Open MPI 5.0.8). To work on a Casper
  node interactively: `qsub -q casper -l select=1:ncpus=34:mpiprocs=32:mem=200GB -- /bin/sleep
  9000`, then `ssh <node>` from a login node attaches to the job; `pbsnodes -a` shows which
  node sizes are free (34-core nodes were, 62-core were not).
  - `manyfiles` → scratch, 1 × 32: K = 1 805 s, K = 8 398 s (2.0×). **K = 8 lands where
    derecho's did (400 s, ~505 files/s)**: two clients on two networks meet at one number, so
    the ceiling on one big directory of 1 MiB files is the storage's, and Casper's slower
    per-stream path (K = 1 twice derecho's 443 s) is what K recovers.
  - **Step 0 on Casper** (`syncprof.pbs`'s body run on the held node, `PBS_JOBID` exported;
    `hfu-bench/syncprof/6586743/`), `manyfiles` rows, ms per file, Casper against derecho:
    the coldness probe 161 against 57; one rsync 117–157 against 49; a serial `cat` 58
    against 41; 16 `cat`s at once **2.98 against 2.90**; the write side (`/dev/shm` →
    scratch) 8.1–9.7 against 8.1–8.6; `/dev/shm` → `/dev/shm` 3.9 against 4.5; the flash rows
    equal (11.9 against 10.3, 5.1 against 5.0). strace: `read` 101 against 46 ms per file,
    and every metadata call about twice derecho's (`openat` 1.66 against 0.95, `rename` 1.13
    against 0.59, `fchmod` 0.95 against 0.54; 8.8 against 4.6 ms per file in all). So
    Casper's path to Lustre doubles each round trip and leaves the parallel capacity as it
    is, which is why K matters more there: N rsyncs pinned to one CPU 117 → 55 → 28 → 12.5 →
    8.1 ms (N = 1, 2, 4, 8, 16: 9.4× at 8, the one-CPU floor at 16), free 105 → 53 → 24 →
    11.2 → 4.9. The write side and GPFS flash are the same from both machines.
  - **`.Radiosonde` (campaign GPFS) is the same from Casper**: one rsync 20.7 against 20.0 ms
    per file, a serial `cat` 21 against 18.6, 16 `cat`s 1.39 against 1.28, N rsyncs pinned
    20.5 → 10.7 → 5.3 → 3.1 → 2.4 (derecho 17.7 → 9.3 → 5.1 → 2.6 → 2.3); strace `read` 20.7
    against 19.3. Only the rows that write onto scratch pay Casper's Lustre latency (`/dev/shm`
    → scratch 4.4 against 3.2 ms; flash → scratch 5.7 against 3.9). So the extra latency is
    on Casper's path to Lustre (LNet routers), not to GPFS.
  - `.Radiosonde` → scratch, 1 × 32: K = 1 402 s, K = 8 362 s (1.1×); derecho's pair was 912
    and 382 under the production scans. **Both machines and both trees meet at the same K = 8
    numbers**: ~500 files/s for 1 MiB files into one directory (398–400 s), ~1,550 files/s for
    80 KB files across 292 directories (362–382 s). K = 1 sits 1.1× to 2.4× below that,
    depending on per-stream latency and the day's load. So at 1 × 32 the scratch's small-file
    create and write path is the ceiling, K = 8 reaches it from either machine, and K buys the
    most where per-stream latency is high (Casper to Lustre, or a loaded GPFS). That is the
    brief for the "K rsyncs" step: measure that ceiling (creates per second into one directory
    against many, by file size, from both machines), not K alone.
  - **Casper's host Open MPI 5.0.8 launcher segfaults under the test suite.** `make test-pbs`
    there (the same script, `-q casper -l select=1:ncpus=34:mpiprocs=34:mem=200GB`) gave 14, 4
    and 6 failures in three runs, every one `exit -11` within 0.2 s with no output at all:
    `mpiexec` itself, before any rank ran, on random tests. Not `TMPDIR` (the same with it on
    `/local_scratch`, the node's tmpfs), and not reproducible outside pytest (1,000 launches of
    `mpiexec -n 4 hostname` and of walkstat, serial, 8 at a time, and with the PBS variables
    set: 0 failures). With the conda-forge Open MPI instead (`HFU_MPI=conda`, built in a
    separate checkout `hpc-file-utils-condampi` so the test copy's `conda-env-casper` stayed
    the host-MPI one): **326 passed, 2 skipped, no `exit -11`**, derecho's result. On Casper,
    use `HFU_MPI=conda`; whether the Makefile's `auto` should pick it there is a site decision.
- **Shape decisions that differ from the plan above:**
  - `in_flight(items, start, depth, discard)` is the common piece of 0.4, in `BaseWorker`:
    walktar's `read_ahead()` is it with the reader threads, walksync's `copy_batch()` is it
    with K `Rsync` processes (`sync/rsync.py`), each yielded handle waited for in turn and
    `tick()` (share + progress) between the polls.
  - `--rsyncs K` exists and `copy_batch()` runs K rsyncs over `split()`'s K parts of a batch,
    the batch still one unit of work that fails if any part does. **The default is 1** (today's
    behaviour: Part A changes none) until the measurement at scale below confirms 8; the
    "walksync's K rsyncs" step of section 8 is then the one-line default change plus its
    README row.
  - `--profile` is in `cli.run_walker()`, around `walker.run()` on every rank (the Manager
    included), not in the worker.
  - `Tool.banner()` prints the `Filters:` line too, so walksync's "partial copy" note can
    follow it as before; `Tool.report()` is the tool's part of `cli.report()` between the
    balance lines and the last lines.
  - `BaseWorker.process_file()` is now defined (the hook walktar overrides); it was only
    ever called before.
  - `common/report.py`'s `columns()` is public; `stat/report.py` imports it as `_columns`.
- **Not done, on purpose:** `MPIClass.collect` still calls `finish_directories` for every
  tool (section 7); E3's `make_options()` test helper; the README's phase notes beyond the
  Layout table, the `--profile` and `--rsyncs` entries.

### "K rsyncs", done (2026-11-09, branch `sweep-B`: the default is 8)
- **The ceiling, measured** (`hfu-bench/createprobe.py` + `probe-matrix.sh`: PROCS processes
  each create 1,000 files of SIZE bytes as rsync's receiver does, a temporary name, write,
  fchmod, close, utime, rename; `benchK.pbs` ran it on derecho, `probe-casper.log` on a
  claimed Casper node; files/s, the 32-process rows twice):

  | | derecho | Casper |
  |---|---:|---:|
  | 1 process, 80 KB / 1 MiB (ms per file) | 1.9 / 3.0 | 2.4 / 3.1 |
  | 32 processes, one directory, 80 KB | 1,231, 1,296 | 1,007, 977 |
  | 32 processes, one directory, 1 MiB | 1,283, 1,262 | 1,030, 1,003 |
  | 32 processes, a directory each, 80 KB | 2,621, 4,278 | 4,258, 3,519 |
  | 32 processes, a directory each, 1 MiB | 3,815, 4,174 | 3,921, 4,240 |

  - **One directory caps creates at ~1,250/s (derecho) or ~1,000/s (Casper), whatever the
    size**; many directories reach 2,600–4,300/s. The file's bytes are not the limit (80 KB
    and 1 MiB are the same), the directory is. A single stream costs 2–3 ms per file, so
    32 streams into one directory are ~10× slower each than alone.
- **walksync at 1 × 32** (`benchK/8129741`, `/usr/bin/rsync`, copies onto scratch, the oracle
  clean, each rank under GNU time, the csgteam scans running):

  | tree | K = 1 | K = 4 | K = 8 | K = 8, `--cpu-bind depth --depth 4` |
  |---|---:|---:|---:|---:|
  | `manyfiles`: 200,000 × 1 MiB, one directory | 451.5 s (443 files/s) | 565.5 s | 385.5 s (519 files/s) | 449.7 s |
  | `.Radiosonde`: 584,851 × 80 KB, 292 directories | 891.9 s (656 files/s) | 405.4 s | 341.7 s (1,712 files/s) | **285.2 s** (2,051 files/s) |

  - **Into one directory walksync makes ~450–520 files/s at any K**, 2.5× below the probe's
    ~1,250 creates/s: rsync's own directory operations per file (the stat of the destination
    name, the temporary name, the rename, the xattr and ACL calls) share the directory's
    rate, so 31 × K rsyncs cannot beat it however many there are. K = 4 was slower than K = 1
    on that tree; the scratch runs are noisy to ±15%, as the probe's many-directory rows are.
  - **Across 292 directories K pays: 892 → 405 → 342 s for K = 1, 4, 8 (2.6×), and 285 s
    (3.1×, 2,050 files/s) with `--cpu-bind depth`**, where the 8 × 31 rsyncs are not pinned
    to 31 CPUs. Part A had 912 → 382 under the same scans.
  - Worker CPU: 49–52 s per rank on `.Radiosonde` at K = 4–8, 69 s at K = 1 (the long run's
    MPI spin); 72–103 s on `manyfiles`. The oracle passed every copy (the one non-time line
    on `.Radiosonde` is the destination root's own attributes, as in Part A).
- **Decision: `--rsyncs` defaults to 8.** Over every pair measured (Part A: derecho 443 → 400
  and 912 → 382, Casper 805 → 398 and 402 → 362; here 451 → 385 and 892 → 342) K = 8 is
  the fastest or within noise of it and never slower than K = 1: 2.6× on a tree of many
  directories, 1.2× into one directory, where the directory's own create rate binds and
  no K can help. 0.4's one-rank 6–7× does not carry to 1 × 32 because that ceiling is per
  destination directory, not per stream; the lever for a one-directory copy is more
  nodes, not more rsyncs. `--cpu-bind depth` adds another 1.2× on the many-directory tree
  (the rsyncs' CPU) and nothing on the one-directory tree. `sync/tool.py`'s help text and
  the walk README say so. `test_options.py` expects 8.

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
  (`tar/worker.py:198-199`): a file read costs far more than an `iprobe`.
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
  - the run-and-poll loop of `copy_batch` (`sync/worker.py:291-336`), as a function that runs K
    rsyncs over K slices of the batch and takes a "while waiting" callback (0.4: K = 8 by default)
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

## 3. Part B: walktar onto stat batches (next after A2–A3)

The sweep's planning first left this as a plan only. Phase 5.5 measured the need (0.1): on one huge
GPFS directory, walktar's scanning rank, which stats every file, sets the pace.

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
- **With the reader threads.** The stat batch's processing hands each selected file's lstat
  along with its path.
  - The reader in `open_member()` can then skip its own lstat. It keeps the `O_NOFOLLOW` open
    and the fstat type check.
  - Part C's index uses the same lstat.
  - Building `TarInfo` from that lstat instead of `gettarinfo(fileobj=)` saves only an fstat
    (0.018 ms), so it is not a reason to change. The members must stay `tar.add()`'s
    (`tests/test_tar_members.py`).
- **Risks and measurements:**
  - Stats move from the scanning rank to whichever rank takes the batch.
  - walktar's golden summary must not change.
  - Re-measure on derecho against the phase 5.5 walktar numbers, with read-ahead 16, 1 node × 32
    ranks, before merging. The filesets:
    - scratch `bigdir` and `manyfiles`, with a `bigdir` run between repeat `manyfiles` runs;
    - the GDEX VIT zarr → csg, the case this is for.

    Record the numbers in a "Part B" entry after the walk README's "Phase 5.5".
  - Watch the stat batches' own rate under read load. If their stats slow as the scanner's did,
    it shows up as per-batch time, not idle ranks.

### Part B, done (2026-11-09, branch `sweep-B`)
- **Shape:** `TarWorker(StatWorker)`. `StatWorker.process_batch` hands every entry it stats
  and counts to a `process_file(path, statinfo)` hook (a no-op for walkstat, one bound-method
  call per entry); walktar's puts a non-directory into the open `FileBatch` through
  `add_to_batch(path, st_size)`, so `--batch-size` closes by the real size and `--batch-files`
  by count, as before. `TarWorker.process_batch` dispatches on `isinstance(batch, StatBatch)`;
  a `FileBatch` goes to `tar_batch()`, phase 5.5's loop unchanged. `TarWorker.process_directory`
  still writes the directory into the dirs tar, then runs walkstat's scan.
  `BaseWorker.process_directory` is a `NotImplementedError` stub, like `process_batch`, and
  `BaseWorker.process_file` is gone. manager.py's docstring gained two descriptive words
  (walktar's stat batches close file batches); the argument is untouched.
- **Not done, on purpose: the reader keeps its own lstat.** Section 3 suggested handing the
  stat batch's lstat to `open_member()` so the reader could skip its own. The reader's lstat
  is a fresh type check just before the open: it is what keeps a file that became a fifo
  from being opened (which blocks) and keeps `tar.add()`'s treatment of one that became a
  symlink or device. It runs in a reader thread, overlapped with the reads, so it is not on
  the critical path; and carrying stats in `FileBatch` (shared with walksync) is a protocol
  choice that Part C, which needs the lstat for the index, should make. Left for C.
- **Tests:** `test_worker.py` builds the real `TarWorker` (no MPI) on a tree beside its tar
  directory: the scan stats no file, a stat batch taken by another rank fills that rank's
  open batch, file batches close by count or real size and stay on the processing rank's
  list, a file that became a directory is counted but archived nowhere, and the drain order
  is directory, stat batches, their tar, next directory. `test_tar_members.py` passes a
  `FileBatch`. `make test` 491 passed; `make test-pbs` 326 passed, 2 skipped; the race test
  500/500 at `HFU_RACE_RUNS=100`; CI green. `HFU_UPDATE_GOLDEN` was never needed.
- **Measured on derecho** (jobs 8128927 scratch, 8128928 VIT → csg; 1 × 32, read-ahead 16,
  two rounds, baseline `hfu-bench/mainB` = main at 3ba9582; the walk README's "Part B" has
  the table). Each rank ran under GNU `time` (`hfu-bench/cpu-wrap.sh`, `cpu-r<rank>.txt`
  per run; `benchtar.pbs` prints the per-rank user+sys summary on each result line).
  - scratch `bigdir`: 434.9, 424.0 s → 447.1, 421.5 s; `manyfiles`: 61.4, 58.8 → 58.8,
    58.5 s. Unchanged.
  - **VIT: 380.1, 360.9 s → 402.3, 404.4 s. No gain, and 6–11% slower.** The scan is over
    within the first 30 s (139 batches waiting, idle 0, where main had 3–15 idle ranks all
    run), but both land at the same ~3.2–3.6 GiB/s, ~850 files/s, phase 5.5's ceiling for
    every read-ahead setting. **Phase 5.5's reading was wrong: the scanner's ~1,000 entries/s
    tracked the archive rate, it did not set it.** The one-node ceiling on VIT is the storage
    path for 4 MiB files; multi-node scaling (phase 5.5 question 5) is the lever, not the
    scan. The slowdown is the serial stat batch: 1,000 GPFS lstats on the worker thread with
    the rank's readers idle, a few percent of each rank's time during which the node reads
    with 30 ranks instead of 31. main did those stats on one rank, overlapped with the
    others' reads. The csgteam `scan_dirs` array (8 × 1 × 128) ran throughout.
  - The halved worker CPU on VIT (~210 → ~105 s per rank) is main's idle ranks spinning in
    MPI's receive, not work saved: user+sys includes the spin, so a waiting rank costs as
    much as a working one (the Manager's CPU equals its wall time).
  - **`--profile` (job 8129483, `hfu-bench/newprof`, 14 intact worker profiles; the run's
    exit 255 was the launcher's cleanup racing, since fixed):** per worker rank, of ~400 s,
    `posix.lstat` 97 s tottime, nearly all of it `StatWorker.process_batch` (~95 s cum
    beyond `tar_batch`'s 290): ~9 ms per lstat on campaign GPFS under the readers' load,
    serial, on the worker thread. The stat batches' rate did slow as the scanner's had.
  - **`--cpu-bind depth --depth 4`** (job 8128929): `bigdir` 417.0 s (unchanged), `manyfiles`
    **45.0 s** against 58.5–58.8: the one-CPU binding costs the reader threads 23% on small
    files. The VIT row with the same binding: job 8129484 (below).
- **B2, the second commit: the stat batch's lstats in the reader threads.**
  `StatWorker.process_batch` takes its stats from `lstats(dirname, names)`, a generator of
  `(path, lstat result or OSError)` in order (walkstat: one call at a time, as before).
  `TarWorker.lstats` overlaps them `--read-ahead` deep in the reader pool through
  `in_flight()`, so the order, the counting and the tars are unchanged; `--read-ahead 0` is
  the serial path. walkstat on campaign GPFS pays the same ~6–9 ms per serial lstat, so
  the same trick (a thread pool for its stat batches, a new option) is the obvious next
  walkstat speed-up; not done here, it is a feature.
  - **Re-measured (jobs 8129867, 8129868, 8131570; the walk README's Part B):** under PALS's
    default binding B2 is where B was (400.8, 402.4 s against mainB's 375.5, 387.2: the
    same 4–7%), its profile shows `lstat` down from 97 to 24 s per rank but the rank waiting
    on the shared reader pool (210 s in `lstats`) with the files' open/read/write/close
    calls dominating (~5 ms per open, ~7 per close on loaded GPFS) and only 110 s of CPU in
    399; **with `--cpu-bind depth --depth 4` it is 268.7 s, 4.81 GiB/s, the best VIT run**
    (B's first commit 287.6 under the same binding, mainB 361–387 under the default). The
    binding is the ceiling on the loaded campaign node; once it is lifted, B2's overlap pays.
    Why the per-rank stats cost 4–7% under the one-CPU binding is left open (the stats' CPU
    on the busy ranks is the hypothesis).

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
- **Decided: atime stays, and both tools write the same columns.** The scans exist to find stale
  files, so the found atime is the point of walkstat's index. One format and one parser beat a
  per-tool column set.
  - For walktar, the atime recorded is the one *before* the file is read: the found atime, not
    the one the archive run leaves behind. Whether a read updates atime depends on the mount
    (`relatime` on Linux; GPFS has its own rules), and recording first makes the index independent
    of that.
  - `TarInfo` carries mode, uid, gid, size and mtime, but not atime, ctime, ino or nlink. So an
    index line cannot come from the `TarInfo` alone (see 4.6).

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
- **`common/index.py`:** `IndexWriter(RankFile)`, which owns the written cache and the `P` logic,
  with one entry point, `add(path, statinfo, kind, offsets=None)`. The stat is always a real
  `lstat` result; `offsets` is walktar's `(offset, offset_data)` pair.
- **walkstat** calls it next to `stats.count(...)`: for subdirectories at scan time, and for files
  in stat batches.
- **walktar** writes the index line from a pre-read lstat plus the offsets of `tar.members[-1]`.
  - With `--read-ahead` (the default), the reader thread's lstat in `open_member()` already
    precedes the read: return it with the file. No extra call.
  - Only `--read-ahead 0` (`tar.add()`) needs one `os.lstat` before `tar.add`.
  - After Part B the stat batch holds the lstat for both paths. A
  batch's index opens and closes with its tar, and the dirs index with the dirs tar.
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
- **Found atime.** On a private golden tree, every `E` record's atime in walktar's index equals the
  fixture's `ages` value for that path, set by `build_golden_tree()` before the run. This fails if
  the index is ever written from a stat taken after the read.
- **Encoding.** `decode_path(encode(p)) == p` for the golden tree's odd names and the non-UTF-8
  tree.
- **Eviction.** The closure and exactly-once checks pass again with the written-cache cap set to
  1 (a test-only knob), so every record's parents are re-derived and repeated `P` lines are
  exercised on a small tree.
- **Off by default.** Without `--index`, no index files are written.

### 4.8 Open questions
- gzip the index (`--index=gz`)?
- Give walksync an `--index` too, as a per-rank list of what it copied?
- A configurable column set, or a fixed v1? (Decided so far: atime is in, and both tools share
  the columns; see 4.4.)

### Part C, done (2026-11-09, branch `sweep-C`)
- **`common/index.py`:** `IndexWriter(RankFile)` with `add(path, statinfo, offsets=None)`
  (4.6's `kind` argument is not needed: the type comes from the stat), the P chain
  (`parents()`), the written cache and `record()`; `encode_path()` / `decode_path()`;
  `read_index()`, `closure_errors()`, `entries()` and a `__main__` that runs the checks
  (closure per file, E exactly once, `--find` completeness) on any set of index files, used
  by the tests and the smoke run. The walk README's "Indexes" section is the format.
- **Shape decisions that differ from the plan above:**
  - **walktar's roots are `E`, not `P`.** 4.2 says the roots appear only as `P`; that holds
    for walkstat (`count()` never sees a root). walktar archives every directory it walks,
    roots included, into a dirs tar, and 4.7 wants each `.idx` to match its tar's
    `getmembers()` with offsets: so in walktar's indexes every member is `E`, the roots and
    the directories that fail a selecting filter included. `E` means "archived here" there,
    "counted here" in walkstat's.
  - **The header's fields are tab-separated** (`# hfu-index v1<TAB>tool=…<TAB>columns=…<TAB>root=…`,
    one `root=` per `--dirs` entry): a root may hold spaces, and the file is a TSV anyway.
  - **The cache is an `OrderedDict`, LRU, not a plain dict FIFO.** Evicting the oldest key of
    a plain dict (`next(iter(d))`) walks the deleted slots at the front until the next
    resize, which is quadratic at the cap; `OrderedDict.popitem(last=False)` is O(1), and
    `move_to_end()` on each hit keeps the directory being processed (and the root) in the
    cache instead of the oldest-inserted. The knob is `HFU_INDEX_CACHE_MAX` in the
    environment, read at import, since the tests run the tools as subprocesses.
  - **`tarfile` sets a member's `offset`/`offset_data` only when it reads a tar**; on write
    `tar.members[-1]` has both at 0. `tar/worker.py`'s `member_offsets(tar, start)` derives
    them from `tar.offset` before the add and after it, less the member's padded size. The
    test compares them with what a reader reports, a pax-headered member included.
  - **`open_member()` returns `(file, data, lstat)`** for a regular file, the lstat taken
    before the open (the found atime), and still `None` for anything else; `add()` and
    `add_member()` take the index and record the member themselves (one lstat before
    `tar.add()` when the index needs it). `TarWorker.open_index()` returns None (walkstat's
    per-rank index is `StatWorker.open_index()`); `TarWorker.dirs_index` is a class
    attribute so the tests' `object.__new__` workers need no edit.
  - **`O_NOATIME`** is in `_open_nofollow()`: tried first, retried without on `EPERM` only
    (an `EACCES` is a real refusal and propagates). The `--read-ahead 0` path reads through
    `tar.add()`, which does not use it. A non-owner, non-root run pays a second open per
    file on the `EPERM`; the uid check that would avoid it (the lstat's `st_uid` against
    `geteuid()`) is not done, the scans run as root.
- **Tests** (`tests/test_index.py`, 48 + 1 skipped on APFS): the encoding on 13 odd names
  (control characters, backslash, NEL, mixed invalid UTF-8) and the non-UTF-8 tree; the
  writer's P chain, a root without a parent, a vanished parent's `-` fields, eviction at
  cap 1 with the exact record sequence; the checkers on a hand-broken file;
  `member_offsets()` against a reader; the `EPERM` fallback with a fake flag. Under MPI:
  walkstat on the golden tree and the big directory (`--batch-files 10`), unfiltered,
  `--mtime-older 3y` (every directory P-only; no file at all from a rank that counted
  nothing) and `--mtime-newer`, each with the default cache and the cap at 1, closure,
  exactly once, completeness against `find`, `--summary-prefix` naming, two roots in one
  header; walktar on the private golden tree (`--read-ahead` 16 and 0, the same three
  filters, both caps) and the big directory: each `.idx` against its tar's `getmembers()`
  with offsets and the bytes at `offset_data`, the found atime of every file equal to the
  fixture's, the fifo, links and hard links' records; no index files without `--index`.
  `test_tar_members.py` changed in one line (`open_member()`'s third value); its
  assertions are as they were. `big_dir_tree` moved from `test_batches.py` to `conftest.py`;
  `Tree.atimes` carries the fixture's ages.
  - **One limitation, found by `make test-pbs`:** with `--read-ahead 0` on Linux the second
    name of a hard link records the read's atime, not the found one: `tar.add()` reads the
    first name without `O_NOATIME` (relatime updates it, the fixture's atime equals its
    mtime), and the second name's lstat comes after. The found-atime test skips hard links
    under `--read-ahead 0` and says why; the default path is right (the readers' lstats
    precede every read and the reads do not touch atimes).
- **Gates:** `make test` 549 passed, 4 skipped (macOS); `make test-pbs` 605 passed, 2
  skipped (job 8132225, after the hard-link fix; 8132016 before it: 601 passed and the 4
  `--read-ahead 0` found-atime cases, nothing else); the race test 500/500 at
  `HFU_RACE_RUNS=100`;
  `make shellcheck` green. Two tests on `main` are timing-dependent and fail now and then
  under a loaded local run (`test_walkstat_stats_a_big_directory_on_every_rank`, as noted
  under E3/E4; and `test_split_directory_parts_merge_across_ranks`, where APFS moved a
  file's ctime between the two walks): both pass alone, and both fail the same way with
  this branch's changes stashed.
- **Smoke run** (job 8132015, `hfu-bench/benchC.pbs`, the walk README's "Part C" has the
  table): 1 × 32, scratch, warm caches, two rounds per pair. walkstat `million` 1.88/1.64 s
  against 1.76/1.73 with `--index` (121 MiB in 31 files, 1,000,000 E + 31 P, the checker
  16 s), `manyfiles` 0.56/0.57 against 0.59/0.68; walktar `manyfiles` 57.4/51.8 s against
  53.5/53.1 (CPU 40.5/40.7 against 41.1/40.3 s per rank), 201 `.idx` matched their tars.
  Lustre honours `O_NOATIME` (sampled atimes unchanged after the warm-up read). Nothing
  stayed on scratch; csg was not used.
- Golden output never changed.

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

### Part D, done (2026-11-09, branch `sweep-B`, one commit)
- `common/log.py`: `setup(rank, verbose)` puts one `StreamHandler(sys.stderr)` on the
  `hpc_file_utils.walk` logger, format `[%(rank)3d] %(levelname)s %(message)s` (the rank from a
  `logging.Filter`), level DEBUG with `--verbose` else INFO, `propagate=False`. `cli.main()`
  calls it right after the rank is known (so rank 0's option errors are logged) and again once
  every rank has the options (the level). Modules use `logging.getLogger(__name__)`.
- Routed: `BaseWorker.error()` (ERROR, still the counter), `vanished()` (WARNING), walksync's
  rsync exit 24 (WARNING), rank 0's "no such directory" and "needs at least 2 MPI ranks"
  (ERROR), and the Manager-against-workers count mismatch (WARNING). The stderr text changed
  from `[  1] Cannot scan: ...` to `[  1] ERROR Cannot scan: ...`; the tests match substrings.
- Not routed, on purpose: the `-->` lines, the Manager's counters and the `--verbose` per-rank
  table stay `print` on stdout. 5.1 listed the counters as INFO, but `test_batches.py` parses
  both from stdout and so would the scans: 5.2 wins. No `--log-level` option: nothing is at
  INFO or DEBUG yet.
- `tests/test_log.py`: the format and levels with `capfd`, the handler replaced on a second
  `setup()`, and `error()` counting while it logs.

### Part E1/E2, done (2026-11-09, three commits, the two E1 ones in `.git-blame-ignore-revs`)
- E1a: 57 trailing bare returns (found by `ast`, so early returns stayed), 70 banners, the
  blank-line runs, 12 one-line bodies, 10 Yoda conditions, 4 `super().__init__()`, 6
  `flush=True` pairs (`cli.py`'s excepthook flush and `stat/report.py`'s after `_write()` are
  not print-then-flush pairs and stayed), `import io, os, stat` split, `class MaxHeap:`.
- E1b: 107 `.format()` calls on literals are f-strings; the seven on templates (`DIR_ROW`,
  `FILE_ROW`, `row`, `pattern`) stay. An `ast`-based converter did 97 (it refused nested
  specs, escapes, concatenated literals and quote clashes under Python 3.10's rules), the
  rest by hand; the lines it collapsed were re-wrapped as implicitly concatenated f-strings.
- E2: the `humanfriendly` fallback, `MaxHeap`'s `heapify([])`, `get_list()` and `empty()`
  (callers test the heap through `__len__`). `MPIClass.i_am_root` and `.dirs` were already
  gone (A1).
- Golden output never changed through any of it; `make test` after every commit.

### Part E3/E4, done (2026-11-09, three commits)
- **E3a:** `protocol.Tag(IntEnum)` replaces `TAGS` and `MPIClass.tags` (16 sites). `WalkStats`'
  seven scalar counters are named once in `COUNTERS`, which `__init__` zeroes and `merge()`
  sums: the plan's dataclass part would have renamed every `st.num_files` across the tools
  and tests, and the tuple gives the same guarantee flat. The Manager's 17 counters are a
  `Counters` dataclass, `self.c` (`maxnumdirs` moved there from `MPIClass`); the `-->` lines
  and `print_counters()` are unchanged.
- **E3b:** `tests/helpers.py` `make_options(app, **overrides)` goes through `parse_options()`
  on the current directory, so every attribute exists with its real default; the hand-built
  `SimpleNamespace`s in `test_worker.py`, `test_tar_members.py` and `test_report.py` use it,
  and the three `getattr(options, 'filters', None)` sites read the attribute. Type hints on
  `tool`, `log`, `options`, `filters` and `stats`' public signatures.
- **E4:** `args.now = time.time()` once on rank 0 in `parse_options()`, given to
  `compile_filters()` and, through the options bcast, to every rank's `WalkStats(heap_size,
  now)`; `record_directory()`'s 3/5/7-year buckets use it (`NOW_TIMESTAMP` is gone).
  `IDCounts.set_id()` keeps the id only; `WalkStats.resolve_names()` fills the names once
  on rank 0 in `collect()`, through `lru_cache`d `uid_name()`/`gid_name()`. The golden
  userlists are unchanged (the names are the same names, looked up once instead of on every
  rank).
- One MPI test, `test_walkstat_stats_a_big_directory_on_every_rank`, failed once in a full
  local run under load and passed 3/3 alone: its "at least two ranks took a batch" check is
  timing-dependent, as its docstring says.

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

walksync's per-file measurement (0.4, done, no code) → A (done: the sweep, A5's loop takes K
rsyncs, A3 has the common in-flight piece) → B (walktar on stat batches, with its derecho
re-measure) → walksync's K rsyncs: find the ceiling at scale and set the default (section 2,
"`--rsyncs` at scale"; it shares B's bench runs) → E1/E2 (mechanical, dead code) → D (logging)
→ E3/E4 → C (indexes, with O_NOATIME). Each part is its own PR, stacked on the one before.
Phase 5.5's remaining questions (0.3) follow Part B.

The original order put B last. It moved up because phase 5.5 measured it as walktar's limit on GPFS
(0.1), and A2–A3 already make it small. The walksync step follows B for the same reason: it
rides on A3's common pieces. E1's tree-wide mechanical changes wait until B has merged,
so they never have to be rebased under it.

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
