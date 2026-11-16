"""walktar's summary line, on rank 0 after the walk (no MPI)."""

from hpc_file_utils.walk.common.formatting import format_number, format_size


def tar_lines(written):
    """The line printed after a walktar run, before walkstat's reports of what was archived.
    written: the workers' (bytes, files, archives) summed (Manager.written()), or None if no
    worker reported any."""
    nbytes, nfiles, narchives = written or (0, 0, 0)
    return [f'--> Wrote {format_number(nfiles)} files, {format_size(nbytes)} ({format_number(nbytes)} bytes) '
            f'in {format_number(narchives)} archives']
