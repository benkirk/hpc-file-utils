#!/bin/bash
# helpers for parsing pairs.conf - source this file.
# override the config with: pairs_conf=/path/to/other.conf

pairs_conf="${pairs_conf:-pairs.conf}"

[[ -f ${pairs_conf} ]] || { echo "${pairs_conf}: no such file!" >&2; return 1 2>/dev/null || exit 1; }


#--------------------------------------------------------------
# list the section names, in order
pair_names()
{
    awk '/^[[:space:]]*\[.*\][[:space:]]*$/ { gsub(/[][[:space:]]/, ""); print }' "${pairs_conf}"
}


#--------------------------------------------------------------
pair_count()
{
    pair_names | wc -l | tr -d ' '
}


#--------------------------------------------------------------
# pair_name_by_index <0-based index>
pair_name_by_index()
{
    pair_names | sed -n "$(($1 + 1))p"
}


#--------------------------------------------------------------
# pair_get <name> <key> [raw] : print the value (empty if unset); 'raw' keeps any
# trailing ' # comment' (used to reject '#' in paths, which the strip would truncate)
pair_get()
{
    awk -v want="$1" -v key="$2" -v raw="${3:-}" '
        /^[[:space:]]*(#|$)/ { next }
        /^[[:space:]]*\[.*\][[:space:]]*$/ { sec = $0; gsub(/[][[:space:]]/, "", sec); next }
        sec == want {
            i = index($0, ":"); if (i == 0) next
            k = substr($0, 1, i-1);  gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
            v = substr($0, i+1);     if (raw == "") sub(/[[:space:]]+#.*$/, "", v)   # trailing comment
                                     gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
            if (k == key) { print v; exit }
        }' "${pairs_conf}"
}


#--------------------------------------------------------------
# conf_get <key> : global setting, i.e. "key: value" before the first [section].
# a host-specific "${NCAR_HOST}.key: value" (e.g. casper.queue) takes precedence.
conf_get()
{
    local v=""
    [[ -n ${NCAR_HOST} ]] && v="$(pair_get "" "${NCAR_HOST}.$1")"
    [[ -n ${v} ]] || v="$(pair_get "" "$1")"
    echo "${v}"
}


#--------------------------------------------------------------
# setting <key> [default] : the tunable for ${pair}: its per-pair value, else the
# global one (host-specific first), else the default
setting()
{
    local v
    v="$(pair_get "${pair}" "$1")"
    [[ -n ${v} ]] || v="$(conf_get "$1")"
    echo "${v:-$2}"
}


#--------------------------------------------------------------
# copier_load : sets copier, pair_chmod and, for walksync, walksync_opts, walksync_bind,
# hfu_root (validated). called by pair_load, with ${pair}, ${hsm} set.
#   copier: dsync|walksync [dsync]   walksync replaces dsync and msrsync (auto) as the
#                                    bulk copy; the final serial rsync --delete stays
copier_load()
{
    copier="$(setting copier dsync)"
    pair_chmod="$(pair_get "${pair}" chmod)"
    walksync_opts="" walksync_bind="" hfu_root="" walksync_inplace=""
    case "${copier}" in
        dsync) return 0 ;;
        walksync) ;;
        *) echo "ERROR: [${pair}] invalid copier: '${copier}' (dsync|walksync)" >&2; return 1 ;;
    esac

    # walksync always copies with --chmod=Du+w (read-only sources, parents several ranks
    # create); a pair may ask for nothing else
    [[ -z ${pair_chmod} || ${pair_chmod} == "Du+w" ]] \
        || { echo "ERROR: [${pair}] copier: walksync copies with --chmod=Du+w, which chmod: '${pair_chmod}' contradicts" >&2; return 1; }

    # (for the job script; walksync_bind is also read below, through ${!k})
    # shellcheck disable=SC2034
    walksync_opts="$(setting walksync_opts "--rsyncs 8")"
    # shellcheck disable=SC2034
    walksync_bind="$(setting walksync_bind "--cpu-bind depth --depth 4")"
    hfu_root="$(setting hfu_root /glade/u/apps/opt/hpc-file-utils/current-derecho)"

    # both are split on blanks into words, never through a shell: no quotes, no globs
    local k v
    for k in walksync_opts walksync_bind; do
        v="${!k}"
        [[ ${v} =~ ^[-A-Za-z0-9_.,=:/@+%[:space:]]*$ ]] \
            || { echo "ERROR: [${pair}] ${k}: '${v}': only letters, digits, blanks and -_.,=:/@+% (split on blanks, no quoting)" >&2; return 1; }
    done
    # the stage sets these itself
    for v in ${walksync_opts}; do
        case "${v}" in
            --dirs|--dirs=*|--dest|--dest=*|-n|--dry-run|--summary-prefix|--summary-prefix=*)
                echo "ERROR: [${pair}] walksync_opts: ${v} is set by the job (src, dest, pair name, dryrun=yes)" >&2; return 1 ;;
            # one word, so walksync_args can add --inplace to it
            --rsync-opts)
                echo "ERROR: [${pair}] walksync_opts: give --rsync-opts=<option> as one word" >&2; return 1 ;;
        esac
    done
    [[ ${hfu_root} =~ ^/[-A-Za-z0-9_./]+$ ]] \
        || { echo "ERROR: [${pair}] hfu_root: '${hfu_root}' must be an absolute path" >&2; return 1; }

    # walksync_inplace: auto|yes|no [auto]: rsync --inplace for walksync's copies (no temp
    # file and rename per file); auto = only when dest is empty at job start, so every file
    # is new and nothing is overwritten in place
    walksync_inplace="$(setting walksync_inplace auto)"
    case "${walksync_inplace}" in
        auto|yes|no) ;;
        *) echo "ERROR: [${pair}] invalid walksync_inplace: '${walksync_inplace}' (auto|yes|no)" >&2; return 1 ;;
    esac
}


