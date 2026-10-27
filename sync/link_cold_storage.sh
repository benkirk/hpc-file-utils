#!/bin/bash
#
# usage: ./link_cold_storage.sh [--dry-run] [<pair> ...]
#
# For each 'hsm: yes' pair (default: all of them), read cold_storage.<pair>.list
# (written by the sync job's dfind preflight) and create
#    <dest>/<rel>/COLD_STORAGE -> <src>/<rel>/COLD_STORAGE
# so the HSM data stays in place on the source fileset but is reachable from the new tree.
# Idempotent - existing correct links are left alone.

dryrun=false
[[ "$1" == "--dry-run" ]] && { dryrun=true; shift; }

. ./pairs.sh || exit 1

pairs=("$@")
[[ ${#pairs[@]} -gt 0 ]] || mapfile -t pairs < <(pair_names)

run()
{
    echo "$@"
    ${dryrun} || "$@"
}

for name in "${pairs[@]}"; do
    pair_load "${name}" || exit 1
    [[ ${hsm} == "yes" ]] || continue

    cs_list="cold_storage.${pair}.list"
    [[ -f ${cs_list} ]] || { echo "[${pair}]: ${cs_list} not found, run the sync job first!"; continue; }

    echo "# [${pair}] $(wc -l < "${cs_list}" | tr -d ' ') entries in ${cs_list}"

    while IFS= read -r target; do
        rel="${target#"${srcdir}"/}"
        [[ ${rel} != "${target}" ]] || { echo "  skipping ${target} (not under ${srcdir})"; continue; }
        # only link the outermost COLD_STORAGE*, nested entries come along with it
        [[ $(dirname "${rel}") == *COLD_STORAGE* ]] && continue

        link="${destdir}/${rel}"

        if [[ -L ${link} ]]; then
            [[ $(readlink "${link}") == "${target}" ]] && continue
            echo "  WARNING: ${link} is a symlink to $(readlink "${link}"), expected ${target}"
        elif [[ -e ${link} ]]; then
            echo "  WARNING: ${link} exists and is not a symlink, skipping"
        elif [[ ! -d $(dirname "${link}") ]]; then
            echo "  WARNING: $(dirname "${link}") does not exist, skipping"
        else
            run ln -s "${target}" "${link}"
        fi
    done < "${cs_list}"
done
