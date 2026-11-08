# Load balancing the walk: manager dispatch, libcircle-style work stealing, and what to do next

> Research note for the walkstat/walktar/walksync family (2026-11-07). Nothing here is
> implemented; it records how the alternatives work, what they would cost in mpi4py, and a
> measured path for deciding. The baseline is the manager/worker dispatch in
> `hpc_file_utils/walk/manager.py` and `base_worker.py`.

## TL;DR

- **"Token passing" and "work stealing" are two halves of one design, not two alternatives.**
  In libcircle (the runtime under mpifileutils' dwalk/dcp/dsync) *random work stealing* does the
  load balancing and *token passing* (a Dijkstra ring, or a k-ary tree of allreduces) does
  only the termination detection. The ring is what replaces our Manager's "every worker is
  idle and my queue is empty" test once there is no Manager.
- **The baseline's ceiling is the Manager's message rate, not the walk.** Every directory
  costs the Manager at least three pickled messages (`dir_reply`, `ready`, `execute`), and
  each pass of its loop probes every worker. A single Python rank handles on the order of
  10^4 such messages per second, so a tree of small directories can keep 256 ranks waiting on
  rank 0. That is the limit work stealing removes. Whether we are at it is measurable today
  (section 2.3) and should be measured before anything is built.
- **Recommended order:** (1) instrument the Manager and measure the saturation signal on a real
  campaign tree; (2) if it saturates, add **local queues with Manager-brokered sharing**
  (section 5), which cuts Manager traffic by one to two orders of magnitude while keeping the
  existing race-free termination argument; (3) only if that still saturates at the rank counts
  we care about, go fully decentralised with libcircle-style stealing plus a termination
  token (section 4). A pure-Python libcircle is about 300 lines; the hard part is testing
  termination, not writing it.
- The phase-5 plan (file batches as work units for walktar/walksync) is orthogonal and is
  needed under any of the three schemes.

## 1. The baseline: manager dispatch

### 1.1 Mechanics (as of phase 2)

Rank 0 is the Manager and owns the only work queue, `self.dirs`. Workers (ranks 1..P-1):

1. send `ready [items, bytes]` with `sendrecv` and block until rank 0 answers `execute dir`
   or `terminate`;
2. `os.scandir` + `lstat` every entry of the directory, keeping all per-file accounting local
   (heaps, uid/gid counters, the tar or rsync queue);
3. send discovered subdirectories back as `dir_reply` every `--max-dirs-per-send` (200)
   entries and at the end of the directory, and `progress` every 20,000 entries;
4. go to 1.

The Manager loops over the workers in rank order, `iprobe`s each with `ANY_TAG`, receives at
most one message per worker per pass, and dispatches while it has both a directory and an idle
worker. Termination is "no queued directories and every worker's `ready` is held unanswered",
which is race-free because a worker that has sent `ready` is blocked and the per-source
non-overtaking rule guarantees the Manager has already seen that worker's earlier `dir_reply`s
(see the module docstring in `manager.py`).

Results are not part of the protocol at all: every rank keeps its own counters and heaps and
`MPIClass.summary()` merges them with collectives after the walk. That stays the same under
every alternative below; only the distribution of directories changes.

### 1.2 What is good about it

- Simple to reason about, and after phase 2 it is correct: the termination argument is three
  sentences long and `test_termination_race.py` exercises it 50 of 50 runs.
- The Manager sees everything, so progress reporting, the `maxnumdirs` statistic, and (later)
  priorities such as "hand out file batches before directories" are trivial.
- Dynamic: a slow rank simply asks for less. Nothing depends on tree shape.

### 1.3 Where it stops scaling

The Manager is a single Python process doing per-directory work:

| per directory walked | Manager cost |
|---|---|
| `dir_reply` (pickled list of paths) | 1 iprobe hit + 1 recv + unpickle + `extend` |
| `ready` | 1 iprobe hit + 1 recv |
| `execute` | 1 pickle + 1 send |
| the probes that find nothing | up to P-1 iprobes per pass, whatever the hit rate |

Order-of-magnitude costs in mpi4py (lowercase, pickled, small messages, same node): an
`iprobe` is a few microseconds; a `recv`/`send` of a short pickled list is 10-30 µs. So the
Manager's dispatch capacity is roughly 10^4 directories per second, and the per-pass probe
sweep costs P × (few µs): at 256 ranks about 1 ms per pass, at 2048 ranks about 10 ms, during
which no worker is answered.

Demand is the aggregate directory rate of the workers. A directory of n entries costs a
worker roughly n × (10 to 100 µs) on GPFS when metadata is cached-ish, so:

| typical directory | one worker | 256 workers want | Manager-bound? |
|---|---|---|---|
| 10 entries | ~1,000 dirs/s | ~250,000 dirs/s | yes, badly |
| 100 entries | ~100 dirs/s | ~25,000 dirs/s | yes |
| 1,000 entries | ~10 dirs/s | ~2,500 dirs/s | no |
| 100,000 entries | ~0.1 dirs/s | ~25 dirs/s | no (the opposite problem: phase 5) |

Campaign storage has both regimes (home-like trees of tiny directories, and `cesm/.../hist`
directories with 10^5 files). The table says the Manager is the bottleneck on the first kind
of tree today, at the rank counts the scan scripts already use (`scans/all_cs.sh` runs 8 nodes
× 32 = 256 ranks). It also says the fix is only worth having on that kind of tree.

Two cheap things narrow the gap without changing the design:

- **Probe `ANY_SOURCE`/`ANY_TAG` instead of sweeping the workers.** The non-overtaking rule
  is per (source, communicator) for messages that match the same receive, so a wildcard probe
  still delivers each worker's messages in order and the termination argument is unchanged.
  The pass becomes O(messages) instead of O(P). We lose the "every worker is heard on every
  pass" fairness; a busy worker's progress messages cannot really starve others because
  there are at most a few per 20,000 entries.
- **Hand out k directories per `ready`** (and let workers keep a short local list). This is
  the first step of section 5 and is where most of the win is.

### 1.4 The saturation signal

The Manager already prints `sends`/`recvs`/`dirlen` on the progress line. Two numbers tell
us whether it is the bottleneck, and both are one-line additions to `Manager.run()`:

- **workers waiting on the Manager:** the mean length of `self.idle` sampled when
  `self.dirs` is non-empty (idle workers while there is work = dispatch latency), and
- **Manager occupancy:** the fraction of passes with `heard > 0` (near 1.0 means the loop
  never sleeps, i.e. it is saturated).

Run `walkstat` on a small-directory tree (`/glade/u/home`-shaped) and on a large-file tree
at 32, 96 and 256 ranks and plot items/s against ranks. Flat items/s with occupancy near 1.0
is the Manager ceiling. If items/s still scales at 256 ranks, nothing below is needed yet.

> **Measured (phase 3.5, 2026-11-07;
> [phase3.5-results.md](phase3.5-results.md)).**
> - **items/s is flat, but the Manager is not the cause.** With section 5 in place it is
>   blocked in `recv` 98–100% of the time at 32, 96, 256 and 384 ranks.
> - **The ceiling is the filesystem client on each node.** One node walks faster with 32
>   ranks than with 96. Across 4 nodes, 4 × 32 beats 4 × 96 by 21–44%.
> - **So section 3 (decentralised stealing) is not needed at these scales.**

## 2. libcircle: how it actually works

libcircle (LLNL, `hpc/libcircle`; LaFon, Misra and Bringhurst, "On Distributed File Tree Walk
of Parallel File Systems", SC12) is a distributed queue: every rank has a local LIFO queue of
strings, a user callback pops one item and may push more, and the library moves items between
ranks and decides when everything is done. mpifileutils builds dwalk, dcp, dsync and the rest
on it. The facts below are from the source (`libcircle/token.c`, `worker.c`, `queue.c`,
`lib.c`) and `src/common/mfu_flist_walk.c` in mpifileutils, read 2026-11-07.

### 2.1 Main loop: single-threaded, everything non-blocking

`CIRCLE_work_loop` is one thread per rank, polling with `MPI_Iprobe`/`MPI_Test`. One
iteration, in source order:

1. service incoming work requests (`CIRCLE_workreq_check`);
2. process work receipts and free completed send buffers;
3. check for abort; run a step of the periodic reduction if enabled;
4. if the local queue is empty, request work (`CIRCLE_request_work`);
5. if the queue is non-empty, pop **one** item and call the process callback;
   otherwise run the termination check, and leave the loop on `TERMINATE`.

Two rules in the header comments carry the design: *a process never blocks in a send on the
work-transfer path, because a blocked process stops servicing requests*, and *termination is
only checked while idle*. Steal requests are answered between items, so the latency of a
steal is bounded by the time to process one item. In dwalk an item is one `lstat`, so that is
tens of microseconds. (This matters for us: our item is a whole directory. See 3.2.)

### 2.2 Work stealing

- **Victim:** an idle rank picks a uniformly random other rank (`rand_r(&seed) % size`, retry
  on self) and `MPI_Isend`s a zero-byte `CIRCLE_TAG_WORK_REQUEST`. It keeps exactly one
  request outstanding and polls for the `CIRCLE_TAG_WORK_REPLY`.
- **Victim's answer:** it drains *all* pending requests (`MPI_Iprobe(MPI_ANY_SOURCE, WORK_REQUEST)`
  in a loop) and answers them together. If its queue is empty, it is cleaning up, aborting, or
  has more than `4 × size` sends still in flight, every requester gets a one-int "no work"
  reply and asks someone else. Otherwise:
  - `CIRCLE_SPLIT_EQUAL`: `spread_counts()` divides the queue evenly among the victim and all
    requesters (with r requesters each gets about count/(r+1));
  - `CIRCLE_SPLIT_RANDOM`: a random total in [1, count-1] is sent, spread evenly over the
    requesters.
  mpifileutils uses `CIRCLE_SPLIT_EQUAL | CIRCLE_TERM_TREE`.
- **Transfer:** two non-blocking sends (string offsets, then the character pool) followed by a
  zero-byte `CIRCLE_TAG_WORK_RECEIPT` from the receiver, which the sender uses to count
  `work_outstanding`. The queue is a flat byte pool with an offset table, so a transfer of
  10,000 paths is two messages, not 10,000.

The expected cost of finding work when a fraction f of ranks hold some is 1/f random probes,
each a round trip plus the victim's poll interval. When almost nobody has work (the end of the
walk) idle ranks are mostly sending requests that come back "no work"; libcircle accepts that
because requests are zero-byte and the termination check runs in the same loop.

