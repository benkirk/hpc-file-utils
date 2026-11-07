"""MPI state shared by the Manager and the workers, and the end-of-walk collectives."""

import platform

from hpc_file_utils.walk.common.protocol import TAGS
from hpc_file_utils.walk.common.stats import RankInfo, WalkStats


################################################################################
class MPIClass:

    tags = TAGS

    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def __init__(self, options=None):
        from mpi4py import MPI
        # initialization, get 'options' data structure from rank 0
        self.comm = MPI.COMM_WORLD
        self.rank   = self.comm.Get_rank()
        self.nranks = self.comm.Get_size()
        self.i_am_root = False if self.rank else True
        self.options = self.comm.bcast(options)
        self.dirs = None
        self.maxnumdirs = 0
        self.stats = WalkStats(self.options.heap_size)
        self.total_errors = 0 # sum over all ranks, set in collect()
        return



    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    def collect(self):
        """Combine every rank's stats on rank 0 (collective: every rank calls it).

        Returns the combined WalkStats on rank 0, None elsewhere. Every rank learns
        total_errors, for the exit-status policy. A tree reduction: each step merges
        two partial results capped at --heap-size per list, so no rank ever holds more
        than two ranks' worth, unlike gathering every rank's per-user lists at once.
        """
        self.stats.ranks = [RankInfo(self.rank, platform.node(), self.stats.num_dirs,
                                     self.stats.num_files, self.stats.total_size)]
        self.total_errors = self.comm.allreduce(self.stats.num_errors)
        result = self.comm.reduce(self.stats, op=WalkStats.merged, root=0)
        if result is not None:
            result.ranks.sort()
        return result
