# Error handler with backtrace
err_report() {
    local exit_code=${?}
    local cmd="${BASH_COMMAND}"

    echo ""
    echo "❌ Command failed: ${cmd}"
    echo "   Exit code: ${exit_code}"
    echo "Backtrace (most recent call last):"

    local i
    for ((i=${#FUNCNAME[@]}-1; i>=0; i--)); do
        # FUNCNAME[0] is always err_report itself, so skip it
        if [[ "${FUNCNAME[$i]}" == "err_report" ]]; then
            continue
        fi
        echo "  at ${FUNCNAME[$i]}() in ${BASH_SOURCE[$i]}:${BASH_LINENO[$i]}"
    done

    exit "${exit_code}"
}

trap err_report ERR