### 2.3 Termination, ring version (the "token passing")

This is the Dijkstra-Feijen-van Gasteren ring, as implemented in `CIRCLE_check_for_term` and
`CIRCLE_token_recv`:

- Ranks form a ring: `token_src = (rank-1) mod P`, `token_dest = (rank+1) mod P`. Rank 0 holds
  the token initially. Each rank has a colour `token_proc`, initially WHITE.
- **A rank turns itself BLACK when it sends work to a rank the token has already passed**:
  in `CIRCLE_send_work`, `if (dest < rank || dest == token_src) token_proc = BLACK`. That is
  the whole correctness idea: work moving "backwards" around the ring could re-activate a rank
  that already reported itself idle, so the current token round must be invalidated.
- **The token only moves while the holder is idle.** When an idle rank holds the token it
  forwards it: rank 0 always sends WHITE (starting a new round); any other rank sends BLACK if
  it is itself BLACK, else forwards the colour it received, and then resets itself to WHITE.
  The token is sent with `MPI_Issend` so the sender knows the receiver has posted a matching
  receive.
- **Rank 0 receiving a WHITE token terminates** ("Master has detected termination"): a full
  round in which every rank was idle when it forwarded the token and nobody sent work
  backwards. It then sends `TERMINATE` around the ring, and each rank leaves its loop when it
  sees it. Rank 0 receiving BLACK simply starts another round when it is next idle.

