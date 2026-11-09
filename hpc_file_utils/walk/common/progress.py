"""The Manager's view of the walk's progress, from the workers' payloads (no MPI)."""

from datetime import datetime

from hpc_file_utils.walk.common.formatting import format_number, format_size, format_timespan


class ProgressTally:
    """The latest (items, bytes, queued, extra) payload from each rank (see protocol.py)."""

    def __init__(self, nranks):
        self.items = [0] * nranks
        self.nbytes = [0] * nranks
        self.queued = [0] * nranks
        self.extra = [None] * nranks

    def update(self, rank, payload):
        self.items[rank], self.nbytes[rank], self.queued[rank], self.extra[rank] = payload

    @property
    def total_items(self):
        return sum(self.items)

    @property
    def total_bytes(self):
        return sum(self.nbytes)

    @property
    def total_queued(self):
        return sum(self.queued)


def progress_line(items, nbytes, elapsed, now=None):
    """'[time] Walked N items / S in T (R items/sec)'."""
    now = now or datetime.now()
    rate = int(items / elapsed) if elapsed > 0 else 0
    return '[{}] Walked {} items / {} in {} ({} items/sec)'.format(now.isoformat(sep=' ', timespec='seconds'),
                                                                  format_number(items),
                                                                  format_size(nbytes),
                                                                  format_timespan(elapsed),
                                                                  format_number(rate))


def written_status(nbytes, elapsed, delta=None, interval=None, waiting=None, verb='wrote'):
    """' | wrote S (R/s now, A/s avg), batches waiting B', appended to the progress line by
    tools that write (walktar; walksync says 'copied'). 'now' is 'delta' bytes over
    'interval' seconds, the time since the last line; without them (the final line) only
    the average."""
    avg = nbytes / elapsed if elapsed > 0 else 0
    if delta is None or interval is None:
        return ' | {} {} ({}/s avg)'.format(verb, format_size(nbytes), format_size(avg))
    now = delta / interval if interval > 0 else 0
    status = ' | {} {} ({}/s now, {}/s avg)'.format(verb, format_size(nbytes), format_size(now), format_size(avg))
    if waiting is not None:
        status += ', batches waiting {}'.format(format_number(waiting))
    return status
