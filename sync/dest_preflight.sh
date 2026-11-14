#!/bin/bash -l
#PBS -N cgd_preflight
#PBS -A SCSG0001
#PBS -q main
#PBS -j oe
#PBS -r n
#PBS -l walltime=02:00:00
#PBS -l select=1:ncpus=128:mpiprocs=32:mem=235G

#--------------------------------------------------------------
# read-only preflight of one [pair]'s dest before 'make delete_dest' removes it:
#   make preflight PAIR=<name> [SINCE='YYYY-MM-DD HH:MM'] [AFTER=<jobid>]
#   (qsub -v pair=<name>[,since=...] dest_preflight.sh)
#
# walkstat --index=gz of dest (from the shared install hfu_root), then from the index:
#   - entries not owned by the job's user (someone else's files: not ours to delete)
#   - directories without u+w (walkrm adds u+w to our own; listed so it is no surprise)
#   - entries with an atime or mtime after 'since' (someone read or wrote the copy)
# since: SINCE, else the pair's done stamp 'finished' time (the copy's last pass, its cmp
# sample included); neither -> refused. Dirs without u+w are reported but do not fail.
# Nothing found -> writes preflight.<pair>.ok (pair, dest, since, finished, jobid, entries),
# which 'make delete_dest' requires (PREFLIGHT=skip overrides); anything found -> exit 1,
# no ok file. Output in preflight.<pair>.<jobid>.<time>/.
#--------------------------------------------------------------

[[ -d /opt/cray/pe ]] && cluster=derecho || cluster=casper
cd "${PBS_O_WORKDIR:-$(pwd)}" || exit 1
. ./pairs.sh || exit 1

pair_load "${pair:?usage: qsub -v pair=<name>[,since=...] dest_preflight.sh}" || exit 1
ok_file="preflight.${pair}.ok"
rm -f "${ok_file}"     # a failed or refused preflight leaves no stale pass behind

[[ -d ${destdir} && ! -L ${destdir} ]] || { echo "ERROR: [${pair}] ${destdir}: not a directory (or a symlink)"; exit 1; }
[[ -f ${hfu_root}/etc/config_env.sh ]] || { echo "ERROR: hfu_root ${hfu_root}: no etc/config_env.sh"; exit 1; }

if [[ -n ${since:-} ]]; then
    since_epoch="$(date -d "${since}" +%s)" || { echo "ERROR: since: '${since}' is not a date"; exit 1; }
else
    since_epoch="$(stamp_get "${pair}" finished)"
    [[ ${since_epoch} =~ ^[0-9]+$ ]] \
        || { echo "ERROR: [${pair}] has no done stamp with a 'finished' time: give SINCE='YYYY-MM-DD HH:MM'"; exit 1; }
fi
owner_uid="$(id -u)"
jobid="${PBS_JOBID%%.*}"
work="preflight.${pair}.${jobid:-local}.$(date +%F@%H:%M)"

echo "[${pair}] preflight of ${destdir} on ${cluster}: since $(date -d "@${since_epoch}" '+%F %T') (${since_epoch}), owner uid ${owner_uid}"
sstart=$(date +%s)
run_hfu "${hfu_bind}" walkstat "${work}" --verbose --progress 60 --summary-prefix "${pair}" --index=gz \
        --dirs "${destdir}" > "${work}.walkstat.out" 2> "${work}.walkstat.err"
rc=$?
echo "   walkstat exit status ${rc} in $(( $(date +%s) - sstart )) s"
LC_ALL=C grep -aE '^\[.*Walked' "${work}.walkstat.out" | tail -n 1 | sed 's/^/   /'
shopt -s nullglob
indexes=("${work}"/index-"${pair}"-r*.tsv.gz)
if [[ ${rc} -ne 0 || ${#indexes[@]} -eq 0 ]]; then
    echo "ERROR: walkstat failed (exit ${rc}, ${#indexes[@]} index files), see ${work}.walkstat.{out,err}"
    exit 1
fi

# index records: rec type size alloc mode uid gid nlink ino atime mtime ctime path (E = entries)
report="${work}/preflight.txt"
zcat "${indexes[@]}" | LC_ALL=C awk -F '\t' -v uid="${owner_uid}" -v since="${since_epoch}" '
    /^#/ || $1 != "E" { next }
    { n++; if ($2 == "d") nd++; else if ($2 == "f") { nf++; bytes += $3 } }
    $6 != uid                                     { nown++;  if (nown <= 5)  print "  not ours: uid " $6 " " $13 > "/dev/stderr" }
    $2 == "d" && and(strtonum("0" $5), 128) == 0  { nrw++;   if (nrw <= 5)   print "  dir without u+w: " $5 " " $13 > "/dev/stderr" }
    $10 > since                                   { natime++; if (natime <= 5) print "  atime after since: " strftime("%F %T", $10) " " $13 > "/dev/stderr" }
    $11 > since                                   { nmtime++; if (nmtime <= 5) print "  mtime after since: " strftime("%F %T", $11) " " $13 > "/dev/stderr" }
    END { printf "entries %d dirs %d files %d bytes %.0f not_ours %d dirs_without_uw %d atime_after %d mtime_after %d\n",
                 n, nd, nf, bytes, nown, nrw, natime, nmtime }' > "${report}" 2> "${work}/preflight.examples"
rc_awk=$?
cat "${work}/preflight.examples"
read -r _ n _ nd _ nf _ bytes _ nown _ nrw _ natime _ nmtime < "${report}"
[[ ${rc_awk} -eq 0 && -n ${nmtime} ]] || { echo "ERROR: could not read the index (awk exit ${rc_awk})"; exit 1; }
printf '   entries %s (dirs %s, files %s, %s bytes); not owned by uid %s: %s; dirs without u+w: %s; atime after since: %s; mtime after since: %s\n' \
       "${n}" "${nd}" "${nf}" "${bytes}" "${owner_uid}" "${nown}" "${nrw}" "${natime}" "${nmtime}"

if [[ ${nown} -ne 0 || ${natime} -ne 0 || ${nmtime} -ne 0 ]]; then
    echo "PREFLIGHT FAILED for [${pair}] ${destdir}: no ${ok_file} written"
    exit 1
fi
{
    echo "pair: ${pair}"
    echo "dest: ${destdir}"
    echo "since: ${since_epoch}"
    echo "finished: $(date +%s)"
    echo "jobid: ${PBS_JOBID:-local}"
    echo "entries: ${n}"
} > "${ok_file}"
echo "preflight passed for [${pair}] ${destdir}: ${ok_file} written"
