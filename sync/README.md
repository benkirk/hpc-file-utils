# cgd_migration

Large cross-fileset copies on `/glade/campaign` for CGD (Nov 2026 derecho downtime),
modeled on `../p_migration`. Each pair in `pairs.conf` is synchronized by one PBS job,
`sync_dir_job_array.sh`:

1. **HSM preflight** - `dfind` for `COLD_STORAGE*` → `cold_storage.<pair>.list`.
   Aborts if any are found and the pair is not `hsm: yes`: HSM stubs are never read, so no tape recalls.
   Also aborts if the source is empty (an `rsync --delete` would empty dest).
2. **dsync** (mpifileutils 0.12) bulk copy across all nodes. **Skipped for `hsm: yes` pairs** -
   dsync has no `--exclude`.
3. Release all but one node, then **msrsync** (multi-stream rsync, one rsync per usable CPU,
   `COLD_STORAGE*` pruned) - by default only for `hsm: yes` pairs, where dsync was skipped.
4. Serial **`rsync --delete --max-delete=N`**, `--exclude='COLD_STORAGE*'` (which also protects
   the dest symlinks).
5. Index every non-directory in dest → `flist-destdir.out.<suffix>.xz`, and compare a random
   sample of copied files byte-for-byte with the source.
6. **Done stamp** `stamps/<pair>.done` - only if rsync exited 0, dsync logged no errors and the
   sample matched.

A job for a pair with a valid done stamp exits immediately. Every stage is idempotent, so
repeated submissions ratchet each pair to completion (see *Done stamps & resubmission*).
Copies run as `${USER}`; ownership is fixed at the very end by root.

## Config: `pairs.conf`

```
# global settings (before the first [section]) - shared by every job and array member
queue:     main
walltime:  12:00:00
nodes:     4
chunk:     ncpus=128:mpiprocs=32:mem=235G    # per-node PBS resources; dfind/dsync run 'mpiprocs' ranks/node
maxconcur: 4

fresh_after: 2026-11-03 08:00                # optional: older done stamps don't count

casper.queue: casper                         # '<host>.key' wins on that host (${NCAR_HOST})
casper.chunk: ncpus=16:mpiprocs=16:mem=160G

# one [section] per pair: its name labels jobs & logs, section order = job array index
[3C-cmip6-timeseries]
src:   /glade/campaign/collections/cdg/data/CMIP6
dest:  /glade/campaign/cesm/cmip/cmip6/cmip-timeseries
hsm:   no             # optional: 'yes' if the tree contains COLD_STORAGE* dirs
owner: asphilli:cesm  # optional: new owner:group (else the source's ownership is kept)
done:  no             # optional: 'yes' = finished for good, never submitted again
batch_files: 50000    # optional tunable, also settable globally (see below)
```
Comments may follow a value, but not a `[section]` header.

Tunables, per pair or global:

| key | default | |
|---|---|---|
| `batch_files` | 250000 | dsync `--batch-files` |
| `msrsync` | `auto` | `auto` = hsm pairs only, `yes`, `no` |
| `msrsync_procs` | `nproc` | on the node left after the release |
| `max_delete` | `1%` | rsync `--max-delete`: N, or N% of the source items (at least 1000) |
| `verify_sample` | 200 | files compared with the source before stamping (0 = off) |

Safety checks on every pair: src and dest absolute and normalized, different, not nested
(also after resolving symlinks), under `allowed_roots` (global, default `/glade/campaign/`),
no `#` on src/dest lines; section names unique and limited to `A-Za-z0-9._-`.
Environment variables override the global settings for one submission, e.g.
`nodes=16 walltime=01:00:00 ./submit_sync_array.sh <pair>`. `pairs_conf=<file>` selects another config.

## Usage

Submit from a login node of the cluster to run on (derecho or casper); the job script picks the
modules for that cluster and launches MPI with `mpibind`.

