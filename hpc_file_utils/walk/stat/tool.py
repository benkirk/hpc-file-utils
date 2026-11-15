"""walkstat: the walk's accounting, its summary and userlists (common/tool.py)."""

from hpc_file_utils.walk.common.index import add_index_argument
from hpc_file_utils.walk.common.tool import Tool


class StatTool(Tool):

    name = 'walkstat'
    description = ('Walk directory trees in parallel (MPI) and summarize their usage: totals, users, groups, '
                   'and the largest files and directories, overall and per user.')
    examples = ('mpiexec -n 32 walkstat --summary-prefix PROJ --dirs /glade/campaign/proj',
                'mpiexec -n 32 walkstat --dirs /scratch/a /scratch/b --atime-older 2y --index=gz')
    batch_help = 'the names of a big directory are stat\'ed a batch at a time'

    def add_arguments(self, parser):
        parser.add_argument('--stat-threads', default=0, type=int, metavar='N', help='Threads each rank stats a batch\'s names in, that many stats in flight at once: a stat is one metadata round trip, ~1 ms on an idle campaign GPFS and 6-9 ms under load, and the threads overlap them; 0 stats one name at a time (default: 0)')
        add_index_argument(parser, 'Every rank also writes an index of the entries it counted, one line of metadata each (type, size, allocation, mode, owner, times, inode, path), to index-r{rank}.tsv (index-<prefix>-r{rank}.tsv with --summary-prefix) in the working directory; see the walk README, "Indexes"')

    def check(self, parser, args):
        if args.stat_threads < 0:
            parser.error('--stat-threads must be at least 0')

    def make_worker(self):
        from hpc_file_utils.walk.stat.worker import StatWorker
        return StatWorker()
