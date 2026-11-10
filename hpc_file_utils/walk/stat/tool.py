"""walkstat: the walk's accounting, its summary and userlists (common/tool.py)."""

from hpc_file_utils.walk.common.tool import Tool


class StatTool(Tool):

    name = 'walkstat'
    description = 'Walk directory trees in parallel (MPI) and summarize their usage.'

    def add_arguments(self, parser):
        parser.add_argument('--stat-threads', default=0, type=int, required=False, help='Threads each rank stats a batch\'s names in, that many stats in flight at once: a stat is one metadata round trip, ~1 ms on an idle campaign GPFS and 6-9 ms under load, and the threads overlap them (default: 0, one stat at a time)')
        parser.add_argument('--index', action='store_true', help='Every rank also writes an index of the entries it counted, one line of metadata each (type, size, allocation, mode, owner, times, inode, path), to index-r{rank}.tsv (index-<prefix>-r{rank}.tsv with --summary-prefix) in the working directory; see the walk README, "Indexes"')

    def check(self, parser, args):
        if args.stat_threads < 0:
            parser.error('--stat-threads must be at least 0')

    def make_worker(self):
        from hpc_file_utils.walk.stat.worker import StatWorker
        return StatWorker()