Cost: one tiny message per rank per round, O(P) rounds latency at the end, and the token
waits on every busy rank in turn. In a walk this is the dominant end-of-run latency when P
is large and items are slow, which is why libcircle grew the alternative:

### 2.4 Termination, tree version (`CIRCLE_TERM_TREE`, what mpifileutils uses)

A k-ary tree rooted at rank 0 (`tree_width` defaults to 64 in `lib.c`; parent `(rank-1)/k`).
`CIRCLE_check_for_term_allreduce` runs a non-blocking AND-reduction of each rank's `term_flag`
("I have been idle and have sent no work since the last round") up the tree, and the root
broadcasts the result back down. A rank with `work_outstanding > 0` (a transfer not yet
acknowledged by a receipt) does not progress the reduction, and a receipt forces a fresh
round. Latency is O(log_k P) rounds instead of O(P).

The same tree carries the optional **periodic reduction** (`CIRCLE_set_reduce_period`): dwalk
sums `reduce_items` every `--progress N` seconds and logs "Walked N items in T secs". That is
libcircle's equivalent of our `progress` messages, and it is pull-based (the root starts a
round) rather than every rank pushing to rank 0.

### 2.5 What mpifileutils puts in the queue

In the default stat mode, `walk_stat_process_dir` reads a directory and **enqueues every
entry**, files included, as a separate string; whichever rank later pops the entry `lstat`s
it. So a 10^6-file directory becomes 10^6 stealable items, which is how dwalk load-balances a
single huge directory. In `--lite` mode it trusts `d_type`, records entries without a stat,
and enqueues only subdirectories. There is no chunking: the rank that pops a big directory
pushes all of its entries before it services another request (one item per loop iteration,
but the push loop is inside that one item).

