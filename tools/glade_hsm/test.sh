#!/usr/bin/env bash

# -e: exit on error
# -u: treat unset variables as errors
# -o pipefail: fail if any command in a pipeline fails
set -euo pipefail

#----------------------------------------------------------------------------
# environment
SCRIPTDIR="$( cd "$( dirname $(realpath "${BASH_SOURCE[0]}") )" >/dev/null 2>&1 && pwd )"
source ${SCRIPTDIR}/common.sh
GLADE_HSM="${GLADE_HSM:-${SCRIPTDIR}/glade_hsm}"
#----------------------------------------------------------------------------
export log_dir=$(pwd)/testlogs && mkdir -p ${log_dir}


# create a series of files and directories to test purging
TESTROOT=${TESTROOT:-/glade/campaign/cisl/csg/${USER}-tmp-hsm_tests}
[ -d ${TESTROOT} ] && { echo -e "Refusing to use existing ${TESTROOT}\nmaybe remove it??"; exit 1; } || true

testdir="${TESTROOT}/testdir_for_hsm"

TIMEFORMAT=$'\nReal time: %3R seconds'

make_test_dirs()
{
    echo "testdir=${testdir}"

    #echo ${testdir}
    #set -x
    mkdir -p "${testdir}"/{2010,2011,2012}

    # create a file in each directory
    while read -r -d '' subdir; do

        date > ${subdir}/tiny-input.txt
	for cnt in $(seq 1 4); do
	    dd if=/dev/random of=${subdir}/large-output-${cnt}.dat bs=1M count=128 >/dev/null 2>&1
	done

    done < <(find "${testdir}" -type d -print0)

    #find ${testdir} ! -type d -ls
}




#----------------------------------------------------------------------------
# main program follows
#----------------------------------------------------------------------------
[[ -x "${GLADE_HSM}" ]] || exit 1

rm -rf ${testdir}



cat <<EOF
#----------------------------------------------------------------------------
# test 0 - no arguments - should print help and exit success
#                --help - should print help and exit success
#----------------------------------------------------------------------------
EOF
${GLADE_HSM}
${GLADE_HSM} --help >/dev/null # print once. above...



cat <<EOF
#----------------------------------------------------------------------------
# test 1 - migrate a couple files
#        - migrate all direcories
#----------------------------------------------------------------------------
EOF
make_test_dirs
${GLADE_HSM} migrate ${testdir}/*.*
${GLADE_HSM} migrate ${testdir}/*/



cat <<EOF
#----------------------------------------------------------------------------
# test 2 - rerun as one step; inspect logs to make sure SHAs are working
#        - migrate all direcories
#----------------------------------------------------------------------------
EOF
rm -rf ${testdir}/COLD_STORAGE
make_test_dirs
${GLADE_HSM} migrate ${testdir}/*.* ${testdir}/*/ --force



cat <<EOF
#----------------------------------------------------------------------------
# test 3 - status - synthetic files
#----------------------------------------------------------------------------
EOF
${GLADE_HSM} status ${testdir}/COLD_STORAGE/*.*
${GLADE_HSM} status ${testdir}/COLD_STORAGE/*/



cat <<EOF
#----------------------------------------------------------------------------
# test 4 - recall - synthetic files - should be no op, since not offline
#----------------------------------------------------------------------------
EOF
${GLADE_HSM} recall ${testdir}/COLD_STORAGE/*.*
${GLADE_HSM} recall ${testdir}/COLD_STORAGE/*/



cat <<EOF
#----------------------------------------------------------------------------
# test 5 - 'fake' recall; then status - known offline directories
#          (so long as our logs are not in the system search path, no recall will occur)
#----------------------------------------------------------------------------
EOF
time ${GLADE_HSM} recall /glade/campaign/univ/uiuc0017/SouthAmerica_WRF4KM_PGW/COLD_STORAGE/{2021,2022}
time ${GLADE_HSM} status /glade/campaign/univ/uiuc0017/SouthAmerica_WRF4KM_PGW/COLD_STORAGE/{2021,2022}



cat <<EOF
#----------------------------------------------------------------------------
# test 6 - 'fake' recall - known offline files
#           (so long as our logs are not in the system search path, no recall will occur)
#----------------------------------------------------------------------------
EOF
time ${GLADE_HSM} recall /glade/campaign/univ/uiuc0017/SouthAmerica_WRF4KM_PGW/COLD_STORAGE/2022/*
time ${GLADE_HSM} recall /glade/campaign/univ/uiuc0017/SouthAmerica_WRF4KM_PGW/COLD_STORAGE/2021/* --force # <-- force allows us to get a repeatable timing
time ${GLADE_HSM} recall /glade/campaign/univ/uiuc0017/SouthAmerica_WRF4KM_PGW/COLD_STORAGE/{2021,2021}/* --force # <-- should take twice as long but filter out duplicates and result in the same SHA



echo "Removing TESTROOT=${TESTROOT}"
echo "(to prevent accidental migration)"
rm -rf ${TESTROOT}