#--------------------------------------------------------------
# walksync_inplace_resolve <dest is empty: yes|no> : prints yes|no; call after pair_load
walksync_inplace_resolve()
{
    case "${walksync_inplace}" in
        yes|no) echo "${walksync_inplace}" ;;
        *) echo "$1" ;;
    esac
}


#--------------------------------------------------------------
# walksync_args <inplace: yes|no> : walksync_opts as one argument per line, with --inplace
# added to its --rsync-opts= (or as --rsync-opts=--inplace) when asked; call after pair_load
walksync_args()
{
    local v merged=no
    for v in ${walksync_opts}; do
        if [[ $1 == yes && ${v} == --rsync-opts=* ]]; then
            v="${v} --inplace"; merged=yes
        fi
        printf '%s\n' "${v}"
    done
    [[ $1 == yes && ${merged} == no ]] && printf '%s\n' "--rsync-opts=--inplace"
    return 0
}


#--------------------------------------------------------------
# copier_host_check <host> : copier: walksync runs on derecho only - casper's Open MPI
# launcher (PRRTE) segfaults now and then. call after pair_load.
copier_host_check()
{
    if [[ ${copier} == walksync && $1 == casper ]]; then
        echo "ERROR: [${pair}] copier: walksync is not supported on casper (its MPI launcher segfaults now and then): run it from derecho" >&2
        return 1
    fi
}


#--------------------------------------------------------------
# msrsync_resolve : prints yes|no, from the 'msrsync' tunable [auto]; call after pair_load.
#   auto = hsm and chmod pairs, which dsync can't copy - but never after walksync, which
#   copies those itself
msrsync_resolve()
{
    local m
    m="$(setting msrsync auto)"
    case "${m}" in
        auto) [[ ${copier} != walksync && ( ${hsm} == "yes" || -n ${pair_chmod} ) ]] && echo yes || echo no ;;
        yes|no) echo "${m}" ;;
        *) echo "ERROR: [${pair}] invalid msrsync setting '${m}' (auto|yes|no)" >&2; return 1 ;;
    esac
}


