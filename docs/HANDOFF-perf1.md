# Hand-off: performance characterization, sprint perf-1 (written 2026-11-10)

Three steps, in order, each its own commit on branch `perf-1` and its own entry in the walk
README (`hpc_file_utils/walk/README.md`, the "perf-1" entries), working through the first
three items of `docs/GLADE_PERFORMANCE.md` section 7 in the order 2, 1, 3:

- A. CPU binding and `--read-ahead`: measure the affinity mask a rank gets under PALS's
  default and under `--cpu-bind depth --depth 2/4/8`, and decide between walktar sizing
  its thread count from the mask and the launcher binding by default.
- B. Multi-node walktar on campaign (phase 5.5 question 5): GDEX VIT → csg at 1, 2 and 4
  nodes.
- C. A thread pool for walkstat's stat batches (`--stat-threads K`).

Bench kit: `/glade/derecho/scratch/benkirk/hfu-bench/` (`benchbind.pbs` for A and B,
`benchstat.pbs` for C, `bindwrap.sh` records each rank's affinity mask next to
`cpu-wrap.sh`'s GNU `time` line, `affinity.py` prints the masks of an `mpiexec` line;
`mainC/` is the baseline side copy, `main` at e8f01c1, with `walktar-mainC` and
`walkstat-mainC`; `stepC/` the step C code as a side copy). Rules as the sweep's
(`HANDOFF-sweep.md`, "Rules for all of it", plus the data rules in `GLADE_PERFORMANCE.md`
section 5).

## Step A, done (2026-11-10)

