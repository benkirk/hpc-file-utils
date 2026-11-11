#!/bin/bash -l
#PBS -N cgd_migration
#PBS -A SCSG0001
#PBS -j oe
#PBS -q main
#PBS -r y
#PBS -l walltime=12:00:00
#PBS -l select=12:ncpus=128:mpiprocs=32:mem=235G

#--------------------------------------------------------------
# synchronize one [pair] from pairs.conf: src -> dest
#
#   pair=<name>          : select the pair by name, else
#   pair_list=<a>:<b>:.. : the ${PBS_ARRAY_INDEX}-th of these, else of pairs.conf
#   dryrun=yes           : HSM preflight + 'dsync --dryrun' (or 'walksync --dry-run') only, then exit
#   fresh_after_epoch=N  : a done stamp only counts if its pass started at or after N
#                          (submit_sync_array.sh resolves fresh_after to this)
#
# exits immediately if the pair has a valid done stamp (stamps/<pair>.done), and
# writes that stamp when the final rsync --delete exits 0, so repeated submissions
# ratchet each pair to completion.
#
# stages:
#   1. HSM preflight: dfind COLD_STORAGE* -> cold_storage.<pair>.list
#                     (abort if found and pair is not 'hsm: yes')
#   2. dsync          (skipped for hsm pairs - dsync cannot exclude), or
#      walksync       (copier: walksync - any pair, COLD_STORAGE* excluded), on all nodes
#   3. msrsync        (multi-stream rsync, COLD_STORAGE* pruned; hsm pairs by default, after dsync)
#   4. rsync --delete (serial cleanup pass, COLD_STORAGE* excluded, --max-delete capped)
#   5. index dest, cmp a random sample of files against src
#   -> done stamp, only if rsync exited 0 and nothing above looked wrong
#--------------------------------------------------------------

# shellcheck disable=SC1091
type module >/dev/null 2>&1 || source /etc/profile.d/z00_modules.sh

# derecho is the one with a Cray PE. (not ${NCAR_HOST}: the purge below
# unloads ncarenv, which unsets it; loading ncarenv sets it again.)
[[ -d /opt/cray/pe ]] && cluster=derecho || cluster=casper

module --force purge >/dev/null 2>&1
case "${cluster}" in
    casper) module load ncarenv/25.10 gcc/14.3.0 openmpi/5.0.8 mpifileutils/0.12 ;;
    *)      module load ncarenv/25.10 gcc/14.3.0 cray-mpich/8.1.32 mpifileutils/0.12 ;;
esac
echo "cluster=${cluster} NCAR_HOST=${NCAR_HOST}"
module list

cd "${PBS_O_WORKDIR:-$(pwd)}" || exit 1

. ./pairs.sh || exit 1

hsm_pattern="COLD_STORAGE*"
dryrun="${dryrun:-no}"

#--------------------------------------------------------------
if [[ -z ${pair} ]]; then
    if [[ -n ${pair_list} ]]; then
        IFS=: read -r -a pairs_in_array <<< "${pair_list}"
        pair="${pairs_in_array[${PBS_ARRAY_INDEX:-0}]}"
    else
        pair="$(pair_name_by_index "${PBS_ARRAY_INDEX:-0}")"
    fi
fi
pair_load "${pair}" || exit 1
copier_host_check "${cluster}" || exit 1

echo "Synchronizing [${pair}] ${srcdir} -> ${destdir} (hsm=${hsm}, copier=${copier})"

# finished for good, e.g. a job queued before 'done: yes' was set
if [[ ${pair_done} == yes ]]; then
    echo "[${pair}] marked 'done: yes' in ${pairs_conf}, nothing to do"
    exit 0
fi

# already done? (no lock needed - just reading the stamp)
fresh_after_epoch="${fresh_after_epoch:-$(fresh_after_resolve)}" || exit 1
case "$(stamp_check "${pair}" "${fresh_after_epoch}")" in
    valid)
        echo "[${pair}] complete as of $(date -d "@$(stamp_get "${pair}" finished)") (job $(stamp_get "${pair}" jobid)), nothing to do"
        exit 0 ;;
    stale)
        echo "[${pair}] done stamp predates fresh_after ($(date -d "@${fresh_after_epoch}")), syncing again" ;;
    mismatch)
        echo "[${pair}] done stamp is for a different src/dest, syncing" ;;
