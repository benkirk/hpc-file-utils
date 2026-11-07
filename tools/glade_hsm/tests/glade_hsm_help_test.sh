#!/usr/bin/env bash
#
# glade_hsm's usage paths, anywhere: no /glade, a scratch log_dir and HOME.
# Today an unknown mode also prints the usage and exits 0.

set -uo pipefail
tool="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/glade_hsm"
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
export log_dir="${scratch}" HOME="${scratch}"

status=0
for args in "" "--help" "-h" "bogus-mode"; do
    # shellcheck disable=SC2086  # word-split on purpose: "" means no arguments
    out="$(bash "${tool}" ${args} 2>&1)"; rc=$?
    if [[ ${rc} -eq 0 && ${out} == *"Usage: glade_hsm <migrate|recall|status|inspect>"* ]]; then
        echo "ok   - glade_hsm ${args:-(no arguments)}: usage, exit 0"
    else
        echo "FAIL - glade_hsm ${args:-(no arguments)}: exit ${rc}"; echo "${out}" | head -n 5; status=1
    fi
done

# without a writable log_dir it refuses to start
out="$(log_dir="${scratch}/missing" bash "${tool}" 2>&1)"; rc=$?
if [[ ${rc} -ne 0 && ${out} == *"No writable logdir"* ]]; then
    echo "ok   - glade_hsm: refuses a missing log_dir"
else
    echo "FAIL - glade_hsm: missing log_dir: exit ${rc}: ${out}"; status=1
fi
exit "${status}"
