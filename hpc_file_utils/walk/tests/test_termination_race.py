"""The Manager termination race (HANDOFF phase 2).

manager.py marks a rank idle when it receives a ``dir_reply``, but workers send those
mid-directory every MAXDIRS_BEFORE_SEND (200) subdirectories. If every other rank is
idle at that moment the Manager decides it is finished and only waits for ``ready``.
The busy rank's next dir_reply is then never received: above the eager limit its send
blocks forever (the hang seen on derecho, 2026-11-05); below it the directories are
silently dropped and the counts come up short.

Each run is bounded by a timeout. HFU_RACE_RUNS sets the number of runs.
"""

import os

import pytest

RUNS = int(os.environ.get("HFU_RACE_RUNS", "10"))
RUN_TIMEOUT = int(os.environ.get("HFU_RACE_TIMEOUT", "30"))


@pytest.mark.mpi
@pytest.mark.timeout(RUNS * (RUN_TIMEOUT + 30))
@pytest.mark.xfail(reason="phase 2: dir_reply marks a busy rank idle (termination race)", strict=False)
def test_wide_directory_terminates_and_counts_everything(walk, wide_tree, tmp_path):
    for i in range(RUNS):
        r = walk.run("walkstat", [wide_tree.root], cwd=tmp_path / f"run{i}", timeout=RUN_TIMEOUT)
        assert not r.timed_out, f"run {i + 1}/{RUNS} hung:\n{r.report()}"
        assert r.returncode == 0, r.report()
        assert r.total_count() == wide_tree.nitems, f"run {i + 1}/{RUNS} lost directories:\n{r.report()}"