- **What a rank gets.** `os.sched_getaffinity(0)` gathered from every rank
  (`affinity.py`, and every bench rank's own `Cpus_allowed_list` in `aff-r<rank>.txt`), 32
  ranks on one derecho node (128 cores, 256 hardware threads; logical CPU n < 128 is core
  n's first thread):

  | `mpiexec` | CPUs per rank | masks |
  |---|---|---|
  | PALS's default | 1 | rank r on CPU r (32 distinct) |
  | `--cpu-bind depth --depth 2` | 2 | `0-1`, `2-3`, …, `62-63`: two cores each, 64 of the 128 |
  | `--cpu-bind depth --depth 4` | 4 | `0-3`, …, `124-127`: all 128 cores |
  | `--cpu-bind depth --depth 8` | 8 | `0-7`, …, `248-255`: all 256 hardware threads |
  | `--cpu-bind none` | 256 | one mask, `0-255`, for every rank |

- **Decision: the launcher binds; walktar does not size `--read-ahead` from the mask.**
  Every binding wider than one CPU gave the whole gain on both filesystems, and the width
  past two cores changed nothing (the README entry has the tables: scratch `manyfiles`
  62 s → 40–45 s, VIT → csg 385/442 s → 267–342 s across the two rounds, `bigdir`
  unchanged); so the one thing to fix is the one-CPU default, which a thread count cannot
  fix (with one CPU, fewer threads are slower still: phase 5.5's read-ahead 4 row), and
  with two or more CPUs the default 16 is already past where more threads pay (the
  read-ahead rows). `--depth 2` is the recommendation: measured best or equal in every
  pair, and it fits any rank count up to 128 per node.
- **Code:** the scan scripts' `mpiexec` lines (`scans/scan_dir_job_array.sh`, `all_cs.sh`,
  `test_scan_dir.sh`) carry `--cpu-bind depth --depth 2`; every run prints
  `CPUs per rank: N (the launcher's binding; ...)` after `Running <tool> on N MPI ranks`,
  gathered from every rank's mask (`cli.cpus_per_rank()`; the line is skipped where there
  is no mask to read, macOS), so each number in a log carries its binding; walktar's
  `--read-ahead` help says the threads need the binding. The walk README's "CPU binding"
  section is the user-facing version. Nothing in the walk changed; the golden output is
  as it was (the summary files; the new line is on stdout only).
- **Measurement notes.** Scratch was under outside load from the second scratch round on
  (`bigdir` at 1.1–1.7 GiB/s instead of 2.5, 617–918 s per run), which pushed that job
  past its walltime (job 8135798, killed in its last run; its 574 GiB of tars were
  deleted by hand) and spoiled the read-ahead rows for `depth 2` (job 8136190) and a
  re-run of the round (job 8136917, stopped after its first pair; its tars deleted). The
  loaded numbers are in the README's table marked as such; the ranking held under the
  load. A clean second scratch round is worth one more job on a quiet day (`benchbind.pbs`
  with `ROUND_OFFSET=1`, 50 minutes). Campaign's second round was slower for every
  binding, the production scans (8 subjobs) running throughout both. Only pairs within a
  round compare; a run's `bigdir` time is its storage-state control.
- **A Part C test fixed on the way:** `make test-pbs` (job 8136944) failed
  `test_walktar_indexes_of_a_big_directory_spread_over_ranks[cap1]` once: it read the
  last batch index by name and expected it to start `P` root, `P` big, `E`, but which
  batch holds what, and whether a small directory's file came first in it, depends on the
  ranks' timing. It now checks every batch index that holds files of the big directory:
  the root first as `P`, and the big directory as `P` before the first of its files.
- **Gates:** `make test` 549 passed, 4 skipped (macOS); `make test-pbs` 355 passed, 2
  skipped (job 8137033, after the test fix); `make shellcheck` green (the three scan
  scripts edited). Golden output never changed.

## Step B (2026-11-10): multi-node walktar on campaign, and a hang at 2 nodes

- **1 node × 32, `--depth 2`, two rounds** (job 8138512): 324.8, 284.9 s (3.98, 4.54
  GiB/s), the scans running; the 1 × 32 reference for the node counts below.
- **2 nodes × 32 hung, twice** (jobs 8138513 and 8139236): the walk reached 1.12–1.16 TiB
  of 1.26 at 8.3 GiB/s avg (the per-node ~4.5 GiB/s does add up) at the 2-minute progress
  line, then every rank spun in MPI's receive until the walltime (the first) or the
  bench's stall watchdog (the second, `STALL=240`). The tars on csg were complete in size
  (332 of 335) and were deleted by hand. With `gdb -p` every sampled rank, the Manager
  included, was in `PMPI_Mprobe` under mpi4py's pickled `recv` (`MPIDI_OFI_do_iprobe`
  spinning in `sched_yield`); the `SIGUSR1` stack dump (`cli.py` registers
  `faulthandler` on it, new here) of the Manager's node shows the 31 workers at
  `worker.run()`'s receive of the answer to their `ready` and the Manager at its
  any-source receive. So on that node every worker had said `ready` and the Manager had
  nothing from anyone for four minutes: not a protocol state the argument allows, unless a
  message is in flight and not delivered. The other node's dump did not arrive (the
  watchdog's `pkill` pattern also hit the `time` wrapper and PALS tore the job down;
  fixed to match the python process only).
  - **Hypothesis under test:** cray-mpich/CXI's rendezvous path (messages over
    `FI_CXI_RDZV_THRESHOLD`, 16 KiB) with a matched probe (`MPI_Mprobe`, `MPI_ANY_SOURCE`)
    from another node. walktar's `FileBatch` shares and dispatches are ~130 KB (1,000 VIT
    paths); one node never leaves shared memory, and phase 4's multi-node walkstat shared
    name-only batches of ~10–20 KB. Two chained 2-node experiments: `HFU_RECV_MPROBE=0`
    (mpi4py's `recv_mprobe = False`: Probe + Recv instead of Mprobe + Mrecv, a one-line
    switch in `cli.py`) and `FI_CXI_RDZV_THRESHOLD=1048576` (the same code, the messages
    eager).
  - **Outcome, first half:** with `HFU_RECV_MPROBE=0` the same 2-node run finished
    (job 8139309): **150.5 s, 8.58 GiB/s**, all 333,110 files, status 0, where the two
    matched-probe runs hung at ~2 minutes. So `cli.py` now sets `mpi4py.rc.recv_mprobe =
    False` before importing MPI (Probe + Recv for every pickled receive; one thread per
    rank talks to MPI, so the unmatched probe is safe), with `HFU_RECV_MPROBE=1` to get
    the matched probe back. `make test` 549 passed, 4 skipped on it.
  - **Outcome, second half:** the matched probe with the rendezvous threshold raised to
    1 MiB (`FI_CXI_RDZV_THRESHOLD=1048576`, job 8139310) hung the same way, slower (2.5
    GiB/s avg before the stall, the big messages going eager), and this time both nodes'
    dumps arrived: **all 63 workers** at `worker.run()`'s receive of the answer to their
    `ready`, the Manager at its any-source receive with its last line saying `idle=59,
    queued=3`: four `ready` messages that were sent were never matched by `MPI_Mprobe`.
    So it is the matched probe across nodes, not the message size, and Probe + Recv is
    the fix, not a tuning.
