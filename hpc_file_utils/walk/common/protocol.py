"""Message tags and the progress payload, shared by the Manager and the workers (no MPI).

The messages themselves, and how the Manager uses them to decide the walk is done, are
described at the top of manager.py.

Progress payload: every worker -> Manager message carries one

    (items, bytes, extra)

the worker's running entry count and byte total, plus 'extra': None in the base tools,
anything picklable a derived tool wants on the progress line (walksync: bytes copied,
bytes queued). Built by BaseWorker.progress_payload(), unpacked by
Manager.update_progress().
"""

TAGS = {
    'ready':     10,   # worker -> manager: payload; sent with sendrecv
    'execute':   11,   # manager -> worker: a directory to scan
    'dir_reply': 31,   # worker -> manager: (subdirectories, payload)
    'progress':  40,   # worker -> manager: payload
    'terminate': 1000, # manager -> worker: None, the walk is done
}
