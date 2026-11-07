# shellcheck shell=bash
# Source me (bash or zsh):  source etc/config_env.sh
#
# Builds (if needed) and activates this host's conda env, ./conda-env-${NCAR_HOST:-local},
# a symlink to a content-addressed ./conda-env-<hash> (see the Makefile).
#
#   HFU_MPI=auto|host|conda   auto: host on derecho/casper, else conda
#     host:  load etc/modules-<host> (at build and here, at activation) and
#            build mpi4py from source against that MPI
#     conda: conda-forge mpi4py + openmpi (laptops, CI)
#
# Sourced, so errors 'return' rather than 'exit'.

#----------------------------------------------------------------------------
# Determine the directory containing this script, compatible with bash and zsh
if [ -n "${BASH_SOURCE[0]}" ]; then
  SCRIPT_PATH="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION}" ]; then
  # shellcheck disable=SC2296  # zsh-only expansion
  SCRIPT_PATH="${(%):-%x}"
else
  echo "Unknown shell!"
  return 1
fi
CONFDIR="$(cd "$(dirname "$(realpath "${SCRIPT_PATH}")")" >/dev/null 2>&1 && pwd)"
#----------------------------------------------------------------------------

ROOT_DIR="$(realpath "${CONFDIR}/..")"
ETC_DIR="${ROOT_DIR}/etc"
ENV_NAME="conda-env-${NCAR_HOST:-local}"
ENV_DIR="${ROOT_DIR}/${ENV_NAME}"

# MPI flavour; the Makefile resolves 'auto' the same way
HFU_MPI="${HFU_MPI:-auto}"
case "${HFU_MPI}" in
    auto) case "${NCAR_HOST:-}" in derecho|casper) HFU_MPI_MODE=host ;; *) HFU_MPI_MODE=conda ;; esac ;;
    host|conda) HFU_MPI_MODE="${HFU_MPI}" ;;
    *) echo "HFU_MPI must be auto, host or conda (got '${HFU_MPI}')"; return 1 ;;
esac

module try-load conda > /dev/null 2>&1 || true   # no module system on laptops
conda --version > /dev/null 2>&1 || {
    echo "Cannot locate conda?"
    return 1
}

# Initialize conda unless 'conda' is already the shell function 'conda activate' needs
# (CONDA_SHLVL alone is not enough: it is exported, the function is not, e.g. into make)
case "$(type conda 2>/dev/null)" in
    *function*) __hfu_conda_init=no ;;
    *)          __hfu_conda_init=yes ;;
esac
if [ "${__hfu_conda_init}" = yes ]; then
    CONDA_ROOT="$(conda info --base 2>/dev/null)"
    if [ -n "${CONDA_ROOT}" ] && [ -f "${CONDA_ROOT}/etc/profile.d/conda.sh" ]; then
        # shellcheck source=/dev/null
        . "${CONDA_ROOT}/etc/profile.d/conda.sh"
    fi
fi

# host MPI: the same modules as the build, loaded before activation
if [ "${HFU_MPI_MODE}" = host ]; then
    [ -f "${ETC_DIR}/modules-${NCAR_HOST}" ] || { echo "HFU_MPI=host: no ${ETC_DIR}/modules-${NCAR_HOST}"; return 1; }
    # unquoted on purpose: a space-separated module list (split by bash and zsh alike)
    # shellcheck disable=SC2046
    module load $(cat "${ETC_DIR}/modules-${NCAR_HOST}") || { echo "module load failed"; return 1; }
fi

# GNU make >= 4 (macOS: the Homebrew 'gmake'; /usr/bin/make is 3.81)
HFU_MAKE="$(command -v gmake 2>/dev/null || command -v make)"
"${HFU_MAKE}" --silent -C "${ROOT_DIR}" HFU_MPI="${HFU_MPI_MODE}" conda-env || {
    echo "Building ${ENV_NAME} failed"
    return 1
}

unset __hfu_conda_init
conda activate "${ENV_DIR}" || return 1
