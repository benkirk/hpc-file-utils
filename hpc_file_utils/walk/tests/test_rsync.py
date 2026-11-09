"""walksync's rsync pieces that need no rsync (sync/rsync.py; sweep A5)."""

from types import SimpleNamespace

import pytest

from hpc_file_utils.walk.sync.rsync import rsync_command, split


@pytest.mark.parametrize("n, k, sizes", [
    (10, 1, [10]), (10, 3, [4, 3, 3]), (10, 10, [1] * 10), (3, 8, [1, 1, 1]), (0, 4, []), (7, 2, [4, 3]),
])
def test_split_cuts_a_batch_into_k_near_equal_parts_in_order(n, k, sizes):
    paths = ["p{}".format(i) for i in range(n)]
    parts = split(paths, k)
    assert [len(p) for p in parts] == sizes
    assert [p for part in parts for p in part] == paths       # every path once, in order


def test_rsync_command_is_walksyncs():
    o = SimpleNamespace(rsync="/usr/bin/rsync", rsync_base=["-aAX", "--numeric-ids", "--chmod=Du+w"],
                        rsync_opts=["--inplace"], dirs=["/src/tree/"], dest="/dest/tree")
    assert rsync_command(o) == ["/usr/bin/rsync", "-aAX", "--numeric-ids", "--chmod=Du+w", "--files-from=-",
                                "--from0", "--inplace", "/src/tree/", "/dest/tree/"]
