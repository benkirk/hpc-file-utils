#!/usr/bin/env bash
#
# sync/pairs.sh + submit_sync_array.sh, laptop-safe: config parsing, the pair safety
# checks, stamp_check, fresh_after, 'done: yes' and the qsub command lines, with a
# stub qsub on PATH. Runs the real scripts (symlinked) in a scratch directory.

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sync_dir="$(dirname "${here}")"

(( BASH_VERSINFO[0] >= 4 )) || { echo "SKIP: pairs.sh needs bash >= 4"; exit 77; }
date -d @0 +%s >/dev/null 2>&1 || { echo "SKIP: pairs.sh needs GNU date (date -d)"; exit 77; }

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
cd "${work}" || exit 1
ln -s "${sync_dir}/pairs.sh" "${sync_dir}/submit_sync_array.sh" .

# stub qsub: log the arguments, print a job id
mkdir bin
cat > bin/qsub <<'EOF'
#!/usr/bin/env bash
n=$(( $(wc -l < "${QSUB_LOG}" 2>/dev/null || echo 0) + 1 ))
echo "$*" >> "${QSUB_LOG}"
# the variables a dest job reads from its environment (qsub -v), for submit_pair_job's checks
for v in confirm dryrun_rm pair preflight since; do
    [[ -v ${v} ]] && printf '%s=%s\n' "${v}" "${!v}"
done >> "${QSUB_LOG}.env"
echo "${n}000.desched1"
EOF
chmod +x bin/qsub
export PATH="${work}/bin:${PATH}" QSUB_LOG="${work}/qsub.log"
unset NCAR_HOST pair pair_list fresh_after after rounds dryrun queue walltime nodes chunk maxconcur

fails=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; fails=$((fails + 1)); }
expect_eq() { [[ $2 == "$3" ]] && pass "$1" || fail "$1: expected '$3', got '$2'"; }
expect_has() { [[ $2 == *"$3"* ]] && pass "$1" || fail "$1: '$3' not in: $2"; }
expect_lacks() { [[ $2 != *"$3"* ]] && pass "$1" || fail "$1: '$3' unexpectedly in: $2"; }

root="${work}/campaign"

write_conf() # <file>, extra global settings and sections on stdin
{
    {
        echo "allowed_roots: ${root}/"
        echo "queue:     main"
        echo "walltime:  12:00:00"
        echo "nodes:     4"
        echo "chunk:     ncpus=128:mpiprocs=32:mem=235G"
        echo "maxconcur: 4"
        echo "casper.queue: casper"
        echo "casper.chunk: ncpus=16:mpiprocs=16:mem=160G"
        cat
    } > "$1"
}

# run a pairs.sh function in a subshell with config $1: prints output, returns status
in_pairs() # <conf> <command...>
{
    local conf="$1"; shift
    pairs_conf="${conf}" bash -c '. ./pairs.sh || exit 1; '"$*" 2>&1
}

#--------------------------------------------------------------
# the good config
write_conf pairs.conf <<EOF
[A-first]
src:   ${root}/src/a///
dest:  ${root}/dest/a
hsm:   yes        # trailing comment
owner: someone:somegroup

[B-second]
src:   ${root}/src/b
dest:  ${root}/dest/b
chmod: Du+w

[C-finished]
src:   ${root}/src/c
dest:  ${root}/dest/c
done:  yes   # handed off
EOF

expect_eq "pair_names in order" "$(in_pairs pairs.conf pair_names | paste -s -d ' ')" "A-first B-second C-finished"
expect_eq "pair_count" "$(in_pairs pairs.conf pair_count)" "3"
expect_eq "pair_name_by_index 1" "$(in_pairs pairs.conf pair_name_by_index 1)" "B-second"
expect_eq "pair_get strips a trailing comment" "$(in_pairs pairs.conf pair_get A-first hsm)" "yes"
expect_eq "pair_get raw keeps it" "$(in_pairs pairs.conf pair_get A-first hsm raw)" "yes        # trailing comment"
expect_eq "pair_get of an unset key is empty" "$(in_pairs pairs.conf pair_get B-second owner)" ""
expect_eq "conf_get global" "$(in_pairs pairs.conf conf_get queue)" "main"
expect_eq "conf_get host-specific wins" "$(NCAR_HOST=casper in_pairs pairs.conf conf_get queue)" "casper"
expect_eq "conf_get falls back to global" "$(NCAR_HOST=casper in_pairs pairs.conf conf_get walltime)" "12:00:00"

