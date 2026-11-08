#!/bin/bash

# Two job arrays over scan_dirs.cfg: most filesets on one node each, and the few that took
# hours on one node (SCAN_SET=large there) on several. 32 ranks per node: the walk is bound
# by each node's filesystem client, and 32 ranks beat 96 (docs/phase3.5-results.md).

maxconcur=8 #12
large_maxconcur="${large_maxconcur:-3}"

queue="${queue:-main}"
walltime="${walltime:-24:00:00}"
ppn="${ppn:-32}"
large_nodes="${large_nodes:-4}"

submit()
{
    local set=$1 nodes=$2 concur=$3 name=$4
    unset large_projnames
    # shellcheck source=scan_dirs.cfg
    SCAN_SET=${set} . scan_dirs.cfg || exit 1
    # a cfg without a large set (scan_dirs_bylab.cfg) would list everything again
    if [ "${set}" = large ] && [ -z "${large_projnames:-}" ]; then
        echo "scan_dirs.cfg names no large filesets; not submitting ${name}"
        return 0
    fi
    local nja=$((n_dirs_to_scan-1))
    [ ${nja} -ge 1 ] || { echo "ERROR: job array too small?!: ${nja}"; exit 1; }

    (
        set -x
        qsub -A SCSG0001 \
             -q "${queue}" \
             -l walltime="${walltime}" \
             -l select="${nodes}":ncpus=128:mpiprocs="${ppn}":mem=235G \
             -v SCAN_SET="${set}" \
             -N "${name}" \
             -J 0-${nja}%"${concur}" \
             scan_dir_job_array.sh
    )
}

#rm -f summary.xlsx

submit main  1                "${maxconcur}"       scan_dirs
submit large "${large_nodes}" "${large_maxconcur}" scan_dirs_large
