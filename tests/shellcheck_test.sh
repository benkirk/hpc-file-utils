#!/usr/bin/env bash
#
# Runs "shellcheck -x -S warning" (less SC2218, see below) over every shell script in the repo (*.sh, or a sh/bash
# shebang). Warnings already in the imported scripts are listed, per file and code,
# in shellcheck-baseline.txt; anything beyond the baseline fails. Fixing those old
# warnings means changing scans/ and tools/ code, which waits for phase 3.
#
#   bash tests/shellcheck_test.sh            # check
#   bash tests/shellcheck_test.sh --update   # rewrite the baseline from the current tree

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
top="$(dirname "${here}")"
baseline="${here}/shellcheck-baseline.txt"

command -v shellcheck >/dev/null 2>&1 || { echo "SKIP: shellcheck not installed"; exit 77; }
cd "${top}" || exit 1

list_files()
{
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git ls-files --cached --others --exclude-standard
    else
        find . -type f ! -path './.git/*' ! -path './conda-env-*' | sed 's|^\./||'
    fi
}

scripts=()
while IFS= read -r f; do
    [[ -f ${f} && ! -L ${f} ]] || continue
    if [[ ${f} == *.sh ]] || head -n 1 "${f}" 2>/dev/null | grep -qE '^#!.*[/ ](ba)?sh([[:space:]]|$)'; then
        scripts+=("${f}")
    fi
done < <(list_files | LC_ALL=C sort)

# SC2218 ("function only defined later") is excluded: the conda-forge osx-arm64
# build of version 0.11.0 reports it for functions defined earlier (Homebrew's 0.11.0 does not).
sc_opts=(-x -P SCRIPTDIR -S warning -e SC2218)

# '<file> <code> <count>' for every warning-or-worse finding
current="$(for f in "${scripts[@]}"; do
               shellcheck "${sc_opts[@]}" -f gcc "${f}" 2>&1 \
                   | grep -oE '\[SC[0-9]+\]$' | tr -d '[]' | sed "s|^|${f} |"
           done | LC_ALL=C sort | uniq -c | awk '{print $2, $3, $1}')"

if [[ ${1:-} == --update ]]; then
    {
        echo "# shellcheck warnings accepted in the imported scripts: <file> <code> <count>"
        echo "# regenerate with: bash tests/shellcheck_test.sh --update"
        [[ -n ${current} ]] && echo "${current}"
    } > "${baseline}"
    echo "wrote ${baseline} ($(grep -vc '^#' "${baseline}") entries)"
    exit 0
fi

echo "checked ${#scripts[@]} scripts"
status=0
while read -r file code count; do
    [[ -n ${file} ]] || continue
    allowed="$(awk -v f="${file}" -v c="${code}" '$1 == f && $2 == c {print $3}' "${baseline}")"
    if [[ ${count} -gt ${allowed:-0} ]]; then
        echo "NEW: ${file}: ${count} x ${code} (baseline ${allowed:-0})"
        shellcheck "${sc_opts[@]}" -i "${code}" "${file}"
        status=1
    elif [[ ${count} -lt ${allowed} ]]; then
        echo "note: ${file}: ${code} down to ${count} from ${allowed}; run --update to lower the baseline"
    fi
done <<< "${current}"

[[ ${status} -eq 0 ]] && echo "no warnings beyond ${baseline##*/}"
exit "${status}"
