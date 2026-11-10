# Hand-off: the indexes' Part C cleanups, sprint indexes-2 (written 2026-11-10)

Three steps, in order, each its own commit on branch `indexes-2` and its own entry in the
walk README (`hpc_file_utils/walk/README.md`, the "indexes-2" entries), closing the open
questions of `HANDOFF-sweep.md` 4.8:

- A. `--index=gz` on walkstat and walktar.
- B. walksync `--index`, a per-rank record of what it copied.
- C. Close 4.8 (the column set stays fixed at v1) and update the README's import contract.

Bench kit: `/glade/derecho/scratch/benkirk/hfu-bench/` (`benchD.pbs` for step A, benchC's
shape with three variants per round; `mainD/` is the baseline side copy, `main` at
ff27254, with `walkstat-mainD`, `walktar-mainD` and `walksync-mainD`; `mainC/` and
`stepC/` are gone). Rules as the sweep's (`HANDOFF-sweep.md`, "Rules for all of it", plus
the data rules in `GLADE_PERFORMANCE.md` section 5).

## Step A, done (2026-11-10)

- **Code:** `--index` takes an optional value through `common/index.py`'s
  `add_index_argument()`. `--index` alone is `tsv`, the plain file as before, and
  `--index=gz` (or `--index gz`) writes the same records through gzip, `.gz` on each
  name. `IndexWriter(..., how=)` owns the suffix, so walkstat's and walktar's naming did
  not change. The level is 6, gzip's own default: on a synthetic million-tree index,
  level 1 ran 1.9M records/s at 8.7×, 6 ran 640k/s at 11.7×, and 9 ran 154k/s at 12.3×.
  Python's `gzip.open()` would have used 9. `open_index()` opens a file for reading
  through gzip when it starts with gzip's magic, whatever its name, and `read_index()`
  and the checkers go through it.
- **Tests** (13 new): the gzip file decompresses to the plain file's bytes as exactly one
  stream; two `.gz` files concatenated read back as both; a truncated one raises
  `EOFError` at the reader; `--index`, `--index=gz` and `--index gz` parse, and bad values
  exit 2. Under MPI, walkstat and walktar `--index=gz` run on the golden tree and on the
  big directory with `--batch-files 10`, with the default cache and with the cap at 1.
  The checks are closure, exactly once, completeness against `find`, `main --find`, and
  each `.idx.gz` against its tar's members, offsets and bytes.
- **Gates:** `make test` 575 passed, 4 skipped (macOS); `make test-pbs` 367 passed, 2
  skipped (job 8142305; 359 on `main` plus the 8 new MPI tests). CI runs when the branch
  is pushed.
- **Measured** (job 8142389, `hfu-bench/benchD.pbs`; the README entry has the table): the
  gzip index is 7–12× smaller and costs ~3 µs of CPU per record, which no wall time shows.
  Every checker passed on round 1's plain and gzip indexes. A first submission (job
  8142303) passed the checkers unexpanded globs and was stopped after its walkstat rows;
  its tars were deleted.

## Step B, done (2026-11-10)

- **Code** (`sync/worker.py`, `sync/tool.py`, `common/index.py`): `walksync --index`
  writes `index-r{rank}.tsv` (`index-<prefix>-r{rank}.tsv`, `.gz` with `--index=gz`), the
  per-rank naming now in `index.rank_index_name()`, shared with walkstat. The header adds
  `dest=` (encoded as a path) and, for a dry run, `dry-run=yes`, through `IndexWriter`'s
  new `fields`. `read_header()` returns the other fields in `Header.fields`.
