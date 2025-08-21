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



# simple function to return a timestamp string
timestamp() { echo "$(date +%F@%T)"; }




#----------------------------------------------------------------------------
# Promt user for confirmation and capture response
#----------------------------------------------------------------------------
get_confirmation()
{
    local msg="$@"
    confirmed=0
    if [[ "NO" == "${CONFIRM}" ]]; then
        confirmed=1
        return 0
    else
        printf "%b" "${msg} " # <-- interprets any \n in ${msg}
        read -n 1 -r reply </dev/tty
        echo
        if [[ ${reply} =~ ^[Yy]$ ]]; then
            confirmed=1
            return 0
        fi
    fi

    return 1
}
CONFIRM="YES" # <-- default value
