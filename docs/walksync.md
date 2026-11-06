> **Imported 2026-11-06** from csg-utils branch `walksync-handoff`
> (`filesystem/scans/walkstat/HANDOFF.md`, commit c341266). Paths below are csg-utils paths:
> `filesystem/scans/walkstat/` is now `hpc_file_utils/walk/`, and `filesystem/cgd_migration/`
> is now `sync/`. The phased plan that supersedes this note's "agreed order of work" is in
> [HANDOFF.md](HANDOFF.md).

# walkstat → walksync: hand-off (2026-11-05)

## Why

Copying CGD campaign trees (`filesystem/cgd_migration`) needs a multi-node copier that:

- copies **read-only sources** as a non-root user. dsync (mpifileutils 0.12) can't: it gives
  directories and files the source mode as it creates them, then can't write into them. This was
  tested three ways: normal batches, one batch, and a pre-built writable tree.
- **excludes** HSM `COLD_STORAGE*` trees, which dsync can't do;
- **re-crawls fast** when nothing changed. msrsync takes 20+ min on one node for a 174 TiB tree.

msrsync is the current fallback: one node, ~3 GiB/s on derecho (4A/4B on 2026-11-05).

walkstat already has a multi-node, dynamically load-balanced MPI directory walk. walktar is a
specialization that hands every file to a tar thread. **walksync** is the same, except that every
file which is new or changed relative to the destination is copied via batched
`rsync --files-from`.

## Agreed order of work

1. **Clean up and modernize walkstat, and add tests**, on its own PR: see *Known walkstat issues*.
2. **Load balancer: file batches as work units**, implemented first for **walktar**. This fixes
   "one big directory blows up the load balance", which both walktar and walksync suffer from.
3. **Layer walksync on top** of 1 and 2, starting from the prototype below.
4. Benchmark it (see *Benchmarks*). Only if it earns its keep, offer it to `cgd_migration` as an
   alternative to dsync/msrsync. The final serial `rsync -H --delete` stays the authoritative pass.

## Known walkstat issues (step 1)

- **Termination race (a real bug, can hang any walkstat run).** `Manager.run()`'s `dir_reply`
  handler sets `any_dirs[rank] = False`. A worker sends `dir_reply` *mid-directory*
  (`MAXDIRS_BEFORE_SEND = 200` subdirs), so a still-busy worker can be marked idle. If all other
  workers are idle at that moment, `finished()` returns True. The Manager then waits only for
  `ready`, and the busy worker's next `send(dirs)` blocks forever.
  - Seen with walksync `--dry-run` on a test tree with a 2,000-subdir directory: 30 ranks were in
    `MPI_Barrier`, the Manager in `Mprobe`, one worker in `MPI_Send`. Found with `gdb -p <pid> -batch -ex bt`;
    py-spy isn't installed.
  - Fix: only a `ready` marks a rank idle; drop the assignment in the `dir_reply` handler. Also
    consider draining any pending `dir_reply`/`progress` in the termination loop.
- **Nondeterministic summaries:** heap ties at the cut-off (`--heap-size`) pick arbitrary entries,
  so two runs on the same tree differ. Regression tests need deterministic tie-breaking (e.g.
  `(size, path)`) or must compare against an old-vs-old baseline. Concurrent readers also change
  atimes between runs, which the summaries show.
- **Errors on rank 0 hang the job:** `parse_options()` runs on rank 0 only and fails with
  `assert`, so the other ranks wait in `bcast` forever. Use `comm.Abort()` or bcast an error.
- **Progress protocol:** the worker → Manager payload used to be `[items, bytes]`, popped and
  asserted in four places. The prototype replaces it with one `(items, bytes, extra)` tuple and
  `Manager.update_progress()`. Keep that.
- No tests, no linting; `mpiclass.summary()` is a 300-line mix of collectives and reporting.
  Envs: only `env/mpi4py-derecho-...` exists, and cray-mpich needs a PBS job to run anything.

## Load balancer design (step 2)

Today the unit of work is a **directory**: one rank handles all of a directory's files, so a
20 TB directory is one tar or rsync stream for hours while the other ranks idle.

Proposal: also make **file batches** units of work.

- While scanning a directory, a worker collects `(path, size)` for files to process. When that list
  passes a threshold (N files or B bytes), it sends the batch to the Manager (new tag,
  e.g. `batch_reply`) and starts a new one. The last partial batch it either processes itself
  (small) or ships too.