out="$(in_pairs pairs.conf 'pair_load A-first && echo "[${srcdir}] [${destdir}] ${hsm} ${owner} ${pair_done}"')"
expect_eq "pair_load A: trailing slashes stripped, hsm normalized" "${out}" "[${root}/src/a] [${root}/dest/a] yes someone:somegroup no"
out="$(in_pairs pairs.conf 'pair_load C-finished && echo "${hsm} ${pair_done}"')"
expect_eq "pair_load C: done: yes" "${out}" "no yes"
expect_has "pairs_list" "$(in_pairs pairs.conf pairs_list)" "C-finished"

#--------------------------------------------------------------
# pair_load safety checks: each config holds one bad [X]
check_bad() # <description> <expected message> <section body...>
{
    local desc="$1" msg="$2"; shift 2
    printf '%s\n' "[X]" "$@" | write_conf bad.conf
    local out rc
    out="$(in_pairs bad.conf pair_load X)"; rc=$?
    if [[ ${rc} -ne 0 && ${out} == *"${msg}"* ]]; then pass "rejects ${desc}"
    else fail "rejects ${desc}: status ${rc}, output: ${out}"; fi
}
check_bad "a relative src"        "must be an absolute, normalized path" "src: campaign/src/x" "dest: ${root}/dest/x"
check_bad "'..' in dest"          "must be an absolute, normalized path" "src: ${root}/src/x" "dest: ${root}/dest/../x"
check_bad "'//' in src"           "must be an absolute, normalized path" "src: ${root}//src/x" "dest: ${root}/dest/x"
check_bad "'#' in src"            "'#' not allowed"                      "src: ${root}/src/x#1" "dest: ${root}/dest/x"
check_bad "src outside allowed_roots" "not under allowed_roots"          "src: /tmp/elsewhere" "dest: ${root}/dest/x"
check_bad "an allowed root itself" "not under allowed_roots"             "src: ${root}" "dest: ${root}/dest/x"
check_bad "nested src and dest"   "must differ and not be nested"        "src: ${root}/src/x" "dest: ${root}/src/x/copy"
check_bad "the same src and dest" "must differ and not be nested"        "src: ${root}/src/x" "dest: ${root}/src/x"
check_bad "no dest"               "has no dest"                          "src: ${root}/src/x"
check_bad "no src"                "has no src"                           "dest: ${root}/dest/x"
check_bad "an invalid hsm"        "invalid hsm"                          "src: ${root}/src/x" "dest: ${root}/dest/x" "hsm: maybe"
check_bad "an invalid done"       "invalid done"                         "src: ${root}/src/x" "dest: ${root}/dest/x" "done: soon"

printf '%s\n' "[X]" "src: ${root}/src/x" "dest: ${root}/dest/x" "[X]" "src: ${root}/src/y" "dest: ${root}/dest/y" \
    | write_conf dup.conf
expect_has "rejects a duplicate section" "$(in_pairs dup.conf pair_load X)" "defined more than once"
printf '%s\n' "[bad!name]" "src: ${root}/src/x" "dest: ${root}/dest/x" | write_conf name.conf
expect_has "rejects a bad section name" "$(in_pairs name.conf 'pair_load "bad!name"')" "pair names may only use"
expect_has "rejects an unknown pair" "$(in_pairs pairs.conf pair_load nope)" "no pair [nope]"

