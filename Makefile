# hpc-file-utils: environment, tests and housekeeping.  Needs GNU make >= 4
# (macOS: 'brew install make', then 'gmake'; etc/config_env.sh picks gmake if present).

ifeq ($(filter oneshell,$(.FEATURES)),)
$(error GNU make >= 4 is required (found $(MAKE_VERSION)); on macOS: brew install make, then use gmake)
endif

.ONESHELL:
SHELL := /bin/bash
# -e is load-bearing: .ONESHELL runs each recipe as ONE script.
.SHELLFLAGS := -ec
CONDA_ROOT := $(shell conda info --base 2>/dev/null)

# Common way to initialize the environment across various types of systems
config_env := module load conda >/dev/null 2>&1 || true && . $(CONDA_ROOT)/etc/profile.d/conda.sh

.PHONY: help conda-env prune-old-envs print-env-hash bytecode test test-pbs shellcheck clean

# -------------------------------------------------------------------
# Conda env: content-addressed ./conda-env-<hash>/ plus one symlink per host,
# ./conda-env-$(ENV_HOST) (casper and derecho share a glade checkout).
# See etc/config_env.sh, the user-facing entry point, which calls 'conda-env'.
#
# HFU_MPI=auto|host|conda.  auto = host on derecho/casper, else conda.
#   host:  load etc/modules-<host> and build mpi4py from source against that MPI
#   conda: add conda-env-mpi.yaml (conda-forge mpi4py + openmpi)
# -------------------------------------------------------------------
ENV_HOST := $(or $(NCAR_HOST),local)
HFU_MPI  ?= auto
ifeq ($(HFU_MPI),auto)
  HFU_MPI_MODE := $(if $(filter derecho casper,$(NCAR_HOST)),host,conda)
else
  HFU_MPI_MODE := $(HFU_MPI)
endif
ifeq ($(filter host conda,$(HFU_MPI_MODE)),)
  $(error HFU_MPI must be auto, host or conda (got '$(HFU_MPI)'))
endif

MODULES_FILE := etc/modules-$(ENV_HOST)
ifeq ($(HFU_MPI_MODE),host)
  HFU_MODULES := $(shell cat $(MODULES_FILE) 2>/dev/null)
  ifeq ($(HFU_MODULES),)
    $(error HFU_MPI=host needs $(MODULES_FILE) (a module list for $(ENV_HOST)))
  endif
endif

SHA256 := $(shell command -v sha256sum >/dev/null 2>&1 && echo sha256sum || echo 'shasum -a 256')

# 12-char content hash of: env specs + python deps + MPI flavour + host + modules.
# Drives both the build dir name and the cache-hit decision.
ENV_HASH := $(shell { cat conda-env.yaml pyproject.toml; \
                      $(if $(filter conda,$(HFU_MPI_MODE)),cat conda-env-mpi.yaml;) \
                      echo "HFU_MPI=$(HFU_MPI_MODE)"; \
                      echo "NCAR_HOST=$(ENV_HOST)"; \
                      echo "MODULES=$(HFU_MODULES)"; \
                    } | $(SHA256) | cut -c1-12)
ENV_PREFIX := conda-env-$(ENV_HASH)
ENV_LINK   := conda-env-$(ENV_HOST)
STAMP_NAME := .hfu-env-ready
ENV_STAMP  := $(ENV_PREFIX)/$(STAMP_NAME)

load_modules := $(if $(HFU_MODULES),{ type module >/dev/null 2>&1 || . /etc/profile.d/z00_modules.sh; } && module load $(HFU_MODULES) && module list,true)

# Remove this host's envs other than the ones some conda-env-<host> link points at,
# keeping the most recent of them. Only envs whose stamp names this host are touched,
# so a derecho prune never removes casper's env (and half-built dirs are left alone).
define prune_envs
targets=" $$(for l in conda-env-*; do if [ -L "$$l" ]; then printf '%s ' "$$(readlink "$$l")"; fi; done)"
ls -1dt conda-env-* 2>/dev/null | while read -r d; do
    [ -L "$$d" ] || [ ! -d "$$d" ] && continue
    case "$$targets" in *" $$d "*) continue ;; esac
    grep -qx "host=$(ENV_HOST)" "$$d/$(STAMP_NAME)" 2>/dev/null || continue
    echo "$$d"
done | tail -n +2 | xargs -r rm -rf
endef

