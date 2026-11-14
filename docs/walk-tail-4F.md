# 4F's late-walk tail and the slow cmp sample (tools-find-rm, step 4)

Investigation only: the findings, a local reproduction, and recommendations. Fixing either is its
own sprint.

Source: 4F (km-scale) round 1, job 8165386, 16 nodes × 32 ranks, `copier: walksync`,
`--rsyncs 8 --rsync-opts=--inplace`. Logs are in `/glade/derecho/scratch/benkirk/cgd_migration/`
(`cgd_4F-km-scale.o8165386`, `walksync.out.4F-km-scale.8165386.2026-11-12@11:04`).
- 146,342 entries, 2.93 PiB.
- walksync ran 11.56 h.
- `dfind` had walked the same tree in 66 s.

## 1. The late-walk tail

### What the progress lines show (every 60 s; sampled here)

| time | walked | queued (workers) | idle ranks | note |
|---|---|---|---|---|
| 11:07 | 87,167 | 12,774 | 0 | 1 min in |
| 11:27 | 138,197 | 23,440 | 0 | |
| 11:27 – 16:47 | **138,226** | 23,440 → 9,570 | 0 | walk count flat for 5 h 20 min |
| 17:27 – 20:39 | **143,819** | 8,546 → 3 | 0 | flat again for 3 h 12 min |
| 20:39 – 20:45 | 143,819 | **3** | 59 → 190 | ranks go idle, 3 items still queued |
| 20:48 | 145,842 | 208 | 0 | +2,023 entries found |
| 21:18 | 146,340 | 1 | 201 → 448 | +497 |
| 21:30 – 22:39 | 146,342 | 0 | 464 → 510 | the last huge copies; ~500 of 511 ranks idle |

The Manager's own lines say the same: "the first idle worker waited 6,077.55 s for the end".
751,291 share requests were sent, and 748,807 were answered empty.

### Two causes, both confirmed

**(a) A busy rank never shares its last directory.** `BaseWorker.oldest_half()` gives away
`len(local_dirs) // 2` directories, which is **0 for a rank holding one**. The batch half is
rounded up (`oldest_batches()`), but the directory half is not. While a rank runs a single-file
copy batch for an hour, the one directory left on its stack stays there, however many ranks are
idle and asking. 4F's `queued=3, idle 59 → 190` for six minutes is that, followed by a burst of
2,023 entries when the copy ended.

**(b) The walk waits behind the copies.** `drain_local_work()` processes batches before
directories, newest first. In walksync a compare batch creates copy batches, so a rank works
through every copy it has found before it walks its next directory. Files over `--batch-size`
(16 GiB) are one batch each, and 4F had files up to ~930 GiB, at ~180 MB/s per rsync (2.93 PiB
over 18.2 M rsync-seconds). With every rank busy (`idle=0` until 20:39), no `share` is ever
sent, so the queued directories and compare batches stay where they are. 4F's walk, which
`dfind` did in 66 s, therefore took 10.4 h. Its last discoveries were huge files, found at
20:48 and 21:18. Each was copied by one rsync on one rank: about 1.5 h for a 900 GiB file,
while ~500 ranks waited. That is the ~2 h tail, about 17 % of the run.

### Reproduction (laptop, 3–4 ranks, one rsync each at `--bwlimit=6000`)

The tree:
- `src/d1/big.dat`, 30 MB;
- `src/d2/big.dat`, 90 MB;
- `src/d1/deep/` holding 50 files and `deeper/` with 50 more.

The run was `--batch-size 10MB`, so each big file is its own batch, as 4F's were.

| run | what the progress line shows |
|---|---|
| 3 ranks (2 workers, both copying) | `walked 5, queued=1, idle=0` for 5 s, then 106: `deep/` waited behind its rank's 5 s copy (cause b) |
| 4 ranks (one worker idle) | `walked 5, queued=1, idle=1`, shares 5 → 8 for 4 s: the idle rank asks, the busy rank answers empty (cause a) |
| 4 ranks, `oldest_half()` rounded up (temporary edit, reverted) | the idle rank takes `deep/`; walk done at 3 s instead of 6 s |

### Recommendations

1. **Round the directory half up**, as the batch half already is:
   `n = len(local_dirs) - len(local_dirs) // 2` in `oldest_half()`.
   - It's a one-line change, for every tool.
   - A rank that is walking its own stack may give away its last directory and then ask for
     work, which costs one round trip.
   - It is safe for termination: shares already move directories.
2. **walksync: walk before copying.** Let SyncWorker pick compare batches and directories
   before copy batches, with a `next_work()` hook in `drain_local_work()`.
   - The walk then finishes in about the time `dfind` takes, and every copy batch is known
     early.
   - It is safe for termination: the rule "batches create no directories" is untouched.
   - The cost is more copy batches held at once. On 4F that's ~29,000 small tuples, a few MB per
     rank at most.
3. **Largest batches first.** Once the copies are known, hand out the biggest first (LPT
   scheduling):
   - the Manager keeps its batches ordered by `nbytes`, a field every batch kind has;
   - a share hands over a rank's biggest batches, not its oldest.

   A 930 GiB file started in the first minutes costs its ~1.5 h inside an 11 h run instead of
   after it.
4. **Single huge files stay the floor.** One rsync copies one file at ~180 MB/s, so a 1 TiB
   file is ~1.6 h whatever the schedule. Beyond that only a chunked parallel copy helps
   (several ranks writing ranges of one file with `--inplace` semantics). That's out of scope;
   noted for the copier sprint.

1 and 2 together would have removed most of 4F's tail. 3 matters once files approach the
run's length.

## 2. The cmp sample (80 min on 4F)

Stage 5 compares `verify_sample` [200] random dest files with their sources. It is **not
serial**, despite the handoff's description: `xargs -0 -n 1 -P 8` runs eight `cmp` processes
at once. Its cost is set by file size:

| 4F's sample (200 files) | |
|---|---|
| total | 4.75 TiB, read twice (source and dest) |
| median | 1.25 GiB |
| 90th percentile | 10 GiB |
| largest | **898 GiB** |
| top 5 files | 48 % of the bytes |

80 min (4,818 s) is the largest file's `cmp` on its own: 898 GiB at ~190 MB/s per stream. The
other 199 files finish long before it, so more parallelism would not help.

### Options

- **(a) A byte cap per file** (recommended): compare files up to a cap, say 16 GiB, whole.
  Above it, compare the first and last 1 GiB plus K random 1 GiB windows (`cmp -i OFF -n LEN`).
  - 4F's worst file becomes ~1–2 min.
  - rsync's size+mtime check already holds the length, and a torn or zero-filled copy shows in
    the ends or the windows.
  - Setting: `verify_max_bytes` [16GiB].
- **(b) A total byte budget:** take shuffled files until the budget is spent, keeping a
  minimum count of small files. This bounds the stage, but a huge file can still take the
  whole budget unless (a) caps it too.
- **(c) A size-stratified sample:** N files per size decade, so small and huge files are both
  represented, and the time stays predictable with (a).
- **(d) More parallelism (`-P 16`):** helps only when many files are large. The largest file
  is still the floor.

Recommendation: (a), then (c) if a sample's mix matters, and keep `-P 8`.

## 3. Noted only

- **walktar's huge-member progress gap:** walktar credits a member's bytes only as
  `tar.offset` moves, while the member is copied in one `addfile()`. A 900 GiB member shows
  nothing until it is done, the same shape as walksync's counter before step 0. It is out of
  scope for this sprint.
