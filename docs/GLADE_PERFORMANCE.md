# GLADE performance: what the walk tools measured

What Derecho's and Casper's filesystems do under a parallel walk, archive, copy or removal, as
measured while building the walk tools (`walkstat`, `walktar`, `walksync`, `walkfind`,
`walkrm`; 2026-11). This is the reference, organized by topic. Every number has a source: an
entry of [walk-changelog.md](walk-changelog.md) (cited by its sprint, "phase 5.5", "perf-1
step B"), a section of [history/HANDOFF-sweep.md](history/HANDOFF-sweep.md) (cited as "sweep
0.4" and so on), or a Derecho job in `/glade/derecho/scratch/benkirk/hfu-bench/` (benchmark
scripts kept outside this repository). Unless said otherwise a run is one Derecho node,
32 MPI ranks (one Manager, 31 workers), PALS's default binding (one CPU per rank), and
"scratch" is `/glade/derecho/scratch` (Lustre), "campaign" `/glade/campaign` (GPFS), "csg"
`/glade/campaign/cisl/csg`.

The one-line version: **a small file costs one storage round trip of ~35 ms whatever its
size; parallelism inside a rank hides it, but only up to the CPU that rank has; a stat costs
1–9 ms depending on the load; one destination directory takes ~1,000 creates per second from
one node however the files are made; and campaign's numbers move with the day's load.**

## 1. The filesystems, from one compute node

| | scratch (Lustre) | campaign (GPFS) | `flash_test` (GPFS, flash pool) |
|---|---|---|---|
| cold read of a small file (one stream) | 21–37 ms, ~35 typical (1 MiB) | ~35–37 ms (4 MiB), 18–20 ms (80 KB) | 10.3 ms (1 MiB), 3.9 ms (80 KB) |
| big-file read, 31 streams, one node | 2.4–2.9 GiB/s | 5.4–6.3 GiB/s | – |
| big-file read, one stream | 0.42–0.61 GiB/s | 2.59 GiB/s | – |
| metadata round trip (open, stat, rename, chmod) | 0.3–1 ms each | ~1 ms idle, 6–9 ms under load | – |
| write side of a 1 MiB / 80 KB file (rsync, warm source) | 8.1–8.6 / 3.2 ms | – | 5.0 / 1.25 ms |

