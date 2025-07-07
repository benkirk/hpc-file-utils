#!/bin/bash

maxconcur=2

queue="${queue:-main}"
walltime="${walltime:-12:00:00}"

topdir=$(pwd)

. scan_dirs.cfg || exit 1

nja=$((${n_dirs_to_scan}-1)); [ ${nja} -ge 1 ] || { echo "ERROR: job array too small?!: ${nja}"; exit 1; }

#rm -f summary.xlsx

set -x
qsub -A SCSG0001 \
     -q ${queue} \
     -l walltime=${walltime} \
     -N scan_dirs \
     -J 0-${nja}%${maxconcur} \
     scan_dir_job_array.sh
