#!/usr/bin/env bash
#
# glade_purge's usage paths, anywhere: no /glade, a scratch HOME.

set -uo pipefail
tool="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/glade_purge"
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
export HOME="${scratch}"

status=0
for args in "" "--help" "-h"; do
    # shellcheck disable=SC2086  # word-split on purpose: "" means no arguments
    out="$(bash "${tool}" ${args} 2>&1)"; rc=$?
    if [[ ${rc} -eq 0 && ${out} == *"usage: glade_purge"* ]]; then
        echo "ok   - glade_purge ${args:-(no arguments)}: usage, exit 0"
    else
        echo "FAIL - glade_purge ${args:-(no arguments)}: exit ${rc}"; echo "${out}" | head -n 5; status=1
    fi
done

out="$(bash "${tool}" --bogus 2>&1)"; rc=$?
if [[ ${rc} -ne 0 ]]; then
    echo "ok   - glade_purge --bogus: exit ${rc}"
else
    echo "FAIL - glade_purge --bogus: exit 0: ${out}"; status=1
fi
[[ -d ${scratch}/.glade_purge ]] && echo "ok   - glade_purge: state dir under HOME" \
    || { echo "FAIL - glade_purge: no ${scratch}/.glade_purge"; status=1; }
exit "${status}"
