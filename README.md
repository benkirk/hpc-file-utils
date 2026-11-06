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

Packaging, the `etc/config_env.sh` environment, tests and CI don't exist yet. The phased plan
is in [docs/HANDOFF.md](docs/HANDOFF.md); the walksync (sync-via-walk) design and prototype
results are in [docs/walksync.md](docs/walksync.md).

Until phase 3, `scans/*.sh` still expect the old csg-utils layout (`./walkstat/walkstat`,
`env/config_env.sh`), so run them from csg-utils. The `sync/` scripts are self-contained and
work as they are.