#--------------------------------------------------------------
# copier: dsync|walksync and its keys
check_bad "an invalid copier"     "invalid copier: 'rclone'"             "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: rclone"
check_bad "walksync with another chmod" "contradicts"                    "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: walksync" "chmod: D755"
check_bad "a quote in walksync_opts" "walksync_opts: "                   "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: walksync" "walksync_opts: --exclude 'x*'"
check_bad "a glob in walksync_bind" "walksync_bind: "                    "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: walksync" "walksync_bind: --cpu-bind *"
check_bad "--dest in walksync_opts" "--dest is set by the job"           "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: walksync" "walksync_opts: --rsyncs 4 --dest /tmp"
check_bad "--dry-run in walksync_opts" "--dry-run is set by the job"     "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: walksync" "walksync_opts: --dry-run"
check_bad "a relative hfu_root"   "must be an absolute path"             "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: walksync" "hfu_root: current-derecho"
# hfu_root and hfu_bind are read for every pair (walkfind runs the HSM preflight); walksync's keys only for walksync
check_bad "dsync: a relative hfu_root" "must be an absolute path"       "src: ${root}/src/x" "dest: ${root}/dest/x" "hfu_root: relative"
check_bad "dsync: a glob in hfu_bind" "hfu_bind: "                      "src: ${root}/src/x" "dest: ${root}/dest/x" "hfu_bind: --cpu-bind *"
printf '%s\n' "[X]" "src: ${root}/src/x" "dest: ${root}/dest/x" "walksync_bind: --cpu-bind *" | write_conf ok.conf
expect_eq "dsync pair: walksync keys ignored, hfu keys defaulted" \
          "$(in_pairs ok.conf 'pair_load X && echo "${copier} [${walksync_bind}] [${hfu_root}] [${hfu_bind}]"')" \
          "dsync [] [/glade/u/apps/opt/hpc-file-utils/current-derecho] [--cpu-bind depth --depth 4]"
expect_eq "casper: its own install, no binding" \
          "$(NCAR_HOST=casper in_pairs ok.conf 'pair_load X && echo "[${hfu_root}] [${hfu_bind}]"')" \
          "[/glade/u/apps/opt/hpc-file-utils/current-casper] []"
printf '%s\n' "casper.hfu_bind: --bind-to core" "[X]" "src: ${root}/src/x" "dest: ${root}/dest/x" | write_conf bind.conf
expect_eq "a host-specific hfu_bind" "$(NCAR_HOST=casper in_pairs bind.conf 'pair_load X && echo "[${hfu_bind}]"')" "[--bind-to core]"

write_conf copier.conf <<EOF
derecho.copier: walksync     # host-specific global
walksync_bind: --cpu-bind depth --depth 2

[W-plain]
src:   ${root}/src/w
dest:  ${root}/dest/w

[W-readonly]
src:   ${root}/src/r
dest:  ${root}/dest/r
copier: walksync     # per pair: on every host
chmod: Du+w
walksync_opts: --rsyncs 4 --batch-size 4GiB
hfu_root: /glade/u/apps/opt/hpc-file-utils/abc1234-derecho

[W-hsm]
src:   ${root}/src/h
dest:  ${root}/dest/h
hsm:   yes

[D-dsync]
src:   ${root}/src/d
dest:  ${root}/dest/d
copier: dsync       # per pair beats the global
chmod: D755
msrsync: yes
EOF
show='pair_load "$0" && echo "${copier}|${walksync_opts}|${walksync_bind}|${hfu_root}|$(msrsync_resolve)"'
copier_of() { pairs_conf=copier.conf bash -c '. ./pairs.sh || exit 1; '"${show}" "$1" 2>&1; }
expect_eq "copier: dsync by default" "$(copier_of W-plain)" "dsync|||/glade/u/apps/opt/hpc-file-utils/current-derecho|no"
export NCAR_HOST=derecho
expect_eq "copier: host-specific global, defaults" "$(copier_of W-plain)" \
          "walksync|--rsyncs 8|--cpu-bind depth --depth 2|/glade/u/apps/opt/hpc-file-utils/current-derecho|no"
expect_eq "copier: per-pair opts and hfu_root; chmod Du+w; no msrsync (auto)" "$(copier_of W-readonly)" \
          "walksync|--rsyncs 4 --batch-size 4GiB|--cpu-bind depth --depth 2|/glade/u/apps/opt/hpc-file-utils/abc1234-derecho|no"
