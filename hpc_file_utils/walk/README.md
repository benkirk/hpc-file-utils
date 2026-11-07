Forked from https://github.com/benkirk/mpi4py_task_runner

## Layout

| | |
|---|---|
| `cli.py` | the `walkstat` and `walktar` console scripts: options on rank 0, Manager on rank 0, a tool's worker on the others, then the collectives and the reports |
| `common/` | the walk itself, shared by every tool: `manager.py` (its docstring is the protocol and the termination rule), `worker.py` (BaseWorker), `protocol.py` (tags, the progress payload), `mpiclass.py` (the end-of-walk collectives), `stats.py` (what a rank accumulates, and how two ranks' combine), `heaps.py`, `progress.py`, `formatting.py`, `options.py` |
| `stat/` | walkstat: `worker.py` (StatWorker), `report.py` (summary and userlists), `spreadsheet.py` |
| `tar/` | walktar: `worker.py` (TarWorker) |

Only `manager.py`, `worker.py`, `mpiclass.py` and `cli.py` touch MPI, and they import mpi4py when
called rather than when imported, so the rest unit-tests without it.

Outputs (in the working directory, written by rank 0):
- `summary-<prefix>` (`summary-output` without `--summary-prefix`), also echoed to stdout. The
  scans publish it as `<dir>/.usage/summary.txt`.
- `userlists/<prefix>/<user>`, one per user above `--threshold-size`/`--threshold-count`,
  published as `<dir>/.usage/users/`.
- `--spreadsheet FILE.xlsx`: three sheets per prefix, added to (or replaced in) FILE.

## Behaviour changes since the csg-utils import

- **Termination is race-free.** The Manager now treats a worker as idle only while it holds
  that worker's unanswered `ready`, and receives each worker's messages in order (see the
  docstring at the top of `manager.py`). Before, a walk could
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
- **`--max-dirs-per-send N`** [200]: subdirectories a worker collects before sending them to
  the Manager.

### Phase 3

- **A package with console scripts.** `walkstat`/`walktar` replace the symlinks to `run.py` and
  its dispatch on `argv[0]`.
- **Progress payload.** Every worker message carries one `(items, bytes, extra)` tuple
  (`protocol.py`); `extra` is for derived tools' own counters. `dir_reply` is `(dirs, payload)`.
- **Collectives.** The ranks' stats combine in one tree reduction (`comm.reduce` with
  `WalkStats.merged`) onto rank 0. Before, each list, and each list of each user, was its own
  gather-and-broadcast to every rank. rank 0 warns if the reduced totals differ from what its
  progress payloads said.
- **Reports.**
  - Column headers, as `#` comment lines.
  - `-`, not `1969-12-31`, for a directory with no files.
  - Aligned user and group tables.
  - A `Scanned:` line.
  - Userlists start with `User:`, `Total Count:` and `Total Size:` lines.
  - Columns (size, items, last access, path) and section names are as before.
- **Paths are printable.** In every report and the spreadsheet, undecodable bytes in a name
  show as `\xNN` and control characters as `\n`, `\t`, `\r`, `\xNN`, so each entry is one line
  of valid UTF-8. Before, a non-UTF-8 name crashed the summary writer, and a newline split an
  entry over two lines.
- **Per-user lists use `--heap-size`.** Before they were 200 entries, whatever the option said.
  Every printed list is capped at 250 entries.
- **Removed `-e/--errors`.** It was never implemented; the scans redirect stderr.
- **No abbreviated options.** `--summary x.xlsx` used to be taken as `--summary-prefix x.xlsx`,
  so `all_cs.sh` and `test_scan_dir.sh` never wrote their spreadsheet; they now pass
  `--spreadsheet`.
- **One blocking receive per message.** The Manager receives with ANY_SOURCE + ANY_TAG
  instead of probing each worker in turn (phase 2's loop). The termination rule is unchanged:
  every message matches the one receive, so each worker's messages still arrive in order.
  Benchmarked on derecho against the deployed scanner, with the CGD-ppc (3.6M items) and
  CMIP6 (4.5M items) filesets:
  - **1 node, 96 ranks:** phase 2's probing loop was 5–8% faster than the deployed scanner.
  - **4 nodes, 384 ranks:** phase 2's loop was 17% slower. Its pass over 383 workers took
    2.6–44 ms, and the Manager almost never slept. The blocking receive keeps the Manager
    blocked in `recv` 98% of the time, at 7–9 µs per message. It was 5–7% faster than the
    probing loop. Against the deployed scanner it was 9% faster on CGD-ppc and 10% slower
    on CMIP6.
  - **Collectives and reports:** about 3 s, against 12–20 s for the deployed scanner.
- **`--verbose`** adds a per-rank table (rank, host, dirs, files, size) and the Manager's
  counters:
  - messages received, and the handling time per message
  - time blocked in `recv`
  - how long each `ready` was held before its `execute`, mean and longest (this includes
    waiting for work to arrive)
- **walktar closes its last tar file.** Its end-of-archive blocks were left to interpreter
  shutdown.
- Dropped the oldest-mtime/atime heaps, which were collected but never reported.