### 2.6 What the SC12 paper claims

The abstract reports three fully distributed algorithms, tested on LANL's Cielo (7 PB file
system), that run "orders of magnitude faster than state-of-the-art algorithms while
achieving ideal load balancing and low communication cost", with a randomized work-stealing
scheduler and a hybrid variant among the keywords. The comparison point in that paper is a
centralised master/worker walker like ours, and the paper's reason for leaving it is the one in
section 1.3: the master's message rate. (The full text was not re-read for this note; the
numbers above are from the abstract and the libcircle README.)

## 3. Could we implement libcircle-style stealing in mpi4py?

Yes. Everything libcircle does is point-to-point with `Iprobe`, `Isend` and `Test`, all of
which mpi4py exposes, and the whole runtime is small. The design transfers almost one-to-one;
the Python-specific issues are polling granularity and message cost, both manageable.

### 3.1 Sketch

Every rank runs the same loop; there is no Manager. Keep the current worker body
(`process_directory`) untouched: it still scans one directory and stats its entries inline,
because for walkstat per-entry items would be pure overhead (section 3.3).

```python
# per rank: self.q is a list of directory paths (LIFO), self.sends a list of
# (Request, buf) for in-flight isends, self.pending_request the rank we asked.
def run(self):
    if self.rank == 0: self.q.extend(self.options.dirs)   # or CREATE_GLOBAL-style split
    while True:
        self.service_requests()          # answer WORK_REQUESTs now; never block
        self.reap_sends()                # Request.Test() on self.sends, drop completed
        if not self.q:
            if self.request_work() == 'terminate': break   # steal or token check
            continue
        self.process_directory(self.q.pop())   # pushes subdirs onto self.q

def service_requests(self):
    requesters = []
    st = MPI.Status()
    while self.comm.iprobe(source=MPI.ANY_SOURCE, tag=T_REQ, status=st):
        self.comm.recv(source=st.Get_source(), tag=T_REQ)
        requesters.append(st.Get_source())
    if not requesters: return
    if len(self.q) < 2 or len(self.sends) > 4*self.nranks:
        for r in requesters: self.isend_to(r, [], T_REPLY)          # "no work"
        return
    share = spread_counts(len(self.q), len(requesters)+1)           # equal split
    for r, n in zip(requesters, share[1:]):
        items, self.q = self.q[:n], self.q[n:]          # give the OLDEST (shallowest) dirs
        if r < self.rank or r == self.token_src: self.colour = BLACK
        self.isend_to(r, items, T_REPLY)

def request_work(self):
    if self.pending_request is None:
        self.pending_request = random_other_rank()
        self.isend_to(self.pending_request, None, T_REQ)
    elif self.comm.iprobe(source=self.pending_request, tag=T_REPLY):
        items = self.comm.recv(source=self.pending_request, tag=T_REPLY)
        self.pending_request = None
        self.q.extend(items)                 # [] means "no work"; ask someone else next
    return self.check_token()               # Dijkstra ring, only reached while idle
```

