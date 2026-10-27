#!/usr/bin/env python3
"""
Split one pairs.conf [pair] whose tree contains HSM COLD_STORAGE* directories
into many smaller pairs, so most of the data can still go through dsync.

    ./split_pair.py 3A-CESM-HR > pairs.3A-CESM-HR.split.conf

Starting at the pair's src, recursively:
  - now a symlink or gone           -> skipped (the scan is stale, e.g. the owner moved it
                                       elsewhere and left a link); the final pass copies the link
  - subtree without COLD_STORAGE*   -> dsync pair (if >= --min-size, else left for the final pass),
                                       with its own batch_files sized for ~--batch-size per batch
  - COLD_STORAGE* itself            -> skipped (stays in place, symlinked later)
  - mixed subtree <= --max-hsm-size,
    or with >= --min-size of its own (non-recursive) files
                                    -> one 'hsm: yes' pair (msrsync, COLD_STORAGE* pruned)
  - other mixed subtree             -> descend into its child directories

Loose files/links in descended directories, small subtrees, and anything newer
than the scan are NOT in the split - the original whole-tree [pair] (hsm: yes)
must be run once all split jobs finish, as the final pass:

    pairs_conf=pairs.3A-CESM-HR.split.conf make run         # -> array job id NNN
    after='NNN[]' ./submit_sync_array.sh 3A-CESM-HR         # final whole-tree pass, after the array

COLD_STORAGE* locations come from a live 'find' (authoritative); directory sizes
and tree shape come from the 'cs-usage' scan database (approximate, possibly stale).
Each dsync pair still runs the job's dfind preflight, so a stale split aborts
rather than recalls from tape.
"""

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from datetime import datetime

HSM_PATTERN = "COLD_STORAGE*"

UNITS = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40, "P": 2**50}


def parse_size(s):
    s = s.strip().upper().rstrip("B").rstrip("I")
    unit = s[-1] if s and s[-1] in UNITS else ""
    return int(float(s[:len(s) - len(unit)]) * UNITS[unit])


def human(n):
    for u in ("P", "T", "G", "M", "K"):
        if n >= UNITS[u]:
            return "%.1f %siB" % (n / UNITS[u], u)
    return "%d B" % n


def load_pair(name):
    """use pairs.sh so there is only one config parser"""
    out = subprocess.run(
        ["bash", "-c",
         '. ./pairs.sh && pair_load "$1" || exit 1; '
         'printf "%s\\n" "${srcdir}" "${destdir}" "${pairs_conf}"',
         "_", name],
        check=True, capture_output=True, text=True).stdout.splitlines()
    # global settings: the raw "key: value" lines before the first [section]
    globals_ = []
    for line in open(out[2]):
        if line.lstrip().startswith("["):
            break
        if ":" in line and not line.lstrip().startswith("#"):
            globals_.append(line.rstrip())
    return out[0], out[1], globals_


def find_cold_storage(src):
    out = subprocess.run(
        ["find", src, "-type", "d", "-name", HSM_PATTERN, "-prune", "-print"],
        check=True, capture_output=True, text=True).stdout
    return sorted(out.splitlines())