- Sources: phase 5 and 5.5 notes (reads, rates), sweep 0.4 (rsync's split, flash), the
  Part B profile (stats under load).
- **Casper reaches the same filesystems over its own network.** Its path to Lustre doubles
  every metadata round trip (openat 1.66 vs 0.95 ms, rename 1.13 vs 0.59, 8.8 vs 4.6 ms per
  file in all) and its cold reads cost more (a serial `cat` 58 vs 41 ms per 1 MiB file), but
  its parallel capacity is Derecho's (16 `cat`s at once: 2.98 vs 2.90 ms per file). Its
  path to GPFS is Derecho's (one rsync 20.7 vs 20.0 ms per 80 KB file). Sweep 2, "The same
  on Casper".
- The node's NIC is 200 Gb/s (~25 GB/s) and `mktree` wrote 17 GB/s from one node in 1 MiB
  writes (phase 5): none of the tools is near the hardware.

## 2. Reading files

- **A small file is one round trip.** Of `tar.add()`'s 38.8 ms per 1 MiB file, 35 ms is the
  one `read()`; lstat, open, close and the header are ~1.2 ms together and the owner lookup
  0.04 ms. The same on campaign (37.5 ms per 4 MiB file). `posix_fadvise(WILLNEED)` changed
  nothing on either filesystem. Phase 5.5.
- **Round trips overlap almost linearly.** 16 reader threads in one rank read 500 files/s on
  scratch and 389 on campaign, against 26–30 one at a time; 16 parallel `cat`s reach 2.9 ms
  per 1 MiB file on scratch and 1.3 per 80 KB file on campaign. Phase 5.5, sweep 0.4. Hence
  walktar's `--read-ahead` [16] and walksync's `--rsyncs` [8].
- **Per node, small files top out at ~3.6 GiB/s on scratch** (31 ranks × 16 readers, 1 MiB
  files: 200,000 in 53–54 s) and **~3.2–3.6 GiB/s on campaign → csg** (VIT zarr, 333,110 ×
  4 MiB, ~850 files/s) under the default binding, ~4.0–4.9 GiB/s on both with two cores
  per rank; big files at 2.4–2.9 GiB/s (scratch) and 5.4–6.3 (campaign). Phase 5.5, Part
  B, perf-1 step A.
- **Nodes add up on campaign.** VIT → csg with two cores per rank: 4.0–4.6 GiB/s on one
  node, 8.0–8.6 on two, 16 on four (333,110 files in 80 s) and 24 on eight (55 s; 24.6 at
  the peak), the GPFS client per node being the limit through four; csg took 24 GiB/s of
  writes without a sign of strain. So the VIT-shaped case scales with nodes, not with
  anything inside a node. The tail is one batch: the walk ends ~20 s after the first rank
  runs dry (a 1,000-file VIT batch is 4 GiB, ~30 s for one rank), which is 40% of the
  8-node walk. A smaller batch trims it: `--batch-size 2GiB` gave 29–30 GiB/s at 8 nodes
  against 23–25 by default. At 16 nodes the average reached ~49 GiB/s (10-second peaks of
  107–109), ~3 GiB/s per node, and neither csg nor GDEX had flattened; VIT (1.3 TiB) is too
  small to say more, since a third or more of a 26 s walk is start-up and tail. perf-1
  step B, with the 8-, 12- and 16-node runs added in indexes-2.
- **The reader threads need CPU, and two cores per rank are enough.** PALS pins each rank
  to one CPU (`os.sched_getaffinity(0)` is `{rank}`) and the 16 readers share it. Any wider
  binding lifts the small-file rate the same, 1.4–1.6× on both filesystems: `manyfiles`
  62 → 40–45 s and the VIT zarr 385–442 → 267–342 s for `mpiexec --cpu-bind depth --depth
  2`, `--depth 4`, `--depth 8` and `--cpu-bind none` alike; big files never move. Past two
  cores the readers wait on the storage, not the CPU, and 8 threads lose 20% where 32 or 64
  gain nothing over 16. **On small files, the binding was the ceiling on both filesystems**,
  which phase 5.5 had read as the scanning rank's stat rate (the scanner's ~1,000 entries/s
  merely tracked the archive rate). So the launcher binds (`--cpu-bind depth --depth 2`, on
  the scan scripts' `mpiexec` lines), every run prints `CPUs per rank: N`, and walktar does
  not size `--read-ahead` from the mask. Part B, perf-1 step A.
- **Scratch's servers cache recent reads.** A file read from another node moments ago costs
  ~5 ms instead of ~35; a client-side `DONTNEED` only drops the local copy (~2 ms re-read).
  Back-to-back runs on one small-file tree flatter the second: put a big tree's I/O between
  them (`benchtar.pbs` runs `bigdir`, 1 TiB, between `manyfiles` runs). Campaign has no
  cheap flush; its coldness probe at 20 ms per file was cold by itself. Phase 5.5, sweep 0.4.

## 3. Metadata: stats, scans and directories

- **A stat costs ~1–2 ms idle and 6–9 ms under load on campaign.** Phase 5 saw ~6 ms per
  stat per rank on campaign GPFS (one rank over a 1M-entry directory took an hour); the
  Part B profile saw 9 ms per lstat while 31 ranks read from the same node; perf-1 step C
  saw 1.5–2 ms with only the production scans running. Lustre's are sub-millisecond. So
  walkstat's stat batches (names from `scandir`'s `d_type`, stats spread over every rank)
  and walktar's the same.
- **A loaded stat is latency, an unloaded one is throughput.** On an unloaded campaign
  walkstat's `--stat-threads` 4 or 16 (K lstats in flight per rank) walked VIT and
  `.Radiosonde` at the same 15–23k entries/s as the serial batches: the node's GPFS
  client's ceiling (phase 3.5's), which overlapping cannot lift. While the same node read
  campaign at 3–4 GiB/s (a walktar beside the walkstat), a stat cost ~18 ms and the serial
  walk of `.Radiosonde` took 343 s against 20–30 s unloaded; 4 threads brought it to 195 s
  and 16 to 65 s (5.3×). So a node that reads should stat with threads (walktar does, Part
  B; walkstat with `--stat-threads 16`), and a node that only stats gains nothing from
  them. perf-1 step C.
- **A walk is bound by the filesystem client on each node, not by ranks.** On one node 32
  ranks walk faster than 96; on 4 nodes 4 × 32 beats 4 × 96 by 21–44%. CGD-ppc (3.6M
  items): 114 s at 1 × 32 (32k items/s), 38 s at 4 × 32 (101k items/s). Phase 3.5, phase 4.
- **Filters cost nothing.** A filtered walk counting 1.2M of 3.6M entries took the same 114 s:
  the walk is bound by the stats, and a filter adds none. Phase 4.
