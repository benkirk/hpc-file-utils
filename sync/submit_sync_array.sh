#!/bin/bash
#
# usage: ./submit_sync_array.sh              # job array over every pair in pairs.conf
#        ./submit_sync_array.sh <pair> ...   # job array over the named pairs (plain job for one)
#
# queue, walltime, nodes, chunk, maxconcur come from the top of pairs.conf
# (host-specific '${NCAR_HOST}.key' first); environment variables of the same name override.
#
# env:   rounds=N      submit the same job N times, each after the previous one ends [1];
#                      pairs with a valid done stamp exit at once, so rounds ratchet to completion
#        fresh_after=  done stamps from passes started before this ('date -d' string, e.g.
#                      "now" or "2026-11-03 08:00") don't count - resolved once, at submission
#        after=<jobid> start the first round only after that job (or job array 'NNN[]') ends
#        dryrun=yes, pairs_conf

. ./pairs.sh || exit 1

queue="${queue:-$(conf_get queue)}"
walltime="${walltime:-$(conf_get walltime)}"
nodes="${nodes:-$(conf_get nodes)}"
chunk="${chunk:-$(conf_get chunk)}"
maxconcur="${maxconcur:-$(conf_get maxconcur)}"

queue="${queue:-main}"
walltime="${walltime:-12:00:00}" # main queue limit
maxconcur="${maxconcur:-4}"
chunk="${chunk:-ncpus=128:mpiprocs=32:mem=235G}" # derecho node
rounds="${rounds:-1}"

# resolve fresh_after now, so every round compares stamps against the same time
fresh_after_epoch="$(fresh_after_resolve)" || exit 1
[[ ${fresh_after_epoch} -gt 0 ]] && echo "fresh_after: $(date -d "@${fresh_after_epoch}")"

qsub_args=(-A SCSG0001 -q "${queue}" -l "walltime=${walltime}")
# without nodes, the job script header default applies
[[ -n ${nodes} ]] && qsub_args+=(-l "select=${nodes}:${chunk}")

vars="pairs_conf=${pairs_conf},fresh_after_epoch=${fresh_after_epoch}"
[[ -n ${dryrun} ]] && vars="${vars},dryrun=${dryrun}"

# which pairs: the named ones, else all of pairs.conf - validated, and minus those
# marked 'done: yes' or already done (valid stamp). always passed explicitly as pair_list,
# so a later edit of pairs.conf can't shift which pair an array index runs.
[[ $# -gt 0 ]] && requested=("$@") || mapfile -t requested < <(pair_names)
pairs=()
for pair in "${requested[@]}"; do
    pair_load "${pair}" || exit 1
    if [[ ${pair_done} == yes ]]; then
        echo "[${pair}] marked 'done: yes' in ${pairs_conf}, not submitting it"
    elif [[ $(stamp_check "${pair}" "${fresh_after_epoch}") == valid ]]; then
        echo "[${pair}] already done (stamps/${pair}.done), not submitting it"
    else
        copier_host_check "${NCAR_HOST}" || exit 1
        pairs+=("${pair}")
    fi
done
[[ ${#pairs[@]} -gt 0 ]] || { echo "nothing to submit"; exit 0; }
vars="${vars},pair_list=$(IFS=:; echo "${pairs[*]}")"

if [[ ${#pairs[@]} -eq 1 ]]; then
    # PBS arrays need at least 2 members
    job_args=(-N "cgd_${pairs[0]}" -v "${vars},pair=${pairs[0]}")
else
    job_args=(-N cgd_migration -v "${vars}" -J "0-$(( ${#pairs[@]} - 1 ))%${maxconcur}")
fi

prev="${after:-}"
for round in $(seq 1 "${rounds}"); do
    dep=()
    [[ -n ${prev} ]] && dep=(-W "depend=afterany:${prev}")
    echo "round ${round}/${rounds}: qsub ${qsub_args[*]} ${dep[*]} ${job_args[*]} sync_dir_job_array.sh"
    prev="$(qsub "${qsub_args[@]}" "${dep[@]}" "${job_args[@]}" sync_dir_job_array.sh)" || exit 1
    echo "${prev}"
done
