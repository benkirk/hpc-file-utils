#!/bin/bash
#
# usage: ./make_root_commands.sh [--chown|--acl] [<pair> ...]
#
# Write a script of commands to hand to the group with root access. We do not run these.
# Every pair goes into exactly one of the two final hand-off flavours; each script lists
# the pairs it skipped and why.
#
#   --acl    root_acl_commands.<timestamp>.sh, BEFORE the copy: read access to each src for
#            ${USER} and csgteam, access + default ACLs in one recursive pass (the default
#            ACL covers what is added before the final sync). One setfacl per pair, so
#            lines can be cherry-picked; pairs whose src already carries both ACLs at the
#            top are commented out. Skips 'done: yes' pairs. -n keeps each file's existing
#            mask: a recomputed mask would re-grant write to the owning group where the mask
#            hid it (e.g. read-only trees with group::rwx, mask::r-x).
#
# After the final sync pass:
#
#   default  root_commands.<timestamp>.sh, for pairs WITHOUT 'owner:' - they keep the
#            source's ownership. A root rsync without --delete copies owner, group and
#            mode bits (the non-root copy could not) onto the files already in place;
#            no data moves. COLD_STORAGE* is excluded (HSM stubs stay unread, and the
#            dest symlinks are left alone).
#   --chown  root_chown_commands.<timestamp>.sh, for pairs WITH 'owner:' - a new
#            owner:group for everything. Note: does not restore setgid/mode bits the
#            non-root copy may have dropped.

flavour="rsync"
case "$1" in
    --chown) flavour="chown"; shift ;;
    --acl)   flavour="acl";   shift ;;
esac

acl="u:${USER}:rX,g:csgteam:rX,d:u:${USER}:rX,d:g:csgteam:rX"

. ./pairs.sh || exit 1

pairs=("$@")
[[ ${#pairs[@]} -gt 0 ]] || mapfile -t pairs < <(pair_names)

case "${flavour}" in
    rsync) outfile="root_commands.$(date +%F@%H:%M).sh" ;;
    chown) outfile="root_chown_commands.$(date +%F@%H:%M).sh" ;;
    acl)   outfile="root_acl_commands.$(date +%F@%H:%M).sh" ;;
esac

# acl_granted <dir> : the top of <dir> already carries both the access and default ACLs
acl_granted()
{
    local f
    f="$(getfacl -p --omit-header "$1" 2>/dev/null)" || return 1
    grep -q "^user:${USER}:r" <<< "${f}" && grep -q "^default:user:${USER}:r" <<< "${f}" \
        && grep -q "^group:csgteam:r" <<< "${f}" && grep -q "^default:group:csgteam:r" <<< "${f}"
}

q()
{
    printf '%q' "$1"
}

{
    echo "#!/bin/bash"
    echo "#"
    echo "# cgd_migration root hand-off (${flavour}), generated $(date) by ${USER}"
    echo "# from ${pairs_conf}"
    echo "#"
    case "${flavour}" in
        rsync) echo "# root rsync, no --delete: copies owner/group/mode from src onto the existing copy" ;;
        chown) echo "# chown -R -h -P: new owner:group; changes symlinks themselves, never follows them" ;;
        acl)   echo "# setfacl -R -n -m: read access for ${USER} and csgteam to each source tree (access +"
               echo "# default ACLs); one line per tree, so lines can be picked individually."
               echo "# -n: keep existing masks - never widens what the owning group can do"  ;;
    esac
    echo
    [[ ${flavour} == acl ]] && echo "ACL='${acl}'"
    echo "set -x"
    skipped=()
    for name in "${pairs[@]}"; do
        pair_load "${name}" || exit 1
        if [[ ${flavour} == rsync && -n ${owner} ]]; then
            skipped+=("${pair}: has owner '${owner}' - see the --chown script"); continue
        elif [[ ${flavour} == chown && -z ${owner} ]]; then
            skipped+=("${pair}: no owner - keeps source ownership, see the rsync script"); continue
        elif [[ ${flavour} == acl && ${pair_done} == yes ]]; then
            skipped+=("${pair}: done: yes"); continue
        fi
        echo
        echo "# [${pair}] ${srcdir} -> ${destdir}"
        case "${flavour}" in
            rsync) echo "rsync -aHAX --numeric-ids --exclude='COLD_STORAGE*' $(q "${srcdir}/") $(q "${destdir}/")" ;;
            chown) echo "chown -R -h -P $(q "${owner}") $(q "${destdir}")" ;;
            acl)   if acl_granted "${srcdir}"; then
                       echo "# already set at the top of the tree (remove the '#' to reapply):"
                       echo "# setfacl -R -n -m \"\${ACL}\" $(q "${srcdir}")"
                   else
                       echo "setfacl -R -n -m \"\${ACL}\" $(q "${srcdir}")"
                   fi ;;
        esac
    done
    echo
    echo "set +x"
    if [[ ${#skipped[@]} -gt 0 ]]; then
        echo "# skipped:"
        printf '#   %s\n' "${skipped[@]}"
    fi
} > "${outfile}" || exit 1

chmod +x "${outfile}"
echo "wrote ${outfile}:"
echo && cat "${outfile}"
