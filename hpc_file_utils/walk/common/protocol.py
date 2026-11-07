"""Message tags and the progress payload, shared by the Manager and the workers (no MPI).

The messages themselves, and how the Manager uses them to decide the walk is done, are
described at the top of manager.py.

Progress payload: every worker -> Manager message carries one

    (items, bytes, queued, extra)

the worker's running entry count and byte total, the length of its local stack of
directories still to walk (phase 3.5), plus 'extra': None in the base tools,
anything picklable a derived tool wants on the progress line (walksync: bytes copied,
bytes queued). Built by BaseWorker.progress_payload(), unpacked by
Manager.update_progress().
"""

TAGS = {
    'ready':       10,   # worker -> manager: payload; the worker then waits for the answer
    'execute':     11,   # manager -> worker: [dir, ...], the answer to a 'ready'
    'dir_reply':   31,   # worker -> manager: (dirs, payload), overflow of its local stack
    'share':       32,   # manager -> worker: None, "send me some of your stack"
    'share_reply': 33,   # worker -> manager: (dirs, payload), possibly no dirs
    'progress':    40,   # worker -> manager: payload
    'terminate':   1000, # manager -> worker: None, the answer to a 'ready': the walk is done
}
