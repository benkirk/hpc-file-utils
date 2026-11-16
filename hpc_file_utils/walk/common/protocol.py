"""Message tags and the progress payload, shared by the Manager and the workers (no MPI).

The messages themselves, and how the Manager uses them to decide the walk is done, are
described at the top of manager.py.

Progress payload: every worker -> Manager message carries one

    (items, bytes, queued, extra)

the worker's running entry count and byte total, the length of its local stack of
directories still to walk plus its closed batches, and 'extra':
None for a tool that writes nothing (walkstat, walkfind, walkrm); walktar's (bytes,
files, archives) written or walksync's (bytes, entries, rsync batches) copied so far,
which the Manager sums into the progress line's rate. Built by BaseWorker.progress_payload(), unpacked by
Manager.update_progress().
"""

from enum import IntEnum

class Tag(IntEnum):
    """The message tags; an int to mpi4py."""
    READY       = 10     # worker -> manager: payload; the worker then waits for the answer
    EXECUTE     = 11     # manager -> worker: (dirs, batches), the answer to a 'ready'
    DIR_REPLY   = 31     # worker -> manager: (dirs, payload), overflow of its local stack
    SHARE       = 32     # manager -> worker: None, "send me some of your stack"
    SHARE_REPLY = 33     # worker -> manager: (dirs, batches, payload), possibly neither
    PROGRESS    = 40     # worker -> manager: payload
    TERMINATE   = 1000   # manager -> worker: None, the answer to a 'ready': the walk is done
