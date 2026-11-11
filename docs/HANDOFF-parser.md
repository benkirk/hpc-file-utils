# Hand-off: the hfu-index parser for hpc-usage-queries (written 2026-11-10)

The next piece of work, in the other repo: a parser in hpc-usage-queries' `fs_scans/parsers`
that imports the index files walkstat, walktar and walksync write with `--index`. This
repo's side is finished and fixed at v1 (`HANDOFF-indexes2.md`, `HANDOFF-sweep.md` 4.8). The
walk README's "Indexes" section, its "Importing" list above all, is the contract. This note
adds what reading hpc-usage-queries showed, so the parser can be written from the two
together. Nothing here changes hpc-file-utils. If the contract turns out wrong for the
importer, say so before changing either side.

## What hpc-usage-queries looks like (read at `devel`, ebe3f38, 2026-10-26)

`~/codes/hpc-usage-queries` holds three checkouts: `devel` (branch `dev`), and `main` and
`staging`, both at 1444d3e. `fs_scans/` is the same code in each, apart from data files.
Start from `devel`, or whatever that repo's own workflow says.

- **`parsers/base.py`:** `FilesystemParser` with `format_name`, `can_parse(file_path)` and
  `parse_line(line) -> ParsedEntry | None`. `ParsedEntry` has `path`, `size`,
  `allocated`, `uid`, `gid`, `is_dir`, `atime: datetime | None`, `inode` and
  `fileset_id`. Parsers register in `parsers/__init__.py`, and `detect_parser()` tries
  each `can_parse()` in registration order. `lustre.py` is the closest model: one line
  per entry, `can_parse()` on the name (`*.lfs-scan`), atime as
  `datetime.fromtimestamp(epoch)`, naive by design.
- **The importers never call `parse_file()`.** `importers/file_handling.py` reads the file
  in byte-sized chunks of lines (`chunk_file_generator()`), and the passes call
  `parser.parse_line()` on each line, in worker processes. So **`parse_line()` sees no
  header and keeps no state between lines.**
- **`open_input_file()` opens plain text only** (`open(..., errors="replace")`). gzip
  needs a change there: open through `gzip.open(..., "rt")` when the file starts with
  `1f 8b`. The `--index=gz` files are one gzip stream each, and `cat` of them is a valid
  multi-stream gzip file, which `gzip.open` reads whole.
- **pass1** (`importers/pass1.py`) keeps every entry with `is_dir` and collects the
  directories by path, so a repeated `P` is harmless there.
- **pass2a** (`importers/pass2a.py`) counts every directory entry into its parent's
  `nr_dirs`, and every file's count, `allocated` and atime into its parent. A `P` record
  reaching it would count a directory again, so it must not.

## How the format meets that

- **Map the columns by position, not by the header.** The column set is fixed at v1 for
  every tool, so a stateless `parse_line()` can split on tabs and know the fields from
  their count. Paths are escaped and never hold a tab, so the split is exact:
  - 13 fields: walkstat and walksync, `rec type size alloc mode uid gid nlink ino atime
    mtime ctime path`;
  - 15 fields: walktar, with `offset offset_data` before `path`;
  - anything else is not a record: return None, and count it if the importer counts
    malformed lines.
- **Headers:** any line starting with `#` returns None. A `cat` of several files holds
  one per file.
- **`P` records: a `structural: bool = False` field on `ParsedEntry`**, set for `P`, which
  pass2a skips and pass1 keeps. The alternative in the contract, a parser mode that drops
  `P` after pass 1, needs state across the passes' calls, and the line-at-a-time workers
  make that awkward. Whichever is chosen, a walkstat root appears only as `P`, so pass1
  must keep `P` directories or the roots never reach the directory table.
- **`-` fields:** only a `P` of a directory that vanished has them. Map them to 0 or None
  as `ParsedEntry` allows; pass2a never sees a `P`.
