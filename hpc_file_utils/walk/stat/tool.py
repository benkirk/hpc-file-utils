"""walkstat: the walk's accounting, its summary and userlists (common/tool.py)."""

from hpc_file_utils.walk.common.tool import Tool


class StatTool(Tool):

    name = 'walkstat'
    description = 'Walk directory trees in parallel (MPI) and summarize their usage.'

    def make_worker(self):
        from hpc_file_utils.walk.stat.worker import StatWorker
        return StatWorker()
