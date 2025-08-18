#!/usr/bin/env bash

# -e: exit on error
# -u: treat unset variables as errors
# -o pipefail: fail if any command in a pipeline fails
set -euo pipefail

#----------------------------------------------------------------------------
# environment
SCRIPTDIR="$( cd "$( dirname $(realpath "${BASH_SOURCE[0]}") )" >/dev/null 2>&1 && pwd )"
source ${SCRIPTDIR}/common.sh
GLADE_PURGE="${GLADE_PURGE:-${SCRIPTDIR}/glade_purge}"
#----------------------------------------------------------------------------


CONFDIR="${HOME}/.glade_purge"
LOGDIR="${CONFDIR}"
LOGFILE="${LOGDIR}/log"
KNOWN_PURGEDIRS="${CONFDIR}/purgedirs.list"

# create a series of files and directories to test purging
make_test_dirs()
{
    testdirs=()
    testfiles=()
    #testdirs+=("~/for_purge_test")
    testdirs+=("${HOME}/for_purge_test")
    testdirs+=("${WORK}/for_purge_test")
    testdirs+=("${SCRATCH}/for_purge_test")
    testdirs+=("${SCRATCH}/../${USER}/for_purge_test2")
    testdirs+=("/glade/campaign/cisl/csg/${USER}")

    echo "testdirs=${testdirs[@]}"

    for dir in "${testdirs[@]}"; do
        #echo ${dir}
        mkdir -p ${dir}/{a,b,c,d}/{w,x,y,z}

        # create a file in each directory
        while read -r -d '' subdir; do

            date > ${subdir}/ofile.txt
            date > ${subdir}/ofile2.txt

            testfiles+=($(realpath ${subdir}/ofile2.txt))

        done < <(find ${dir} -type d -print0)

        #find ${dir} ! -type d -ls
    done
}



inspect_purge_dirs()
{
    # quick return if KNOWN_PURGEDIRS is empty
    [ -s "${KNOWN_PURGEDIRS}" ] || return 0

    echo -e "\nListing known purge directories from ${KNOWN_PURGEDIRS}"

    while IFS= read -r purgedir; do

        # be sure purgedir is available, and usable, so
        # we don't remove stuff in the wrong place!!
        [ -d ${purgedir} ] || { echo "no such directory: ${purgedir}"; continue; }

        echo "${purgedir} :"
        ls -A ${purgedir}

    done < "${KNOWN_PURGEDIRS}"
    # end loop on purgedirs
}


#----------------------------------------------------------------------------
# main program follows
#----------------------------------------------------------------------------
[ -x "${GLADE_PURGE}" ] || exit 1



cat <<EOF
#----------------------------------------------------------------------------
# test 0 - no arguments - should print help and exit success
#----------------------------------------------------------------------------
EOF
${GLADE_PURGE}


cat <<EOF
#----------------------------------------------------------------------------
# test 1 - clean & purge as separate steps
#----------------------------------------------------------------------------
EOF
make_test_dirs
${GLADE_PURGE} ${testfiles[@]} ${testdirs[@]}
${GLADE_PURGE} --clean
inspect_purge_dirs



cat <<EOF
#----------------------------------------------------------------------------
# test 2 - clean & purge as one step; (force/quiet)
#----------------------------------------------------------------------------
EOF
make_test_dirs
${GLADE_PURGE} ${testdirs[@]} ${testfiles[@]} --quiet --force --clean
inspect_purge_dirs



cat <<EOF
#----------------------------------------------------------------------------
# test 3 - files, then dirs
#----------------------------------------------------------------------------
EOF
make_test_dirs
${GLADE_PURGE} ${testfiles[@]} --quiet --force
${GLADE_PURGE} ${testdirs[@]} --quiet --force
inspect_purge_dirs
${GLADE_PURGE} --clean
inspect_purge_dirs
