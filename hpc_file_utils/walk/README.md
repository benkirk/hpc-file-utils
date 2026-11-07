Forked from https://github.com/benkirk/mpi4py_task_runner

## Behaviour changes since the csg-utils import

- **Termination is race-free.** The Manager now treats a worker as idle only while it holds
  that worker's unanswered `ready`, receives each worker's messages in order, and polls every
  worker on every pass (see the docstring at the top of `manager.py`). Before, a walk could
  hang (a rank stuck in `MPI_Send`) or silently drop directories.
- **Errors end every rank.** A bad option or directory on rank 0 exits all ranks with rank 0's
  status (1 for a missing directory, 2 for a usage error) instead of hanging them. (Open MPI's
  mpiexec returns that status; Cray PALS's returns 143, as it SIGTERMs the remaining ranks
  once the first exits non-zero.) Any other
  uncaught exception, on any rank, calls `MPI_Abort`.
- **Errors are counted.** Directories that cannot be scanned are summed over all ranks. When
  there are any, the summary gets a `Scan Errors: N entries could not be scanned` line and
  stdout gets a final `--> N entries could not be scanned` line. `--fail-on-errors` makes the
  exit status 2 in that case (non-zero, 143, under PALS); the default stays 0.
- **Deterministic ordering.** Type, user and group lists break ties by name, so the summary no
  longer depends on which rank saw what first. (The size heaps already broke ties by path.)
- **Workers walk their own queue (phase 3.5).** A worker pushes the subdirectories it finds
  onto a local stack and walks them itself, depth-first, and asks the Manager for work only
  when that stack is empty. Directories reach the Manager only to fix imbalance: as
  *overflow* (the oldest half of a stack that grew past `--local-queue-max`) or as the
  answer to a *share* request, which the Manager sends to the busiest workers whenever it
  has idle workers and nothing to give them. The termination argument is unchanged (see
  `manager.py`); the design and its alternatives are in
  [docs/walk-load-balancing.md](../../docs/walk-load-balancing.md).
  - **`--local-queue-max N`** [1000]: subdirectories a worker keeps before it ships the
    oldest half to the Manager (replaces `--max-dirs-per-send`).
  - **`--dirs-per-dispatch K`** [8]: directories the Manager hands an idle worker at once.
  - The progress line shows `queued` (directories on the workers' local queues), `idle`
    and `shares`; the end of the run reports where the Manager's directories came from,
    its occupancy, the directories walked per rank and the fraction of the walk the workers
    spent waiting for it. Occupancy near 100% with a large waiting fraction means the
    Manager is the bottleneck.