esac

# tunables (pairs.sh 'setting'): per-pair value, else global (host-specific first), else default.
# dsync sets file metadata per batch, so a batch is the unit of work lost on
# a restart - size it so one batch takes well under the walltime.
batch_files="$(setting batch_files 250000)"
#--------------------------------------------------------------

sha=$(echo "${srcdir}+${destdir}" | md5sum | awk '{print $1}')
LOCK=${HOME}/.sync_dir.${sha}.lock

remove_lock()
{
    rm -f "${LOCK}"
}

another_instance()
{
    echo "Cannot acquire lock on ${LOCK}"
    echo "There is another instance running, exiting"
    echo "(if not, remove the stale lock file and resubmit)"
    exit 1
}

# no stale-lock timeout: these jobs run for days. traps release the lock on
# normal exit & preemption; a SIGKILL'ed job may leave a stale lock behind.
lockfile -r 5 "${LOCK}" || another_instance
trap remove_lock EXIT
trap 'exit 1' TERM INT HUP

#--------------------------------------------------------------
report_elapsed()
{
    local elapsed=$(( $(date +%s) - $1 ))
    printf "%.2f hours / %.2f minutes / %d seconds\n" \
           "$(echo "${elapsed} / 3600" | bc -l)" "$(echo "${elapsed} / 60" | bc -l)" ${elapsed}
}

report_status()
{
    echo "   $1 exit status: $2${3:+ ($3)}"
}

echo && echo "Beginning ${0} at $(date)"

tstart=$(date +%s)
timestamp=$(date +%F@%H:%M)
jobid="${PBS_JOBID%%.*}"; jobid="${jobid/\[/_}"; jobid="${jobid/\]/}"   # 1234[5] -> 1234_5
suffix="${pair}.${jobid:+${jobid}.}${timestamp}"

#--------------------------------------------------------------
[[ -d ${srcdir} ]]  || { echo "${srcdir}: no such directory!"; exit 1; }

