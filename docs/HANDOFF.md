# Hand-off: phased walkstat development (written 2026-11-06)

The repo was created mechanically: csg-utils history was imported with `git filter-repo` (see
the README's path table) and **no code was changed**. Development happens in five phases, in
order, each closed by an acceptance gate. Do one phase per PR.

**Where things stand:**
- **`hpc_file_utils/walk/`** is the csg-utils walkstat as it was. Flat imports
  (`from mpiclass import ...`), started through the `walkstat`/`walktar` symlinks to `run.py`,
  which dispatches on `argv[0]`. Not a package yet (no `__init__.py`).
- **`scans/*.sh`** still `. env/config_env.sh` and call `./walkstat/walkstat`, so they don't run
  from this layout until phase 3. The old env is the conda env
  `/glade/work/csgteam/csg-utils/filesystem/scans/env/mpi4py-derecho-...`, built from
  `env/conda.yaml`. That directory wasn't imported.
- **`sync/`** (was `cgd_migration`) is self-contained, deployed, and in use for CGD campaign
  copies. It runs from `/glade/derecho/scratch/benkirk/cgd_migration`, a copy of the scripts, not
  this checkout. Don't break its behaviour; its own README is the runbook.
- **The walksync prototype** (sync files via the walk + batched rsync) is **not** in this repo.
  Its latest code is `/glade/derecho/scratch/benkirk/walksync_dev/walkstat/` (`sync_worker.py`
  plus small edits to the shared files), with the smoke test `walksync_smoke.pbs` beside it.
  Design and results: [walksync.md](walksync.md).

---

## Phase 1: test infrastructure
Goal: you can build an environment and run tests on a laptop, in CI and on derecho, and today's
behaviour is pinned by tests before anything changes.

### Environment: follow the `source etc/config_env.sh` pattern
- `etc/config_env.sh`, copied in shape from `project_samuel/devel/etc/config_env.sh`:
  - bash/zsh `SCRIPT_PATH` detection, `ROOT_DIR`, `module try-load conda || true`;
  - conda shell init, then `make --silent -C ${ROOT_DIR} conda-env`, then `conda activate`;
  - drop the `.env` walk (no secrets here).
- `Makefile`, from project_samuel: `ENV_HASH` drives a content-addressed `conda-env-<hash>`
  build and stamp file, with `prune-old-envs`, `print-env-hash`, `bytecode` (shared installs:
  run as the owner after each pull) and `help`.
  - **Hash** `conda-env.yaml`, `pyproject.toml`, `HFU_MPI`, `NCAR_HOST` and the module list.
  - **Per-host symlink:** use `conda-env-${NCAR_HOST:-local}` rather than one `conda-env`.
    casper and derecho share a glade checkout and need different builds.
- **MPI flavour:** `HFU_MPI=auto|host|conda`. `auto` means `host` when `NCAR_HOST` is derecho or
  casper, else `conda`.
  - **host:** load the same modules *before the build and at activation*:
    - derecho: `ncarenv/25.10 gcc/14.3.0 cray-mpich/8.1.32`
    - casper: `ncarenv/25.10 gcc/14.3.0 openmpi/5.0.8`

    These are what `sync/sync_dir_job_array.sh` loads today. Then `export MPICC=$(which mpicc)`
    and `PIP_NO_BINARY=mpi4py pip install -e ".[mpi,dev]"`, which builds mpi4py from source
    against the host MPI (as in `miles-credit/envs/create_env.sh`).
  - **conda:** `conda-env-mpi.yaml` adds conda-forge `mpi4py` + `openmpi` (laptops, CI).
- `conda-env.yaml`: `python`, `pip`, `rsync` (the macOS rsync is too old for the oracle below),
  `shellcheck`, `pipdeptree`; `humanfriendly` for walkstat's sizes, plus `pandas`/`openpyxl`
  only if the spreadsheet output stays.

### Packaging and pytest (house style: hpc-usage-queries)
- `pyproject.toml`:
  - `name = "hpc-file-utils"`, setuptools;
  - extras `mpi = ["mpi4py"]` and `dev = ["pytest>=7", "pytest-cov", "pytest-xdist", "pytest-timeout"]`;
  - `[tool.pytest.ini_options]` with `testpaths` = each `*/tests`, `python_files = ["test_*.py"]`,
    `addopts = "-v --tb=short -n auto --timeout=300"`.
- Root `conftest.py`: copy hpc-usage-queries' `ShellScriptItem`/`ShellScriptFile`, which collect
  every `*_test.sh` as a pytest item. **Extend it:** exit status 77 means `pytest.skip()`, so
  site-only scripts can skip off-site.
- **Markers:**
  - `mpi`: needs `mpiexec`; the tests run the CLIs as `mpiexec -n 4` (`--oversubscribe` for
    openmpi).
  - `site`: needs /glade or PBS.
- **Make targets:** `make test` (whatever can run here) and `make test-pbs` (submits a 1-node
  derecho job running `pytest -m "mpi or site"`; cray-mpich only runs inside a PBS job).

### Tests that pin today's behaviour
- **Fixtures** (`hpc_file_utils/walk/tests/conftest.py`): small trees with odd names (space,
  newline, non-UTF-8 bytes), symlinks (file, dir, dangling), empty dirs, a fifo, hard links, a
  read-only tree (plus ACLs where `setfacl` exists), `COLD_STORAGE*` dirs, and **one directory
  with 2,000 subdirectories**.
- **walkstat golden output** on a fixed tree. Compare with tie tolerance (sorted lines) until
  phase 2 makes it deterministic.
- **The termination race reproducer, marked `xfail`:** walkstat on the 2,000-subdir tree,
  many runs, each with a timeout. See phase 2 for the bug.
- **Shell:**
  - `shellcheck_test.sh`: `shellcheck -x -S warning` over every script.
  - `sync/tests/pairs_test.sh`: config parsing, the safety checks, `stamp_check`, `fresh_after`
    (including a future time, which forces a re-run), `done: yes`, with a stub `qsub`, so it's
    laptop-safe.
  - `tools/glade_*/test.sh` → `tests/*_test.sh`, exiting 77 when /glade is absent.
- **CI:** `.github/workflows/ci.yaml` shaped like `hpc-usage-queries-ci.yaml`: setup-miniconda
  with `conda-env-mpi.yaml`, `pip install -e ".[dev,mpi]"`, `pytest -v`, then the shell tests.

**Gate:** green on the laptop, in CI, and in `make test-pbs` on derecho (the xfail counts as
expected).

---

## Phase 2: baseline bug fixes
Each fix turns a phase-1 test from xfail to pass, or adds a failing test first.
- **Termination race (can hang any run).** In `manager.py`, the `dir_reply` handler sets
  `any_dirs[rank] = False`.
  - Workers send `dir_reply` mid-directory, every `MAXDIRS_BEFORE_SEND = 200` subdirs, so a busy
    rank looks idle.
  - If every other rank is idle at that moment, `finished()` is true and the Manager waits only
    for `ready`; the busy rank's next `send(dirs)` then blocks forever.
  - Seen 2026-11-05: 30 ranks in `MPI_Barrier`, the Manager in `Mprobe`, one rank in `MPI_Send`.
    Found with `gdb -p <pid> -batch -ex bt`; py-spy isn't available.
  - **Fix:** only `ready` marks a rank idle. Also drain stray `dir_reply`/`progress` in the
    termination loop. Make `MAXDIRS_BEFORE_SEND` configurable so the test triggers fast.
- **An error on rank 0 hangs the job.** `parse_options()` runs on rank 0 only and fails with
  `assert`, which leaves the other ranks in `bcast`. Bcast an error status, or `comm.Abort(1)`.
- **Nondeterministic summaries.** Heap ties at the `--heap-size` cut-off keep arbitrary entries.
  Break ties on `(size, path)`.
- **Errors are invisible.** "Cannot scan" is printed but not counted, and the exit status is
  always 0. Count errors, and make an exit-status policy an option.

**Gate:** golden output compared exactly, and the race test passing 50 of 50 runs.

---

## Phase 3: baseline polish
- **Make it a package:** `git mv` into `hpc_file_utils/walk/common/` (the algorithm: protocol and
  tags, `manager.py`, `worker.py` with BaseWorker, `heaps.py`, `progress.py`, the collective
  part of `summary`, `options.py`), `walk/stat/` (StatWorker plus reporting: summary text,
  userlists, spreadsheet) and `walk/tar/`. Use package imports. Keep `git mv` so blame follows.
- **`cli.py` with console scripts** `walkstat` and `walktar` in `pyproject.toml`, replacing the
  `run.py` dispatch on `argv[0]` and its symlinks.
- **Progress protocol:** today the worker → Manager payload `[items, bytes]` is popped and
  asserted in four places. Replace it with one `(items, bytes, extra)` tuple unpacked by
  `Manager.update_progress()`; `extra` is for derived tools. The prototype already has this
  (walksync_dev `manager.py`/`base_worker.py`).
- **Split `MPIClass.summary()`** (300 lines) into collectives and reporting.
- **Import mpi4py lazily**, so pure modules unit-test without MPI.
- **Move `scans/` scripts** to `source <repo>/etc/config_env.sh` and the console scripts.
- **Reporting** today there is some klugdy and crufty reporting at the end of a run with
  aggregation to rank 0.  lets polish the output.  we can change the styling of reported files
  so long as the content is similar.

**Gate:** the published formats (`summary-<prefix>`, `userlists/`, and the copy-out to
`<dir>/.usage/`, which end users read) need not be byte-identical to phase 2 on the golden tree.
(format changes allowed, perhaps rich or other syling incoprorated.)

---

## Phase 3.5: local work queues (done)
Research: [walk-load-balancing.md](walk-load-balancing.md). The Manager's message rate (three
pickled messages per directory plus a probe sweep per pass) was the scaling ceiling on trees of
small directories. Now workers keep the subdirectories they find on a local stack and walk them
depth-first, sending `ready` only when it is empty; the Manager sees directories only as overflow
past `--local-queue-max` or as answers to its `share` requests, which it sends to the busiest
workers whenever it holds idle `ready`s and has nothing to give. The termination argument from
phase 2 is unchanged (ported onto phase 3's blocking ANY_SOURCE Manager). The run reports where
the Manager's directories came from, directories per rank, and the fraction of the walk the
workers spent waiting for it, which is
the signal for whether the next step (fully decentralised stealing, section 3 of the note) is
ever needed. Phase 5's file batches become a second item type on the same local queue.

---

## Phase 4: flitering
Right now everything is all or nothing; consider some standard filtering options
useful for the toolkit (date; owner/group; size; other filters??)


---

## Phase 5: load balancing for tar
Problem: the unit of work is a directory, so one huge directory is one rank's tar (or rsync)
stream for hours while the other ranks idle.
- **File batches become units of work:**
  - while scanning, a worker collects `(path, size)`; past N files or B bytes it sends the batch
    to the Manager (new tag) and starts another;
  - the Manager queues batches ahead of directories and counts outstanding batches for
    termination;
  - a worker given a batch calls the tool's `process_batch()`;
  - small directories still combine on the scanning rank, so tiny dirs don't each become a
    message.
  - maybe lift (`| copied X (Y/s now, Z/s avg), queued Q`) here before phase 6
- **walktar:** one tar per batch, replacing the per-rank `MAXSIZE_BYTES` roll-over.

**Gate:** a tree with one directory of many large files is spread over all ranks (test by
counting each rank's share), and the union of the tars equals the tree.

---

## Phase 6: adding sync
**Status (2026-11-08): implemented, gate passing on laptop/CI-style runs; derecho smoke run and
benchmarks to do.** See [walk/README.md, walksync](../hpc_file_utils/walk/README.md#walksync-phase-6).
Rebuilt on the phase 3–5 walk rather than ported (the prototype's copy thread and back-pressure
are replaced by batches + `share`). Decisions taken along the way:
- the src/dest compare happens in **compare batches** (like walkstat's stat batches), so a huge
  directory's lstats spread over ranks; changed files go into cross-directory **copy batches**;
- `--dry-run` lists to per-rank `walksync-dryrun-r*.txt` (ranks' stdout lines got spliced);
- the selecting filters (time, user, group, type, size) choose which files are copied;
- rsync's exit 24 (vanished source files) is a warning; any other failure exits 1.

Original plan: walksync on top of phases 3 and 4. Start from the prototype (`walksync_dev/walkstat/sync_worker.py`)
and [walksync.md](walksync.md):
- **`walk/sync/compare.py`:** a pure `needs_copy(src_stat, dest_entry)`. Copy when the file is
  missing, a different type, a different size or whole-second mtime, or a symlink with a
  different target.
- **One `scandir` per destination directory**, as the lookup.
- **`process_batch()`** runs
  `rsync -aAX --numeric-ids --chmod=Du+w --files-from=- --from0 SRC/ DEST/`.
  - `Du+w` is required: ranks' batches create the same parent dirs, and read-only sources need it.
  - Deletes, hard links and final dir modes stay with a final serial `rsync -H --delete`.
- **Options:** `--exclude PAT...` (never stat'ed or descended; for `COLD_STORAGE*`), `--dry-run`
  (lists N/C/X), and the live copy rate on the progress line
  (`| copied X (Y/s now, Z/s avg), queued Q`).
- **Gate:** the oracle `rsync -naxAX --delete -i SRC/ DEST/` shows only directory-time lines on
  every fixture tree (add `--chmod=Du+w` or `--exclude` as used), and a rerun copies nothing.
- **Then benchmark** against dsync and msrsync (the table in walksync.md). If it earns its keep,
  offer it to `sync/` as an alternative to the dsync/msrsync stages, keeping the final serial
  rsync.

---

## After the phases: deployment
- **Before deploying, the sweep:** [HANDOFF-sweep.md](HANDOFF-sweep.md) is the retrospective on
  phases 3–6: what to consolidate (one scan, one accounting routine, typed batches, tool classes),
  optional per-rank and per-tar indexes, logging, and the old idioms to retire.
- **Shared install** at `/glade/work/csgteam/hpc-file-utils`: one env per host via
  `etc/config_env.sh`, and `make bytecode` after each pull.
- **Move the scans cron and the `sync/` run dirs** to it.
- **csg-utils:** a PR that replaces the moved directories with a pointer README and a submoule.
  Also delete the `walksync-handoff` branch and the prototype's uncommitted working-tree edits there.

## References: copy these rather than reinventing them
- `~/codes/project_samuel/devel/etc/config_env.sh`, and that repo's `Makefile` (`ENV_HASH`,
  `conda-env`, `$(ENV_STAMP)`, `prune-old-envs`, `bytecode`)
- `~/codes/hpc-usage-queries/devel/`: `etc/config_env.sh`, `pyproject.toml` (pytest config, dev
  extras), `conftest.py` (shell-script items), `.github/workflows/hpc-usage-queries-ci.yaml`
- `~/codes/miles-credit/envs/create_env.sh` (`PIP_NO_BINARY=mpi4py`) and `versions_config.sh`
  (per-host modules)
- `sync/sync_dir_job_array.sh` (module versions; the `mpibind` launch)
- derecho: `/glade/derecho/scratch/benkirk/walksync_dev/`, holding the prototype, `walksync_smoke.pbs`,
  and the 2026-11-05 test output
