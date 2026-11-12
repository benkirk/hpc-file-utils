# GLADE performance: what the walk tools measured

What derecho and Casper's filesystems do under a parallel walk, archive or copy, as
measured while building walkstat, walktar and walksync (phases 3–6 and the sweep, 2026-11).
Every number has a source: a phase note in `hpc_file_utils/walk/README.md`, a section of
`docs/HANDOFF-sweep.md` (cited as "sweep 0.4" and so on), or a derecho job in
`/glade/derecho/scratch/benkirk/hfu-bench/`. Unless said otherwise a run is one derecho node,
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
  its parallel capacity is derecho's (16 `cat`s at once: 2.98 vs 2.90 ms per file). Its
  path to GPFS is derecho's (one rsync 20.7 vs 20.0 ms per 80 KB file). Sweep 2, "The same
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
  8-node walk, so a smaller `--batch-size` (1–2 GiB) is the next lever there. perf-1
  step B.
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
- **mpi4py's matched probe hangs across derecho nodes.** A walktar over 2 nodes stopped
  every time at ~2 minutes with every rank spinning in `MPI_Mprobe`: `ready` messages that
  were sent were never matched by the Manager's any-source receive (four of 63 in the
  dumped run), with or without the rendezvous threshold raised. `mpi4py.rc.recv_mprobe =
  False` (Probe + Recv) fixed it; the walk tools set it before importing MPI. One node
  never leaves shared memory, so single-node runs never saw it. perf-1 step B.
- **One destination directory takes ~1,250 creates per second from derecho and ~1,000 from
  Casper, whatever the file size.** A probe that makes files as rsync's receiver does (a
  temporary name, write, fchmod, close, utime, rename) with 32 processes: 1,231–1,296/s into
  one directory for 80 KB and 1 MiB alike, 2,621–4,278/s into a directory per process, and
  1.9–3.1 ms per file for a single process. The directory is the serialization point; the
  bytes are not. Sweep "K rsyncs". A login-node probe had seen the same shape (26.5 s into
  one directory against 14.1 into 32, Part A).
- **The production scans load campaign's metadata.** With csgteam's `scan_dirs` array (8
  subjobs of 1 × 128) running, the same `.Radiosonde` copy took 912 s against 367 s in the
  morning. Only pairs from one job compare; note `qstat -t -u csgteam | grep -c " R "` with
  any campaign number. Part A, Part B.
- **Lustre honours `O_NOATIME`, and a per-entry line of output costs nothing.** walktar's
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
  token away. Large files in many directories, same job: 9.4–11.8 GiB/s. Keep a huge
  directory's creates on one node, and cut the directory operations per file (rsync
  `--inplace`: no temporary name and rename). Lustre scratch has its own rules (it also adds a
  default ACL when an access ACL is set on a directory): measure campaign on campaign.
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
- **Check the copy with the oracle** (`rsync -naxAX --delete -i --chmod=Du+w SRC/ DEST/`):
  nothing but the destination root's own time, permission and ACL line should remain.
- **Say what else was running** (`qstat -t -u csgteam`), and stay within the data rules:
  scratch within the quota, csg for tars only and removed after, GDEX read-only, no other
  campaign data read for a tar or copy (atimes drive the purge reports), flash for profiling
  only and emptied after.

## 6. Rules of thumb

1. Small files: parallelize the round trips (readers, rsyncs), then give the rank the CPU
   for them: `mpiexec --cpu-bind depth --depth 2` (two cores per rank; wider buys nothing)
   before any change to the walk, and read the `CPUs per rank` line of every log.
2. Big files: one node reads scratch at ~2.5 GiB/s and campaign at ~6; more nodes, not more
   threads. Small files on campaign scale with nodes too: 4, 8, 16, 24 GiB/s at 1, 2, 4,
   8 nodes (the last one tail-bound: shrink the batches).
3. Metadata: spread stats over ranks and nodes; expect 1–2 ms each on an idle campaign,
   6–18 ms on a node that is also reading, where threads (walktar's readers, walkstat
   `--stat-threads 16`) give it back; a node that only stats is at its client's ceiling
   (~15–30k entries/s) and threads add nothing; don't add per-entry work on the hot path.
4. Copies: the destination directory sets the create rate (~1,000–1,250/s per node); many
   directories or many nodes beat any K; K = 8 is safe everywhere. But one directory written
   from several nodes at once is far worse than from one (campaign: seconds per lookup).
5. Measure in pairs, record the load and the binding, flush scratch, read CPU with the idle
   counts.

## 7. Open

- Where campaign's write side or GDEX's read side stops scaling with nodes: 8 nodes gave
  24 GiB/s with the one-batch tail taking 40% of the walk, so the per-node rate is not
  known to have fallen yet; a smaller `--batch-size` at 8 nodes, then 16 nodes, would
  say (phase 5.5 question 5 is answered to 8 nodes).
- Why Part B's per-rank stats cost 4–7% on VIT under the one-CPU binding even threaded
  (the stats' CPU on the busy ranks is the hypothesis; with four CPUs per rank the threaded
  stats pay 7% instead). Also: a file's open and close cost ~5 and ~7 ms on a loaded
  campaign node (the B2 profile), as much as a stat; whether that is the client or the
  server is unmeasured.
- LPT batch order, share ping-pong at 4 nodes (phase 5.5 questions 3 and 4).
