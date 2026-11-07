# hpc-file-utils

File tools for NCAR HPC storage:

- **`hpc_file_utils/walk/`**: walkstat and walktar, MPI tree walkers (mpi4py) with a dynamically
  load-balanced manager/worker directory walk. walkstat aggregates usage statistics (summaries,
  per-user lists, spreadsheets); walktar archives a tree into per-rank tar files.
  Origin: forked from [benkirk/mpi4py_task_runner](https://github.com/benkirk/mpi4py_task_runner).
- **`sync/`**: a PBS job-array framework that copies directory pairs (dfind HSM preflight → dsync →
  msrsync → serial `rsync --delete`). It has done stamps, ratcheting resubmission, HSM
  `COLD_STORAGE` handling and root hand-off scripts. See [sync/README.md](sync/README.md).
- **`scans/`**: PBS drivers that run walkstat over campaign storage and publish
  `<dir>/.usage/summary.txt` and `.usage/users/`.
- **`tools/glade_hsm/`, `tools/glade_purge/`**: GLADE HSM and purge helper tools.

## Status

**This is a mechanical import** (2026-11-06). The files are unchanged from csg-utils, and their
git history was carried over with `git filter-repo`:

| here | was (csg-utils) |
|---|---|
| `hpc_file_utils/walk/` | `filesystem/scans/walkstat/` |
| `scans/` | `filesystem/scans/` (minus `env/`) |
| `sync/` | `filesystem/cgd_migration/` |
| `tools/glade_hsm/` | `filesystem/glade_hsm/` |
| `tools/glade_purge/` | `filesystem/glade_purge/` |

The phased plan is in [docs/HANDOFF.md](docs/HANDOFF.md); the walksync (sync-via-walk) design
and prototype results are in [docs/walksync.md](docs/walksync.md); the load-balancing research note
(manager dispatch vs libcircle-style work stealing) is [docs/walk-load-balancing.md](docs/walk-load-balancing.md).

## Development

```bash
source etc/config_env.sh   # build (once per change of the env specs) and activate ./conda-env-<host>
make test                  # everything that can run here
make test-pbs              # derecho: 1-node PBS job running pytest -m "mpi or site"
make help
```

Needs conda and GNU make >= 4 (macOS: `brew install make`; `config_env.sh` uses `gmake`).
`etc/config_env.sh` builds a content-addressed `conda-env-<hash>` and points the per-host link
`conda-env-${NCAR_HOST:-local}` at it, so casper and derecho can share one checkout.
`HFU_MPI=auto|host|conda` picks the MPI: `host` (the default on derecho and casper) loads
`etc/modules-<host>` and builds mpi4py from source against it; `conda` uses conda-forge
mpi4py + openmpi.

Tests: `*/tests/test_*.py` and every `*_test.sh` (exit 77 = skip; a `# pytest-mark: site` header
marks a test that needs GLADE). Markers: `mpi` (runs the CLIs as `mpiexec -n 4`; skipped on
derecho outside PBS) and `site`. The walkstat golden output lives in
`hpc_file_utils/walk/tests/golden/`; `HFU_UPDATE_GOLDEN=1 pytest -n 0 -k golden` regenerates it.
The glade tools' own `test.sh` scripts write to campaign storage and only run with
`HFU_SITE_WRITE_TESTS=1`.

Until phase 3, `scans/*.sh` still expect the old csg-utils layout (`./walkstat/walkstat`,
`env/config_env.sh`), so run them from csg-utils. The `sync/` scripts are self-contained and
work as they are.