```bash
make list                                  # parsed settings & pairs - sanity check
make status                                # done stamps & latest job log line per pair
make status_brief                          # counts + only the pairs not yet done (big split configs)
make run                                   # job array over all pairs (make run nodes=8 ...)
make run PAIRS="<pair> <pair>" rounds=2    # job array over named pairs, submitted twice in a chain
./submit_sync_array.sh <pair> ...          # the same: array over named pairs (plain job for one)
after=<jobid> ./submit_sync_array.sh ...   # start after another job (or array 'NNN[]') ends
fresh_after=now ./submit_sync_array.sh ... # ignore existing done stamps (re-sync everything once)
dryrun=yes ./submit_sync_array.sh <pair>   # preflight + 'dsync --dryrun' only (see caveats)
make link                                  # COLD_STORAGE symlinks for hsm pairs
make compress PAIRS=<pair>                 # request GPFS compression of the copies
make compress_status                       # fileset usage; ./compress_dest.sh --status --exact <pair>
make root_acl_commands PAIRS="..."         # before copying: read ACLs on the sources (root group)
make root_commands                         # root hand-off for pairs keeping source ownership
make root_chown_commands                   # root hand-off for pairs with a new 'owner:'
make archive_logs                          # move logs into logs/<timestamp>/ (refused while jobs run)
```

Logs: the job log (`cgd_migration.o<jobid>.<index>` for an array member, `cgd_<pair>.o<jobid>`
otherwise) has per-stage timings and exit status; each stage writes
`{dfind,dsync,msrsync,rsync}.{out,err}.<pair>.<jobid>.<timestamp>`.

## Done stamps & resubmission

A pair is **done** when its final serial `rsync --delete` exits 0, dsync logged no errors and
the sampled files match: the job then writes `stamps/<pair>.done` (in the run directory) with
src, dest, the pass's start/finish time, the job id and rsync's created/deleted/transferred
counts. rsync exit 23, 24 (source files vanished) or 25 (`max_delete` reached) is logged but not
stamped - another pass, or a look at the logs, is needed. Submissions leave out pairs that
already have a valid stamp.

A stamp is *valid* when its src/dest match `pairs.conf` and its pass **started** at or after
`fresh_after`. Every job checks first and exits within seconds for a valid stamp, so the same
array can be submitted repeatedly and each member resumes where it stopped (dsync keeps
completed batches) until it is stamped:

```bash
make run PAIRS="3C-cmip6-timeseries 3D-cmip5-timeseries" rounds=3
```

`rounds=N` submits the array N times, each round `afterany` the previous one.
`fresh_after` (setting or environment, any `date -d` string) is resolved to a fixed time **at
submission**, so all rounds of a chain share it; `fresh_after=now` re-syncs every pair once and
then ratchets as usual. `make status` shows each pair's stamp (valid / stale / mismatch / none)
and the last line of its newest job log.

A stamp says the copy was complete as of its pass; a later `fresh_after` reopens it. Once a
pair is finished for good - typically after the root hand-off has changed the dest permissions,
so we could not write anyway - set `done: yes` in its section by hand. It is then never
submitted (named or not, whatever `fresh_after` says), a job still queued for it exits at once,
and `make status` shows `done`. `make link`, `compress` and `root_commands` still accept it.

A lock `~/.sync_dir.<md5(src+dest)>.lock` (GPFS, so cluster-wide) prevents two jobs syncing the
same pair. It is removed on exit, including walltime kills; a SIGKILLed job can leave it
behind - remove it by hand.

## Behavior & caveats

- **Reruns are cheap.** dsync re-copies a file only if type, size or mtime differ; a rerun of an
  unchanged 174 TiB tree took 74 s in dsync, 36 min in total (21 of them msrsync's single-node
  crawl - hence `msrsync: auto`).
