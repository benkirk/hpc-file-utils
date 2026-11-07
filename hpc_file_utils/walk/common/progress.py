"""The Manager's view of the walk's progress, from the workers' payloads (no MPI)."""

from datetime import datetime

from hpc_file_utils.walk.common.formatting import format_number, format_size, format_timespan


class ProgressTally:
    """The latest (items, bytes, extra) payload from each rank (see protocol.py)."""

    def __init__(self, nranks):
        self.items = [0] * nranks
        self.nbytes = [0] * nranks
        self.extra = [None] * nranks

    def update(self, rank, payload):
        self.items[rank], self.nbytes[rank], self.extra[rank] = payload

    @property
    def total_items(self):
        return sum(self.items)

    @property
    def total_bytes(self):
        return sum(self.nbytes)


def progress_line(items, nbytes, elapsed, now=None):
    """'[time] Walked N items / S in T (R items/sec)'."""
    now = now or datetime.now()
    rate = int(items / elapsed) if elapsed > 0 else 0
    return '[{}] Walked {} items / {} in {} ({} items/sec)'.format(now.isoformat(sep=' ', timespec='seconds'),
                                                                  format_number(items),
                                                                  format_size(nbytes),
                                                                  format_timespan(elapsed),
                                                                  format_number(rate))
