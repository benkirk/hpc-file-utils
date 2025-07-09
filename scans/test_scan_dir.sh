#!/bin/bash -l
#PBS -A SCSG0001
#PBS -j oe
#PBS -r n
#PBS -l walltime=00:30:00
#PBS -l select=4:ncpus=128:mpiprocs=32:mem=235G
#PBS -q main

. env/config_env.sh || exit 1
module load peak-memusage

#--------------------------------------------------------------
projname="TEST"
srcdir="$(ls -d /glade/campaign/cisl/{csg,aiml,dares})"
#srcdir="/glade/campaign/cisl/csg/*/"

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

tstart=$(date +%s)
timestamp=$(date +%F@%H:%M)


#--------------------------------------------------------------
sstart=$(date +%s)
rm -f ${projname}.DONE
time \
    mpiexec --no-transfer \
        peak_memusage \
          ./walkstat/walkstat \
            --verbose \
            --summary summary.xlsx \
            --summary-prefix "${projname}" \
            --progress $((1*60)) \
            --dirs ${srcdir} \
            1> ${projname}.out \
            2> ${projname}.err

sstop=$(date +%s)
elapsed=$((${sstop} - ${sstart}))
cp ${projname}.out ${projname}.DONE
echo "# Done at $(date) in $(python -c "print('{:.2f}'.format(${elapsed}/60/60))") hours" | tee -a ${projname}.DONE

remove_lock