The ring termination is another ~40 lines following section 2.3 literally: `colour`,
`has_token`, `token_colour`, `isend` the token to `(rank+1) % P`, `iprobe` for it from
`(rank-1) % P`, rank 0 starts rounds, rank 0 receiving WHITE sends TERMINATE around. Order of
events inside `check_token` matters (service requests and reap sends *before* forwarding the
token, so a reply you just sent backwards has already turned you BLACK).

`isend_to` pickles once and appends `(req, buf)` to `self.sends`; the buffer must stay
referenced until `req.Test()` is true (mpi4py pickles into a temporary and the send is
non-blocking, so holding the request is enough, but be explicit). Use `comm.isend`, never
`comm.send`, anywhere in the loop: a rank blocked in `send` above the eager limit while its
peer is also blocked in `send` is the deadlock libcircle's comment warns about, and the phase
2 hang was exactly that shape.

### 3.2 Polling granularity: the Python-specific problem

libcircle answers a steal request within one item, i.e. one `lstat`. Our item is a directory,
and a campaign directory can take minutes to scan. A rank inside `os.scandir` of a 10^6-entry
directory would not service requests at all, so every idle rank that picks it as a victim
waits for the whole scan, and the termination token stalls behind it.

The fix is the hook we already have: `process_directory` calls `report_progress()` every
`PROGRESS_INCREMENT` (20,000) entries. Make that hook `service_requests(); reap_sends()`
instead, and lower the interval to about 1,000 entries (10-50 ms of stats). Note what a busy
rank can give away: only the subdirectories it has already pushed, never the remainder of the
directory it is in. That is the same limitation as today and is addressed by phase 5 (file
batches), not by stealing.

The token has the same issue in the other direction: it only moves while a rank is idle, so
the ring's end-of-run latency is the sum of the longest in-progress directory on each rank it
visits. With P = 256 and poll every 1,000 entries that is fine; with P = 2,048 use the tree
variant. In mpi4py the tree is simplest as repeated `comm.Iallreduce` of a one-element int
array (MPI_LAND of `term_flag`): every rank posts one at each poll point, busy ranks with
`term_flag = 0`, and tests it later; a round completes when the slowest rank has polled once.
That is a dozen lines instead of a hand-rolled k-ary tree, at the cost of one collective per
poll interval per rank.

### 3.3 Message cost in Python

- A steal reply of n paths is one pickled list: ~100 bytes per path, so a 5,000-directory
  split is ~500 kB and well above the eager limit, hence `isend`. Pickle/unpickle of that
  list is ~1 ms. Fine.
- "No work" replies and requests are tiny but numerous at the end of the run. Add a short
  back-off after k consecutive "no work" answers (libcircle does not, but it polls at C speed).
- Keep all MPI on the main thread (`MPI_THREAD_FUNNELED` is enough). walktar's tar thread and
  walksync's rsync thread never touch MPI, so nothing changes for them; do not be tempted to
  move the stealing protocol into a communication thread, because cray-mpich with
  `MPICH_MAX_THREAD_SAFETY=multiple` plus the GIL is a worse problem than polling.
- Do **not** copy dwalk's per-entry items for walkstat. Our per-file work is a `lstat` plus
  Python bookkeeping that is already in the scandir loop; shipping each path to another rank
  to do the same `lstat` would add a pickled round trip per file. Per-entry items only make
  sense where per-file work is heavy (tar, rsync), which is phase 5's file batches.

### 3.4 What we would lose relative to the Manager

- **Global knowledge.** Nobody knows `dirlen` or the total queued work; progress needs a
  periodic reduction (libcircle's reduce tree, or a non-blocking `Iallreduce` every N seconds
  initiated at a poll point). The `maxnumdirs` statistic becomes a per-rank maximum.
