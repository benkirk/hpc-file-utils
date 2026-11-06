"""The Manager termination race (HANDOFF phase 2), fixed in manager.py.

Before the fix, the Manager marked a rank idle when it received a ``dir_reply``, but
workers send those mid-directory every --max-dirs-per-send (200) subdirectories. If
every other rank was idle at that moment the Manager decided it was finished and only
waited for ``ready``; the busy rank's next dir_reply was never received. Above the
eager limit its send blocked forever (the hang seen on derecho, 2026-11-05); below
it the directories were silently dropped. On the wide tree this hung 10 of 10 runs.

Each run is bounded by a timeout and must count every entry. HFU_RACE_RUNS sets the
number of runs per --max-dirs-per-send value (default 50: the phase 2 gate).
"""

import os

import pytest

RUNS = int(os.environ.get("HFU_RACE_RUNS", "50"))
RUN_TIMEOUT = int(os.environ.get("HFU_RACE_TIMEOUT", "60"))


@pytest.mark.mpi
@pytest.mark.timeout(RUN_TIMEOUT + 60)
@pytest.mark.parametrize("max_dirs_per_send", [200, 5])
@pytest.mark.parametrize("run", range(RUNS))
def test_wide_directory_terminates_and_counts_everything(walk, wide_tree, tmp_path, run, max_dirs_per_send):
    r = walk.run("walkstat", [wide_tree.root], cwd=tmp_path, timeout=RUN_TIMEOUT,
                 args=["--max-dirs-per-send", max_dirs_per_send])
    assert not r.timed_out, f"run {run} hung:\n{r.report()}"
    assert r.returncode == 0, r.report()
    assert r.total_count() == wide_tree.nitems, f"run {run} lost entries:\n{r.report()}"
