# Deploying on glade: the shared install

A shared install of this repo, one checkout per commit, so that production scans and
everyone else run reviewed code from one place, on derecho and on casper.
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
    conda-env-casper          casper's env (Open MPI), and .deployed-casper once it passed
  current-derecho -> <sha12>  what derecho's jobs and users name
  previous-derecho -> <sha12> what `make rollback` on derecho returns to
  current-casper -> <sha12>   the same for casper
  previous-casper -> <sha12>
```

**Why two envs in one checkout.** derecho and casper need different conda envs: each
builds mpi4py against its own MPI and modules (`etc/modules-<host>`). This repo's own
Makefile already builds them side by side in one checkout, as `conda-env-<hash>` with a
`conda-env-<host>` link, the hash covering the host and its modules. So one checkout per
commit serves both hosts, and each host gets its own stamp and its own links. One host's
update, rollback or failed build never moves the other's `current`. A commit can be
current on derecho before casper is updated. `make status` shows which hosts each build
is complete for.

**An env is built where it runs.** `make update` builds this host's env only, from a
login node of that host (`NCAR_HOST` names it). Deploying a commit everywhere is two
updates, one on each host. The checkout is made once, by the first update, and the
second finds it.

## Routine use (as csgteam)

```sh
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
minutes, almost all of it the conda env (derecho's also builds mpi4py from source). A
second update of the same commit on the same host does nothing. One update or prune
runs at a time in the directory (`.update.lock`), from either host. `make update`
refuses to run inside a checkout, so `make -f deploy/Makefile update` in a repo cannot
turn that repo into a deploy directory.

## Using the install

`make usage` prints the lines. In a shell or a job script:

```sh
export HFU_ROOT=<install>/current-${NCAR_HOST}
source ${HFU_ROOT}/etc/config_env.sh       # loads this host's modules, activates its env
mpiexec ... walkstat --dirs ...
```

The scan scripts (`scans/scan_dir_job_array.sh`, `all_cs.sh`, `test_scan_dir.sh`)
already source `${HFU_ROOT}/etc/config_env.sh` when `HFU_ROOT` is set, so production
scans move to the install by setting `HFU_ROOT` in their job environment.

**`HFU_MPI` must match the build.** `config_env.sh` finds the env by a hash that covers
`HFU_MPI`. update builds with `HFU_MPI=auto`, host MPI on both hosts, as `config_env.sh`
defaults to. A casper build with conda-forge's Open MPI (`make update HFU_MPI=conda`,
which the test suite prefers there: the host `mpiexec` segfaults now and then under
`pytest -n`) must then be used with `HFU_MPI=conda` set before sourcing. Otherwise
`config_env.sh` looks for an env that was never built, and fails, since users cannot
write the install.

## Prerequisites

- **Read access to GitHub.** The repo is public, so the default `REPO` (https) needs no
  key. `REPO=/path/to/a/readable/checkout` deploys from a clone on glade instead.
- **GNU Make 3.82 or later** for this Makefile, and 4.x for the repo's own (the env
  build). derecho has 4.2.1. casper's login nodes need the same check: run `make
  --version` there before the first update.
- **conda on PATH** (`module load conda`).

## First deploy

1. **Make the directory and copy the Makefile.** The install location is csgteam's
   call; `/glade/u/apps/opt/hpc-file-utils` would sit next to hpc-scheduling-tools.
   ```sh
   mkdir <install> && cd <install>
   git clone -q --depth 1 https://github.com/benkirk/hpc-file-utils.git .bootstrap
   cp .bootstrap/deploy/Makefile Makefile && rm -rf .bootstrap
   ```
2. **Build on derecho.** On a derecho login node: `module load conda; make update; make
   status`.
3. **Build on casper.** On a casper login node, in the same directory: `module load conda;
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

## Not here yet

- No production job uses the install yet: that is the scan scripts' `HFU_ROOT`, set where
  they are submitted.
- Nothing runs `make update` on a schedule; a person runs it on each host.