#--------------------------------------------------------------
# pair_load <name> : sets pair, srcdir, destdir, hsm, owner, pair_done and copier_load's
# copier, pair_chmod, walksync_opts, walksync_bind, hfu_root (validated)
pair_load()
{
    pair="$1"
    [[ -n ${pair} ]] || { echo "ERROR: no pair specified!" >&2; return 1; }
    pair_names | grep -qxF -- "${pair}" || { echo "ERROR: no pair [${pair}] in ${pairs_conf}!" >&2; return 1; }

    [[ ${pair} =~ ^[A-Za-z0-9._-]+$ ]] \
        || { echo "ERROR: [${pair}]: pair names may only use A-Z a-z 0-9 . _ -" >&2; return 1; }
    [[ $(pair_names | grep -cxF -- "${pair}") -eq 1 ]] \
        || { echo "ERROR: [${pair}] is defined more than once in ${pairs_conf}!" >&2; return 1; }

    srcdir="$(pair_get "${pair}" src)"
    destdir="$(pair_get "${pair}" dest)"
    hsm="$(pair_get "${pair}" hsm)"
    owner="$(pair_get "${pair}" owner)"
    pair_done="$(pair_get "${pair}" "done")"

    [[ -n ${srcdir} ]]  || { echo "ERROR: [${pair}] has no src!" >&2;  return 1; }
    [[ -n ${destdir} ]] || { echo "ERROR: [${pair}] has no dest!" >&2; return 1; }

    # path sanity - the serial rsync --delete makes dest match src exactly.
    local p raw root ok roots
    for p in src dest; do
        raw="$(pair_get "${pair}" ${p} raw)"
        [[ ${raw} != *"#"* ]] \
            || { echo "ERROR: [${pair}] ${p}: '#' not allowed (no comments on src/dest lines)" >&2; return 1; }
    done
    # strip all trailing slashes, we add them explicitly
    while [[ ${srcdir}  == */ ]]; do srcdir="${srcdir%/}";   done
    while [[ ${destdir} == */ ]]; do destdir="${destdir%/}"; done
    for p in "${srcdir}" "${destdir}"; do
        [[ ${p} == /* && ${p} != *//* && ${p} != */./* && ${p} != */../* && ${p} != */. && ${p} != */.. ]] \
            || { echo "ERROR: [${pair}] ${p}: must be an absolute, normalized path" >&2; return 1; }
        # allowed_roots: global, space-separated [/glade/campaign/]; a root itself is not allowed
        roots="$(conf_get allowed_roots)"
        ok=no
        for root in ${roots:-/glade/campaign/}; do
            [[ ${p}/ == "${root%/}"/?* ]] && ok=yes
        done
        [[ ${ok} == yes ]] \
            || { echo "ERROR: [${pair}] ${p}: not under allowed_roots (${pairs_conf})" >&2; return 1; }
    done
    if [[ ${srcdir}/ == "${destdir}"/* || ${destdir}/ == "${srcdir}"/* ]]; then
        echo "ERROR: [${pair}] src and dest must differ and not be nested: ${srcdir} vs ${destdir}" >&2
        return 1
    fi

    case "${hsm}" in
        yes|true|1) hsm="yes" ;;
        ""|no|false|0) hsm="no" ;;
        *) echo "ERROR: [${pair}] invalid hsm: '${hsm}'" >&2; return 1 ;;
    esac

    # done: yes -> finished for good (e.g. dest no longer writable), never submitted
    case "${pair_done}" in
        yes|true|1) pair_done="yes" ;;
        ""|no|false|0) pair_done="no" ;;
        *) echo "ERROR: [${pair}] invalid done: '${pair_done}'" >&2; return 1 ;;
    esac

    copier_load
}


#--------------------------------------------------------------
# pairs_list : human-readable summary of the parsed config
pairs_list()
{
    local idx=0 name key
    echo "# ${pairs_conf}: $(pair_count) pairs"
    echo "# settings for ${NCAR_HOST:-(unknown host)}:"
    for key in queue walltime nodes chunk maxconcur copier batch_files msrsync msrsync_procs chmod \
               walksync_opts walksync_bind hfu_root; do
        echo "#   ${key}: $(conf_get ${key})"
    done
    for name in $(pair_names); do
        pair_load "${name}" || return 1
        printf "%2d  %-22s hsm=%-3s copier=%-8s owner=%s%s\n      %s\n   -> %s\n" \
               ${idx} "${pair}" "${hsm}" "${copier}" "${owner:-(none)}" "$( [[ ${pair_done} == yes ]] && echo " done")" \
               "${srcdir}" "${destdir}"
        idx=$((idx + 1))
    done
}


#--------------------------------------------------------------
# done stamps: stamps/<pair>.done, written by the sync job when its final
# rsync --delete exits 0. "key: value" lines (pair, src, dest, started, ...).
stamp_file()
{
    echo "stamps/$1.done"
}

# stamp_get <pair> <key>
stamp_get()
{
    local f
    f="$(stamp_file "$1")"
    [[ -f ${f} ]] || return 0
    awk -v key="$2" '{ i = index($0, ":"); if (i && substr($0, 1, i-1) == key) {
                           v = substr($0, i+1); gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); print v; exit } }' "${f}"
}

# fresh_after_resolve : epoch of ${fresh_after} (env) or the 'fresh_after:' setting
# (any 'date -d' string, e.g. "now", "2026-11-03 08:00"); 0 when unset.
fresh_after_resolve()
{
    local f="${fresh_after:-$(conf_get fresh_after)}"
    [[ -n ${f} ]] || { echo 0; return 0; }
    date -d "${f}" +%s || { echo "ERROR: cannot parse fresh_after '${f}'" >&2; return 1; }
}

# stamp_check <pair> <fresh_after_epoch> : prints none|mismatch|stale|valid
#   valid = stamp's src/dest match the current config, and its pass started at or
#           after fresh_after_epoch
stamp_check()
{
    [[ -f $(stamp_file "$1") ]] || { echo none; return 0; }
    local src dest
    src="$(pair_get "$1" src)";   while [[ ${src}  == */ ]]; do src="${src%/}";   done
    dest="$(pair_get "$1" dest)"; while [[ ${dest} == */ ]]; do dest="${dest%/}"; done
    local started
    started="$(stamp_get "$1" started)"
    if [[ $(stamp_get "$1" src) != "${src}" || $(stamp_get "$1" dest) != "${dest}" || ! ${started} =~ ^[0-9]+$ ]]; then
        echo mismatch
    elif [[ ${started} -ge ${2:-0} ]]; then
        echo valid
    else
        echo stale
    fi
}


