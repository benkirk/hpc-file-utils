"""walktar: one tar per batch of files, plus one per rank for the directories (common/tool.py)."""

from hpc_file_utils.walk.common.index import add_index_argument
from hpc_file_utils.walk.common.tool import Tool


class TarTool(Tool):

    name = 'walktar'
    description = ('Walk directory trees in parallel (MPI) and archive them: one tar file per batch '
                   'of files, plus one per rank for the directories.')
    examples = ('mpiexec -n 32 --cpu-bind depth --depth 2 walktar --dirs /glade/campaign/proj/run1',
                'mpiexec -n 32 --cpu-bind depth --depth 2 walktar --dirs run1 --index --batch-size 4GiB')
    writes = ('wrote', 'archives')
    batches_bytes = True
    batch_help = 'stat\'ed a batch at a time, and each batch of files is written as one tar'
    selects = 'archived'

    def add_arguments(self, parser):
        parser.add_argument('--read-ahead', default=16, type=int, metavar='N', help='Files each rank opens and reads (whole, up to 16 MiB) ahead of the one it is archiving, in as many reader threads: a small file costs one storage round trip, ~35 ms on campaign GPFS and scratch, and these overlap them; 0 reads one file at a time. The threads need CPU: under PALS\'s default binding of one CPU per rank they lose a third of the rate on small files, so launch with mpiexec --cpu-bind depth --depth 2 (the walk README, "CPU binding") (default: 16)')
        add_index_argument(parser, 'Write an index beside each tar, <tar>.idx: one line per member with its metadata (type, size, allocation, mode, owner, the times as found before the read, inode) and its offsets in the tar, so a member can be found or extracted without reading the whole tar; see the walk README, "Indexes"')

    def check(self, parser, args):
        if args.read_ahead < 0:
            parser.error('--read-ahead must be at least 0')

    def make_worker(self):
        from hpc_file_utils.walk.tar.worker import TarWorker
        return TarWorker()
