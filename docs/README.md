# Documentation map

Start with the document that matches what you are doing. "Maintained" documents describe the
code as it is; "record" documents are dated and kept as written.

## Using the tools

| document | read it for |
|---|---|
| [../README.md](../README.md) | What is in the repository, and how to set up an environment |
| [../hpc_file_utils/walk/README.md](../hpc_file_utils/walk/README.md) | **The walk tools' manual**: `walkstat`, `walktar`, `walksync`, `walkfind`, `walkrm`. Quick start, options, outputs, exit statuses, index format |
| [../sync/README.md](../sync/README.md) | The PBS framework that copies directory pairs: the runbook |
| [../deploy/README.md](../deploy/README.md) | The shared install on GLADE: using it, updating it, rolling back |

Each tool's `--help` lists its options with their defaults.

## Understanding performance and design (maintained)

| document | read it for |
|---|---|
| [GLADE_PERFORMANCE.md](GLADE_PERFORMANCE.md) | What the tools measured about GLADE's filesystems, by topic, with rules of thumb and open questions |
| [walk-load-balancing.md](walk-load-balancing.md) | Why the walk balances load the way it does, the alternatives (work stealing), and a design not yet built (splitting the last batch) |
| [HANDOFF-parser.md](HANDOFF-parser.md) | Notes for the index importer in hpc-usage-queries, a separate repository |

## Records (dated, kept as written)

| document | what it records |
|---|---|
| [walk-changelog.md](walk-changelog.md) | Every change to the walk tools, sprint by sprint, with the measurements each was judged by |
| [walk-tail-4F.md](walk-tail-4F.md) | The investigation of walksync's late-walk tail on the 4F copy, its fixes, and the slow verification sample |
| [phase3.5-results.md](phase3.5-results.md) | Measurements of the load balancing at up to 384 ranks; also the description of the production scan configuration |

## History (not maintained)

[history/](history/) holds the sprint plans and hand-off notes the code was built from. They
describe the code and the plans as of their dates, and contradict current behavior in
places. Read them for why something was done, not for how it works now.

| document | what it was |
|---|---|
| [history/HANDOFF.md](history/HANDOFF.md) | The original phased plan (2026-11-06) |
| [history/HANDOFF-phase3.5.md](history/HANDOFF-phase3.5.md) | Finishing phase 3.5, local work queues |
| [history/HANDOFF-sweep.md](history/HANDOFF-sweep.md) | The sweep: refactor, walktar onto stat batches, indexes, logging. Cited by section from GLADE_PERFORMANCE.md |
| [history/HANDOFF-perf1.md](history/HANDOFF-perf1.md) | perf-1: CPU binding, `--stat-threads`, multi-node walktar |
| [history/HANDOFF-indexes2.md](history/HANDOFF-indexes2.md) | indexes-2: `--index=gz`, walksync `--index`, the import contract |
| [history/walksync.md](history/walksync.md) | The walksync prototype's hand-off, from csg-utils |
| [history/sync-REVIEW.2026-10-25.md](history/sync-REVIEW.2026-10-25.md) | A review of the copy pipeline before it was imported here |

## Names used throughout

- **Manager, worker.** Rank 0 and every other rank of a walk tool.
- **Derecho, Casper, GLADE.** NCAR's systems and their filesystems: scratch is Lustre
  (`/glade/derecho/scratch`), campaign is GPFS (`/glade/campaign`).
- **Sprint names** (phase 3.5, the sweep, perf-1, indexes-2, tools-find-rm, tail-fixes) are
  listed in order at the top of [walk-changelog.md](walk-changelog.md).
- **4F, 4D, 3A** and similar are directory pairs of the CGD migration (`sync/pairs.conf`).