def scan_dirs(db, dbroot):
    out = subprocess.run(
        ["cs-usage", "query", db, "-P", dbroot, "-n", "0", "--min-size", "0", "--format", "json"],
        check=True, capture_output=True, text=True).stdout
    rows = json.loads(out)["rows"]
    return {r["path"]: r for r in rows
            if r["path"] == dbroot or r["path"].startswith(dbroot + "/")}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pair", help="pair name in pairs.conf (or ${pairs_conf})")
    p.add_argument("--max-hsm-size", default="40TiB",
                   help="mixed subtrees up to this size become one hsm pair [%(default)s]")
    p.add_argument("--min-size", default="1TiB",
                   help="clean subtrees smaller than this are left for the final pass [%(default)s]")
    p.add_argument("--batch-size", default="25TiB",
                   help="target data per dsync batch (= work lost on restart), via per-pair batch_files [%(default)s]")
    p.add_argument("--mount", default="/glade/campaign",
                   help="mount point stripped to form scan database paths [%(default)s]")
    p.add_argument("--db", help="cs-usage database (default: first path component below --mount)")
    args = p.parse_args()

    max_hsm = parse_size(args.max_hsm_size)
    min_size = parse_size(args.min_size)
    batch_size = parse_size(args.batch_size)

    src, dest, globals_ = load_pair(args.pair)

    if not src.startswith(args.mount + "/"):
        sys.exit("ERROR: %s is not under --mount %s" % (src, args.mount))
    dbpath = lambda path: path[len(args.mount):]
    fspath = lambda path: args.mount + path
    dbroot = dbpath(src)
    db = args.db or dbroot.split("/")[1]

    cold = [dbpath(c) for c in find_cold_storage(src)]
    dirs = scan_dirs(db, dbroot)
    if dbroot not in dirs:
        sys.exit("ERROR: %s not found in cs-usage database '%s'" % (dbroot, db))

    children = {}
    for path in dirs:
        if path != dbroot:
            children.setdefault(os.path.dirname(path), []).append(path)

    def is_mixed(path):
        return any(c.startswith(path + "/") for c in cold)

    size = lambda path: dirs[path]["total_size_r"]
    files = lambda path: dirs[path]["file_count_r"]

    pairs = []      # (kind, path)
    leftover = []   # (reason, path, bytes)

    def split(path):
        name = os.path.basename(path)
        if fnmatch.fnmatch(name, HSM_PATTERN) or path in cold:
            return
        # the scan database can be stale: a directory since moved away and replaced by a link
        if os.path.islink(fspath(path)) or not os.path.isdir(fspath(path)):
            leftover.append(("link" if os.path.islink(fspath(path)) else "gone", path, size(path)))
            return
        if not is_mixed(path):
            if size(path) >= min_size:
                pairs.append(("dsync", path))
            else:
                leftover.append(("small", path, size(path)))
            return
        # small enough, or substantial loose files of its own: one msrsync job
        if size(path) <= max_hsm or dirs[path]["total_size_nr"] >= min_size:
            pairs.append(("hsm", path))
            return
        if dirs[path]["total_size_nr"]:
            leftover.append(("loose", path, dirs[path]["total_size_nr"]))
        for child in sorted(children.get(path, [])):
            split(child)

    split(dbroot)

    if not pairs:
        sys.exit("ERROR: nothing to split; is %s small enough to be one hsm pair?" % src)

    pairs.sort(key=lambda kp: -size(kp[1]))

    tot = lambda kind: sum(size(path) for k, path in pairs if k == kind)
    moved = [(path, n) for r, path, n in leftover if r in ("link", "gone")]
    left = sum(n for r, _, n in leftover if r not in ("link", "gone"))

    w = sys.stdout.write
    w("# split of [%s] by %s on %s\n" % (args.pair, os.path.basename(sys.argv[0]), datetime.now().strftime("%F %H:%M")))
    w("#   %s -> %s\n" % (src, dest))
    w("#   %d COLD_STORAGE* dirs (live find), scan db '%s'\n" % (len(cold), db))
    w("#   --max-hsm-size %s --min-size %s\n" % (args.max_hsm_size, args.min_size))
    w("#\n")
    w("#   %4d dsync pairs  %12s\n" % (sum(k == "dsync" for k, _ in pairs), human(tot("dsync"))))
    w("#   %4d hsm pairs    %12s (incl. resident COLD_STORAGE)\n" % (sum(k == "hsm" for k, _ in pairs), human(tot("hsm"))))
    hsm_pairs = [path for k, path in pairs if k == "hsm"]
    if hsm_pairs:
        # one node of msrsync each - this one sets the walltime
        w("#        largest hsm  %12s %s\n" % (human(size(hsm_pairs[0])), os.path.relpath(hsm_pairs[0], dbroot)))
    w("#   %4s final pass   %12s (small subtrees + loose files, plus anything newer than the scan)\n" % ("", human(left)))
    if moved:
        w("#   %4d moved away   %12s (per the scan; now a link or gone - the final pass copies links)\n"
          % (len(moved), human(sum(n for _, n in moved))))
    w("#   --batch-size %s per dsync batch (per-pair batch_files)\n" % args.batch_size)
    w("#\n")
    w("# run [%s] itself after this array completes, as the final whole-tree pass.\n\n" % args.pair)

    for line in globals_:
        w(line + "\n")

    for i, (kind, path) in enumerate(pairs):
        rel = os.path.relpath(path, dbroot)
        w("\n# %s, %s files: %s\n" % (human(size(path)), format(files(path), ","), rel))
        w("[%s.%03d]\n" % (args.pair, i))
        w("src:   %s\n" % fspath(path))
        w("dest:  %s/%s\n" % (dest, rel))
        if kind == "hsm":
            w("hsm:   yes\n")
        elif files(path):
            # few huge files would otherwise make the whole piece one batch
            avg = size(path) / files(path)
            w("batch_files: %d\n" % max(100, -(-batch_size // max(1, int(avg)))))

    if leftover:
        w("\n# left for the final pass:\n")
        for reason, path, n in sorted(leftover, key=lambda x: -x[2]):
            w("#   %-5s %10s  %s\n" % (reason, human(n), os.path.relpath(path, dbroot)))


if __name__ == "__main__":
    main()