# pair_load checked the configured paths; check again with symlinks resolved
src_real="$(realpath "${srcdir}")"
dest_real="$(realpath -m "${destdir}")"
if [[ ${src_real}/ == "${dest_real}"/* || ${dest_real}/ == "${src_real}"/* ]]; then
    echo "ERROR: ${srcdir} and ${destdir} resolve to the same or nested directories (${src_real} vs ${dest_real})"
    exit 1
fi

if [[ ${dryrun} == "yes" ]]; then
    [[ -d ${destdir} ]] || { echo "dryrun: ${destdir} does not exist yet, nothing to compare"; exit 0; }
else
    mkdir -p "${destdir}" || exit 1
fi
[[ -d ${destdir} ]] || { echo "${destdir}: no such directory!"; exit 1; }
[[ -w ${destdir} ]] || { echo "${destdir}: not writeable by ${USER}!"; exit 1; }

# walksync_inplace: auto = rsync --inplace only into a dest that is empty now, where every file
# is new (no temp file and rename per file, ~1/3 fewer directory operations in a big directory)
if [[ ${copier} == walksync ]]; then
    [[ -z $(find "${destdir}" -mindepth 1 -maxdepth 1 -print -quit) ]] && dest_empty=yes || dest_empty=no
    walksync_use_inplace="$(walksync_inplace_resolve "${dest_empty}")"
    echo "   walksync --inplace: ${walksync_use_inplace} (walksync_inplace: ${walksync_inplace}, dest empty at start: ${dest_empty})"
fi

# walksync's walk does not check st_dev (the final rsync's -x does, dsync does not either):
# refuse a source with another filesystem mounted inside it. (GPFS filesets are not mounts
# and share one st_dev, so -x would cross them too.)
if [[ ${copier} == walksync ]]; then
    mounts_inside="$(awk -v r="${src_real}/" 'index($2, r) == 1 { print $2 }' /proc/mounts)"
    if [[ -n ${mounts_inside} ]]; then
        echo "ERROR: [${pair}] another filesystem is mounted inside ${srcdir}, which walksync would cross:"
        echo "${mounts_inside}" | head -n 5 | sed 's/^/       /'
        exit 1
    fi
fi

# anything that should keep this pass from being stamped done, even if rsync exits 0
unclean=()

msrsync=$(pwd)/msrsync

[[ -f ${msrsync} ]] || { echo "${msrsync}: no such file!";   exit 1; }
[[ -x ${msrsync} ]] || { echo "${msrsync}: not executable?"; exit 1; }

if [[ ${copier} == walksync ]]; then
    [[ -f ${hfu_root}/etc/config_env.sh ]] || { echo "ERROR: hfu_root ${hfu_root}: no etc/config_env.sh"; exit 1; }
fi

which dfind >/dev/null 2>&1 || { echo "Cannot locate dfind!!"; exit 1; }
which dsync >/dev/null 2>&1 || { echo "Cannot locate dsync!!"; exit 1; }

# ranks & ranks/node from the PBS select statement (one nodefile line per mpiproc)
if [[ -n ${PBS_NODEFILE} ]]; then
    nnodes=$(sort -u "${PBS_NODEFILE}" | wc -l)
    nranks=$(wc -l < "${PBS_NODEFILE}")
else
    nnodes=1
    nranks=8
fi
ppn=$((nranks / nnodes))

# MPI launches use NCAR's mpibind wrapper: rank layout from the PBS select line,
# correct flags for the loaded MPI (cray-mpich on derecho, openmpi on casper).
which mpibind >/dev/null 2>&1 || { echo "Cannot locate mpibind!!"; exit 1; }
echo "${cluster}: ${nnodes} nodes, ${nranks} MPI ranks (${ppn}/node)"

# copier: walksync - from the shared install (hfu_root), on every node of the job (the select
# line's mpiprocs ranks per node), in its own directory walksync.<suffix>/ (summary, userlists,
# per-rank indexes of what it copied, or the dry-run lists). In a subshell, config_env.sh loads
# the install's modules (etc/modules-derecho) and activates its env; dfind and dsync keep the
# job's mpifileutils. /usr/bin/rsync: the env's conda-forge rsync has no ACLs.
run_walksync() # [--dry-run]
{
    local ws_bind ws_opts
    read -r -a ws_bind <<< "${walksync_bind}"
    mapfile -t ws_opts < <(walksync_args "${walksync_use_inplace:-no}")
    mkdir -p "walksync.${suffix}" || return 1
    (
        cd "walksync.${suffix}" || exit 1
        export NCAR_HOST="${NCAR_HOST:-${cluster}}"
        # shellcheck source=/dev/null
        source "${hfu_root}/etc/config_env.sh" || { echo "ERROR: cannot activate ${hfu_root}"; exit 1; }
        echo "walksync: $(command -v walksync), mpiexec: $(command -v mpiexec)"
        mpiexec --no-transfer "${ws_bind[@]}" \
            walksync \
            --rsync /usr/bin/rsync \
            --exclude "${hsm_pattern}" \
            --verbose --progress 60 \
            "${ws_opts[@]}" \
            --summary-prefix "${pair}" \
            --index=gz \
            "$@" \
            --dirs "${srcdir}" --dest "${destdir}"
    ) 1> "walksync.out.${suffix}" 2> "walksync.err.${suffix}"
}

# walksync's summary lines, for the job log
walksync_summary()
{
    LC_ALL=C grep -aE '^--> (Sync|Not compared|Dry run|Copied)' "walksync.out.${suffix}" | sed 's/^/   /'
}

#--------------------------------------------------------------
rsync_opts=(-axAHX "--exclude=${hsm_pattern}")

# chmod: rsync --chmod for every rsync pass, e.g. 'Du+w' for a read-only source. dsync copies
# modes as they are, so it would create read-only directories it then can't fill - pairs with
# chmod skip dsync, and msrsync (auto) does the bulk copy instead.
rsync_chmod="${pair_chmod}"
if [[ -n ${rsync_chmod} ]]; then
    # no blanks: msrsync gets the rsync options as one string
    [[ ${rsync_chmod} =~ ^[-+=,ugoaDFrwxXst0-7]+$ ]] || { echo "ERROR: invalid chmod '${rsync_chmod}'"; exit 1; }
    rsync_opts+=("--chmod=${rsync_chmod}")
fi


# HSM preflight: find any COLD_STORAGE* entries (stat only, no recalls).
# dfind prints absolute realpaths, mixed with '[timestamp] ...' progress lines and
# mpibind notes on stdout; keep the paths and map the realpath prefix back to ${srcdir}.
# LC_ALL=C and grep -a: names with non-UTF-8 bytes must not make grep drop matches.
cs_list="cold_storage.${pair}.list"
echo -n "...running dfind ${srcdir} -name '${hsm_pattern}' at $(date)... "
sstart=$(date +%s)
mpibind \
        "$(which dfind)" \
        --quiet \
        "${srcdir}/" \
        --name "${hsm_pattern}" --print \
    2> dfind.err.${suffix} \
    | tee dfind.out.${suffix} \
    | LC_ALL=C grep -a '^/' \
    | LC_ALL=C awk -v r="${src_real}/" -v s="${srcdir}/" 'index($0, r) == 1 { $0 = s substr($0, length(r) + 1) } { print }' \
    | LC_ALL=C sort > "${cs_list}.tmp"
rcs=("${PIPESTATUS[@]}")
# mpibind can exit 0 even when the launcher crashes, so also require dfind's final
# summary line - an empty list from a dfind that never ran must not pass the guard.
# (pipeline: 0 mpibind/dfind, 1 tee, 2 grep - 1 just means no paths, 3 awk, 4 sort/write)
walked="$(LC_ALL=C grep -aoE 'Walked [0-9]+ items in [0-9.]+ seconds' dfind.out.${suffix} | tail -n 1 | awk '{print $2}')"
if [[ ${rcs[0]} -ne 0 || ${rcs[1]} -ne 0 || ${rcs[2]} -gt 1 || ${rcs[3]} -ne 0 || ${rcs[4]} -ne 0 || -z ${walked} ]]; then
    echo "ERROR: dfind failed (pipeline status: ${rcs[*]}), see dfind.{out,err}.${suffix}"
    exit 1
fi
mv -f "${cs_list}.tmp" "${cs_list}" || exit 1
report_elapsed ${sstart}

# an empty (or vanished, e.g. unlinked fileset) source would make rsync --delete empty dest
[[ ${walked} -ge 2 ]] \
    || { echo "ERROR: ${srcdir} is empty (dfind walked ${walked} item), refusing to sync"; exit 1; }

ncs=$(wc -l < "${cs_list}" | tr -d ' ')
# fail closed: also count any non-progress dfind line naming the pattern, parsed or not
nraw=$(LC_ALL=C grep -av '^\[' dfind.out.${suffix} | LC_ALL=C grep -ac -- "${hsm_pattern%\*}")
echo "   walked ${walked} items, found ${ncs} '${hsm_pattern}' entries (${nraw} raw dfind lines), see ${cs_list}"

if [[ ( ${ncs} -gt 0 || ${nraw} -gt 0 ) && ${hsm} != "yes" ]]; then
    echo "ERROR: ${srcdir} contains '${hsm_pattern}' but [${pair}] is not 'hsm: yes'!!"
    echo "       refusing to dsync (would recall from tape). Inspect ${cs_list}"
    exit 1
fi



# dryrun: what would dsync (or walksync) change? then quit.
if [[ ${dryrun} == "yes" && ${copier} == walksync ]]; then
    # unlike dsync's, walksync's dry run works on read-only sources, hsm pairs, and source-only files
    echo -n "...running walksync --dry-run ${srcdir} -> ${destdir} at $(date)... "
    sstart=$(date +%s)
    run_walksync --dry-run
    rc=$?
    report_elapsed ${sstart}
    report_status walksync ${rc}
    walksync_summary
    echo "   lists (N new, C changed, X only in the destination): walksync.${suffix}/walksync-dryrun-r*.txt"
    exit 0
fi
if [[ ${dryrun} == "yes" ]]; then
    [[ ${hsm} == "yes" || -n ${rsync_chmod} ]] && { echo "dsync --dryrun is not applicable to hsm/chmod pairs, exiting."; exit 0; }
    echo -n "...running dsync --dryrun ${srcdir} -> ${destdir} at $(date)... "
    sstart=$(date +%s)
    mpibind \
            "$(which dsync)" \
            --dryrun \
            --no-dereference \
            --xattrs non-lustre \
        "${srcdir}/" "${destdir}/" \
        1> dsync-dryrun.out.${suffix} \
        2> dsync-dryrun.err.${suffix}
    report_elapsed ${sstart}
    exit 0
fi



# the bulk copy, on all nodes: walksync (copier: walksync), else mpifileutils' dsync - which
# cannot exclude or chmod, so not for hsm/chmod pairs.
if [[ ${copier} == walksync ]]; then
    echo -n "...running walksync (${walksync_bind}; $(walksync_args "${walksync_use_inplace}" | paste -sd ' ')) ${srcdir} -> ${destdir} at $(date)... "
    sstart=$(date +%s)
    run_walksync
    rc=$?
    report_elapsed ${sstart}
    report_status walksync ${rc} "non-zero: a failed stat, scan or rsync batch"
    walksync_summary
    walksync_copied="$(LC_ALL=C grep -a '^--> Copied ' "walksync.out.${suffix}" | tail -n 1 | sed 's/^--> //')"
    if [[ ${rc} -ne 0 || -z ${walksync_copied} ]]; then
        echo "   WARNING: walksync did not finish cleanly, see walksync.{out,err}.${suffix}"
        { LC_ALL=C grep -a 'ERROR' "walksync.out.${suffix}"; LC_ALL=C grep -av '^\[' "walksync.err.${suffix}"; } \
            | tail -n 5 | sed 's/^/      /'
    fi
    # no summary: it never ran, or a rank was lost (the launcher can exit 0 even then). like
    # dsync, don't fall through to the serial rsync - on a fresh dest that is a single-stream
    # copy of the whole tree on one node.
    [[ -n ${walksync_copied} ]] || { echo "ERROR: walksync did not complete (no summary)"; exit 1; }
    # a failed batch: no stamp for this pass, but go on - the serial rsync below fills in
    # whatever is missing, and a rerun stamps the pair
    [[ ${rc} -eq 0 ]] || unclean+=("walksync exit status ${rc}")
elif [[ ${hsm} == "yes" ]]; then
    echo "...skipping dsync for hsm pair [${pair}]"
elif [[ -n ${rsync_chmod} ]]; then
    echo "...skipping dsync for [${pair}] (chmod: ${rsync_chmod})"
else
    echo -n "...running dsync ${srcdir} -> ${destdir} at $(date)... "
    sstart=$(date +%s)
    mpibind \
            "$(which dsync)" \
            --no-dereference \
            --xattrs non-lustre \
            --batch-files "${batch_files}" \
        "${srcdir}/" "${destdir}/" \
        1> dsync.out.${suffix} \
        2> dsync.err.${suffix}
    rc=$?
    report_elapsed ${sstart}
    # non-root: dsync's lchown() to the source uid/gid fails with EPERM (silently), but
    # still makes dsync exit non-zero - the subsequent rsync pass is the real check.
    report_status dsync ${rc} "batch-files ${batch_files}; non-zero expected when not root"
    # ...so success is judged by dsync's own completion message (mpibind may also
    # exit 0 after a launcher crash). don't fall through to a single-node msrsync.
    grep -q "Completed sync" dsync.out.${suffix} \
        || { echo "ERROR: dsync did not complete, see dsync.{out,err}.${suffix}"; exit 1; }
    # ...and a non-zero exit can't flag real errors (ENOSPC, EDQUOT, EIO, a lost rank),
    # so look for them: mpifileutils logs '[time] [rank] [file:line] ERROR: ...' to stdout,
    # and dsync.err should hold nothing but known launcher noise for the lchown exit.
    dsync_errors=$(LC_ALL=C grep -ac '\] ERROR: ' dsync.out.${suffix})
    noise='^$|^\[[^]]*\] SET OMP_NUM_THREADS=|^-+$'
    noise+='|^prterun detected that one or more processes exited with non-zero status,$'
    noise+='|^thus causing the job to be terminated. The first process to do so was:$'
    noise+='|^[[:space:]]+Process name:|^[[:space:]]+Exit code:[[:space:]]+1$|: rank [0-9]+ exited with code 1$'
    # derecho (PALS) SIGTERMs the remaining ranks once one exits 1; a kill before the end
    # is still caught, by the 'Completed sync' check above
    noise+='|: rank [0-9]+ died from signal 15$'
    dsync_other=$(LC_ALL=C grep -avcE "${noise}" dsync.err.${suffix})
    if [[ ${dsync_errors} -gt 0 || ${dsync_other} -gt 0 ]]; then
        echo "   WARNING: dsync reported ${dsync_errors} ERROR lines (dsync.out), ${dsync_other} unexpected lines (dsync.err):"
        { LC_ALL=C grep -a '\] ERROR: ' dsync.out.${suffix}; LC_ALL=C grep -avE "${noise}" dsync.err.${suffix}; } | head -n 5 | sed 's/^/      /'
        unclean+=("dsync errors: ${dsync_errors} in dsync.out, ${dsync_other} in dsync.err")
    fi
fi



# drop all but 1 node, leaving only serial processes next...
if [[ ${nnodes} -gt 1 ]]; then
    exe_host=$(head -n 1 "${PBS_NODEFILE}"); exe_host="${exe_host%%.*}"
    # shellcheck disable=SC2046
    pbs_release_nodes $(cut -f 1 -d '.' "${PBS_NODEFILE}" | sort -u | grep -vxF "${exe_host}" | paste -s -d ' ')
    report_status pbs_release_nodes $?
    sleep 15 # qstat is cached
    echo "   job now holds (qstat may lag): $(qstat -f "${PBS_JOBID}" 2>/dev/null | sed -n 's/^ *exec_host = //p')"
fi



# a multi-stream rsync for secondary transfer, one rsync per usable CPU on this node.
# needed for hsm pairs (dsync skipped); after a completed dsync it only re-crawls the tree.
# hard links are restored by the serial rsync -H pass below either way (dsync and
# msrsync both break them), so msrsync: auto = hsm and chmod pairs only, and never after
# walksync (pairs.sh msrsync_resolve); yes/no to force.
use_msrsync="$(msrsync_resolve)" || exit 1

if [[ ${use_msrsync} == "no" ]]; then
    echo "...skipping msrsync for [${pair}] (msrsync: $(setting msrsync auto), copier: ${copier})"
else
    # (nproc honors the job cpuset, but also OMP_NUM_THREADS, which PBS sets to 1)
    msrsync_procs="$(setting msrsync_procs "$(env -u OMP_NUM_THREADS -u OMP_THREAD_LIMIT nproc)")"
    echo -n "...running msrsync (${msrsync_procs} processes) ${srcdir} -> ${destdir} on $(date) ... "
    sstart=$(date +%s)
    MSRSYNC_PRUNE="${hsm_pattern}" \
    ${msrsync} \
        --processes "${msrsync_procs}" \
        --size  250G \
        --files 50000 \
        --stats \
        --rsync "${rsync_opts[*]}" \
        "${srcdir}/" "${destdir}/" \
        1> msrsync.out.${suffix} \
        2> msrsync.err.${suffix}
    rc=$?
    report_elapsed ${sstart}
    report_status msrsync ${rc}
fi



# clean up with a serial pass, including the delete flag (which is not supported in msrsync).
# the exclude also protects the dest COLD_STORAGE* -> src symlinks from --delete.
# --max-delete caps the damage if src is wrong or mostly empty: rsync stops deleting and
# exits 25 (no stamp). max_delete: N, or N% of the source items dfind walked [1%, >= 1000].
max_delete="$(setting max_delete 1%)"
if [[ ${max_delete} == *% ]]; then
    max_delete=$(( walked * ${max_delete%\%} / 100 ))
    [[ ${max_delete} -ge 1000 ]] || max_delete=1000
fi
[[ ${max_delete} =~ ^[0-9]+$ ]] || { echo "ERROR: invalid max_delete '${max_delete}'"; exit 1; }
echo -n "...running rsync (max-delete ${max_delete}) ${srcdir} -> ${destdir} on $(date) ... "
sstart=$(date +%s)
rsync "${rsync_opts[@]}" --delete --max-delete="${max_delete}" --info=STATS2 \
    "${srcdir}/" "${destdir}/" \
    1> rsync.out.${suffix} \
    2> rsync.err.${suffix}
rc=$?
report_elapsed ${sstart}
report_status rsync ${rc} "0 = complete; 23/24 = some files not transferred/vanished; 25 = max-delete reached, see rsync.err"
[[ ${rc} -eq 25 ]] && echo "   rsync wanted to delete more than ${max_delete} items - check src, or raise max_delete for [${pair}]"



if [[ ${rc} -eq 0 ]]; then
    # list every non-directory in ${destdir}
    echo -n "...indexing ${destdir} on $(date) ... "
    sstart=$(date +%s)
    find "${destdir}" ! -type d 2> flist-destdir.err.${suffix} \
        | xz > flist-destdir.out.${suffix}.xz
    report_elapsed ${sstart}

    # rsync's quick check trusts size+mtime; compare the contents of a random sample of
    # copied files with their source. (dest COLD_STORAGE* are symlinks - never sampled.)
    verify_sample="$(setting verify_sample 200)"
    if [[ ${verify_sample} -gt 0 ]]; then
        echo -n "...comparing ${verify_sample} random files with the source on $(date) ... "
        sstart=$(date +%s)
        xz -dc flist-destdir.out.${suffix}.xz | LC_ALL=C shuf -n $(( verify_sample * 4 )) \
            | while IFS= read -r f; do [[ -f ${f} && ! -L ${f} ]] && echo "${f}"; done \
            | head -n "${verify_sample}" > verify_sample.${suffix}
        nsample=$(wc -l < verify_sample.${suffix} | tr -d ' ')
        # SRC/DST must be in xargs' environment: as a prefix on 'tr' they were empty, and cmp
        # compared each dest file with itself (GNU cmp: same file -> identical, instantly).
        # An empty SRC or a self-comparison counts as a mismatch.
        # shellcheck disable=SC2016
        tr '\n' '\0' < verify_sample.${suffix} \
            | SRC="${srcdir}" DST="${destdir}" xargs -0 -r -n 1 -P 8 bash -c \
                's="${SRC}/${1#"${DST}"/}"; [[ -n ${SRC} && ${s} != "$1" ]] && cmp -s -- "${s}" "$1" || echo "MISMATCH $1"' _ \
            > verify_mismatch.${suffix}
        nbad=$(wc -l < verify_mismatch.${suffix} | tr -d ' ')
        report_elapsed ${sstart}
        echo "   compared ${nsample} files, ${nbad} mismatches (verify_mismatch.${suffix})"
        [[ ${nbad} -eq 0 ]] || unclean+=("${nbad} of ${nsample} sampled files differ from the source")
    fi
fi



# the done stamp: only for a clean rsync and no other problems. 24 (source files
# vanished) means the source is still changing - leave it for another pass.
if [[ ${rc} -eq 0 && ${#unclean[@]} -eq 0 ]]; then
    mkdir -p stamps || exit 1
    {
        echo "pair: ${pair}"
        echo "src: ${srcdir}"
        echo "dest: ${destdir}"
        echo "started: ${tstart}"
        echo "finished: $(date +%s)"
        echo "jobid: ${PBS_JOBID:-interactive}"
        echo "cluster: ${cluster}"
        echo "copier: ${copier}"
        [[ ${copier} == walksync ]] && echo "walksync: ${walksync_copied}; --inplace: ${walksync_use_inplace}"
        echo "rsync: $(grep -E '^Number of (created|deleted|regular files transferred)' rsync.out.${suffix} | sed 's/^Number of //' | paste -s -d ',')"
        echo "verified: ${nsample:-0} sampled files identical"
    } > "$(stamp_file "${pair}").tmp" && mv -f "$(stamp_file "${pair}").tmp" "$(stamp_file "${pair}")"
    echo "   wrote done stamp $(stamp_file "${pair}")"
else
    echo "   no done stamp: rsync exit status ${rc}${unclean[*]:+; ${unclean[*]}}"
fi



#--------------------------------------------------------------
echo -n "Done at $(date) "
report_elapsed ${tstart}