- **The Manager is never the limit.** Blocked in `recv` 98–100% of the time at every rank
  count, handling a message in microseconds; the work moves by local stacks, overflow and
  shares. At 4 × 32 on VIT (perf-1 step B) it handled 37,424 messages in 0.6 s of an 80 s
  walk; 36,253 of them were share requests, 35,520 answered empty (phase 5.5's question 4:
  the ping-pong is there, 9× the one-node count for 4× the ranks, and it costs the busy
  workers one probe and one small reply each, nothing measurable). Phase 3, 3.5, perf-1.
- **mpi4py's matched probe hangs across Derecho nodes.** A walktar over 2 nodes stopped
  every time at ~2 minutes with every rank spinning in `MPI_Mprobe`: `ready` messages that
  were sent were never matched by the Manager's any-source receive (four of 63 in the
  dumped run), with or without the rendezvous threshold raised. `mpi4py.rc.recv_mprobe =
  False` (Probe + Recv) fixed it; the walk tools set it before importing MPI. One node
  never leaves shared memory, so single-node runs never saw it. perf-1 step B.
- **One destination directory takes ~1,250 creates per second from Derecho and ~1,000 from
  Casper, whatever the file size.** A probe that makes files as rsync's receiver does (a
  temporary name, write, fchmod, close, utime, rename) with 32 processes: 1,231–1,296/s into
  one directory for 80 KB and 1 MiB alike, 2,621–4,278/s into a directory per process, and
  1.9–3.1 ms per file for a single process. The directory is the serialization point; the
  bytes are not. Sweep "K rsyncs". A login-node probe had seen the same shape (26.5 s into
  one directory against 14.1 into 32, Part A).
- **One scratch directory takes ~6,000 unlinks per second, whichever tool.** A mock of 4D's
  `cpl` on Derecho scratch (741,000 × 4 KiB files in `hist/`, 917,204 entries in all), 2 nodes
  × 32 (tools-find-rm, step 2): `drm` 154 s (5,940 items/s), walkrm 144 s (137.7 s walk, 0.1 s
  for the 204 directories). walkrm scanned all 917K names in 12 s and spread the 741 unlink
  batches over the ranks (730 moved by shares). Both then ran at the directory's unlink rate,
  so the gain is small. drm's ~17-minute stall in 4D's real delete (`cpl/hist` on campaign
  GPFS, 1,048 s for ~1M items) did not happen on scratch; a campaign comparison is still open.
- **A tree's names cost a scandir each, not a stat each.** walkfind `--name 'COLD_STORAGE*'`
  stats only its matches: 240,921 entries of CESM-HR on campaign in 4 s after dfind's 57 s,
  though dfind's walk had just warmed the metadata cache (tools-find-rm, step 1).
- **The production scans load campaign's metadata.** With csgteam's `scan_dirs` array (8
  subjobs of 1 × 128) running, the same `.Radiosonde` copy took 912 s against 367 s in the
  morning. Only pairs from one job compare; note `qstat -t -u csgteam | grep -c " R "` with
  any campaign number. Part A, Part B.
- **Lustre honors `O_NOATIME`, and a per-entry line of output costs nothing.** walktar's
  readers open with `O_NOATIME` (the owner or root); a full read of `manyfiles` left the
  sampled atimes untouched. Writing an index line per entry (walkstat `--index`, walktar
  `--index`: a dict lookup, a formatted line, a buffered write) was within the noise of
  the same walks without it: 1,000,000 cached stats in 1.6–1.9 s either way, 200,000
  1 MiB files archived in 52–57 s either way. `du` of a file just written on Lustre
  under-reports its blocks for a while (16 KiB for a 25 MB file). Part C.

## 4. Writing and copying