- **Interrupted runs resume.** A rerun keeps the files of completed `--batch-files` batches and
  deletes & recopies the partial batch (3C: killed at the walltime, the next run removed 8,834
  items and copied only the remaining 528 of 589 TiB).
- **dsync's exit status means nothing.** As non-root its `lchown` to the source uid/gid fails
  (EPERM, silently), which can make it exit non-zero on a perfect copy; and it exited 0 on a run
  that logged real `ERROR:` lines (an unreadable file). The job judges it by its log instead:
  `Completed sync` in `dsync.out`, no `ERROR:` lines there, and nothing but known launcher noise
  in `dsync.err` - otherwise no stamp. rsync's size+mtime check is not a content check, hence
  the byte-for-byte sample before stamping.
- **mpibind can exit 0 when the MPI launcher crashed.** The job therefore requires dfind's final
  `Walked ... seconds` summary and dsync's `Completed sync`, and aborts otherwise.
- **Casper: Open MPI's launcher (PRRTE 3.0.11a1) segfaults for some multi-node allocations**,
  depending on the node set, before any rank starts. Such jobs abort at the preflight; derecho
  (cray-mpich) is not affected.
- **Size `batch_files` by data volume.** dsync sets file metadata per batch, so files of an
  interrupted batch are recopied on restart: keep one batch well under the walltime. A batch
  counts *all* items - directories and symlinks too, not just files.
- **Throughput** (3D, 174 TiB): 7.1 GiB/s on 4 casper nodes x 16 ranks; 3C reached 50-60 GiB/s
  on 16 casper nodes x 9 ranks.
- **`dsync --dryrun` ignores source-only files**, so against an empty or partial dest it
  under-reports; it is only meaningful for an almost-complete dest.
- **Copies are written uncompressed.** GPFS-compressed sources expand on copy (1.65x for 3D,
  1.07x for 3C): check destination quotas, see Compression below.
- **Non-UTF-8 file names** occur in these trees; the preflight parses dfind's output byte-wise
  (`LC_ALL=C grep -a`) and also counts raw lines naming `COLD_STORAGE`, failing closed.
- **`make clean` / `make archive_logs` refuse while `cgd_` jobs exist** on the cluster you run
  them on (a running job reads its own logs); `clean` keeps the `.xz` indexes.
- **Hard links:** dsync copies every name of a hard-linked file separately (mpifileutils keeps
  no link counts), and msrsync does too whenever the names land in different buckets. The final
  serial `rsync -H --delete` re-links them, so the result is correct - but a link-heavy tree
  (e.g. conda) temporarily occupies N x the space.

The patched `msrsync` is `../p_migration/msrsync.BSK` (skip vs. fail on non-UTF-8 names) plus
`MSRSYNC_PRUNE` pruning (directories and files) in `crawl()`.

## HSM / COLD_STORAGE (3A)

`COLD_STORAGE*` trees stay on the source fileset. After the final sync,
`make link` creates `dest/.../COLD_STORAGE -> src/.../COLD_STORAGE` from the preflight list.

A single `hsm: yes` pair sends the whole tree through single-node msrsync - fine for small
trees, far too slow for CESM-HR (~1.6 PiB). `split_pair.py` (run on a login node) splits such a
pair into an array of dsync-able subtrees plus smaller hsm subtrees:

```bash
./split_pair.py 3A-CESM-HR > pairs.3A-CESM-HR.split.conf   # review the header & leftovers
pairs_conf=pairs.3A-CESM-HR.split.conf rounds=2 make run    # -> last round's array id NNN[]
after='NNN[]' ./submit_sync_array.sh 3A-CESM-HR             # final whole-tree pass, after the array
pairs_conf=pairs.3A-CESM-HR.split.conf make status_brief    # counts + only the pieces not yet done
make link
```

Resources are per submission (`nodes=`, `chunk=`, `maxconcur=` in the environment); all pieces of
one array share them, and an hsm piece releases all but one node after its dfind guard.