- **Decisions:**
  - **`E` is "an rsync copied it"**, from the compare's source lstat, written by the rank
    that ran the rsync after it returned. With `--index`, `SyncWorker.close_batch()` makes
    a `CopyBatch(paths, nbytes, stats)` instead of a `FileBatch`; the stats are
    `IndexStat`s (ten integers, named like `os.stat_result`'s fields), small enough to ride
    in a share reply. A new directory's stat is one `lstat` at the scan, only with
    `--index`; if it fails, the copy goes ahead as without `--index` and the entry is not
    recorded.
  - **The parts of a batch record separately**, as their rsyncs return: exit 0 records the
    part, exit 24 records the entries still in the source (one `lexists` each, only then),
    anything else records nothing. The batch still fails as one unit, as before.
  - **Compared-but-unchanged entries are not recorded.** Three reasons. The index answers
    "what did this run copy", and listing the unchanged would make it a full listing,
    which is walkstat `--index`'s job. `E` must stay "done here, exactly once" across
    every tool. A third record kind would change v1's `rec` column, and with it the parser.
    An unchanged directory still appears as `P` when it is an ancestor of a copied entry.
  - **`--dry-run` writes the index the run would write**, recorded at the compare, with
    `dry-run=yes` in the header. It carries what the `N`/`C` list lacks: the sizes, owners
    and times of the pending copy. Entries only in the destination (`X`) have no source
    stat and are not recorded.
- **Tests** (11 new): in process, the parts of one batch whose rsyncs exit 0, 24 and 23
  (only the first part, and the surviving entry of the second, are recorded; the batch
  fails as a unit); `CopyBatch` carrying stats through a pickle, and `FileBatch` without
  `--index`; a dry run recording at the compare; header fields round-tripping. Under MPI
  (`test_walksync.py`): the `E` set equals rsync's own itemized dry run (`--out-format
  '%i %n'`, its `\#ooo` escapes decoded) on a fresh destination, plain, cap 1, and gzip
  with cap 1, every record's fields equal to the source's lstat, `main --find` passing,
  and a rerun recording nothing. After changes in the destination, the dry run's index and
  the real run's both equal the oracle and each other. A partial copy (`--min-size 2MiB`)
  records the selected files and every new directory. A failing rsync records nothing.
- **Gates:** `make test` 585 passed, 1 failed, 4 skipped (macOS). The failure is
  `test_walkstat_stats_a_big_directory_on_every_rank`, the timing-dependent test the
  sweep's E3/E4 notes name: one rank did all 403 stats (`{1: 0, 2: 0, 3: 403}`). It passes
  alone, and it failed the same way on `main`'s code in one of three full runs today; see
  "Open" below. `make test-pbs` 373 passed, 2 skipped (job on hfu-test after the sync; 367
  after step A plus the 6 new MPI tests). CI runs when the branch is pushed.

## Step C, done (2026-11-10)

- **`HANDOFF-sweep.md` 4.8 closed:** gzip and walksync's index are done (steps A and B),
  and the column set stays fixed at v1 for every tool. The reasons are recorded there:
  gzip answers the size question, every column comes from the one `lstat` already taken,
  one format means one parser, and a change would be `# hfu-index v2`, which a v1 reader
  refuses.
- **The README's import contract** ("Indexes", "Importing") is now a list a parser can be
  written from: opening (plain or gzip by the magic), detection (the first line, or the
  names `index-r*.tsv`, `index-*-r*.tsv` and `*.tar.idx` with an optional `.gz`; the old
  `index-*-r*.tsv` alone missed the unprefixed name), the header (map columns by name,
  ignore unknown fields, a `#` line per file in a `cat`), the records, `P` out of the
  totals, and what each tool's `E` set means. A usage database wants walkstat's indexes;
  walksync's are one run's copies.
- No code beyond `common/index.py`'s docstring, so the gates are step B's.

## Loose ends

- **The quiet-day repeat of perf-1 step A's scratch round, done** (job 8142408,
  `benchbind.pbs`, `ROUND_OFFSET=1`, `TOOL=mainD`, 51 min, nothing left on scratch). The
  `bigdir` controls agree with round 1 (412–432 s against 404–413), the default's
  `manyfiles` reproduced (60.5 s against 62.3), and `depth 4`, `depth 8` and `none` beat
  it again at 50.4 s. `depth 2` did not: 60.5 s against round 1's 39.7, its workers 54%
  busy. It ran first in the job. The walk README's perf-1 step A entry has the table. The
  `--depth 2` recommendation rests on the campaign rows; on scratch's small files a third
  round with `depth 2` not first would settle it.
- **`--batch-size` 1–2 GiB at 8 nodes on VIT: not run; it needs your go-ahead.** It is
  beyond 4 nodes. The estimate: one 8 × 32 job on GDEX VIT → csg, `--read-ahead` 16,
  `--cpu-bind depth --depth 2`, `--batch-size` 1 GiB and 2 GiB, two rounds each, plus one
  default 16 GiB run as the control. That is 5 runs of ~60 s walk and ~75 s wall each,
  about 15 minutes of 8 nodes with the job's start-up, under a 30-minute walltime. Each run
  reads ~1.3 TiB from GDEX and writes it as tars to csg, deleted after each run. Expected:
  the one-batch tail (22–23 s of a 55 s walk) shrinks toward ~6–12 s, ~24 → ~28–30 GiB/s.
  It must not overlap another campaign bench (`-W depend=afterany`).

## Open

- **`test_walkstat_stats_a_big_directory_on_every_rank` failed in every full local run on
  this branch** (four of four, macOS, 12 xdist workers), and in one of three on `main`'s
  code today, the same way each time: one rank stats all 403 entries, so the test's "at
  least two ranks" fails. It passes alone and in `make test-pbs`. Nothing on the branch
  touches walkstat's scheduling without `--index`. It is the flake the sweep's E3/E4 notes
  name, perhaps more likely now, and worth a look on its own: the test asserts a timing
  outcome. A share reply needs the scanning rank to tick while others are idle, and on a
  loaded laptop the scanning rank can finish 40 batches of local stats before an idle
  rank's request arrives.

## Next

- **Push `indexes-2` and open the PR** when you say so; CI has not run on it yet.
- **Then, in hpc-usage-queries:** the hfu-index parser for `fs_scans/parsers`, from the
  walk README's "Indexes" import contract as step C left it.