- **Gates on the Probe + Recv default:** `make test` 549 passed, 4 skipped; the race test
  500/500 at `HFU_RACE_RUNS=100`; `make test-pbs` 355 passed, 2 skipped (job 8139458).
- **With the fix, 1, 2 and 4 nodes × 32** (jobs 8139452, 8139453, 8139454; two cores per
  rank; the walk README's "perf-1, step B" has the table): 280.6 s / 150.5–160.5 s / 90.3
  s (walk 80.5–82.6 s): 4.6, 8.0–8.6, 16 GiB/s, ~4 GiB/s per node every time. The Manager
  handled 37,424 messages at 4 nodes in 0.6 s (99.3% blocked in its receive); the share
  ping-pong is 36,253 requests, 35,520 empty, for 607 batches (phase 5.5 question 4: it
  grows ~9× for 4× the ranks and costs nothing measurable). The tail is one ~4 GiB batch
  (~20 s at 4 nodes). The user offered 8 nodes (job 8140120, two rounds): 60.2, 60.1 s (walk 55.3, 53.5 s),
  23.4–24.1 GiB/s avg, 24.6 peak, ~3 GiB/s per node, the one-batch tail now 22–23 s of
  the walk; the Manager 96.9% blocked, 70k messages (69k share requests) in 1.7 s. So
  the next lever at 8 nodes is a smaller `--batch-size` for the tail, not the Manager.
- **Bench notes:** `benchbind.pbs` gained `STALL` (a watchdog: SIGUSR1 to every rank on
  every node, then kill); its first version's `pkill` pattern also hit the `time` wrapper
  (fixed), and the stalled threshold job (8139310) ended without running its cleanup
  (its 258 tars on csg were deleted by hand; the ssh calls now have timeouts). Every
  other run deleted its own tars; csg's `hfu-bench-tar` is empty.

## Step C, done (2026-11-10)

- **Code:** `StatWorker.lstats()` runs a stat batch's lstats `stat_depth()` at a time in
  `stat_pool()`'s threads through `in_flight()` (0: one at a time on the worker thread,
  as before); walkstat's `--stat-threads K` [0] is the depth, the pool is a
  `ThreadPoolExecutor` started at its first batch and shut down by `run()`
  (`StatWorker.stat_threads`, a class attribute so the tests' `object.__new__` workers
  need no edit). `TarWorker.lstats()` is gone: it overrides `stat_depth()`
  (`--read-ahead`) and `stat_pool()` (its readers) instead, the same behaviour as Part B's
  second commit. The members, the counting and the golden output are unchanged;
  `test_tar_members.py` is untouched.
- **Tests:** `test_walkstat.py`'s golden summary and userlists gain `--stat-threads 4` and
  `--batch-files 3 --stat-threads 2`; `test_worker.py` runs the split-directory and
  vanished-file tests at 0 and 3–4 threads and checks `lstats()`'s order and in-place
  errors at 0, 2 and 16; `test_options.py` the bounds (walktar has no such option).
- **Measured** (jobs 8138431 and 8138503, `benchstat.pbs` and `benchstatload.pbs`; the
  walk README's "perf-1, step C" has the tables): on campaign with only the production
  scans running a stat costs 1.5–2 ms and the node walks 15–23k entries/s whatever the
  thread count (the GPFS client's throughput, the phase 3.5 ceiling, not latency); while
  the same node reads campaign at 3–4 GiB/s (a walktar of VIT beside the walkstat) a stat
  costs ~18 ms and the serial walk of `.Radiosonde` takes 343 s against 20–30 s unloaded,
  195 s with 4 threads and 65 s with 16 (5.3×). The default stays 0 (the scans run alone
  on their nodes); `--stat-threads 16` is for a node that is also reading.
  - In `benchstatload.pbs` the walkstats run `--cpu-bind none` beside the walktar's 64
    bound cores (two `mpiexec`s in one job cannot both bind from CPU 0); round 1's
    serial run outlasted the load (343 s against the walktar's 375), so its 4- and
    16-thread runs were unloaded, and round 2's rotation put those two under the load and
    the serial run after it: the table pairs them by what the load was doing.
- **Gates:** (filled in when the code lands after A and B: `make test`, `make test-pbs`,
  the race test, golden unchanged with `--stat-threads 4` in the golden tests.)
