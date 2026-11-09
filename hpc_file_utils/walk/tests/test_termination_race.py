"""The Manager termination race (HANDOFF phase 2), fixed in manager.py.

Before the fix, the Manager marked a rank idle when it received a ``dir_reply``, but
workers send those mid-directory every 200 subdirectories (then ``--max-dirs-per-send``). If
every other rank was idle at that moment the Manager decided it was finished and only
waited for ``ready``; the busy rank's next dir_reply was never received. Above the
eager limit its send blocked forever (the hang seen on derecho, 2026-11-05); below
it the directories were silently dropped. On the wide tree this hung 10 of 10 runs.

Each run is bounded by a timeout and must count every entry. HFU_RACE_RUNS sets the
number of runs per --local-queue-max value (default 50: the phase 2 gate).

Since phase 3.5 the workers keep a local queue and ship its oldest half as overflow
past --local-queue-max, so 2 is the equivalent stress: a dir_reply every other
subdirectory, while the Manager also asks the busy rank to share with the idle ones.

Phase 5 adds two batch variants: walkstat with tiny stat batches (lqm2-statbatch20; a
lost batch there loses its entries from the Total Count), and walktar with tiny batches, so the busy rank's share
replies carry batches as well as directories. A lost batch would not show in the Total
Count (the scan counted it), only in the tars, so it checks that their union is the tree.
Phase 5.5 runs it with the reader threads (--read-ahead, default 16) and without (0).
"""

import os

import pytest

RUNS = int(os.environ.get("HFU_RACE_RUNS", "50"))
RUN_TIMEOUT = int(os.environ.get("HFU_RACE_TIMEOUT", "60"))


@pytest.mark.mpi
@pytest.mark.timeout(RUN_TIMEOUT + 60)
@pytest.mark.parametrize("args", [["--local-queue-max", 1000], ["--local-queue-max", 2],
                                  ["--local-queue-max", 2, "--batch-files", 20]],
                         ids=["lqm1000", "lqm2", "lqm2-statbatch20"])
@pytest.mark.parametrize("run", range(RUNS))
def test_wide_directory_terminates_and_counts_everything(walk, wide_tree, tmp_path, run, args):
    r = walk.run("walkstat", [wide_tree.root], cwd=tmp_path, timeout=RUN_TIMEOUT, args=args)
    assert not r.timed_out, f"run {run} hung:\n{r.report()}"
    assert r.returncode == 0, r.report()
    assert r.total_count() == wide_tree.nitems, f"run {run} lost entries:\n{r.report()}"


@pytest.mark.mpi
@pytest.mark.timeout(RUN_TIMEOUT + 60)
@pytest.mark.parametrize("read_ahead", ["16", "0"], ids=["readahead16", "readahead0"])
@pytest.mark.parametrize("run", range(RUNS))
def test_batch_storm_terminates_and_archives_everything(walk, wide_tree, tmp_path, run, read_ahead):
    r = walk.run("walktar", [wide_tree.root], cwd=tmp_path, timeout=RUN_TIMEOUT,
                 args=["--batch-files", "20", "--local-queue-max", "2", "--read-ahead", read_ahead])
    assert not r.timed_out, f"run {run} hung:\n{r.report()}"
    assert r.returncode == 0, r.report()
    assert r.total_count() == wide_tree.nitems, f"run {run} lost entries:\n{r.report()}"
    names = walk.tar_names(tmp_path)
    assert len(names) == len(set(names)), f"run {run} archived an entry twice"
    assert set(names) == walk.tree_names(wide_tree.root), f"run {run} lost a batch:\n{r.report()}"
