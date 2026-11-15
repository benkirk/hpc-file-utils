# hpc-file-utils

File tools for NCAR HPC storage (GLADE: Lustre scratch and GPFS campaign storage, on Derecho
and Casper).

| | |
|---|---|
| **[`hpc_file_utils/walk/`](hpc_file_utils/walk/README.md)** | The walk tools: five MPI programs (mpi4py) built on one load-balanced parallel directory walk. `walkstat` summarizes usage, `walktar` archives a tree into many tars, `walksync` copies what is new or changed with batched `rsync`, `walkfind` lists what matches, like `find`, and `walkrm` removes trees, with guard rails. |
| **[`sync/`](sync/README.md)** | A PBS job framework that copies directory pairs: a `walkfind` preflight, a bulk copy by `dsync` or `walksync`, `msrsync`, then a serial `rsync --delete`, with done stamps, verification, HSM `COLD_STORAGE` handling and root hand-off scripts. |
| **`scans/`** | PBS drivers that run `walkstat` over campaign storage and publish `<dir>/.usage/summary.txt` and `.usage/users/`. |
| **`tools/glade_hsm/`, `tools/glade_purge/`** | GLADE HSM and purge helper tools (bash). |
| **[`deploy/`](deploy/README.md)** | The shared install on GLADE: one checkout per commit, an environment per host. |
| **[`docs/`](docs/README.md)** | Performance reference, design notes, the changelog, and history. Start at the map. |

## Try it

On Derecho or Casper, from the shared install (ask csgteam for `<install>`):

```bash
export HFU_ROOT=<install>/current-${NCAR_HOST}
source ${HFU_ROOT}/etc/config_env.sh
```

or from a checkout, which builds the environment the first time (several minutes):

```bash
source etc/config_env.sh
```

Then, inside a PBS job on Derecho (anywhere on a laptop):

```bash
mpiexec -n 32 walkstat --dirs /glade/campaign/proj --summary-prefix PROJ
```

The walk tools need at least 2 MPI ranks: rank 0 coordinates, the others walk. 32 ranks per
node is the measured sweet spot on campaign storage. Every tool has `--help`, and the
[walk README](hpc_file_utils/walk/README.md) is the manual.

## Development

```bash
source etc/config_env.sh   # build (once per change of the env specs) and activate ./conda-env-<host>
make test                  # everything that can run here
make test-pbs              # Derecho: a one-node PBS job running pytest -m "mpi or site"
make help
```

Needs conda and GNU make 4 or later (macOS: `brew install make`; `config_env.sh` uses `gmake`).

- **The environment.** `etc/config_env.sh` builds a content-addressed `conda-env-<hash>` and
  points the per-host link `conda-env-${NCAR_HOST:-local}` at it, so Casper and Derecho can
  share one checkout. It installs this package with `pip install -e ".[mpi,dev]"`, which puts
  `walkstat`, `walktar`, `walksync`, `walkfind` and `walkrm` on `PATH`. `conda-env.yaml` adds
  what pip does not: `rsync`, `shellcheck`, and pandas and openpyxl for `walkstat --spreadsheet`.
- **Which MPI.** `HFU_MPI=auto|host|conda`. `host` (the default on Derecho) loads
  `etc/modules-<host>` and builds mpi4py from source against it. `conda` (the default
  elsewhere, Casper included) uses conda-forge's mpi4py and Open MPI; Casper's host Open MPI
  launcher segfaults now and then, and conda-forge's does not.
- **Tests.** `*/tests/test_*.py` and every `*_test.sh` (exit 77 means skip; a
  `# pytest-mark: site` header marks a test that needs GLADE). The `mpi` marker runs the tools
  as `mpiexec -n 4` and is skipped without MPI, and on Derecho outside a PBS job. `walkstat`'s
  golden output lives in `hpc_file_utils/walk/tests/golden/`;
  `HFU_UPDATE_GOLDEN=1 pytest -n 0 -k golden` regenerates it. The GLADE tools' own `test.sh`
  scripts write to campaign storage and run only with `HFU_SITE_WRITE_TESTS=1`.
- **Python.** 3.10 or later.

## Where things came from

The walk tools were forked from
[benkirk/mpi4py_task_runner](https://github.com/benkirk/mpi4py_task_runner). The rest was
imported from csg-utils on 2026-11-06, with its git history (`git filter-repo`), and has been
developed here since:

| here | was (csg-utils) |
|---|---|
| `hpc_file_utils/walk/` | `filesystem/scans/walkstat/` |
| `scans/` | `filesystem/scans/` (minus `env/`) |
| `sync/` | `filesystem/cgd_migration/` |
| `tools/glade_hsm/` | `filesystem/glade_hsm/` |
| `tools/glade_purge/` | `filesystem/glade_purge/` |

What has changed since is in [docs/walk-changelog.md](docs/walk-changelog.md).