- **Priorities.** "Batches before directories" (phase 5) becomes a local policy per rank,
  which is fine, but there is no single place to implement "steal from the rank with the
  deepest queue" either; libcircle deliberately steals at random.
- **Debuggability.** The Manager's hang was found by looking at one rank's state. A stuck ring
  needs every rank's colour and token state. Budget for a `--debug-termination` log and a
  simulation test (section 6).
- **Determinism** of which rank walked which directory is already absent; nothing changes.

## 4. Would work stealing help "farther scaling"?

Separate two kinds of scaling.

**More ranks on the same tree (strong scaling).** Here stealing wins exactly when the Manager
saturates, which section 1.3 argues is the small-directory regime at 256 ranks, and probably
everything by 1,000+ ranks. Stealing's own costs are O(1) per rank per steal and O(log P) or
O(P) at termination only, so it has no equivalent ceiling. The SC12 paper's result is that
claim, measured. It will not help on trees whose walk is bound by metadata server throughput
(GPFS with many ranks hitting one fileset), and nothing in the client does.

**Bigger trees at the same rank count.** Stealing does not change per-directory cost and has
nothing to offer; the Manager handles 10^8 directories at 256 ranks already, modulo the
`self.dirs` list on rank 0 growing to the width of the tree (the `maxnumdirs` number). A
local-queue design (section 5) spreads that memory across ranks as a side effect.

**One huge directory.** Neither scheme helps while the unit of work is a directory. dwalk
handles it by making entries the unit of work (2.5); our plan handles it with file batches
(phase 5). Under stealing, a file batch is just another queue item type, and the rank that
created the batch can give it to a thief without a Manager round trip, which is nice but not
the reason to switch.

So: yes for strong scaling on small-directory trees, otherwise no. And there is a cheaper way
to get most of that gain:

## 5. The middle option: local queues with Manager-brokered sharing

> Implemented as phase 3.5 (see `manager.py`'s module docstring for the protocol as built,
> and `tests/test_load_balance.py`). The one detail that differs from the sketch below: the
> worker's `ready` is a separate send and receive loop rather than a `sendrecv`, so a `share`
> that crosses a `ready` is received and ignored by the worker, and the Manager drops the
> request when the `ready` arrives.

Keep the Manager and the termination argument, but stop routing every directory through
rank 0.

- A worker that finds subdirectories **keeps them in a local LIFO** and processes them
  itself (depth-first), instead of sending them back.
- It sends `ready` **only when its local queue is empty**. Everything the phase 2 docstring
  says still holds: a worker that has sent `ready` has no local work and is blocked, so "every
  `ready` held and nothing queued on rank 0" still means nothing is queued anywhere.
- Work reaches rank 0 only to fix imbalance, two ways:
  - **push:** when a worker's local queue exceeds a threshold T (say 1,000) it ships the
    oldest (shallowest) half as a `dir_reply`, exactly as today;
  - **pull:** when the Manager holds idle `ready`s and an empty queue while some workers
    are busy, it sends `share` to the busiest workers (it knows who is busy: anyone whose
    `ready` it does not hold). A worker answers `share` at its next poll point with a
    `dir_reply` of half its queue, or an empty one.
  The pull case is the only new protocol state, and it is safe for termination because a
  worker only sends `ready` once its queue is empty, regardless of outstanding `share`s; the
  Manager simply keeps asking while it has idle ranks and non-idle ranks.
- The Manager hands out **k directories per `ready`** (its queue is now mostly big batches
  from `share` and overflow, so there is no reason to dribble).

Effect: Manager traffic drops from three messages per directory to a few messages per
imbalance event, which is what libcircle's stealing also achieves, except that the broker is
rank 0 instead of a random victim. At 256 ranks this is almost certainly enough; LaFon et al.
list a "hybrid" variant for the same reason. It is maybe 80 lines of change to
`manager.py`/`base_worker.py`, keeps `test_termination_race.py` meaningful, and keeps progress
and `dirlen` global (`dirlen` becomes "on the Manager", plus a per-rank queue length in the
`progress` payload if we want it).