- **rsync's per-file cost is the cold read.** One rsync over 1,000 cold 1 MiB files on
  scratch: 48.9 ms per file, of which 41 ms is the source read and ~8 the whole destination
  side (create, write, chmod, owner, ACL, xattrs, times, rsync's stats of both sides); on
  campaign, 20.0 ms per 80 KB file with 18.6 in the read. `-A`, `-X` and `-t` together are
  within the noise (≤ 0.9 ms), so dropping them buys nothing. rsync's own CPU is 7.7 ms per
  1 MiB file (three processes, two pipes, a whole-file checksum) and 1.6 per 80 KB file.
  Sweep 0.4.
- **N rsyncs side by side scale like reader threads, to the CPU.** Pinned to one CPU, 1 MiB
  files go 49.7 → 24.2 → 12.0 → 8.1 ms per file for N = 1, 2, 4, 8 and saturate there
  (91% CPU); 80 KB files reach 2.3 ms at N = 16. Free of the pin, N = 16 reaches 3.1 and 1.6
  ms, the storage's parallel read rate. Sweep 0.4.
- **At 1 × 32 the destination directory decides what K buys.** walksync into one directory
  (manyfiles, 200,000 × 1 MiB) makes ~450–520 files/s at any K (K = 1 451 s, K = 8 385 s,
  with `--cpu-bind depth` 450 s), 2.5× below the probe's ~1,250 creates/s: rsync's own
  directory operations per file (the stat of the destination name, the temporary name, the
  rename, the xattr and ACL calls) share the directory's rate. Across `.Radiosonde`'s 292
  directories (584,851 × 80 KB) K pays: 892 → 405 → 342 s for K = 1, 4, 8, and 285 s
  (2,050 files/s) with `--cpu-bind depth`. The same from Casper (K = 8: 398 s on manyfiles,
  362 on `.Radiosonde`), so the ceiling is the storage's. Sweep "K rsyncs", Part A.
- **Several nodes creating in one campaign directory stall each other.** 4D's walksync
  re-copy (`sync/README.md`, "Measured: 4D"): ranks on 4 nodes creating files in one GPFS
  directory (`cpl/hist`, 741K files) made name lookups there take 2.5–7.3 s (strace), while
  lookups elsewhere took microseconds; ~214 rsyncs per node sat in `path_openat`, the nodes
  97% idle, ~170 files/s for the whole job. Each create on another node takes the directory's
  token away. Large files in many directories, same job: 9.4–11.8 GiB/s. Cutting the directory
  operations per file helps most: rsync `--inplace` (no temporary name and rename) copied the
  same directory ~5–6× faster by time and files (579 files/s, 8.5 GiB/s at 4 nodes), though
  28–210 rsyncs per node still waited in `path_openat`. Lustre scratch has its own rules (it
  also adds a default ACL when an access ACL is set on a directory): measure campaign on
  campaign.
- **Campaign writes scale with nodes to at least ~86 GiB/s.** walksync copies of large files
  in many directories, campaign to campaign, into empty destinations with `--inplace`
  (`sync/README.md`, "Measured: 4B, 4E and 4F"): 4 nodes 12.9 GiB/s (4B, ~195 MB files, ~3.2
  GiB/s per node), 16 nodes **~86 GiB/s sustained for ~10 h** (4F, 3 PiB of ~23 GB files, ~5.4
  GiB/s per node), the nodes 85–92% idle throughout. Grafana's csfs1 panel showed ~90–100 GiB/s
  written plus as much read, about 3× the prior week's peak writes (~35 GiB/s); average write
  requests 7.4 MiB (near-full 8 MiB blocks), HDD ~12K reads + ~12K writes/s, metadata (SSD)
  ~20K io/s. The node side was never the limit; the next ceiling is csfs1 itself, and the
  other users who share it. The DATA pool had 16.2 of 141 PiB free before 4F's 3 PiB.
- **Writing is cheap next to reading.** Writing the tars to Lustre costs a little against
  GPFS (6.31 → 5.43 GiB/s reading campaign); the write side of a copy is ~8 ms per 1 MiB
  file on Lustre, ~3 per 80 KB file, and 0.5–1.4 ms over rsync's own floor on flash.
  Phase 5, sweep 0.4.
- **Batches of 1,000 files or 16 GiB balance a node.** 100 GiB batches reached only 11 of
  31 ranks on 1 GiB files; 5,000-file batches left one rank with 9.8 GiB against a mean of
  6.3. Phase 5.

## 5. Measuring: what to control

- **Pair every before/after in one job**, rotating the order; campaign varies with the
  center's load (912 vs 367 s), scratch's server cache with what ran just before.
- **Flush scratch between small-file runs** with a big tree's read; campaign cannot be
  flushed, so its cold probe is the control.
- **Record the binding** with any thread or rsync count: PALS's one CPU per rank is the
  default and often the limit. `benchtar.pbs` and `benchK.pbs` take `MPIEXEC_ARGS`.
- **CPU per rank includes MPI's spin.** An idle rank busy-waits in `recv` (the Manager's
  CPU time equals its wall time), so user+sys from GNU `time` reads "less idle" as "less
  work". `cpu-wrap.sh` records it per rank; compare it with the idle counts on the progress
  line.