# -------------------------------------------------------------------
help: ## Show this help message
	@echo ""
	@echo -e "\033[1;36mhpc-file-utils - Makefile Help\033[0m"
	@echo -e "\033[1;36m==============================\033[0m"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[32m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo -e "\033[1mConda env:\033[0m"
	@echo -e "  \033[33msource etc/config_env.sh\033[0m            build (if needed) and activate ./$(ENV_LINK)"
	@echo -e "  \033[33mHFU_MPI=conda source etc/config_env.sh\033[0m  conda-forge MPI even on derecho/casper"
	@echo ""

conda-env: $(ENV_STAMP) ## Build (if needed) ./conda-env-<hash>, symlink ./conda-env-<host> to it
	@# Swap + prune inlined here, not a recursive make: any recipe line referencing
	@# $$(MAKE) is force-executed even under 'make -n'.
	@current=$$(readlink $(ENV_LINK) 2>/dev/null || true)
	if [ "$$current" != "$(ENV_PREFIX)" ]; then
	    ln -snfv $(ENV_PREFIX) $(ENV_LINK)
	    ( $(prune_envs) ) &
	fi

# The stamp file is content-addressed (its path encodes the hash), so it has NO
# mtime-based prereqs: a real edit produces a new hash, a new ENV_PREFIX and a rebuild.
# The rm -rf guards against a half-built dir from an interrupted run with this hash.
$(ENV_STAMP):
	@echo ">>> Building $(ENV_PREFIX) (host $(ENV_HOST), HFU_MPI=$(HFU_MPI_MODE)$(if $(HFU_MODULES),; modules: $(HFU_MODULES)))"
	rm -rf $(ENV_PREFIX)
	$(config_env)
	$(load_modules)
	conda env create --yes --quiet --file conda-env.yaml --prefix $(CURDIR)/$(ENV_PREFIX)
	$(if $(filter conda,$(HFU_MPI_MODE)),conda env update --quiet --file conda-env-mpi.yaml --prefix $(CURDIR)/$(ENV_PREFIX))
	conda activate $(CURDIR)/$(ENV_PREFIX)
	if [ "$(HFU_MPI_MODE)" = host ]; then
	    MPICC="$$(command -v mpicc)" || { echo "mpicc not found after 'module load $(HFU_MODULES)'" >&2; exit 1; }
	    export MPICC
	    PIP_NO_BINARY=mpi4py pip install --no-cache-dir -e ".[mpi,dev]"
	else
	    pip install -e ".[mpi,dev]"
	fi
	python -c "from mpi4py import MPI; print('mpi4py', MPI.Get_library_version().splitlines()[0])"
	pipdeptree --all 2>/dev/null || true
	printf 'host=%s\nhfu_mpi=%s\nmodules=%s\n' "$(ENV_HOST)" "$(HFU_MPI_MODE)" "$(HFU_MODULES)" > $(ENV_STAMP)

prune-old-envs: ## Keep this host's current + most-recent-previous conda-env-*; remove older
	@$(prune_envs)

# A shared install's users cannot write __pycache__: run as the owner after every git pull.
bytecode: ## Precompile the Python sources for ./conda-env-<host> (shared installs, after each pull)
	./$(ENV_LINK)/bin/python -m compileall -q hpc_file_utils sync tools

print-env-hash: ## Print the computed ENV_HASH / ENV_PREFIX (debug)
	@echo "ENV_HOST=$(ENV_HOST)"
	@echo "HFU_MPI=$(HFU_MPI) -> $(HFU_MPI_MODE)"
	@echo "HFU_MODULES=$(HFU_MODULES)"
	@echo "ENV_HASH=$(ENV_HASH)"
	@echo "ENV_PREFIX=$(ENV_PREFIX)"
	@echo "ENV_LINK=$(ENV_LINK)"
	@echo "ENV_STAMP=$(ENV_STAMP)"

# -------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------
PYTEST_ARGS ?=

test: ## Run every test that can run here (mpi tests skip on derecho outside PBS)
	source etc/config_env.sh
	python -m pytest $(PYTEST_ARGS)

shellcheck: ## shellcheck -x -S warning over every shell script, against tests/shellcheck-baseline.txt
	bash tests/shellcheck_test.sh

# cray-mpich only launches inside a PBS job. Builds/activates the env on the
# login node first, then blocks until the job ends and returns its exit status.
PBS_ACCOUNT  ?= SCSG0001
PBS_QUEUE    ?= main
PBS_WALLTIME ?= 00:30:00
test-pbs: ## Submit a 1-node PBS job running pytest -m "mpi or site" and wait for it
	source etc/config_env.sh
	mkdir -p logs
	qsub -W block=true -A $(PBS_ACCOUNT) -q $(PBS_QUEUE) -l walltime=$(PBS_WALLTIME) \
	    -o logs/ -v HFU_MPI=$(HFU_MPI),PYTEST_ARGS="$(PYTEST_ARGS)" etc/test-pbs.sh

clean: ## Remove editor backups and test run output
	find . -name '*~' -not -path './conda-env-*' -delete
	rm -rf .pytest_cache