Its limit is the same in kind as the baseline's: a single broker, and one message per
imbalance event per rank. When *that* saturates (a few thousand ranks on a very bushy tree),
the fully decentralised version in section 3 is the next step, and by then the local-queue
code, the poll hook in `process_directory`, and the `isend` bookkeeping are already there.

## 6. Comparison

| | Manager dispatch (today) | + local queues, Manager-brokered (5) | libcircle-style stealing + token (3) |
|---|---|---|---|
| Messages per directory on rank 0 | 3 | ~0 (only on imbalance) | 0 (no rank 0 role) |
| Per-pass cost on rank 0 | O(P) probes | O(P) probes, far fewer passes | none |
| Steal / dispatch latency | one Manager pass | one Manager pass, rarely needed | victim poll interval, 1/f random tries |
| Termination | "all `ready` held" (proven, tested) | unchanged | Dijkstra ring (O(P)) or tree (O(log P)); needs new tests |
| Global progress, `dirlen` | free | free | periodic reduction |
| Huge single directory | needs phase 5 | needs phase 5 | needs phase 5 (dwalk: per-entry items) |
| Memory for the frontier | all on rank 0 | spread | spread |
| New code | 0 | ~80 lines | ~300 lines + simulator |
| Ceiling | Manager msg rate (~10^4 dirs/s) | Manager imbalance rate | metadata server |

## 7. Suggested plan

1. **Measure first (half a day).** Add the two saturation counters from 1.4 to `Manager.run()`
   and run walkstat at 32/96/256 ranks on one small-directory tree and one large-file tree
   on derecho. Decide from the plot.
2. **Baseline cheap wins (one PR).** `ANY_SOURCE` probing and k-per-`ready` dispatch. Both are
   compatible with the current tests.
3. **Local queues + brokered sharing (one PR, after phase 5 or alongside it).** Gate: the
   small-directory tree scales to 256 ranks with Manager occupancy well below 1.0, and the race
   test still passes 50 of 50 with `--max-dirs-per-send 5` and a tiny local threshold.
   *Done as phase 3.5 and measured. The Manager is idle at 384 ranks, and the waiting that
   remains is set by the tree's shape (see section 1.4 and
   [phase3.5-results.md](phase3.5-results.md)).*
4. **Decentralised stealing (only if 3 is not enough; it is, for now).** Prototype as a separate
   `walk/experiments/steal_walk.py` that reuses `process_directory`. Before running it at
   scale, write a **termination simulator**: the same loop driven by a fake `comm` (a dict of
   deques) with randomised interleavings, asserting that TERMINATE is never delivered while any
   queue is non-empty and that it is always delivered eventually. That test is the reason this
   option costs more than its line count suggests.

## References

- libcircle source, read 2026-11-07: `libcircle/token.c` (`CIRCLE_workreq_check`,
  `CIRCLE_send_work_to_many`, `spread_counts`, `CIRCLE_send_work`, `CIRCLE_check_for_term`,
  `CIRCLE_token_recv`, `CIRCLE_check_for_term_allreduce`), `worker.c` (`CIRCLE_work_loop`),
  `queue.c`, `lib.c` (`tree_width = 64`). https://github.com/hpc/libcircle
- mpifileutils `src/common/mfu_flist_walk.c` (`walk_stat_process_dir`,
  `CIRCLE_init(0, NULL, CIRCLE_SPLIT_EQUAL | CIRCLE_TERM_TREE)`, `reduce_*`) and
  `src/dwalk/dwalk.c`. https://github.com/hpc/mpifileutils
- J. LaFon, S. Misra, J. Bringhurst, "On Distributed File Tree Walk of Parallel File Systems",
  SC12. https://sc12.supercomputing.org/schedule/event_detail.php-evid=pap573.html
- E. W. Dijkstra, W. H. J. Feijen, A. J. M. van Gasteren, "Derivation of a termination
  detection algorithm for distributed computations", Information Processing Letters 16 (1983):
  the ring/colour algorithm libcircle implements.
- This repo: `hpc_file_utils/walk/manager.py` (protocol and termination argument),
  `base_worker.py`, `tests/test_termination_race.py`, [HANDOFF.md](HANDOFF.md) phase 5,
  [walksync.md](walksync.md).
