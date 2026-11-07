#!/bin/bash -l
#PBS -A SCSG0001
#PBS -j oe
#PBS -r n
#PBS -l walltime=48:00:00
#PBS -l select=8:ncpus=128:mpiprocs=32:mem=235G
#PBS -q main

# the hpc-file-utils checkout is the parent of scans/ unless HFU_ROOT names another
cd "${PBS_O_WORKDIR:-.}" || exit 1
# shellcheck source=../etc/config_env.sh
source "${HFU_ROOT:-..}/etc/config_env.sh" || exit 1
module load peak-memusage

#--------------------------------------------------------------
projname="Campaign"
srcdir="$(ls -d /glade/campaign/*/ | egrep -v 'univ|uwyo|work' | tr '\n' ' ')"

echo "Scanning ${srcdir}"
#--------------------------------------------------------------

sha=$(echo "${srcdir}" | md5sum | awk '{print $1}')
LOCK=${HOME}/.scan_dir.${sha}.lock

remove_lock()
{
    rm -f "${LOCK}"
}

another_instance()
{
    echo "Cannot acquire lock on ${LOCK}"
    echo "There is another instance running, exiting"
    exit 1
}

lockfile -r 5 -l 3600 "${LOCK}" || another_instance
trap remove_lock EXIT

#--------------------------------------------------------------
echo && echo "Beginning ${0} at $(date)"

#--------------------------------------------------------------
sstart=$(date +%s)
rm -f ${projname}.DONE
time \
    mpiexec --no-transfer \
        peak_memusage \
          walkstat \
            --verbose \
            --spreadsheet summary.xlsx \
            --summary-prefix "${projname}" \
            --progress $((10*60)) \
            --dirs ${srcdir} \
            1> ${projname}.out \
            2> ${projname}.err

sstop=$(date +%s)
elapsed=$((${sstop} - ${sstart}))
cp ${projname}.out ${projname}.DONE
echo "# Done at $(date) in $(python -c "print('{:.2f}'.format(${elapsed}/60/60))") hours" | tee -a ${projname}.DONE

remove_lock
