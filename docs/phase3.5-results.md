# Phase 3.5 close-out: measurements (2026-11-07)

Phase 3.5 (workers walk a local stack; the Manager brokers only overflow and `share`
requests) measured on derecho against phase 3 (`ebfd026`) and the deployed csg-utils
walkstat. The plan and the gate are in [HANDOFF-phase3.5.md](HANDOFF-phase3.5.md); the
design is section 5 of [walk-load-balancing.md](walk-load-balancing.md).

## How these were measured

- **Trees** (read-only campaign filesets):
  - CGD-ppc: 3.6M items, 132k directories, 27 items per directory.
  - COLLECTIONS-CMIP-CMIP6: 4.5M items, 73k directories, 62 items per directory.
  - COLLECTIONS-CDG-cdg (3 items per directory) and HAO-radmhd (787 items per directory) in
    [Other tree shapes](#other-tree-shapes).
- **Arguments:** the production ones from `scans/scan_dir_job_array.sh` (`--verbose
  --progress 600 --dirs <fileset>/*/`), under `peak_memusage`.
- **Rounds:** 3, with the order of the configurations rotated every round. Round 1 is
  dropped (cold cache) unless a table says otherwise. Round-to-round spread is about ±10%,
  so differences under about 5% are noise.
- **Columns:**
  - *wall*: the whole `mpiexec`, including start-up, the reduction and the reports.
  - *walk items/s*: the walk alone, from the final progress line.
  - *waited*: the fraction of the walk the workers spent waiting for the Manager to
    answer a `ready` with work.
  - *recv*: the fraction of its loop the Manager spent blocked in `recv` (near 100% means
    idle; near 0% means saturated).
- **Scripts:** `/glade/derecho/scratch/benkirk/hfu-bench/`: `bench35.pbs` (configurations
  `tool[:np[:lqm[:dpd[:ppn]]]]`), `summarize35.py`, `scanshapes.py`. Phase 3 runs from
  `hfu-bench/phase3/`, a `git archive` of `ebfd026`.
- **Code under test:**
  - Jobs 8105332–8105334: `main` at `d4b18f2` plus the uid-0 fix (it doesn't touch
    the walk loop's timing).
  - Jobs 8106112 onwards: also the worker update timer (see
    [Changes](#changes-in-this-close-out)).

Every run of a fileset walked the same number of items. CGD-ppc's 21 scan errors are the same
unreadable directories in every run.

## 1. Against phase 3 and the deployed scanner

### 1 node × 96 ranks (job 8105332)

| fileset | 3.5 | phase 3 | deployed | 3.5 vs phase 3 | 3.5 vs deployed |
|---|---|---|---|---|---|
| CGD-ppc | **160.5 s** | 198.7 s | 211.5 s | −19% | −24% |
| CMIP6 | **183.0 s** | 209.7 s | 211.0 s | −13% | −13% |

### 4 nodes × 96 ranks = 384 (job 8105333)

| fileset | 3.5 | phase 3 | deployed | 3.5 vs phase 3 | 3.5 vs deployed |
|---|---|---|---|---|---|
| CGD-ppc | **48.7 s** | 84.8 s | 98.0 s | −43% | −50% |
| CMIP6 | **45.6 s** | 60.6 s | 65.6 s | −25% | −30% |

Phase 3's earlier 4-node numbers were 87.3 s and 66.8 s, and the deployed scanner's were
95.9 s and 60.4 s. 3.5 beats both at both job shapes.

**The Manager's load:**

| | CGD-ppc, 3.5 | CGD-ppc, phase 3 | CMIP6, 3.5 | CMIP6, phase 3 |
|---|---|---|---|---|
| messages, 1 node | 8.6–9.8k | 179k | 2.6–3.1k | 112k |
| messages, 4 nodes | 29–38k | 179k | 10–12k | 112k |
| blocked in `recv`, 1 node | 99.9% | 99.6% | 100% | 99.8% |
| blocked in `recv`, 4 nodes | 97.7% | 98.8% | 98.0% | 98.8% |

**The CMIP6 gap did not reproduce.** Phase 3's 4-node CMIP6 walk (60.6 s) was faster than
the deployed scanner's (65.6 s) this time, the reverse of the earlier measurement. These
runs cannot explain a gap they do not show; it is within day-to-day variation on campaign
storage.

**Starvation was not what limited phase 3.** From its `ready → execute` counters, phase 3's
workers waited about 7% of the walk at 4 nodes on CGD-ppc and about 0.15% on CMIP6.

**What changed is the time per directory.** This is busy rank-seconds per directory at
4 nodes, rounds 2–3:

| | phase 3 | 3.5 | change |
|---|---|---|---|
| CGD-ppc | 0.22 s | 0.11 s | −52% |
| CMIP6 | 0.29 s | 0.20 s | −30% |

Messages cost microseconds, so they cannot account for this. Two things point instead to
locality:
- In 3.5 a worker walks a directory's subdirectories itself, right after stat'ing them.
  Phase 3 sent each directory to whichever worker was free, on any of the 4 nodes.
- The gain is larger at 4 nodes than at 1, and the tuning runs below show that moving work
  between ranks costs more than it saves.

This is an inference: nothing here measures GPFS directly.

## 2. Scaling with ranks (section 1.4)

Job 8105334: 3.5 only, both rounds (the trees were warm from the job before).

| ranks | layout | CGD-ppc walk items/s | CMIP6 walk items/s | waited | blocked in `recv` |
|---|---|---|---|---|---|
| 32 | 1 node | **30.6k** | **50.7k** | 3–7% | 100.0% |
| 96 | 1 node | 22.4k | 26.2k | 5–13% | 99.9% |
| 256 | 2 nodes × 128 | 24.8k | 36.3k | 10–17% | 99.6% |

Job 8106113: 4 nodes, varying ranks per node, rounds 2–3. The 4 × 96 row is the default
configuration from job 8106112.

| ranks | layout | CGD-ppc wall | CGD-ppc items/s | CMIP6 wall | CMIP6 items/s | waited (CGD-ppc / CMIP6) |
|---|---|---|---|---|---|---|
| 64 | 4 × 16 | **30.5 s** | 131k | 31.8 s | 152k | 7.5% / 3.9% |
| 128 | 4 × 32 | 35.3 s | 110k | **25.2 s** | 197k | 11.6% / 5.5% |
| 256 | 4 × 64 | 37.2 s | 105k | 30.7 s | 160k | 18.3% / 8.8% |
| 384 | 4 × 96 | 44.7 s | 87k | 44.9 s | 108k | 24.6% / 7.2% |

**The Manager is not the ceiling.** It is blocked in `recv` 98–100% of the time at every
rank count and layout, so section 3's decentralised stealing would buy nothing at these
scales.

**The ceiling is per node.** Throughput depends on how many nodes there are, and more ranks
on a node lower it. One node walks CMIP6 at 51k items/s with 32 ranks and at 26k with 96. 4 × 32
is 21% faster than 4 × 96 on CGD-ppc and 44% faster on CMIP6, with a third of the ranks.
Plain scanning is bound by GPFS metadata, and the per-node client saturates well below 96
ranks.

## 3. Tuning `--local-queue-max` and `--dirs-per-dispatch`

Job 8106112: 4 nodes × 96, rounds 2–3.

| `--local-queue-max` / `--dirs-per-dispatch` | CGD-ppc | CMIP6 | waited (CGD-ppc / CMIP6) |
|---|---|---|---|
| **1000 / 8 (default)** | **44.7 s** | **44.9 s** | 24.6% / 7.2% |
| 300 / 8 | 52.3 s | 50.9 s | 19.9% / 4.3% |
| 100 / 8 | 46.2 s | 51.5 s | 20.4% / 3.4% |
| 100 / 2 | 51.3 s | 49.0 s | 18.2% / 3.0% |
| 30 / 8 | 48.6 s | 48.5 s | 20.2% / 3.0% |

**The defaults stay.**
- With 1000, no stack ever overflows. The high-water marks are about 550 directories on
  CGD-ppc and 250–290 on CMIP6, so every directory the Manager hands out came from a share.
- A smaller limit pushes overflow to the Manager. That cuts the waiting, yet every such
  setting is slower: the directories it moves get walked on other ranks, usually on other
  nodes, and lose their locality.
- `--dirs-per-dispatch` makes no consistent difference. A dispatch carries about 1.1
  directories at the default, because the Manager rarely holds more directories than it has
  idle workers.

## 4. Share ping-pong and `request_shares()`

**No fix needed.** At 4 nodes × 96:

| | share requests | per second | answered empty |
|---|---|---|---|
| CGD-ppc | 15–21k per walk | about 400 | about 20% |
| CMIP6 | 6.6–8k per walk | | 31–36% |

The Manager still handles only 10–21% of phase 3's message count at 4 nodes and is blocked 97–99% of the
time. `request_shares()`'s O(nranks) scan is part of its 1–3% busy time. Neither the
ping-pong nor the scan shows up as a cost. HAO-radmhd, whose large directories are the
ping-pong case, is in [Other tree shapes](#other-tree-shapes). It doesn't change the
answer: its empty shares cost nothing at the default, and looking for shares more often
helps nothing there and is what makes the ping-pong cost.

## Other tree shapes

Job 8106114: 4 × 96, 2 rounds, both shown (neither tree had been walked that day). Round 1
put phase 3 first.

| fileset | items | items/dir | 3.5 (r1 / r2) | phase 3 (r1 / r2) | Manager messages (3.5 / phase 3) |
|---|---|---|---|---|---|
| COLLECTIONS-CDG-cdg | 23.0M | 3 | **380 / 382 s** | 856 / 786 s | 75k / 10.3M |
| HAO-radmhd | 26.6M | 787 | 448 / 432 s | 426 / 432 s | 23k / 48k |

**CDG-cdg (tiny directories): 3.5 is 2.1–2.3× faster.** Phase 3 needed 10.3 million Manager
messages, and its Manager fell to 95.8% blocked in `recv`, the closest any run came to the
section 1.4 ceiling. 3.5's workers waited 0.6% of the walk.

**HAO-radmhd (large directories): a tie, the one tree where 3.5 gains nothing.** Its workers
waited 12.5–14.6% of the walk, and about 89% of share requests came back empty. The
Manager stayed 99.8% blocked, so the messages cost nothing.

**Answering shares sooner does not help (job 8107007).** A worker looks for a `share` every
1,000 entries of a big directory. At about 6 ms per stat per rank, that can be 6 s, so the
first guess was that the subdirectories found mid-directory waited on a busy worker's stack
while others idled. Job 8107007 tested looking every 50 entries, 4 × 96, 3 rounds, all
rounds shown:

| | every 1,000 (default) | every 50 |
|---|---|---|
| HAO-radmhd wall | 409.0 s | 411.9 s |
| HAO-radmhd workers waited | 15.7% | 16.6% |
| HAO-radmhd share requests (empty) | 17.8–18.3k (89%) | 331–341k (99%) |
| HAO-radmhd Manager blocked in `recv` | 99.7% | 95.8% |
| CMIP6 wall | 48.6 s | 50.4 s |

The waiting did not move, and the empty replies are the reason. radmhd's big directories hold
files, not subdirectories, so the busy workers have nothing to share. The waiting is the tail
of the walk: a few huge directories left at the end, each one unit of work for one worker.
That is phase 5's job (a big directory's files as several units of work), not the broker's.

Looking more often did set off the ping-pong the hand-off predicted. Each empty reply comes
back sooner and triggers another request, so there were 19× the requests, and the Manager
fell to 95.8% blocked. That is the only measurable cost the ping-pong has shown, and only
with the 50-entry check. The default stays at 1,000 (the change was reverted), and the
skip-empty-stacks fix is not needed while it does.

Job 8106451: the same trees, 3.5 at fewer ranks per node, rounds 2–3. The last walk ran past
the job's walltime, so HAO-radmhd at 4 × 16 has round 2 only.

| fileset | 4 × 96 (8106114, round 2) | 4 × 32 | 4 × 16 |
|---|---|---|---|
| COLLECTIONS-CDG-cdg | 382 s | **140.7 s** | 151.8 s |
| HAO-radmhd | 432 s | 385.2 s | **240.3 s** |

- **CDG-cdg is 2.7× faster at 4 × 32 than at 4 × 96.** That is 5.6× faster than phase 3 at
  4 × 96.
- **HAO-radmhd gains little at 4 × 32 (−11%), but 4 × 16 is 1.8× faster.** At 32 and 16 ranks
  per node its workers waited only 3–6% of the walk, so the gain is not less starvation.
  Large directories seem to hit the per-node limit hardest. Contention for one directory's
  GPFS tokens would fit, but nothing here measures it.

The radmhd item counts differ slightly between runs (26.636–26.639M): the fileset was being
written to during the walks.

## Production scans today

These are the deployed scanner's last production runs (`*.DONE` in
`/glade/work/csgteam/csg-utils/filesystem/scans`, late October 2026). All ran at 1 node × 96
and all walked at 14–28k items/s, the per-node ceiling above.

| fileset | items | dirs | items/dir | walk |
|---|---|---|---|---|
| RAL-hap | 351M | 3.05M | 115 | 5.2 h |
| UWYO-wyom0169 | 166M | 3.25M | 51 | 4.9 h |
| NCAR-USGS_Water | 191M | 20.7M | 9 | 4.0 h |
| MMM-parc | 223M | 16.3M | 14 | 3.3 h |
| CGD-tss | 133M | 6.75M | 20 | 2.2 h |
| CGD-cas | 168M | 6.75M | 25 | 2.1 h |

The ranks-per-node result matters more for these than any of the knobs above. Two
estimates from the CGD-ppc and CMIP6 rates, not measurements:
- At 4 × 32, RAL-hap would take about 30–50 minutes.
- Even at 1 node, 32 ranks walked 35–95% faster than 96.

Across all four trees, 4 × 32 took 11–63% less time than 4 × 96 (HAO-radmhd 11%, CGD-ppc 21%, CMIP6 44%,
CDG-cdg 63%).
16 per node was best for the large-directory tree.

## Changes in this close-out

- **uid 0** (`common/worker.py`): `single_owner` is `None`, not `False`, when a directory
  has several owners, and the checks are `is not None`. Before, walks run as root dropped
  uid 0's per-user directory lists. Tested in-process in `tests/test_worker.py`.
- **Worker update timer** (`common/worker.py`): a worker walking its own stack sends its
  counts at least every `WORKER_UPDATE_SECONDS` (10). Before, it sent them only every
  `--progress` seconds, and the scans use 600, so the Manager's progress line went stale.
  `--progress` is now only the screen's interval. The timer made no measurable difference
  to throughput or to the fraction of empty share replies (job 8106112 against 8105333).