- COLD_STORAGE* locations: live `find` (authoritative, seconds for CESM-HR).
- sizes & tree shape: `cs-usage query collections -P /collections/...` (scan DB, paths relative to
  `/gpfs/csfs1`, possibly days old). A scanned directory that is now a symlink or gone (an owner
  moved it away) is skipped and listed as `link`/`gone`; the final pass copies the link itself.
- tune with `--max-hsm-size` (mixed subtrees up to this size, or with at least `--min-size` of
  their own files, become one hsm pair) and `--min-size` (smaller clean subtrees are left for
  the final pass). A mixed directory with large loose files of its own (CESM-HR's `hour_6`
  dirs) has no smaller split - it is one hsm piece, or all of it lands in the final pass.
- each dsync piece gets its own `batch_files`, sized for `--batch-size` (25 TiB) per batch from
  its average file size: a few thousand huge files would otherwise be one batch, all of it
  recopied after a walltime kill.
- the final whole-tree pass catches loose files, small subtrees, and anything newer than the scan;
  every split dsync pair still runs the dfind guard.

What to expect (3A, Oct 2026: casper, 4 nodes x 8 cores per piece, 8 pieces at a time):
- ~50-60 GiB/s aggregate while dsync pieces dominate; ~1.1 PiB in ~9 h.
- msrsync manages only ~1 GiB/s per hsm piece (8 rsync streams), so the hsm pieces set the
  tail. `msrsync_procs` above the core count may help (rsync mostly waits on I/O) - untested.
- dsync stops printing progress near the end of a batch while a few ranks finish the largest
  files - for a single huge batch that can be hours. Judge progress by the destination
  (`find <dest> -type f -mmin -5`, or bytes vs. the source), not by `dsync.out`.
- casper's launcher (PRRTE) segfaults on some node sets, either at startup (`ERROR: dfind
  failed`, `Segmentation fault ... $mpicmd` in `dfind.err`) or at shutdown after `Completed sync`
  (the segfault line in `dsync.err` leaves the pass unstamped). Both fail closed and the next
  round retries; a crashing node set can take several pieces in a row as each failure frees it
  for the next one. Prefer derecho for multi-node pieces when it has room.

## Compression

`compress_dest.sh` requests GPFS compression of a pair's copies instead of waiting for the
Campaign Storage policy (files >= 100 MiB, >= 180 days;
[docs](https://ncar-hpc-docs.readthedocs.io/en/latest/storage-systems/glade/campaign/#compression-details)):

- default `mmchattr -I defer --compression z` marks the files; the next scheduled policy run
  compresses them.
- `--now` compresses immediately on the calling node - ~10 MB/s per file stream, so only with
  a large `-P` in a batch job.
- compression keeps mtime and logical size, so later sync passes are unaffected.
- run it as the files' owner, i.e. **before** the root chown.
- `--status --exact` stats every file (`../glade_hsm/libexec/glade_hsm_scan_filelist.py`):
  true vs. on-disk bytes, ~3 min for 152K files.

## Downtime-day runbook

1. Sources set read-only (step 4 of the request); note the time.
2. Set `fresh_after: <read-only time>` in `pairs.conf` (not just in the environment - `make status`
   reads the config), then `make run PAIRS="..." rounds=N` until `make status` shows every pair
   valid; check that `rsync.err.*` are empty.
3. `make link`
4. `make compress`
5. `make root_commands` and `make root_chown_commands` → review → hand both scripts to the root
   group (each lists the pairs it skips; every pair is in exactly one).
6. Once the root group has run them: `done: yes` on each finished pair.

## Not handled here

- Step 1 (`cesm/development/*` → `cesm/`): `development/cmip` collides with the existing `cesm/cmip`.
- `CESM-HR/HighResMIP -> /glade/campaign/cesm/development/...` will dangle after step 1;
  general dangling-symlink cleanup is deferred (see `../p_migration/find_broken_symlinks`).