#--------------------------------------------------------------
# pairs_status [brief] : stamp state of every pair, and the last stage line of its newest
# job log; 'brief' (for big split configs) prints the counts and only the pairs not yet done
pairs_status()
{
    local name state log fa brief="$1"
    local -A count=()
    fa="$(fresh_after_resolve)" || return 1
    echo "# ${pairs_conf}: $(pair_count) pairs; fresh_after: $( [[ ${fa} -gt 0 ]] && date -d "@${fa}" || echo "(unset)")"
    for name in $(pair_names); do
        pair_load "${name}" || return 1
        state="$(stamp_check "${name}" "${fa}")"
        [[ ${pair_done} == yes ]] && count[done]=$(( ${count[done]:-0} + 1 )) || count[${state}]=$(( ${count[${state}]:-0} + 1 ))
        [[ -n ${brief} && ( ${pair_done} == yes || ${state} == valid ) ]] && continue
        # 'done: yes' pairs are never submitted again; their stamp is shown for the record
        if [[ ${pair_done} == yes ]]; then
            printf "%-24s %-8s" "${name}" "done"
            [[ ${state} == none ]] || printf " (stamp %s)" "${state}"
        else
            printf "%-24s %-8s" "${name}" "${state}"
        fi
        [[ ${state} == none ]] \
            || printf " finished %s, job %s" "$(date -d "@$(stamp_get "${name}" finished)" "+%F %H:%M")" "$(stamp_get "${name}" jobid)"
        echo
        log="$(grep -l "^Synchronizing \[${name}\]" cgd_*.o* 2>/dev/null | xargs -r ls -t 2>/dev/null | head -n 1)"
        if [[ -n ${log} ]]; then
            echo "    ${log}: $(grep -E '^\.\.\.|ERROR|^Done|nothing to do' "${log}" | tail -n 1 | cut -c1-110)"
        fi
    done
    echo "# totals:$(for state in "${!count[@]}"; do printf ' %s %s' "${count[${state}]}" "${state}"; done)"
}
