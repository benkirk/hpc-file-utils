#!/bin/bash

maxconcur=8 #12

queue="${queue:-main}"
walltime="${walltime:-24:00:00}"

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