expect_eq "copier: hsm pair, no msrsync (auto)" "$(copier_of W-hsm)" \
          "walksync|--rsyncs 8|--cpu-bind depth --depth 2|/glade/u/apps/opt/hpc-file-utils/current-derecho|no"
expect_eq "copier: per-pair dsync beats the global" "$(copier_of D-dsync)" "dsync|||/glade/u/apps/opt/hpc-file-utils/current-derecho|yes"
expect_has "pairs_list shows the copier" "$(in_pairs copier.conf pairs_list)" "copier=walksync"
export NCAR_HOST=casper
expect_eq "copier: derecho.copier does not apply on casper" "$(copier_of W-plain)" "dsync|||/glade/u/apps/opt/hpc-file-utils/current-casper|no"
unset NCAR_HOST

# walksync_inplace: auto|yes|no, and --inplace merged into walksync's --rsync-opts
check_bad "an invalid walksync_inplace" "invalid walksync_inplace: 'maybe'" "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: walksync" "walksync_inplace: maybe"
check_bad "--rsync-opts as two words" "give --rsync-opts=<option> as one word" "src: ${root}/src/x" "dest: ${root}/dest/x" "copier: walksync" "walksync_opts: --rsync-opts --bwlimit=1000"
inplace_of() # <conf> <pair> <dest empty> : resolved inplace, then walksync_args, one line
{
    pairs_conf="$1" bash -c '. ./pairs.sh || exit 1; pair_load "$0" || exit 1; i="$(walksync_inplace_resolve "$1")"
                             echo "${i}: $(walksync_args "${i}" | paste -sd "|")"' "$2" "$3" 2>&1
}
expect_eq "inplace auto, dest empty" "$(NCAR_HOST=derecho inplace_of copier.conf W-plain yes)" "yes: --rsyncs|8|--rsync-opts=--inplace"
expect_eq "inplace auto, dest not empty" "$(NCAR_HOST=derecho inplace_of copier.conf W-plain no)" "no: --rsyncs|8"
printf '%s\n' "copier: walksync" \
    "[Y]" "src: ${root}/src/y" "dest: ${root}/dest/y" "walksync_inplace: yes" "walksync_opts: --rsync-opts=--bwlimit=1000 --rsyncs 4" \
    "[N]" "src: ${root}/src/n" "dest: ${root}/dest/n" "walksync_inplace: no" | write_conf inplace.conf
expect_eq "inplace yes: merged into --rsync-opts=" "$(inplace_of inplace.conf Y no)" "yes: --rsync-opts=--bwlimit=1000 --inplace|--rsyncs|4"
expect_eq "inplace no, dest empty" "$(inplace_of inplace.conf N yes)" "no: --rsyncs|8"

# msrsync auto after dsync: hsm and chmod pairs only (A-first is hsm, B-second chmod)
expect_eq "msrsync auto: hsm dsync pair" "$(in_pairs pairs.conf 'pair_load A-first && msrsync_resolve')" "yes"
expect_eq "msrsync auto: chmod dsync pair" "$(in_pairs pairs.conf 'pair_load B-second && msrsync_resolve')" "yes"
expect_eq "msrsync auto: plain dsync pair" "$(in_pairs pairs.conf 'pair_load C-finished && msrsync_resolve')" "no"
printf '%s\n' "msrsync: sometimes" "[X]" "src: ${root}/src/x" "dest: ${root}/dest/x" | write_conf ms.conf
expect_has "msrsync: an invalid setting" "$(in_pairs ms.conf 'pair_load X && msrsync_resolve')" "invalid msrsync setting 'sometimes'"

# walksync on casper (conda-forge Open MPI): no PALS binding words by default
printf '%s\n' "copier: walksync" "[X]" "src: ${root}/src/x" "dest: ${root}/dest/x" | write_conf wscasper.conf
expect_eq "walksync on casper: binds nothing by default" \
    "$(NCAR_HOST=casper in_pairs wscasper.conf 'pair_load X && echo "${copier}|${walksync_bind}|${hfu_root}"')" \
    "walksync|--bind-to none|/glade/u/apps/opt/hpc-file-utils/current-casper"