- **Check a big copy's rate outside the tool.** A counter that credits work when it finishes
  lags when the units are large: walksync's progress line said 36.7 GiB/s for 4F while the
  destination fileset's `df` grew 85.8 GiB/s (5,146 GiB in 60 s), Grafana's csfs1 writes read
  ~88 and the job nodes' summed HSN `tx_bytes` 88.2. Since tools-find-rm (step 0) walksync
  counts its big files while rsync writes them: on an 842 GiB mock (8 × 100 GiB) it tracked
  `du -sb` of the destination at 0.96–1.00 (temp files) and 0.91–1.00 (`--inplace`) after the
  first minute. walktar still credits a member when it is done. Take `df -B1G` of the destination fileset
  a minute apart, or the GPFS dashboard (grafana.hpc.ucar.edu, "GPFS Filesystems / Storage
  systems", row csfs1); its legend's "Last" is a partial interval, so read the curve.
- **Check the copy with the oracle** (`rsync -naxAX --delete -i --chmod=Du+w SRC/ DEST/`):
  nothing but the destination root's own time, permission and ACL line should remain.
- **Say what else was running** (`qstat -t -u csgteam`), and stay within the data rules:
  scratch within the quota, csg for tars only and removed after, GDEX read-only, no other
  campaign data read for a tar or copy (atimes drive the purge reports), flash for profiling
  only and emptied after.
- **The flash fileset** (`/glade/campaign/flash_test/csg`, fileset `flash_test`, 600 TiB
  quota) takes the disks out of a per-file cost and leaves the software. It is a profiling
  aid only: stay below 200 TiB, remove everything after each run, and check its use with
  `/usr/lpp/mmfs/bin/mmlsquota --block-size auto -j flash_test csfs1` (it is not in
  `gladequota`).
- **A Casper node to work on interactively:**
  `qsub -q casper -l select=1:ncpus=34:mpiprocs=32:mem=200GB -- /bin/sleep 9000`, then
  `ssh <node>` from a login node attaches to the job. `pbsnodes -a` shows which node sizes
  are free.

## 6. Rules of thumb

1. Small files: parallelize the round trips (readers, rsyncs), then give the rank the CPU
   for them: `mpiexec --cpu-bind depth --depth 2` (two cores per rank; wider buys nothing)
   before any change to the walk, and read the `CPUs per rank` line of every log.
2. Big files: one node reads scratch at ~2.5 GiB/s and campaign at ~6; more nodes, not more
   threads. Small files on campaign scale with nodes too: 4, 8, 16 GiB/s at 1, 2, 4
   nodes, then ~30 at 8 and ~49 at 16 with `--batch-size 2GiB` (at the default the 8-node
   run is tail-bound at 24: shrink the batches as nodes are added).
3. Metadata: spread stats over ranks and nodes; expect 1–2 ms each on an idle campaign,
   6–18 ms on a node that is also reading, where threads (walktar's readers, walkstat
   `--stat-threads 16`) give it back; a node that only stats is at its client's ceiling
   (~15–30k entries/s) and threads add nothing; don't add per-entry work on the hot path.
4. Copies: the destination directory sets the create rate (~1,000–1,250/s per node); many
   directories or many nodes beat any K; K = 8 is safe everywhere. But one directory written
   from several nodes at once is far worse than from one (campaign: seconds per lookup);
   into an empty destination, rsync `--inplace`. Large files: campaign-to-campaign copies
   gave 3.2–5.4 GiB/s per node up to 16 nodes (~86 GiB/s); before a multi-PiB copy, check the pool's
   free space and remember the other campaign users at that rate.
5. Measure in pairs, record the load and the binding, flush scratch, read CPU with the idle
   counts.

## 7. Open

- **Where campaign's write side or GDEX's read side stops scaling with nodes.** Small files
  (VIT → csg) had not flattened at 16 nodes (~49 GiB/s average), but VIT is too small to
  give a sustained number there; that needs a tree several times its size, each run writing
  ~5 TiB to csg. Large-file copies campaign to campaign had not flattened at 16 nodes either
  (~86 GiB/s, section 4). Where csfs1 stops is untested: a question for a maintenance window
  rather than production.
- **Why walktar's per-rank stats cost 4–7% on VIT** under the one-CPU binding even threaded
  (the stats' CPU on the busy ranks is the hypothesis; with four CPUs per rank the threaded
  stats pay 7% instead). Also: a file's open and close cost ~5 and ~7 ms on a loaded
  campaign node, as much as a stat; whether that is the client or the server is unmeasured.
- **Which side limits walksync on small files past ~12 nodes** (~18,000 files/s, campaign →
  scratch): scratch's metadata servers or campaign's reads. A scratch-to-scratch copy, or
  walktar reading the same tree, would separate them.
- **walksync's costliest-first copy order** is implemented and not yet measured at scale
  ([walk-tail-4F.md](walk-tail-4F.md)).

Closed: share ping-pong (the empty share replies grow with ranks but cost nothing
measurable, at 4 nodes × 96 and at 8 × 32; [phase3.5-results.md](phase3.5-results.md),
perf-1 step B).
