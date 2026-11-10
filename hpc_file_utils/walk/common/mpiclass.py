"""MPI state shared by the Manager and the workers, and the end-of-walk collectives."""

import platform

from hpc_file_utils.walk.common.stats import RankInfo, WalkStats


class MPIClass:

    def __init__(self, options=None):
        from mpi4py import MPI
        self.MPI = MPI
        # initialization, get 'options' data structure from rank 0
        self.comm = MPI.COMM_WORLD
        self.rank   = self.comm.Get_rank()
        self.nranks = self.comm.Get_size()
        self.options = self.comm.bcast(options)
        self.stats = WalkStats(self.options.heap_size)
        self.total_errors = 0 # sum over all ranks, set in collect()

    def rank_info(self):
        """This rank's own counts for --verbose. The Manager waits for no work and keeps
        no stack: zeros; the workers fill those in."""
        return RankInfo(self.rank, platform.node(), self.stats.num_dirs, self.stats.num_files,
                        self.stats.total_size, 0.0, 0)

    def collect(self):
        """Combine every rank's stats on rank 0 (collective: every rank calls it).

        Returns the combined WalkStats on rank 0, None elsewhere. Every rank learns
        total_errors, for the exit-status policy. A tree reduction: each step merges
        two partial results capped at --heap-size per list, so no rank ever holds more
        than two ranks' worth, unlike gathering every rank's per-user lists at once.
        """
        self.stats.ranks = [self.rank_info()]
        self.total_errors = self.comm.allreduce(self.stats.num_errors)
        result = self.comm.reduce(self.stats, op=WalkStats.merged, root=0)
        if result is not None:
            result.ranks.sort()
            # walkstat's directories split over stat batches: their parts are all here now
            result.finish_directories(self.options.threshold_count, self.options.threshold_size,
                                      self.options.filters is not None)
        return result
