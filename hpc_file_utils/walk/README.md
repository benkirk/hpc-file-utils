# The walk tools

Five MPI programs that walk directory trees in parallel, built on one load-balanced walk:

| tool | what it does | what it changes |
|---|---|---|
| [`walkstat`](#walkstat) | Summarizes usage: totals, users, groups, the largest files and directories | nothing |
| [`walktar`](#walktar) | Archives a tree into many tar files, written in parallel | writes tars in the working directory |
| [`walksync`](#walksync) | Copies what is new or changed to a destination, with batched `rsync` | writes the destination; never deletes |
| [`walkfind`](#walkfind) | Lists the entries that match, like `find` | nothing |
| [`walkrm`](#walkrm) | Removes whole trees, with guard rails | **deletes** |

They exist because a serial `find`, `du`, `tar`, `rsync` or `rm` on a tree of hundreds of
millions of files takes days, and the time goes to metadata round trips that run perfectly well
side by side.

Forked from [benkirk/mpi4py_task_runner](https://github.com/benkirk/mpi4py_task_runner).

## Quick start

Get the environment (this builds it the first time; see the [top-level README](../../README.md)),
or use the shared install on GLADE ([deploy/README.md](../../deploy/README.md)):

```bash
source etc/config_env.sh
```

Then run a tool under MPI. Rank 0 only coordinates, so `-n 32` is 31 walkers:

```bash
mpiexec -n 32 walkstat --dirs /glade/campaign/proj --summary-prefix PROJ
```

While it runs, rank 0 prints a progress line every 5 seconds:

```
[2026-11-15 16:19:08] Walked 1,204,331 items / 3.2 TiB in 1 minute and 5 seconds (18,528 items/sec)
```

When it ends you have, in the working directory, `summary-PROJ` (also printed) and one file per
user under `userlists/PROJ/`. The other tools follow the same pattern:

```bash
mpiexec -n 32 --cpu-bind depth --depth 2 walktar --dirs /glade/campaign/proj/run1
mpiexec -n 32 walksync --dirs /src/tree --dest /dest/tree --dry-run
mpiexec -n 32 walkfind --dirs /glade/campaign/proj --name '*.nc' --mtime-older 5y --output old-nc.list
mpiexec -n 32 walkrm --allowed-root /glade/derecho/scratch/$USER --dirs /glade/derecho/scratch/$USER/old-run --dry-run
```

Things to know before a first real run:

- **Outputs go to the working directory**, and a new run overwrites an earlier run's files of
  the same name. Give each run its own directory.
- **Every tool has `--help`**, which lists exactly the options that tool uses.
- **Try `--dry-run` first** with `walksync` and `walkrm`.
- **On Derecho, pass `--cpu-bind depth --depth 2`** (or 4) to `mpiexec`; see [CPU binding](#cpu-binding).
- **32 ranks per node** is the measured sweet spot for scans of campaign storage: the node's
  filesystem client is the limit, and 96 ranks were slower
  ([docs/phase3.5-results.md](../../docs/phase3.5-results.md)).

## How the walk works

Rank 0 is the **Manager**; every other rank is a **worker**. A worker walks its own stack of
directories depth-first and talks to the Manager only when the stack overflows
(`--local-queue-max`), when it runs dry, or when the Manager asks it to share with idle workers.
So at least 2 ranks are needed, and the Manager does no file I/O.

A worker reads a directory with one `scandir`, which gives names and types without a `stat`. It
walks the subdirectories and cuts the other names into **batches** of `--batch-files`. A batch
is a unit of work like a directory: an idle worker can take one from a busy worker, so a single
directory of a million files is spread over every rank. What happens to a batch is the tool:
`stat` and count, archive, compare and copy, list, or unlink.

The walk ends when the Manager holds nothing and every worker has reported itself idle. The
argument that this cannot end early or hang is the docstring at the top of
[`common/manager.py`](common/manager.py); the design and its alternatives are in
[docs/walk-load-balancing.md](../../docs/walk-load-balancing.md).

After the walk, every tool prints two lines about balance, then its own summary:

```
--> Directories per rank: min 41,022 (rank 7) / mean 43,870 / max 46,115 (rank 19); local queue high-water mark: 812
--> Workers waited for the Manager 0.4% of the walk (31.2 of 7,812.0 rank-seconds)
```

## Common options

Every tool takes these. Sizes are binary whatever the suffix: `10MB` is 10 × 1024².

| option | default | meaning |
|---|---|---|
| `-d`, `--dirs DIR [DIR ...]` | required | The directories to walk (`walksync`: exactly one, the source; `walkrm`: the targets) |
| `-p`, `--progress SECONDS` | 5 | Seconds between progress lines; 0 or less prints none |
| `-v`, `--verbose` | off | Per-rank counts and the Manager's counters |
| `--batch-files N` | 1000 | Names per batch |
| `--batch-size SIZE` | 16GiB | `walktar`, `walksync` only: bytes per batch, the other limit; a bigger file is a batch on its own. See [Sizing the batches](#sizing-the-batches) |
| `--local-queue-max N` | 1000 | Subdirectories a worker keeps; past this it sends the oldest half to the Manager |
| `--dirs-per-dispatch N` | 8 | Directories the Manager hands an idle worker at once |
| `--profile` | off | Every rank writes `profile-r{rank}.prof` (cProfile; read with `python -m pstats`) |

The report options belong to `walkstat`, and to `walksync` when it is asked for a summary:

| option | default | meaning |
|---|---|---|
| `--summary-prefix PREFIX` | none | Names the outputs: `summary-PREFIX`, `userlists/PREFIX/`, the spreadsheet's sheets, and `index-PREFIX-r{rank}.tsv` (also `walkfind`'s index) |
| `-s`, `--spreadsheet FILE` | none | Also write the lists to this `.xlsx` workbook (needs pandas and openpyxl) |
| `--heap-size N` | 500 | Length of the largest-files and largest-directories lists kept, overall and per user |
| `--threshold-size SIZE`, `--threshold-count N` | 10MB, 10000 | What is too small to list: see [walkstat](#walkstat) |
| `--fail-on-errors` | off | `walkstat`, `walktar`: exit 2 if anything could not be read. See [Exit status](#exit-status) |

`walkfind` and `walkrm` still accept the report options, and the other three tools accept
`--fail-on-errors`, but they do nothing there and `--help` leaves them out.

## Filters

`walkstat`, `walktar`, `walksync` and `walkfind` take the same find-like filters. `walkrm`
removes whole trees and refuses them. There are two kinds, and they behave differently:

- **Pruning: `--exclude GLOB`** (repeatable) is tested on an entry's *name*, before any `stat`
  (fnmatch, case-sensitive; `*` matches a leading dot, as in `find`). A match is never stat'ed
  or descended, so nothing below it is seen: `find TREE -name GLOB -prune`. The `--dirs` roots
  are never tested. A GLOB holding `/` is refused.
- **Selecting** filters decide what is counted, archived, copied or listed. They are tested on
  the `stat` already fetched, on every entry, directories included, and never stop the descent:
  a directory that fails is still walked for the entries inside it that pass.

| option | an entry is selected if | `find` |
|---|---|---|
| `--mtime-older WHEN`, `--mtime-newer WHEN` (also `--atime-*`, `--ctime-*`) | its time is before (older) or after (newer) WHEN: an age, `N[smhdwy]` (a bare N is days, y = 365 days), or a local date, `YYYY-MM-DD[THH:MM[:SS]]`. Older and newer together give a window. | `-mtime +N`, `-newermt DATE` |
| `--user U[,U...]`, `--group G[,G...]` (repeatable) | it is owned by one of them; each is a name, or else a numeric id | `-user`, `-group` |
| `--min-size SIZE`, `--max-size SIZE` | its `st_size` is within the bounds | `-size +Nc`, `-size -Nc` |
| `--type T[,T...]` | its type is one of `f d l p s b c` | `-type` |

Different kinds AND together; the values of one kind OR together. Rank 0 resolves everything
once (names to ids, ages and dates to absolute times, globs to one regex) and sends it with the
options. `--min-size` and `--max-size` test `st_size`, the apparent size, as `find` does; the
reports add up `min(st_size, st_blocks × 512)`, which is smaller for sparse or HSM-migrated
files.

## walkstat

```bash
mpiexec -n 32 walkstat --dirs DIR [DIR ...] [--summary-prefix PREFIX] [filters] [--index[=gz]]
```

Counts every entry below the `--dirs` roots and reports who uses what.

**Outputs**, in the working directory, written by rank 0:

- **`summary-PREFIX`** (`summary-output` without `--summary-prefix`), also printed. Plain text:
  `Scanned:`, `Total Count:`, `Total Size:`, `Type Counts:`, a Users and a Groups table, then
  the lists, each up to 250 lines: largest directories (overall, and by last access 3 to 5,
  5 to 7 and over 7 years ago), directories with the most entries, and largest files. An
  example is [tests/golden/summary-golden.txt](tests/golden/summary-golden.txt).
- **`userlists/PREFIX/USER`**, the same lists for one user, for each user above the thresholds.
- **`--spreadsheet FILE.xlsx`**: sheets `PREFIX - Directories`, `PREFIX - Files` and
  `PREFIX - Users & Groups` (and `PREFIX - Filters` for a filtered walk), added to or replaced
  in FILE, so several scans can share one workbook.
- **`--index`**: one line of metadata per entry, per rank. See [Indexes](#indexes).

How to read them:

- **Sizes are `min(st_size, st_blocks × 512)`**: what the entry occupies, so a sparse or
  HSM-migrated file counts what is on disk.
- **A directory's figures are its own contents only**, not recursive. Its date is the most
  recent access time of anything directly in it.
- **The thresholds keep small things out.** A user or group below both `--threshold-size` and
  `--threshold-count` gets no row in the tables and no userlist; a directory below both stays
  out of the per-user and by-age directory lists. On a small test tree the Users table is
  therefore empty: pass `--threshold-size 0`.
- **Errors:** an entry that fails its `stat`, or a directory that cannot be read, counts one
  error and the walk goes on. The summary gains a `Scan Errors:` line.

With filters, every figure covers the selected entries only, so `--user bob`'s totals are bob's
row of the unfiltered Users table. Type Counts then leave out the roots and sum to Total Count,
which is the count of `find TREE -mindepth 1 <tests>`. The summary gains `Filters:`,
`Excluded:` (entries pruned) and `Unmatched:` (entries scanned but not selected) lines, and a
directory that holds no selected entry is left out of the lists.

**`--stat-threads N`** (default 0): each rank runs a batch's `stat` calls N at a time in
threads. A `stat` is a metadata round trip, about 1 ms on an idle campaign GPFS and 6 to 9 ms
under load, and the threads overlap them. They need CPU: see [CPU binding](#cpu-binding).

The column order of the lists (size, items, last access, path) is parsed by
`scans/sort_results.sh`; see [For developers](#for-developers).

## walktar

```bash
mpiexec -n 32 --cpu-bind depth --depth 2 walktar --dirs DIR [DIR ...] [filters] [--index[=gz]]
```

Archives the trees into tar files in the working directory, many at once.

**Outputs:**

- `output-r{rank}-b{n}.tar`: one batch of files, the `n`-th written by `rank`.
- `output-r{rank}-dirs.tar`: every directory `rank` walked, the roots included, with its mode,
  owner and times.
- With `--index`, `<tar>.idx` beside each tar. See [Indexes](#indexes).
- A final `--> Batches:` line with the files, bytes and tars written, and `walkstat`'s summary
  of what was archived.

**Restoring:**

1. Extract the batch tars, in any order or in parallel.
2. Then extract the `-dirs` tars. This sets each directory's mode and times after its files are
   in place; a read-only directory would otherwise refuse files from another tar.

What to expect:

- **Batches.** A file batch closes after `--batch-files` files or `--batch-size` bytes,
  whichever comes first, and becomes one tar. A file bigger than `--batch-size` is a tar on its
  own. The open batch carries over between directories, so small directories combine.
- **Filters.** Every directory walked is archived, the roots included, plus exactly the
  selected non-directories. Nothing below an `--exclude` is archived.
- **Hard links.** Two names of one inode that land in different tars are stored as two full
  copies.
- **`--read-ahead N`** (default 16): reader threads per rank open and read the batch's next
  files while the worker writes the current one. A small file costs one storage round trip,
  about 35 ms on campaign GPFS and on scratch, and the readers overlap them. A reader reads a
  regular file whole up to 16 MiB (so a rank holds at most about 16 × 16 MiB) and only opens a
  bigger one. `--read-ahead 0` reads one file at a time through `tarfile.add()`.
- **Members are `tarfile.add()`'s** (names, modes, owners, times, hard links), whatever
  `--read-ahead` is.
- **A file that shrinks between its `stat` and its read** counts one error and is left out.
- **Access times.** The readers open with `O_NOATIME`, which Linux allows the file's owner and
  root, so a `walktar` run as root leaves atimes as it found them. Run as anyone else it falls
  back to a plain open for files it does not own.
- **Progress** adds `| wrote S (R/s now, A/s avg), batches waiting B`.

### Sizing the batches

The defaults are right for one node on a large tree. Choose `--batch-size` per run when the
tree is small for its rank count.

- **The tail is one batch.** The walk ends one batch-time after the last rank takes its last
  batch, so what matters is batches per worker: about total bytes ÷ batch bytes ÷ workers.
- **Aim for at least about 5 batches per worker**: batch bytes ≈ total bytes ÷ (5 × workers),
  kept within about 1 to 16 GiB. Below about 3 the tail shows: at 8 nodes on a 1.3 TiB tree the
  default gave 1.3 batches per worker and a tail of about 40% of the walk; `--batch-size 2GiB`
  was about 20% faster.
- **Which limit closes a batch depends on the files.** Trees of small files close at
  `--batch-files` first: keep the 1,000-file limit there.
- **Smaller batches mean more tars.** If the tars go somewhere that prefers multi-GiB files,
  that sets the floor.

The walk does not know the tree's size when it starts, so this is a choice per run, not a
computed default. The measurements behind these numbers are in
[docs/walk-changelog.md](../../docs/walk-changelog.md) (perf-1, step B).

## walksync

```bash
mpiexec -n 32 walksync --dirs SRC --dest DEST [--exclude GLOB] [--dry-run]
rsync -aHAX --delete [--exclude GLOB] SRC/ DEST/     # afterwards: the final, authoritative pass
```

Copies every entry of SRC that DEST lacks or has in a different version, with many `rsync`
processes over the ranks. DEST must exist. A rerun with nothing changed copies nothing: it
costs only the walk.

**It does not finish the job alone.** It never deletes, it copies hard links as separate files,
and it leaves directories writable (`u+w`) with the times the copying gave them. A final serial
`rsync -aHAX --delete` settles all three, and has little left to do. The summary's last line
prints that command.

How it decides and copies:

- **Compare.** Each source name is `lstat`'ed, and the destination's when it has one, then
  judged as `rsync`'s quick check does: a regular file is copied if it is missing or differs in
  type, size or whole-second mtime; a symlink if its target differs; a device if its `st_rdev`
  differs; a directory only if missing or of another type. Ownership, modes, ACLs and xattrs
  are not compared.
- **Copy.** What must be copied is collected into copy batches of `--batch-files` entries or
  `--batch-size` bytes, which carry over between directories. Each batch is copied by
  `--rsyncs` (default 8) processes side by side, each over a share of its files, running
  `rsync -aAX --numeric-ids --chmod=Du+w --files-from=- --from0 SRC/ DEST/`. A batch fails as a
  unit if any of its `rsync`s does.
- **Order.** A rank walks before it copies, so every copy is known early and idle ranks can
  take them. Copy batches then go **costliest first**, where a batch's cost is the bytes its
  longest `rsync` stream moves: `max(largest file, batch bytes ÷ --rsyncs)`. A copy that takes
  hours so starts in the run's first minutes instead of becoming its tail. One file is still
  copied by one `rsync`: a 1 TiB file takes about 1.6 h at about 180 MB/s whatever the schedule
  ([docs/walk-tail-4F.md](../../docs/walk-tail-4F.md)).
- **Entries only in the destination** are counted and kept.

Options of its own:

| option | default | meaning |
|---|---|---|
| `--dest DIR` | required | The destination; `SRC/` is copied into it. Neither may contain the other |
| `-n`, `--dry-run` | off | Copy nothing; each rank lists `N path` (new), `C path` (changed) and `X path` (only in the destination) in `walksync-dryrun-r{rank}.txt` |
| `--rsyncs N` | 8 | `rsync` processes per copy batch |
| `--rsync FILE` | chosen | The `rsync` to run. By default the one on `PATH`, unless it lacks ACLs or xattrs and `/usr/bin/rsync` has both; the banner says which and why |
| `--rsync-opts=OPTS` | none | More `rsync` options for every batch, as one string, for example `--rsync-opts=--inplace` |
| `--index[=gz]` | off | A record of what each rank copied. See [Indexes](#indexes) |

What to expect:

- **An `rsync` built without ACLs or xattrs** cannot copy them: `-A` or `-X` is dropped, the
  banner warns, and the final serial `rsync` must use a capable one. This matters where a
  conda `rsync` comes first on `PATH`.
- **Filters.** `--exclude` prunes: never compared, copied or descended. The selecting filters
  choose which files are copied, a partial copy, and the banner says so. Every directory is
  still walked and created. A file that later leaves the selection keeps its old copy in the
  destination.
- **Progress** adds `| copied S (R/s now, A/s avg), batches waiting B`. A batch's bytes count
  when its last `rsync` returns; meanwhile entries of at least 256 MiB count as `rsync` writes
  them, read from the destination file (`--inplace`) or from `rsync`'s temporary file beside
  it. `--rsync-opts` that put the temporary file elsewhere (`-T`, `--partial-dir`,
  `--delay-updates`) turn that off, and the banner says so.
- **Summary:** `--> Sync: N new, C changed, U unchanged; X only in the destination (kept)`,
  then `--> Copied ...` with the entries, bytes, rate, `rsync` batches and failures. No
  `walkstat` summary unless `--summary-prefix` or `--spreadsheet` is given.
- **`rsync` exit 24** (source files vanished) is counted and reported but is not an error.
- **Scaling.** Many small files into one directory are limited by that directory's create
  rate, about 450 to 520 files/s whatever `--rsyncs` is; spread over many directories,
  `--rsyncs 8` was 2.6 times faster than 1. Large files scale with nodes. The numbers are in
  [docs/GLADE_PERFORMANCE.md](../../docs/GLADE_PERFORMANCE.md).

The `sync/` framework runs `walksync` as one of its copiers, with stamps, verification and the
final `rsync` around it: [sync/README.md](../../sync/README.md).

## walkfind

```bash
mpiexec -n 32 walkfind --dirs ROOT [--name GLOB ...] [--prune-matches] [filters] [--print0] [--output FILE]
find ROOT -mindepth 1 \( -name GLOB -o ... \) [-prune] [tests] -print      # the same list
```

Lists the entries that match, like `find`.

| option | default | meaning |
|---|---|---|
| `--name GLOB` | every entry | List entries whose name matches (fnmatch, case-sensitive; repeatable, the globs OR together). Tested before any `stat`, so a non-match costs nothing more |
| `--prune-matches` | off | Do not descend into a directory that matches `--name`: it is listed, its contents are not walked (`find -name GLOB -prune -print`) |
| `--print0` | off | End each path with a NUL and write the name's raw bytes |
| `--output FILE` | per-rank lists | Rank 0 joins the lists into FILE and removes them |
| `--index[=gz]` | off | Metadata for each match, like `find -ls`. See [Indexes](#indexes) |

What to expect:

- **Matching.** `--name` is tested on the name; the selecting filters are then tested on the
  matches' `stat`, ANDed with it. `--exclude` prunes before anything. The `--dirs` roots are
  never tested (`-mindepth 1`). A root that is a symlink to a directory is walked, as
  `rsync SRC/` does. With `--prune-matches`, a matching directory is pruned even when a
  selecting filter keeps it off the list.
- **Lists.** Each worker writes `walkfind-r{rank}.list` in the working directory, one path per
  line as walked, with `\n`, `\t` and `\xNN` escapes for control characters and undecodable
  bytes. `--output FILE` joins them in rank order (through `FILE.tmp`, then a rename); FILE
  exists, empty, when nothing matched. The order is the walk's: sort it if it matters.
- **A run first removes `walkfind-r*.list` from the working directory**, so a rank that matches
  nothing leaves no stale list.
- **Summary.** The first line is the one scripts read:

  ```
  --> Find: walked N entries below the roots (D directories scanned); matched M (f F, d D, l L), S (B bytes)
  ```

  then what was excluded or pruned, what the filters dropped, and where the list is.

`sync/` runs it as its HSM preflight: `--name 'COLD_STORAGE*' --prune-matches`.

## walkrm

```bash
mpiexec -n 32 walkrm --allowed-root ROOT --dirs TARGET [TARGET ...] [--keep-root] [--dry-run]
```

Removes whole trees: files, links and specials are unlinked in batches during the walk, then
the directories, deepest first, after it. **There is no undo.** Run it with `--dry-run` first.

| option | default | meaning |
|---|---|---|
| `--allowed-root DIR` | required | Repeatable. Every target must be strictly below one of these |
| `-n`, `--dry-run` | off | Remove nothing; each rank lists what it would remove in `walkrm-dryrun-r{rank}.txt` |
| `--keep-root` | off | Empty each target but keep the target directory itself |

**Guard rails**, all checked on rank 0 before any rank touches anything; each refusal exits non-zero:

- `--allowed-root` is required, and `/` is refused as one.
- Every target must be strictly below an allowed root after resolving symlinks; a root itself
  is refused.
- A target that is a symlink, `/`, a target given twice, or one inside another is refused.
- Filters are refused: `walkrm` removes everything below its targets.

During the walk:

- **Symlinks are removed, never followed.** A directory is opened with `O_NOFOLLOW`, and an
  unlink batch reopens its directory and checks that it is the one scanned (`st_dev`,
  `st_ino`) before unlinking by name relative to it.
- **No filesystem boundary is crossed.** A subdirectory on another device is an error and is
  left alone.
- **Read-only directories** that the user owns are given `u+rwx` first; one owned by someone
  else is an error and is left.
- **Directories go last**, depth by depth, each rank removing the ones it scanned. Then rank 0
  checks each target: gone, or empty with `--keep-root`.

**Summary:**

```
--> Removed F files, links and specials and D directories (C made u+rwx first); walk W s, directories R s
```

then what vanished, what was on another filesystem, and the errors.

**Not guarded:** a directory higher up the path swapped for a symlink between a scan and the
open of a directory below it. Only the last component is opened with `O_NOFOLLOW`.

Unlinks in one huge directory are limited by that directory, about 6,000 per second on Derecho
scratch whatever the tool ([docs/GLADE_PERFORMANCE.md](../../docs/GLADE_PERFORMANCE.md)).

## Exit status

Under Open MPI, `mpiexec` returns the status below. Under Cray PALS (Derecho), any non-zero
status may be reported as 143 instead, because PALS terminates the remaining ranks once one
exits non-zero. Treat any non-zero status as failure.

| status | meaning |
|---|---|
| 0 | The run completed. For `walkstat` and `walktar` this includes runs that counted errors, unless `--fail-on-errors` was given |
| 1 | `walksync`, `walkfind`, `walkrm`: something failed (a directory that could not be read, an entry that could not be stat'ed, a failed `rsync`, something left behind). Any tool: a directory named on the command line does not exist |
| 2 | A usage error or a refused option, or fewer than 2 ranks. `walkstat`, `walktar` with `--fail-on-errors`: something could not be read |

The two groups differ on purpose. A usage summary with a few unreadable directories is still
useful, and the error count is in it. An incomplete copy, list or removal must not look
finished.

Errors go to stderr as `[rank] LEVEL message`, and every tool ends with
`--> N entries could not be scanned (see stderr)` when there were any.

## Indexes

`--index` makes `walkstat`, `walktar`, `walksync` and `walkfind` also write one line of metadata
per entry, tab-separated, every rank in parallel. It is off by default.

| tool | file | holds |
|---|---|---|
| `walkstat` | `index-r{rank}.tsv` | every entry the rank counted: a metadata dump at walk speed, like a GPFS policy list or `find -ls` |
| `walktar` | `<tar>.idx` beside each tar | every member with its offsets, so one member can be found or extracted without reading the tar |
| `walksync` | `index-r{rank}.tsv` | every entry the rank's `rsync`s copied; the header adds `dest=`, and `dry-run=yes` for a dry run |
| `walkfind` | `index-r{rank}.tsv` | every entry the rank listed |

- With `--summary-prefix` the per-rank name is `index-PREFIX-r{rank}.tsv`.
- **`--index=gz`** writes the same records through gzip, with `.gz` on each name: one stream
  per file at level 6. It was 7 to 12 times smaller and the walk no slower. `zcat` reads it,
  and `cat` of several `.gz` files is still one valid gzip file. A rank killed mid-file leaves
  a stream without its end, which a reader refuses.
- **`walkstat`, `walksync` and `walkfind` use the same file name**, so run them in different
  directories or with different prefixes.
- A rank with nothing to record leaves no file; `walktar` makes each `.idx` with its tar.
- **What is listed** is what the tool counted, archived, copied or listed: an excluded entry
  never appears, and with a selecting filter only the selected entries do.
- **`walksync`'s index is what one run copied**, not a listing of the tree. Unchanged entries
  are not recorded, a failed `rsync` records nothing, and a rerun that copies nothing writes no
  index. With `--dry-run` it is the index the real run would write. For the usage of a tree,
  use `walkstat --index`.

### Format

A versioned header line, then one record per line, the path last so `sort` and `cut` work:

```
# hfu-index v1	tool=walkstat	columns=rec,type,size,alloc,mode,uid,gid,nlink,ino,atime,mtime,ctime,path	root=/glade/campaign/x
P	d	4096	4096	0755	1001	100	3	1234	1700000000	1700000000	1700000000	/glade/campaign/x
E	f	1048576	1048576	0644	1001	100	1	5678	1699990000	1600000000	1600000000	/glade/campaign/x/a.dat
```

| column | |
|---|---|
| `rec` | `E` or `P` (below) |
| `type` | `f d l p s b c` (`?` otherwise) |
| `size`, `alloc` | `st_size`, `st_blocks × 512` |
| `mode` | the permission bits, octal |
| `uid`, `gid`, `nlink`, `ino` | `nlink` and `ino` let an importer find the hard links |
| `atime`, `mtime`, `ctime` | integer epochs |
| `offset`, `offset_data` | `walktar` only: the member's header and data positions in its tar; `-` on `P` records |
| `path` | as walked, encoded reversibly (below) |

- **`E` is an entry**, recorded exactly once across the whole set of files. **`P` is a
  parent**: a structural record, repeated across files as needed, which an importer uses for
  the hierarchy and skips for totals. In `walkstat`'s index the `--dirs` roots and directories
  that fail a selecting filter appear only as `P`; `walktar` archives every directory it
  walks, so there they are `E`. A `P` of a directory that vanished has `-` in every stat field.
- **Parents before children, in every file.** Before any record, every ancestor directory up
  to its root has a record earlier in the *same* file. So each file can be imported on its
  own, in any order or in parallel, and `cat index-*.tsv` keeps the property.
- **Paths** are encoded reversibly: `\` as `\\`, newline, tab and return as `\n` `\t` `\r`,
  other control characters and undecodable bytes as `\xNN`. `index.decode_path()` reverses it
  and `index.read_index()` parses a file.
- **`walktar`'s times are the ones found**, from the `lstat` taken before the member is read.

### Checking and importing

```bash
python -m hpc_file_utils.walk.common.index [--find] index-*.tsv
```

reads plain or gzip files (told apart by their first two bytes), checks each for parents
before children and the `E` paths for repeats across files. With `--find` it also compares the
`E` set with `find` of the header's roots. That comparison is meaningful for `walkstat` and
`walktar`, and for a `walksync` copy into an empty destination; a `walkfind` index or an
incremental `walksync` index lists only part of the tree and will not match.

For an importer:

- **Opening.** A file is plain text or gzip: test the first two bytes for `1f 8b`. Read as
  UTF-8; the paths are escaped, so a strict decode never fails.
- **Detection.** The first line starts `# hfu-index v1`. The names are `index-r*.tsv`,
  `index-*-r*.tsv` and `*.tar.idx`, each with an optional `.gz`.
- **The header** is tab-separated `key=value` fields: `tool=`, `columns=` (the column names,
  in order), one `root=` per `--dirs` entry, and for `walksync` `dest=` and `dry-run=yes`.
  **Map columns by name**, and ignore fields you do not know. Any line starting with `#` is a
  header: `cat` of several files has one per file.
- **The column set is fixed at v1.** A different set would be a new version, so refuse a
  header that is not v1.
- **`P` records stay out of the totals.** A `-` field is unknown.

## CPU binding

The launcher decides how many CPUs a rank may run on, and a rank's threads and child processes
share them: `walktar`'s `--read-ahead` readers, `walkstat`'s `--stat-threads`, `walksync`'s
`rsync`s. On Derecho, PALS binds each rank to **one** CPU by default, and that one CPU was the
ceiling on small files: `walktar`'s readers lost a third of their rate. Any wider binding gives
the whole gain. So launch with:

```bash
mpiexec --cpu-bind depth --depth 2 walktar ...     # two cores per rank
```

- `--depth 2` fits any rank count up to the node's 128 cores and is what the scan scripts use
  for `walkstat`.
- `--depth 4` fits 32 ranks per node exactly and is what `sync/` uses for `walksync` and
  `walkfind`: with 8 `rsync`s per rank it measured faster than a tighter binding.
- `--cpu-bind none` measured the same as a wide binding, within noise.

Every run prints the binding it got, so a number in a log always says what it was measured
under:

```
CPUs per rank: 2 (the launcher's binding; ...)
```

With conda's Open MPI (Casper), the equivalent of no binding is `--bind-to none`.

## Environment variables

| variable | default | effect |
|---|---|---|
| `HFU_EXIT_GRACE` | 2 | Seconds rank 0 waits, after every rank has finalized MPI, before exiting non-zero. Keeps PALS from killing ranks still shutting down |
| `HFU_RECV_MPROBE` | unset | `1` restores mpi4py's matched-probe receives. The default (plain probe) avoids a hang seen on Derecho with `walktar` over two nodes |
| `HFU_INDEX_CACHE_MAX` | 100000 | Directories an index writer remembers as already recorded. Smaller means more repeated `P` lines, never a wrong file |

A rank that seems stuck can be asked where it is: `kill -USR1 <pid>` writes every thread's
Python stack to that rank's stderr, and the rank goes on.

## For developers

### Layout

| | |
|---|---|
| `cli.py` | The console scripts: options on rank 0, the Manager on rank 0, a tool's worker on the others, then the end-of-walk collectives, the reports and the exit status |
| `tools.py` | `TOOLS`, the tools by name |
| `common/tool.py` | `Tool`: what a tool adds (options, checks, banner, a collective phase, report, exit status, worker) |
| `common/manager.py` | The Manager, `BatchQueue`; its docstring is the protocol and the termination argument |
| `common/worker.py` | `BaseWorker`: `entries()`, the local stack, sharing, batches, `in_flight()`; `FileBatch`, `RankFile` |
| `common/protocol.py`, `mpiclass.py`, `progress.py` | Message tags and the progress payload; the end-of-walk reduction; the progress line |
| `common/stats.py`, `heaps.py`, `report.py` | What a rank accumulates and how two ranks' results merge; the balance and per-rank lines |
| `common/options.py`, `filters.py` | The common options and the filters |
| `common/index.py` | `IndexWriter`, the reader and the checker |
| `common/formatting.py`, `log.py` | Number, size and path formatting; diagnostics on stderr |
| `stat/` | `walkstat`: `StatWorker`, `StatBatch`, `report.py` (summary and userlists), `spreadsheet.py` |
| `tar/` | `walktar`: `TarWorker`, a `StatWorker` whose stat batches fill tar batches |
| `sync/` | `walksync`: `SyncWorker`, `CompareBatch`, `CopyBatch`, `compare.py` (the copy decision), `rsync.py`, `report.py` |
| `find/` | `walkfind`: `FindWorker`, `FindBatch`, `report.py` |
| `rm/` | `walkrm`: the guard rails and the rmdir pass in `tool.py`, `RmWorker`, `RmBatch`, `report.py` |

mpi4py is imported when called, not when a module is imported, so everything but the MPI
calls unit-tests without it. MPI is used in `cli.py`, `common/manager.py`, `common/worker.py`,
`common/mpiclass.py`, and `rm/tool.py` (the rmdir pass).

### Adding a tool

A tool is one `<tool>/tool.py` (a `Tool` subclass) and one `<tool>/worker.py` (a `BaseWorker`
subclass with `process_directory()` and `process_batch()`), added to `TOOLS` in `tools.py` and
to `[project.scripts]` in `pyproject.toml`, plus a wrapper in `cli.py`. A tool that needs a
collective phase after the walk does it in `Tool.finish()`, as `walkrm`'s rmdir pass does.

A batch must be a module-level `NamedTuple` (it is pickled in a share), and processing a batch
must create no directories and no batches that outlive the call on another rank: the
termination argument in `manager.py` depends on it.

### Contracts

- **Output on stdout is parsed.** The summary, the userlists, the progress line and the `-->`
  lines are read by `scans/sort_results.sh`, the `sync/` scripts and the tests. Changing them
  is an interface change. The golden files in `tests/golden/` pin `walkstat`'s reports;
  `HFU_UPDATE_GOLDEN=1 pytest -n 0 -k golden` regenerates them.
- **The index format is fixed at v1** (above).
- **The Manager's protocol and termination argument.** A change there needs the race test
  (`tests/test_termination_race.py`; `HFU_RACE_RUNS` sets the runs, default 50).

### Do not "modernize" the hot loops

`entries()`, the `process_batch()` methods and `count()` run once per file, hundreds of
millions of times. In them:

- Keep `str` paths, `os.path` and `os.scandir`. `pathlib` costs several times as much per entry.
- Keep the local-variable caching (`st = self.stats`, `join = os.path.join`).
- Keep the `select is None` and `exclude is None` fast paths: no filter costs one `is None`.
- `FileEntry` and `DirEntry` stay `NamedTuple`s: they go into heaps that compare tuples, and
  they pickle small.

### Tests

```bash
make test         # everything that can run here
make test-pbs     # Derecho: a one-node PBS job running the MPI and site tests
```

Tests marked `mpi` run the tools as `mpiexec -n 4` and are skipped without mpi4py or `mpiexec`
(`HFU_MPIEXEC` overrides the launcher). About half the suite needs no MPI. A few tests assert
that work reached several ranks, which is a timing outcome: on a machine with more test
workers than cores they can fail, and pass when run alone.

### History and measurements

- [docs/walk-changelog.md](../../docs/walk-changelog.md): what changed, sprint by sprint, with
  the measurements each change was judged by.
- [docs/GLADE_PERFORMANCE.md](../../docs/GLADE_PERFORMANCE.md): what the tools measured about
  GLADE itself, by topic.
- [docs/README.md](../../docs/README.md): the index of everything else.
