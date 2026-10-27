#!/bin/bash
#
# usage: ./compress_dest.sh [--now] [--dry-run] [-P N] [--min-size SIZE] [<pair> ...]
#        ./compress_dest.sh --status [--exact] [<pair> ...]
#
# Request GPFS compression of the copied files under each pair's dest (default: all
# pairs) instead of waiting for the Campaign Storage policy (>= 180 days, >= 100 MiB).
# Copies are written uncompressed even when the source was compressed.
#
#   default    mmchattr -I defer --compression z : mark files; compressed at the next
#              scheduled policy run (fast to issue)
#   --now      mmchattr --compression z : compress immediately on this node (slow,
#              ~10 MB/s per file stream - use a batch job and a large -P)
#   -P N       parallel mmchattr streams [8]
#   --min-size find -size argument, files larger than this [+100M, as the policy]
#   --status   fileset usage of each dest (df: compressed, whole fileset - instant)
#   --exact    with --status: stat every file in dest (glade_hsm_scan_filelist.py):
#              true vs. on-disk bytes, i.e. how much is (still) uncompressed
#
# Compression keeps mtime and logical size, so later sync passes are unaffected.
# Run as the files' owner - i.e. before the root chown hand-off.
#
# See https://ncar-hpc-docs.readthedocs.io/en/latest/storage-systems/glade/campaign/#compression-details

MMCHATTR=/usr/lpp/mmfs/bin/mmchattr

# per-file true vs. on-disk sizes; from this repo if present, else the installed copy
SCAN_FILELIST="$(dirname "$(realpath "$0")")/../glade_hsm/libexec/glade_hsm_scan_filelist.py"
[[ -x ${SCAN_FILELIST} ]] || SCAN_FILELIST=/glade/u/apps/opt/csg-utils/filesystem/glade_hsm/libexec/glade_hsm_scan_filelist.py

mode="defer"
dryrun=false
exact=false
nprocs=8
min_size="+100M"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --now)      mode="now" ;;
        --status)   mode="status" ;;
        --dry-run)  dryrun=true ;;
        --exact)    exact=true ;;
        -P)         nprocs="$2"; shift ;;
        --min-size) min_size="$2"; shift ;;
        -*)         echo "unknown option: $1"; exit 1 ;;
        *)          break ;;
    esac
    shift
done

. ./pairs.sh || exit 1

pairs=("$@")
[[ ${#pairs[@]} -gt 0 ]] || mapfile -t pairs < <(pair_names)

[[ -x ${MMCHATTR} ]] || { echo "${MMCHATTR}: not found (run on a GLADE GPFS client)"; exit 1; }

case "${mode}" in
    defer) chattr_args=(-I defer --compression z) ;;
    now)   chattr_args=(--compression z) ;;
esac

for name in "${pairs[@]}"; do
    pair_load "${name}" || exit 1
    [[ -d ${destdir} ]] || { echo "[${pair}] ${destdir}: no such directory, skipping"; continue; }

    if [[ ${mode} == "status" ]]; then
        echo "[${pair}] ${destdir}"
        df -h --output=used,size,pcent "${destdir}" | sed 's/^/   fileset /'
        if ${exact}; then
            find "${destdir}" -type f -print0 | "${SCAN_FILELIST}" | grep -E "^  (Total|HSM)"
        fi
        continue
    fi

    echo "[${pair}] ${destdir}: files ${min_size}, mmchattr ${chattr_args[*]}, ${nprocs} streams, $(date)"
    if ${dryrun}; then
        echo "   (dry run) $(find "${destdir}" -type f -size "${min_size}" | wc -l) files would be requested"
        continue
    fi

    tstart=$(date +%s)
    # find never follows symlinks, so COLD_STORAGE -> source links are not touched
    find "${destdir}" -type f -size "${min_size}" -print0 \
        | xargs -0 -r -n 64 -P "${nprocs}" "${MMCHATTR}" "${chattr_args[@]}"
    rcs=("${PIPESTATUS[@]}")
    echo "   done in $(( $(date +%s) - tstart )) seconds (find/xargs status: ${rcs[*]})"
done