- **Paths:** `decode_path()` (`hpc_file_utils/walk/common/index.py`) reverses the escapes:
  `\\` is a backslash, `\n` `\t` `\r` are those characters, and `\xNN` is the byte NN.
  The result is the bytes as walked, decoded with `os.fsdecode()`, so a non-UTF-8 name
  comes back with surrogates. SQLite will not store a surrogate in a text column. Decide
  whether such a path stays escaped in the database (the escaped text is valid UTF-8 and
  reversible), or is decoded and handled. The test vectors to copy are `ODD` in
  `hpc_file_utils/walk/tests/test_index.py`, plus the `non_utf8_tree` fixture's names.
  The ~15-line function can be copied rather than importing hpc-file-utils.
- **`atime`:** `datetime.fromtimestamp(int(field))`, naive, as the Lustre parser does.
  walktar's atime is the one found before its read, and walkstat never reads.
- **`inode` = `ino`.** `nlink` is there too, if the importer ever wants to count hard links
  once.
- **Detection:** `can_parse()` on the names `index-r*.tsv`, `index-*-r*.tsv` and
  `*.tar.idx`, each with an optional `.gz`, or better, a peek at the first line for
  `# hfu-index v1`, through gzip when the file starts with `1f 8b`. A file whose header
  is not v1 should be refused, not misread.

## What to import

- **walkstat's indexes are the usage listing**: its `E` records are what it counted below
  its roots, exactly once across a run's files.
- **walktar's** list what its tars hold, the roots included as `E`. They are a fine listing
  of an archived tree.
- **walksync's** are one run's copies, or with `dry-run=yes` in the header what a run
  would copy: real source stats, but a subset of the tree. A usage import should refuse
  them, or at least say so; `tool=` in the header tells. With the header unseen by
  `parse_line()`, that check belongs in `can_parse()` or the import wrapper.

## Test material

- **Make real files with the tools**: `walkstat --index` and `--index=gz` on a small tree
  with odd names, plus one walktar and one walksync run, `cat`ed and not. Commit the
  outputs as fixtures. The golden tree in `hpc_file_utils/walk/tests/conftest.py`
  (`build_golden_tree()`) has the right mix: odd names, links, a fifo, hard links, empty
  directories.
- **The checks worth having:** pass1 sees every directory once by path; pass2a's totals
  per directory equal `find`'s, with every `P` skipped; a `cat` of several files imports
  the same as the files one by one; a `.gz` the same as its plain twin; and the `-`
  fields of a vanished-directory `P` parse.

## Kickoff prompt

```
Sprint: the hfu-index parser in hpc-usage-queries (fs_scans/parsers), its own repo and PR.

Repo: ~/codes/hpc-usage-queries (devel, main and staging checkouts; read that repo's own
workflow first). hpc-file-utils main holds indexes-2: --index=gz, walksync --index, the
column set fixed at v1, the import contract rewritten.

Read first, in order:
1. hpc-file-utils docs/HANDOFF-parser.md: all of it
2. hpc-file-utils hpc_file_utils/walk/README.md: "Indexes", above all "Importing" (the contract)
3. hpc-file-utils hpc_file_utils/walk/common/index.py: encode_path, decode_path, read_header
4. hpc-usage-queries fs_scans/parsers/base.py and lustre.py, importers/file_handling.py,
   pass1.py, pass2a.py

Scope: an hfu-index v1 parser, plain and gzip (open_input_file learns gzip by the magic),
stateless parse_line() mapping by field count (13, or 15 for walktar), P records kept out of
pass 2a (a structural flag on ParsedEntry is the recommendation; decide and document), paths
decoded or kept escaped (decide, given SQLite and surrogates), walksync indexes refused or
flagged for a usage import. Tests from real index files the tools write, committed as fixtures.

Deliverables: the parser, its tests, a note in that repo's docs; nothing changed in
hpc-file-utils unless the contract is wrong (then say so first).
Gate: that repo's tests and CI green after every commit.
Rules: as before. Explicit paths only, never -a or -A; one step, one commit; commit, push and PR
when I ask. Any derecho job: account SCSG0001, main queue, expedite with sudo -n -u csgteam qalter
-l priority_boost=2000, never qrun; a watcher waits for a job to appear in qstat before waiting for
it to end; ask before anything beyond 4 nodes or an hour; shellcheck the shell you write.
```