expect_eq "walksync on derecho: PALS depth binding by default" \
    "$(NCAR_HOST=derecho in_pairs wscasper.conf 'pair_load X && echo "${walksync_bind}"')" "--cpu-bind depth --depth 4"
printf '%s\n' "copier: walksync" "casper.walksync_bind: --bind-to socket" "[X]" "src: ${root}/src/x" "dest: ${root}/dest/x" | write_conf wsbind.conf
expect_eq "walksync on casper: a host-specific binding" \
    "$(NCAR_HOST=casper in_pairs wsbind.conf 'pair_load X && echo "${walksync_bind}"')" "--bind-to socket"

#--------------------------------------------------------------
# done stamps
now=$(date +%s)
write_stamp() # <pair> <src> <dest> <started>
{
    mkdir -p stamps
    printf 'pair: %s\nsrc: %s\ndest: %s\nstarted: %s\nfinished: %s\njobid: 1.desched1\n' \
           "$1" "$2" "$3" "$4" "$((now + 60))" > "stamps/$1.done"
}
expect_eq "stamp_check without a stamp" "$(in_pairs pairs.conf stamp_check A-first 0)" "none"
write_stamp A-first "${root}/src/a" "${root}/dest/a" "${now}"
expect_eq "stamp_check matching stamp" "$(in_pairs pairs.conf stamp_check A-first 0)" "valid"
expect_eq "stamp_check started == fresh_after" "$(in_pairs pairs.conf stamp_check A-first "${now}")" "valid"
expect_eq "stamp_check started before fresh_after" "$(in_pairs pairs.conf stamp_check A-first $(( now + 1 )))" "stale"
expect_eq "stamp_get" "$(in_pairs pairs.conf stamp_get A-first jobid)" "1.desched1"
write_stamp B-second "${root}/src/b" "${root}/dest/OTHER" "${now}"
expect_eq "stamp_check other dest" "$(in_pairs pairs.conf stamp_check B-second 0)" "mismatch"
write_stamp B-second "${root}/src/b" "${root}/dest/b" "yesterday"
expect_eq "stamp_check non-numeric started" "$(in_pairs pairs.conf stamp_check B-second 0)" "mismatch"
rm -f stamps/B-second.done

expect_eq "fresh_after unset" "$(in_pairs pairs.conf fresh_after_resolve)" "0"
expect_eq "fresh_after from the environment" "$(fresh_after="2026-11-03 08:00 UTC" in_pairs pairs.conf fresh_after_resolve)" \
          "$(date -d "2026-11-03 08:00 UTC" +%s)"
printf '%s\n' "fresh_after: 2026-11-03 08:00 UTC" | write_conf fa.conf
expect_eq "fresh_after from the config" "$(in_pairs fa.conf fresh_after_resolve)" "$(date -d "2026-11-03 08:00 UTC" +%s)"
in_pairs pairs.conf 'fresh_after="not a date" fresh_after_resolve' >/dev/null \
    && fail "fresh_after: an unparsable date fails" || pass "fresh_after: an unparsable date fails"

