# Hand-off: finish phase 3.5, local work queues (written 2026-11-07)

Phase 3.5 is on `main`: PR #5, ported onto phase 3 (PR #4) and merged as `f06778a`. **It works and is tested,
but it has not been measured at scale.** This note is for a fresh session that measures it, tunes
it and closes it out. Read first:
- [walk-load-balancing.md](walk-load-balancing.md): section 1.4 (the saturation signal), section
  5 (this design) and section 7 (the plan).
- The module docstring of `hpc_file_utils/walk/common/manager.py`: the protocol and the
  termination argument, including why `share` cannot upset it.
- `hpc_file_utils/walk/README.md`, the "Phase 3" and "Phase 3.5" sections.

## What is there now
- **Workers keep a local stack.** A worker pushes the subdirectories it finds onto a local stack
  (`BaseWorker.local_dirs`), walks them depth-first, and sends `ready` only when the stack is
  empty and its last directory is done.
- **How directories reach the Manager.** Only two ways:
  - *overflow*: the oldest half of a stack past `--local-queue-max` [1000], sent as `dir_reply`;
  - *sharing*: the answer to a `share` (`share_reply`). The Manager sends shares, at most one
    unanswered per idle worker, to the busy workers with the longest last-reported stacks, while
    it holds idle `ready`s and has nothing to give. Workers look for a share at every directory
    and every 1,000 entries of a big one.
- **The Manager** is phase 3's: one blocking `recv(ANY_SOURCE, ANY_TAG)` per message. It answers
  a `ready` with up to `--dirs-per-dispatch` [8] directories.
- **Payload.** `(items, bytes, queued, extra)`; `execute` carries a list of directories.
- **Reports.**
  - Every run prints:
    - Dispatches, plus directories received as overflow and as shares (share requests, and how
      many came back empty).
    - Directories per rank (min / mean / max), and the high-water mark of the local stacks.
    - The fraction of the walk the workers spent waiting for the Manager to answer a `ready`
      with work.
  - `--verbose` adds each worker's wait and stack high-water mark to the per-rank table, plus
    the Manager counters: messages, handling time, time blocked in `recv`.
- **Tests.** `test_load_balance.py` uses a binary tree that never overflows, so work can only
  move by sharing. The race test runs `--local-queue-max` 1000 and 2, 50 runs each. Laptop: 158
  passed. derecho `make test-pbs`: 120 passed, race test 100/100.
- **One measurement so far:** a 4,040-directory tree with 5 workers. The Manager needed 49
  messages, against several per directory in phase 3, and each worker walked 784–831
  directories.

Section 1.4 of the note defines "Manager occupancy" as the fraction of passes that heard a
message. The blocking loop has no passes. Its equivalent is the inverse of "Manager blocked in
recv" (`--verbose`): near 0% blocked means the Manager is saturated.

## To do (the gate for closing 3.5)
1. **The section 1.4 measurement.** Run `walkstat` on a tree of small directories (shaped like
   `/glade/u/home`) and on a tree of large files, at 32, 96 and 256 ranks, and plot items/s
   against ranks. Record the waiting fraction and the time blocked in `recv`. If items/s still
   scales at 256 ranks, section 3 of the note (decentralised stealing) is not needed.
2. **Benchmark against phase 3 and the deployed scanner.**
   - Same shape as phase 3: CGD-ppc and COLLECTIONS-CMIP-CMIP6, 1 node × 96 and 4 nodes × 384,
     3 rounds with the order rotated, and drop round 1 (cold cache).
   - The scripts are in `/glade/derecho/scratch/benkirk/hfu-bench/`:
     - `bench.pbs`: 1 node
     - `bench4.pbs`: 4 nodes
     - `bench3.pbs`: three-way rotation
     - `summarize.py`
   - Point "new" at the 3.5 code. Keep phase 3 as a third tool: check out a worktree at
     `ebfd026` and give it a launcher like `fallback/walkstat-fallback`.
   - Phase 3's 4-node numbers to beat: CGD-ppc 87.3 s, CMIP6 66.8 s. The deployed scanner did
     95.9 s and 60.4 s. Section 1.4 predicts that 3.5 closes the CMIP6 gap, which phase 3 left
     inside the walk loop. The workers' waiting fraction now says whether starvation was the
     cause.
3. **Tune the defaults.** Try `--local-queue-max` and `--dirs-per-dispatch` on both tree
   shapes. The 1000 and 8 now in use were never measured.
4. **`make test-pbs`** on derecho after any change. Keep the race test at 100/100.

## Known issues and open questions
- **uid 0 (bug, also before 3.5).** `single_owner` is the uid, and `if single_owner:` is false
  for root, so walks run as root lose the per-user directory lists
  (`test_walkstat_golden_userlist` fails as uid 0). Fix: use `None` and `is not None` in
  `common/worker.py`. Root runs the scans, so this matters.
- **Share ping-pong.** When the busy workers have empty stacks (one huge directory each), every
  empty `share_reply` triggers a new `share`, up to one per directory boundary or 1,000 entries
  per busy worker. Measure whether that message rate matters. A cheap fix would skip workers
  whose last reported stack was empty until they report a non-empty one.
- **`request_shares()` scans every worker.** It runs after each message while the Manager is
  starving, and costs O(nranks). That's fine at 384 ranks; at thousands, keep a heap keyed by
  queue length.
- **Progress lag.** Workers walking their own stacks send `progress` only every `--progress`
  seconds (the scans use 600) or every 20,000 entries of one directory, so the Manager's
  progress line can lag. The final totals are exact (tree reduction, cross-checked).
- **The walksync prototype** (`/glade/derecho/scratch/benkirk/walksync_dev/`) uses the
  pre-phase-3 payload. Phase 6 adapts it.

## Rules (as in phase 3)
- `source etc/config_env.sh`. GNU make >= 4 (`gmake` on macOS).
- `make test` everywhere. `make test-pbs` on derecho: a 1-node job, because cray-mpich only runs
  inside PBS.
- **derecho access:** `~/bin/ssh-tunnels up` with `SSH_TUNNEL_HOSTS=derecho`, then a Duo push.
  The test copy is `/glade/derecho/scratch/benkirk/hpc-file-utils`. Sync it with rsync and
  `--delete` for `hpc_file_utils/`; it holds uncommitted synced files, so don't `git pull` there.
- **derecho jobs:** account SCSG0001, queue `main`. Expedite only with
  `sudo -n -u csgteam qalter -p 1023 <job>`. qstat lags a few seconds behind qsub; qselect sees
  the job at once. Ask before anything multi-node or multi-hour. Walks of campaign filesets are
  read-only; outputs go to scratch.
- **Git:**
  - Branch first. Stage explicit paths only, never `git commit -a` or `git add -A`: the user keeps
    uncommitted edits, for example in `docs/HANDOFF.md`.
  - Commit, push and open a PR only when asked.
  - Phases merge as merge commits.
- Don't touch `sync/` logic; it runs in production from deployed copies.
