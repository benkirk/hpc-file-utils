#!/bin/bash -l
#PBS -N cgd_delete
#PBS -A SCSG0001
#PBS -q main
#PBS -j oe
#PBS -r n
#PBS -l walltime=04:00:00
#PBS -l select=2:ncpus=128:mpiprocs=32:mem=235G

#--------------------------------------------------------------
# remove one [pair]'s dest with walkrm (from the shared install hfu_root), on every rank:
#   make delete_dest PAIR=<name> CONFIRM=<dest> [PREFLIGHT=skip] [DRYRUN=yes] [AFTER=<jobid>] [NODES=2]
#   (qsub -v pair,confirm,preflight,dryrun_rm delete_dest.sh)
#
# Refused (delete_dest_check, pairs.sh, at submission and again here) unless CONFIRM is exactly
# the pair's dest, dest is a real directory still under allowed_roots, no sync of the pair holds
# its lock, and a passing 'make preflight' (preflight.<pair>.ok, under 7 days, for this dest)
# exists - PREFLIGHT=skip overrides only that. The job then takes the pair's sync lock, so no
# sync starts beside it, and runs
#   walkrm --allowed-root <each allowed_root> --dirs <dest>
# which refuses anything not strictly below an allowed root, never follows a symlink or
# crosses a filesystem, and gives u+rwx to our read-only directories first. When walkrm exits
# 0 and dest is gone, the pair's done stamp moves aside (stamps/<pair>.done.deleted-<time>):
# a later 'make run' copies it again. DRYRUN=yes: walkrm --dry-run, nothing removed.
#--------------------------------------------------------------

[[ -d /opt/cray/pe ]] && cluster=derecho || cluster=casper
cd "${PBS_O_WORKDIR:-$(pwd)}" || exit 1
. ./pairs.sh || exit 1

pair_load "${pair:?usage: qsub -v pair,confirm[,preflight,dryrun_rm] delete_dest.sh}" || exit 1
delete_dest_check "${confirm:-}" "${preflight:-}" || exit 1
[[ -f ${hfu_root}/etc/config_env.sh ]] || { echo "ERROR: hfu_root ${hfu_root}: no etc/config_env.sh"; exit 1; }

lock="$(sync_lock)"
lockfile -r 0 "${lock}" || { echo "ERROR: [${pair}] cannot take ${lock}: a sync job is running"; exit 1; }
trap 'rm -f "${lock}"' EXIT
trap 'exit 1' TERM INT HUP

jobid="${PBS_JOBID%%.*}"
timestamp=$(date +%F@%H:%M)
suffix="${pair}.${jobid:-local}.${timestamp}"
roots=()
for r in $(conf_get allowed_roots); do roots+=(--allowed-root "${r}"); done
[[ ${#roots[@]} -gt 0 ]] || roots=(--allowed-root /glade/campaign/)
dry=()
[[ ${dryrun_rm:-no} == yes ]] && dry=(--dry-run)

echo "[${pair}] ${dry[*]:+DRY RUN: }removing ${destdir} on ${cluster} at $(date) ($(sort -u "${PBS_NODEFILE:-/dev/null}" | wc -l) nodes)"
sstart=$(date +%s)
run_hfu "${hfu_bind}" walkrm "walkrm.${suffix}" --progress 60 --verbose "${roots[@]}" "${dry[@]}" --dirs "${destdir}" \
    > "walkrm.out.${suffix}" 2> "walkrm.err.${suffix}"
rc=$?
echo "   walkrm exit status ${rc} in $(( $(date +%s) - sstart )) s"
LC_ALL=C grep -aE '^--> (Removed|Dry run|Kept|[0-9,]+ (errors|entries|directories))' "walkrm.out.${suffix}" | sed 's/^/   /'

if [[ ${dry[*]} ]]; then
    exit "${rc}"
fi
if [[ ${rc} -ne 0 || -e ${destdir} ]]; then
    echo "ERROR: [${pair}] ${destdir} was not removed completely (walkrm exit ${rc}), see walkrm.{out,err}.${suffix}"
    exit 1
fi
stamp="$(stamp_file "${pair}")"
if [[ -f ${stamp} ]]; then
    mv -f "${stamp}" "${stamp}.deleted-${timestamp}" && echo "   done stamp moved aside: ${stamp}.deleted-${timestamp}"
fi
rm -f "preflight.${pair}.ok"
echo "[${pair}] ${destdir} removed at $(date)"
