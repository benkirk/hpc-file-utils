"""The tools by console-script name (no MPI: a tool imports its worker only when asked)."""

from hpc_file_utils.walk.stat.tool import StatTool
from hpc_file_utils.walk.sync.tool import SyncTool
from hpc_file_utils.walk.tar.tool import TarTool

TOOLS = {tool.name: tool for tool in (StatTool(), TarTool(), SyncTool())}
