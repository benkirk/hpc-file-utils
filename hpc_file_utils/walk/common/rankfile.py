"""RankFile: a per-rank output file, opened at its first write (no MPI)."""


class RankFile:
    """A text file one rank writes (walksync's dry-run list, walkfind's list): opened at its
    first write, so a rank that writes nothing leaves no file; closed by the worker's run(),
    which knows the ones made with BaseWorker.rank_file(). binary: written as bytes
    (walkfind --print0), not UTF-8 text."""

    binary = False

    def __init__(self, name, binary=False):
        self.name = name
        self.file = None
        self.binary = binary

    def write(self, text):
        if self.file is None:
            self.file = open(self.name, 'wb') if self.binary else open(self.name, 'w', encoding='utf-8')
        self.file.write(text)

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
