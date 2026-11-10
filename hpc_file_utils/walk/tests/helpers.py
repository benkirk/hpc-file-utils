"""Helpers the tests import (fixtures live in conftest.py)."""

import os

from hpc_file_utils.walk.common.options import parse_options


def make_options(app="walkstat", **overrides):
    """A tool's options as parse_options() builds them, every attribute present with its
    real default, for --dirs of the current directory; then the given ones overridden.
    Replaces the hand-built SimpleNamespaces, so the code needs no getattr defaults."""
    args = parse_options(app, ["--dirs", os.getcwd()])
    for name, value in overrides.items():
        setattr(args, name, value)
    return args