#--------------------------------------------------------------
# submit_sync_array.sh with the stub qsub
# (run with the PATH bash: its #!/bin/bash is 3.2 on macOS, which has no mapfile)
submit() # [VAR=value ...] [pairs...] : runs submit_sync_array.sh (pairs_conf=pairs.conf unless given), sets out, rc, qsubs
{
    local vars=()
    while [[ $# -gt 0 && $1 == *=* ]]; do vars+=("$1"); shift; done
    rm -f "${QSUB_LOG}"
    out="$(env pairs_conf=pairs.conf "${vars[@]}" bash ./submit_sync_array.sh "$@" 2>&1)"; rc=$?
    qsubs="$(cat "${QSUB_LOG}" 2>/dev/null)"
}

# A-first has a valid stamp, C-finished is done: yes -> only B-second
submit
expect_eq "submit all: exit status" "${rc}" "0"
expect_has "submit all: skips a valid stamp" "${out}" "[A-first] already done"
expect_has "submit all: skips done: yes" "${out}" "[C-finished] marked 'done: yes'"
expect_eq "submit all: one qsub" "$(wc -l <<< "${qsubs}" | tr -d ' ')" "1"
expect_has "submit all: a plain job for one pair" "${qsubs}" "-N cgd_B-second"
expect_has "submit all: pair=" "${qsubs}" "pair_list=B-second,pair=B-second"
expect_lacks "submit all: no array for one pair" "${qsubs}" " -J "
expect_has "submit all: account and queue" "${qsubs}" "-A SCSG0001 -q main -l walltime=12:00:00"
expect_has "submit all: select" "${qsubs}" "-l select=4:ncpus=128:mpiprocs=32:mem=235G"
expect_has "submit all: fresh_after_epoch=0" "${qsubs}" "fresh_after_epoch=0"

# a future fresh_after makes A-first's stamp stale: re-run it, as an array with B-second
submit fresh_after=tomorrow
expect_has "fresh_after in the future: A-first resubmitted" "${qsubs}" "pair_list=A-first:B-second"
expect_has "fresh_after in the future: array of 2" "${qsubs}" "-J 0-1%4"
expect_has "fresh_after in the future: -N cgd_migration" "${qsubs}" "-N cgd_migration"
expect_has "fresh_after in the future: epoch passed on" "${qsubs}" "fresh_after_epoch=$(date -d tomorrow +%s | cut -c1-6)"

# done: yes wins over fresh_after, even when named
submit fresh_after=tomorrow C-finished
expect_eq "named done: yes pair: exit status" "${rc}" "0"
expect_has "named done: yes pair: nothing to submit" "${out}" "nothing to submit"
expect_eq "named done: yes pair: no qsub" "${qsubs}" ""

# rounds chain on the previous job id; after= chains the first round
submit rounds=2 after=42.desched1 B-second
expect_eq "rounds=2: two qsubs" "$(wc -l <<< "${qsubs}" | tr -d ' ')" "2"
expect_has "rounds=2: first round after=" "$(head -n 1 <<< "${qsubs}")" "-W depend=afterany:42.desched1"
expect_has "rounds=2: second round after the first" "$(tail -n 1 <<< "${qsubs}")" "-W depend=afterany:1000.desched1"

# host-specific settings and environment overrides
submit NCAR_HOST=casper nodes=2 dryrun=yes B-second
expect_has "casper: queue" "${qsubs}" "-q casper"
expect_has "casper: chunk, nodes from the environment" "${qsubs}" "-l select=2:ncpus=16:mpiprocs=16:mem=160G"
expect_has "dryrun passed on" "${qsubs}" "dryrun=yes"

# copier: walksync - submitted from either host (casper since its conda-forge Open MPI)
submit NCAR_HOST=derecho pairs_conf=copier.conf W-readonly
expect_eq "walksync on derecho: exit status" "${rc}" "0"
expect_has "walksync on derecho: submitted" "${qsubs}" "-N cgd_W-readonly"
submit NCAR_HOST=casper pairs_conf=copier.conf W-readonly
expect_eq "walksync on casper: exit status" "${rc}" "0"
expect_has "walksync on casper: submitted" "${qsubs}" "-N cgd_W-readonly"
submit NCAR_HOST=casper pairs_conf=copier.conf D-dsync
expect_has "dsync on casper: submitted" "${qsubs}" "-N cgd_D-dsync"

# an invalid pair: error, nothing submitted
submit nope
expect_eq "unknown pair: exit status" "${rc}" "1"
expect_eq "unknown pair: no qsub" "${qsubs}" ""

#--------------------------------------------------------------
# submit_pair_job: the dest jobs (make preflight, make delete_dest) on this host's queue and chunk
pair_job() # <env...> -- <submit_pair_job args...> : sets out, rc, qsubs, qenv
{
    local envs=()
    while [[ $1 != -- ]]; do envs+=("$1"); shift; done
    shift
    rm -f "${QSUB_LOG}" "${QSUB_LOG}.env"
    out="$(env "${envs[@]}" bash -c '. ./pairs.sh || exit 1; pair_load B-second || exit 1; export since="2026-11-06 18:00"; submit_pair_job "$@"' _ "$@" 2>&1)"
    rc=$?
    qsubs="$(cat "${QSUB_LOG}" 2>/dev/null)"
    qenv="$(paste -sd ' ' "${QSUB_LOG}.env" 2>/dev/null)"
}
pair_job -- dest_preflight.sh 1 "" since
expect_eq "submit_pair_job: queue, chunk, variables" "${qsubs}" "-q main -l select=1:ncpus=128:mpiprocs=32:mem=235G -v pair,since dest_preflight.sh"
expect_eq "submit_pair_job: exported to qsub" "${qenv}" "pair=B-second since=2026-11-06 18:00"
pair_job NCAR_HOST=casper -- delete_dest.sh 2 1234.desched1
expect_eq "submit_pair_job: casper, after a job" "${qsubs}" "-q casper -l select=2:ncpus=16:mpiprocs=16:mem=160G -v pair -W depend=afterany:1234.desched1 delete_dest.sh"

#--------------------------------------------------------------
# delete_dest_check: the guards before 'make delete_dest' removes a pair's dest
mkdir -p "${root}/dest/b" "${root}/dest/real" "${work}/elsewhere/x"
ln -s "${root}/dest/real" "${root}/dest/link"
ln -s "${work}/elsewhere" "${root}/dest/out"
printf '%s\n' "[L]" "src: ${root}/src/l" "dest: ${root}/dest/link" "[O]" "src: ${root}/src/o" "dest: ${root}/dest/out/x" \
    | write_conf deletes.conf
sed -n '/^\[A-first\]/,$p' pairs.conf >> deletes.conf     # B-second (dest ${root}/dest/b) too
write_ok() # <pair> <dest> <finished epoch>
{
    printf 'pair: %s\ndest: %s\nsince: 1\nfinished: %s\njobid: 1.desched1\nentries: 3\n' "$1" "$2" "$3" > "preflight.$1.ok"
}
check_delete() # <pair> <confirm> <preflight> : sets out, rc
{
    out="$(pairs_conf=deletes.conf bash -c '. ./pairs.sh || exit 1; pair_load "$0" || exit 1; delete_dest_check "$1" "$2"' "$@" 2>&1)"
    rc=$?
}
now=$(date +%s)
check_delete B-second "" ""
expect_has "delete: CONFIRM required" "${out}" "give CONFIRM=<dest>"
check_delete B-second "${root}/dest/c" ""
expect_has "delete: CONFIRM must be the dest" "${out}" "is not the pair's dest"
check_delete B-second "${root}/dest/b/" ""
expect_has "delete: CONFIRM exactly (no trailing slash)" "${out}" "is not the pair's dest"
check_delete L "${root}/dest/link" "skip"
expect_has "delete: a symlinked dest" "${out}" "not a directory, or a symlink"
check_delete O "${root}/dest/out/x" "skip"
expect_has "delete: a dest resolving outside allowed_roots" "${out}" "not under allowed_roots"
check_delete B-second "${root}/dest/b" ""
expect_has "delete: no preflight" "${out}" "run 'make preflight PAIR=B-second' first"
write_ok B-second "${root}/dest/a" "${now}"
check_delete B-second "${root}/dest/b" ""
expect_has "delete: a preflight for another dest" "${out}" "is for '${root}/dest/a'"
write_ok B-second "${root}/dest/b" "$(( now - 8 * 86400 ))"
check_delete B-second "${root}/dest/b" ""
expect_has "delete: a stale preflight" "${out}" "older than 7 days"
check_delete B-second "${root}/dest/b" "later"
expect_has "delete: PREFLIGHT takes only skip" "${out}" "only 'skip' overrides"
write_ok B-second "${root}/dest/b" "${now}"
check_delete B-second "${root}/dest/b" ""
expect_eq "delete: a passing preflight" "${rc}:${out}" "0:"
write_ok B-second "${root}/dest/a" "${now}"           # only skip gets past a wrong preflight
check_delete B-second "${root}/dest/b" "skip"
expect_eq "delete: PREFLIGHT=skip passes, loudly" "${rc}" "0"
expect_has "delete: PREFLIGHT=skip warns" "${out}" "without a passing preflight"
# the sync lock: a HOME of the test's own, so a real lock is never touched
lock="$(HOME="${work}" pairs_conf=deletes.conf bash -c '. ./pairs.sh && pair_load B-second && sync_lock')"
expect_has "sync_lock: in HOME, per pair" "${lock}" "${work}/.sync_dir."
touch "${lock}"
out="$(HOME="${work}" pairs_conf=deletes.conf bash -c '. ./pairs.sh && pair_load B-second && delete_dest_check "$0" skip' "${root}/dest/b" 2>&1)"
expect_has "delete: refused while the sync lock is held" "${out}" "sync lock"

#--------------------------------------------------------------
# the cmp sample: verify_max_bytes, and verify_cmp's whole-file and windowed compares
expect_eq "verify_max_bytes: default 16G" "$(in_pairs pairs.conf 'pair_load B-second && verify_max_resolve')" "17179869184"
printf '%s\n' "verify_max_bytes: 1M" "[X]" "src: ${root}/src/x" "dest: ${root}/dest/x" | write_conf vmax.conf
expect_eq "verify_max_bytes: 1M" "$(in_pairs vmax.conf 'pair_load X && verify_max_resolve')" "1048576"
printf '%s\n' "[X]" "src: ${root}/src/x" "dest: ${root}/dest/x" "verify_max_bytes: lots" | write_conf vbad.conf
expect_has "verify_max_bytes: refused" "$(in_pairs vbad.conf 'pair_load X && verify_max_resolve')" "invalid verify_max_bytes"
mkdir -p vc
head -c 3000 /dev/urandom > vc/small && cp vc/small vc/small.copy
head -c 100000 /dev/urandom > vc/big && cp vc/big vc/big.copy
poke() # <file> <offset> : change one byte in place
{
    local b
    b="$(dd if="$1" bs=1 skip="$2" count=1 2>/dev/null | od -An -tu1 | tr -d ' ')"
    printf "\\$(printf '%03o' $(( (b + 1) % 256 )))" | dd of="$1" bs=1 seek="$2" conv=notrunc 2>/dev/null
}
vcmp() # <src> <dest> : verify_cmp with a 4 KiB cap and 512-byte windows
{
    VERIFY_WINDOW=512 VERIFY_WINDOWS=4 bash -c '. ./pairs.sh >/dev/null 2>&1; verify_cmp "$0" "$1" 4096' "$1" "$2" 2>&1
}
expect_eq "verify_cmp: small, equal" "$(vcmp vc/small vc/small.copy)" ""
poke vc/small.copy 1500
expect_eq "verify_cmp: small, one byte differs" "$(vcmp vc/small vc/small.copy)" "MISMATCH vc/small.copy"
expect_eq "verify_cmp: big, equal (windows)" "$(vcmp vc/big vc/big.copy)" ""
cp vc/big vc/big.head && poke vc/big.head 100
expect_eq "verify_cmp: big, the head differs" "$(vcmp vc/big vc/big.head)" "MISMATCH vc/big.head"
cp vc/big vc/big.tail && poke vc/big.tail 99999
expect_eq "verify_cmp: big, the last byte differs" "$(vcmp vc/big vc/big.tail)" "MISMATCH vc/big.tail"
head -c 99999 vc/big > vc/big.short
expect_eq "verify_cmp: sizes differ" "$(vcmp vc/big vc/big.short)" "MISMATCH vc/big.short"
expect_eq "verify_cmp: no source" "$(vcmp vc/missing vc/big)" "MISMATCH vc/big"

echo
if [[ ${fails} -gt 0 ]]; then
    echo "${fails} check(s) failed"
    exit 1
fi
echo "all checks passed"