- The Manager queues batches alongside directories. On `ready` it hands out a batch before a
  directory (batches are bounded, already-known work), and it tracks outstanding batches for
  termination.
- A worker given a batch runs the specialization's `process_batch(batch)`:
  - **walktar:** one tar file per batch (replaces the per-rank `MAXSIZE_BYTES` roll-over);
  - **walksync:** one `rsync --files-from` per batch.
- Small directories still combine across directories on the scanning rank. A batch is "everything
  this rank queued since its last flush", so tiny directories don't each become a message.
- Thresholds: walksync's 5,000 files / 100 GiB per batch is a starting point. Measure it.

## walksync prototype (step 3 starting point)

The prototype code is **not on this branch**, which holds only this doc. The latest version is in
`/glade/derecho/scratch/benkirk/walksync_dev/walkstat`: `sync_worker.py`, the
`walksync -> run.py` symlink, and small edits to `parse_args.py`, `base_worker.py`,
`mpiclass.py`, `manager.py`, `worker_factory.py`, `run.py` (diff it against `main`).
Test job: `/glade/derecho/scratch/benkirk/walksync_dev/walksync_smoke.pbs`.

How it works:

- **Compare:** `SyncWorker.process_directory()` makes one `scandir` of the matching dest
  directory into a name → entry dict. `process_file()` queues an entry only if it's missing, a
  different type, a different size or whole-second mtime, or a symlink with a different target.
  Dest-only names are counted (and listed in `--dry-run`) as extraneous, never deleted.
- **Copy:** a thread per rank drains the queue and flushes
  `rsync -aAX --numeric-ids --chmod=Du+w --files-from=- --from0 SRC/ DEST/` at 5,000 items,
  100 GiB, 1 s idle, or the end of the run.
  - `Du+w` is required: batches from different ranks create the same parent directories.
  - The final serial rsync restores the real dir modes and times, plus hard links and deletes.
- **Back-pressure:** after a directory, a rank waits while its un-copied backlog is over one
  batch, reporting progress meanwhile.
- **Options:** `--exclude PAT...` (common; never stat'ed or descended), `--dest`, `--dry-run`,
  `--rsync-opts`, `--batch-files`, `--batch-size`.
- **Counters:** `sync_counts` in the summary (new/changed/unchanged, bytes, rsync batches and
  seconds, failures). Live: `| copied X (Y/s now, Z/s avg), queued Q` on the progress line.
- **Exit status:** 1 if any rank had errors.

Results from the 2026-11-05 smoke job (32 ranks, 1 derecho node):

- **Plain tree** (6,036 items: 2,007 dirs, odd names, links, fifo, hard link): copied in 4 s
  in **31 rsync batches**, i.e. cross-directory batching works. `rsync -n -i` oracle clean,
  content identical (the test's `diff -r` hit the fifo; use `-x fifo`).
- **Rerun:** 0 queued, 0 rsync, 1 s.
- **`--dry-run`:** hung in the termination race above.
- **Not reached:** read-only source with ACLs (t2), `--exclude COLD_STORAGE*` (t3), the 3D crawl
  benchmark. The live copy-rate code has not run yet.
- **walkstat old vs new:** CESM2-SF identical apart from one atime (a concurrent copy job was
  reading it). The smoke tree differs only in heap-tie entries, which is the nondeterminism above
  and needs an old-vs-old baseline.

## Benchmarks (step 4)

| test | data | compare |
|---|---|---|
| writable source | ~2-3 TiB of CESM2-LE (~61 MB files) | walksync 1 and 2 nodes / dsync 2 nodes / msrsync 1 node |
| read-only source | ~2-3 TiB of COVID (~177 MB files) | walksync / msrsync |
| no-op re-crawl | 3D: `collections/cdg/data/.HIDE-*cmip5*` vs `cesm/cmip/cmip5/cmip-timeseries`, `--dry-run` | msrsync ~21 min |
| rerun | the above, again | |

Report GiB/s, batch counts, and rsync seconds against wall time (the cost of starting rsync).
Scratch has ~29 TiB free.

## Testing approach

- **Oracle:** after walksync, `rsync -naxAX --delete -i SRC/ DEST/` should list only directory
  time lines (`.d..t......`). Add `--chmod=Du+w` for read-only sources and `--exclude` as used.
- **Test trees:** odd names (space, newline, non-UTF-8), symlinks (file, dir, dangling), empty
  dirs, fifo, hard links, a 2,000-subdir directory (it triggers the race), a read-only tree with
  ACLs, `COLD_STORAGE*` dirs.
