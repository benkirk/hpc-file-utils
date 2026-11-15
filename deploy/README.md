# Deploying on GLADE: the shared install

A shared install of this repo, one checkout per commit, so that production jobs and
everyone else run reviewed code from one place, on Derecho and on Casper. If you only want
to *use* the tools, skip to [Using the install](#using-the-install).
[`deploy/Makefile`](Makefile) manages it. A copy lives in the install directory as
`./Makefile`, and its header is the reference for each target. This file is the runbook.
It follows hpc-scheduling-tools' `deploy/` (the same targets and rules), with one
difference: two hosts.

```
<install>/                    csgteam, mode 755 (set by update)
  Makefile                    a copy of deploy/Makefile
  repo.git                    bare mirror of the GitHub repo, fetched by every update
  <sha12>/                    one commit's checkout (.checkout marks it whole), holding
    conda-env-derecho         derecho's env (cray-mpich), and .deployed-derecho once it passed
    conda-env-casper          casper's env (conda-forge Open MPI), and .deployed-casper once it passed
  current-derecho -> <sha12>  what derecho's jobs and users name
  previous-derecho -> <sha12> what `make rollback` on derecho returns to
  current-casper -> <sha12>   the same for casper
  previous-casper -> <sha12>
```

**Why two envs in one checkout.** Derecho and Casper need different conda envs: Derecho's
builds mpi4py against the host's cray-mpich (`etc/modules-derecho`), and Casper's uses
conda-forge's mpi4py and Open MPI. This repo's own
Makefile already builds them side by side in one checkout, as `conda-env-<hash>` with a
`conda-env-<host>` link, the hash covering the host and its modules. So one checkout per
commit serves both hosts, and each host gets its own stamp and its own links. One host's
update, rollback or failed build never moves the other's `current`. A commit can be
current on Derecho before Casper is updated. `make status` shows which hosts each build
is complete for.

**An env is built where it runs.** `make update` builds this host's env only, from a
login node of that host (`NCAR_HOST` names it). Deploying a commit everywhere is two
updates, one on each host. The checkout is made once, by the first update, and the
second finds it. An update ends by running `--help` of all five tools from the new env,
and marks the build deployed only if they pass.

## Routine use (as csgteam)

```bash
sudo -u csgteam -i
cd <install>
module load conda
make status                  # each host's current/previous, every build, Makefile drift
make -n update               # what an update would run; changes nothing
make update                  # deploy main for this host (REF=<branch|tag|sha> for another)
make rollback                # swap this host's current and previous
make prune                   # keep only builds some host's current or previous names
make self-update             # when status says ./Makefile has drifted
```

Then the same `make update` on a login node of the other host. An update takes several
minutes, almost all of it the conda env (Derecho's also builds mpi4py from source). A
second update of the same commit on the same host does nothing. `make update` refuses
to run inside a checkout, so `make -f deploy/Makefile update` in a repo cannot turn that
repo into a deploy directory.

**One update or prune at a time, from either host.** They take an `flock` on
`$SCRATCH/.hfu-deploy.lock` (`LOCKFILE=`), and a second one waits for the first, up to an
hour (`LOCK_WAIT=` seconds), then carries on. Running the two hosts' updates at once is
therefore safe: the second waits, finds the checkout, and builds only its own env. The
lock is on Derecho's Lustre scratch, not in the install directory, because `flock` on
GPFS is not coherent between Derecho and Casper (Casper mounts GLADE from its own GPFS
cluster). On 2026-11-11 the first deploy's two updates both took a lock in the install
directory and raced in one checkout. Scratch is mounted with `flock` on both hosts, and
a lock held on one is refused on the other.

## Using the install

`make usage` prints the lines. In a shell or a job script:

```bash
export HFU_ROOT=<install>/current-${NCAR_HOST}
source ${HFU_ROOT}/etc/config_env.sh       # loads this host's modules, activates its env
mpiexec ... walkstat --dirs ...
```

The scan scripts (`scans/scan_dir_job_array.sh`, `all_cs.sh`, `test_scan_dir.sh`)
already source `${HFU_ROOT}/etc/config_env.sh` when `HFU_ROOT` is set, so production
scans move to the install by setting `HFU_ROOT` in their job environment.

**`HFU_MPI` must match the build.** `config_env.sh` finds the env by a hash that covers
`HFU_MPI`. update builds with `HFU_MPI=auto`, as `config_env.sh` defaults to: host MPI
(cray-mpich) on Derecho, conda-forge's Open MPI on Casper. Casper's host `mpiexec` (Open MPI
5.0.8's PRRTE) segfaults now and then, under `pytest -n` and in multi-node sync jobs, and
conda-forge's does not, so Casper switched on 2026-11-14. On Casper, launch across nodes with
`mpiexec -x PATH --hostfile $PBS_NODEFILE ...`: the env is active on the first node only.
An env built with another `HFU_MPI` (`make update HFU_MPI=host` on Casper) must be used with
the same `HFU_MPI` set before sourcing. Otherwise `config_env.sh` looks for an env that was
never built, and fails, since users cannot write the install.

## Prerequisites

- **Read access to GitHub.** The repo is public, so the default `REPO` (https) needs no
  key. `REPO=/path/to/a/readable/checkout` deploys from a clone on glade instead.
- **GNU Make 3.82 or later** for this Makefile, and 4.x for the repo's own (the env
  build). Derecho's and Casper's login nodes both have 4.2.1.
- **conda on PATH** (`module load conda`).

## First deploy

1. **Make the directory and copy the Makefile.** The install location is csgteam's
   call. `/glade/u/apps/opt/hpc-file-utils`, next to hpc-scheduling-tools, is what
   `sync/`'s `hfu_root` defaults to.
   ```bash
   mkdir <install> && cd <install>
   git clone -q --depth 1 https://github.com/benkirk/hpc-file-utils.git .bootstrap
   cp .bootstrap/deploy/Makefile Makefile && rm -rf .bootstrap
   ```
2. **Build on Derecho.** On a Derecho login node: `module load conda; make update; make
   status`.
3. **Build on Casper.** On a Casper login node, in the same directory: `module load conda;
   make update; make status`. status should show the build complete for both hosts.
4. **Check as an ordinary user** on each host: `make usage`'s lines, then `walkstat --help`
   and a small walk.
5. **Move production over:** set `HFU_ROOT=<install>/current-${NCAR_HOST}` where the scan
   jobs are submitted.

## Rolling back

| what | how |
|:--|:--|
| a bad deploy on one host | `make rollback` on that host |
| a bad deploy everywhere | `make rollback` on each host |
| production | point `HFU_ROOT` back at the old checkout |

Each host's `previous-<host>` is kept by `make prune`, so a rollback always has its env.

## Who uses the install

- **The `sync/` jobs** run `walkfind`, `walksync` and `walkrm` from it (`hfu_root` in
  `sync/pairs.conf`; [sync/README.md](../sync/README.md)).
- **The scan scripts** use it when `HFU_ROOT` is set where they are submitted; otherwise they
  use the checkout they are in.

## Not here yet

- Nothing runs `make update` on a schedule; a person runs it on each host.
