#!/bin/bash -l
#PBS -A <project_code>
#PBS -j oe
#PBS -r y
#PBS -l walltime=12:00:00
#PBS -l select=1:ncpus=128:mpiprocs=96:mem=235G

. env/config_env.sh || exit 1
. scan_dirs.cfg || exit 1

#--------------------------------------------------------------
projname="${projnames[${PBS_ARRAY_INDEX}]}"
srcdir="${srcdirs[${PBS_ARRAY_INDEX}]}"
srcdir="$(realpath ${srcdir})" # <-- clean up any /foo//bar type paths to /foo/bar
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
[[ -z ${PBS_ARRAY_ID} ]] && suffix="${timestamp}" || suffix=${PBS_ARRAY_ID/"[]"/}.${timestamp}


#--------------------------------------------------------------
sstart=$(date +%s)
rm -f ${projname}.DONE
time \
mpiexec --no-transfer \
        ./walkstat/walkstat \
        --verbose \
        --summary-prefix "${projname}" \
        --progress $((10*60)) \
        --dirs ${srcdir}/*/ \
        1> ${projname}.out \
        2> ${projname}.err

#         --summary summary.xlsx \

sstop=$(date +%s)
elapsed=$((${sstop} - ${sstart}))
mv -f ${projname}.out ${projname}.DONE
echo "# Done at $(date) in $(python -c "print('{:.2f}'.format(${elapsed}/60/60))") hours" | tee -a ${projname}.DONE

if [ -w "${srcdir}/.usage" ]; then
    destfile=${srcdir}/.usage/summary.txt

    echo "Copying out results to ${destfile} ..."

    rm -f ${destfile}
    cp summary-${projname} ${destfile}
    mkdir -p "${srcdir}/.usage/userlists/"
    rsync -axv --force --delete ./userlists/${projname}/ ${srcdir}/.usage/userlists/
fi

remove_lock
