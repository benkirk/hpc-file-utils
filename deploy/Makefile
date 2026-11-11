# The shared install of hpc-file-utils on glade: one checkout per commit, holding one
# conda env per host (derecho's and casper's differ: the MPI and its modules), and a
# current-<host> link per host that jobs and users name.
#
#   make status        what is installed, what each host's current is, Makefile drift
#   make update        fetch REF, build <sha>/ for this host if new, point current-<host> at it
#   make rollback      swap this host's current and previous
#   make prune         remove every build no host's current or previous names
#   make self-update   replace this Makefile with current-<host>/deploy/Makefile
#   make usage         print the lines that run the tools from this install (prints only)
#
# THIS FILE IS A COPY.  The tracked original is deploy/Makefile in the repo; it lives in
# the deploy directory as ./Makefile and finds that directory as ROOT (default: the
# directory make runs in).  deploy/README.md is the runbook.
#
#   ROOT/                      csgteam; update sets mode 755 so everyone can run the tools
#     Makefile                 this file
#     repo.git                 bare mirror of REPO, fetched by every update
#     <sha12>/                 a checkout of that commit (.checkout marks it whole), with
#                              ./conda-env-<host> for each host built there, and a
#                              .deployed-<host> stamp once that host's env passed its smoke test
#     current-<host> -> <sha12>    what that host's jobs and users name
#     previous-<host> -> <sha12>   what rollback on that host returns to
#
# Run update once on each host, from a login node of that host (NCAR_HOST names it): the
# env is built where it will run, with that host's modules.  The two hosts share the
# checkout, and each has its own links, so one host's update, rollback or failure never
# moves the other's current.  A conda env cannot be moved after it is built, so each env
# is built in its final <sha12>/ and linked only once it passes its smoke test.  A
# <sha12>/ without .checkout is a half-made clone: update removes it, unless some host
# has a stamp in it.
#
# Rules for editing (tests/test_deploy.py checks them):
#   - Self-contained: GNU Make 3.82+, bash, git; conda on PATH for update.
#   - Never name $(MAKE) in a recipe.  GNU make runs any recipe that does even under
#     `make -n`, and `make -n <target>` must stay a true dry run.  The nested build
#     therefore calls $(BUILD), a literal `make`.
#   - Under .ONESHELL: only a recipe's FIRST line may carry @; a later @ would reach the
#     shell as a command.
#   - No same-line # comment after a default: make keeps the spaces before it.

ifeq ($(filter oneshell,$(.FEATURES)),)
  $(error this Makefile needs GNU Make 3.82+ for .ONESHELL: -- you have $(MAKE_VERSION))
endif

SHELL         := /bin/bash
.SHELLFLAGS   := -e -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

# --------------------------------------------------------------------------
# Configuration (override on the command line: make update REF=v1.2)
# --------------------------------------------------------------------------

ROOT    ?= $(CURDIR)
REPO    ?= https://github.com/benkirk/hpc-file-utils.git
REF     ?= main
HOSTS   ?= derecho casper
HFU_MPI ?= auto
TOOLS   ?= walkstat walktar walksync

# The lock that keeps updates and prunes one at a time, whichever host runs them: a file
# on derecho's Lustre scratch, not in ROOT.  ROOT is on GPFS (/glade/u), and casper mounts
# glade from a separate GPFS cluster whose flock() is not coherent with derecho's: on
# 2026-11-11 both hosts' updates took a lock in ROOT at once and raced in one checkout
# (casper's chmod -R met a file derecho's build removed).  derecho scratch is Lustre,
# mounted with -o flock on both hosts, and a lock held on one is refused on the other
# (tested the same day).  $SCRATCH is /glade/derecho/scratch/<user> on both, so one
# user's updates on the two hosts share the file.  Without SCRATCH, ROOT's own file,
# which serializes one host's updates only.  A second update waits up to LOCK_WAIT s.
LOCKFILE  ?= $(if $(SCRATCH),$(SCRATCH)/.hfu-deploy.lock,$(ROOT)/.update.lock)
LOCK_WAIT ?= 3600

# The host: HOST= on the command line, else NCAR_HOST.  Never HOST from the environment:
# derecho's login shells export HOST=derecho5 (the node), which a ?= would take.
ifneq ($(origin HOST),command line)
  HOST := $(NCAR_HOST)
endif

# A literal `make`, deliberately not $(MAKE): see the rules above.
BUILD  := make
MIRROR := $(ROOT)/repo.git

# Every recipe that writes starts here.  The guard stops a run from inside a checkout
# (`make -f deploy/Makefile update` in the repo) from turning the repo into a deploy
# directory.  relink replaces a link atomically where mv -T exists (GNU, the deploy
# hosts), and with a plain ln -sfn elsewhere.
define PRELUDE
if [ -e "$(ROOT)/pyproject.toml" ]; then
  echo "$@: ROOT=$(ROOT) is a checkout, not the deploy directory: copy deploy/Makefile there, or pass ROOT=" >&2
  exit 1
fi
cd "$(ROOT)"
umask 022
relink() { ln -sfn "$$1" ".$$2.new"; mv -T ".$$2.new" "$$2" 2>/dev/null || { rm -f ".$$2.new"; ln -sfn "$$1" "$$2"; }; }
endef

# The recipes that use this host's links: a host name is required, and it becomes part
# of file names.
define NEEDHOST
if ! [[ "$(HOST)" =~ ^[a-z0-9][a-z0-9_-]*$$ ]]; then
  echo "$@: no host: NCAR_HOST is unset (or pass HOST=derecho|casper); got '$(HOST)'" >&2
  exit 1
fi
endef

# One update or prune at a time (LOCKFILE, above: why it is on scratch).  flock releases
# by itself when the holder exits or dies, so there is no stale lock to clear.
define TAKE_LOCK
if command -v flock >/dev/null; then
  exec 9>"$(LOCKFILE)"
  if ! flock -n 9; then
    echo "$@: another update or prune holds $(LOCKFILE); waiting up to $(LOCK_WAIT) s"
    flock -w "$(LOCK_WAIT)" 9 || { echo "$@: $(LOCKFILE) still held after $(LOCK_WAIT) s" >&2; exit 1; }
  fi
fi
endef

# Reading the checkouts as anyone but their owner: git refuses another user's repo
# ("dubious ownership") unless told it is safe.  Read-only uses only.
GIT_RO := git -c safe.directory=*

.PHONY: help status update rollback prune self-update usage

help:
	@echo "hpc-file-utils deploy (ROOT=$(ROOT), HOST=$(or $(HOST),(unset)))"
	echo
	echo "  make status        builds, each host's current/previous, Makefile drift"
	echo "  make update        fetch REF, build <sha>/ for this host if new, point current-<host> at it"
	echo "  make rollback      swap this host's current and previous"
	echo "  make prune         remove every build no host's current or previous names"
	echo "  make self-update   replace ./Makefile with current-<host>/deploy/Makefile"
	echo "  make usage         print the lines that run the tools from this install"
	echo
	echo "  REPO=$(REPO)"
	echo "  REF=$(REF)  HOST=$(or $(HOST),(unset))  HFU_MPI=$(HFU_MPI)"
	echo "  LOCKFILE=$(LOCKFILE)  LOCK_WAIT=$(LOCK_WAIT)"
	echo
	echo "  Run update on each host's login node, with conda on PATH (module load conda)."
	echo "  make -n <target> prints what it would run and changes nothing."

status:
	@cd "$(ROOT)"
	echo "ROOT      $(ROOT)"
	for h in $(HOSTS); do
	  printf '%-9s current %s, previous %s\n' "$$h" "$$(readlink "current-$$h" || echo '(none)')" "$$(readlink "previous-$$h" || echo '(none)')"
	done
	echo "builds:"
	found=0
	for d in */; do
	  d=$${d%/}
	  [[ $$d =~ ^[0-9a-f]{12}$$ ]] || continue
	  found=1
	  if [ -e "$$d/.checkout" ]; then state=""; else state=" INCOMPLETE-CHECKOUT"; fi
	  hosts=""
	  for h in $(HOSTS); do
	    if [ -e "$$d/.deployed-$$h" ]; then hosts="$$hosts $$h"; elif [ -e "$$d/conda-env-$$h" ]; then hosts="$$hosts $$h(INCOMPLETE)"; fi
	  done
	  roles=""
	  for l in current-* previous-*; do
	    [ -L "$$l" ] && [ "$$(readlink "$$l")" = "$$d" ] && roles="$$roles $$l"
	  done
	  if [ -n "$$roles" ]; then roles="named by$$roles"; else roles="prune removes it"; fi
	  when=$$($(GIT_RO) -C "$$d" log -1 --format=%cs 2>/dev/null || echo "?")
	  echo "  $$d  committed $$when  built for:$${hosts:- (none)}$$state  ($$roles)"
	done
	[ "$$found" = 1 ] || echo "  (none)"
	cur=$$(readlink "current-$(HOST)" 2>/dev/null || true)
	if [ -d repo.git ] && [ -n "$$cur" ]; then
	  tip=$$($(GIT_RO) -C repo.git rev-parse --short=12 "$(REF)^{commit}" 2>/dev/null || echo "")
	  if [ -n "$$tip" ]; then
	    ahead=$$($(GIT_RO) -C repo.git rev-list --count "$$cur..$$tip" 2>/dev/null || echo "?")
	    echo "$(REF) as last fetched: $$tip; it has $$ahead commit(s) current-$(HOST) lacks"
	  fi
	fi
	if [ -n "$$cur" ] && [ -f "$$cur/deploy/Makefile" ] && ! cmp -s Makefile "$$cur/deploy/Makefile"; then
	  echo "./Makefile differs from current-$(HOST)/deploy/Makefile: run make self-update"
	fi

update:
	@$(PRELUDE)
	$(NEEDHOST)
	command -v git >/dev/null || { echo "update: git is not on PATH" >&2; exit 1; }
	command -v conda >/dev/null || { echo "update: conda is not on PATH; run 'module load conda' first" >&2; exit 1; }
	chmod 755 .
	$(TAKE_LOCK)
	if [ -d repo.git ]; then
	  git -C repo.git remote set-url origin "$(REPO)"
	  git -C repo.git fetch --quiet --prune origin
	else
	  git clone --quiet --mirror "$(REPO)" repo.git
	fi
	sha=$$(git -C repo.git rev-parse --short=12 "$(REF)^{commit}")
	# the checkout, shared by the hosts: made once, whole, by a rename
	if [ ! -e "$$sha/.checkout" ]; then
	  if [ -e "$$sha" ]; then
	    if compgen -G "$$sha/.deployed-*" >/dev/null; then
	      echo "update: $$sha has a host's stamp but no .checkout; refusing to touch it" >&2
	      exit 1
	    fi
	    rm -rf "$$sha"
	  fi
	  tmp=".clone-$$sha-$(HOST)"
	  rm -rf "$$tmp"
	  git clone --quiet --no-checkout repo.git "$$tmp"
	  git -C "$$tmp" checkout --quiet --detach "$$sha"
	  git -C "$$tmp" remote set-url origin "$(REPO)"
	  touch "$$tmp/.checkout"
	  # GNU mv -T refuses an existing target; elsewhere, look first.  Either way a
	  # checkout another host made meanwhile wins, and ours is dropped.
	  mv -T "$$tmp" "$$sha" 2>/dev/null || { [ -e "$$sha" ] || mv "$$tmp" "$$sha"; rm -rf "$$tmp"; }
	  echo "update: checked out $$sha ($(REF))"
	fi
	# this host's env in it
	if [ -e "$$sha/.deployed-$(HOST)" ]; then
	  echo "update: $$sha is already built for $(HOST)"
	else
	  echo "update: building $$sha's env for $(HOST) (HFU_MPI=$(HFU_MPI))"
	  ( cd "$$sha" && env -u MAKEFLAGS -u MFLAGS -u MAKELEVEL NCAR_HOST="$(HOST)" HFU_MPI="$(HFU_MPI)" $(BUILD) conda-env )
	  "$$sha/conda-env-$(HOST)/bin/python" -m compileall -q "$$sha/hpc_file_utils" "$$sha/sync" "$$sha/tools"
	  for t in $(TOOLS); do "$$sha/conda-env-$(HOST)/bin/$$t" --help >/dev/null; done
	  # open only what this update made: this host's env.  Not chmod -R of the checkout:
	  # another host may be building its env there (the 2026-11-11 race), and the
	  # checkout itself was made under umask 022.
	  chmod -R a+rX "$$sha/$$(readlink "$$sha/conda-env-$(HOST)")"
	  touch "$$sha/.deployed-$(HOST)"
	  echo "update: built $$sha for $(HOST)"
	fi
	cur=$$(readlink "current-$(HOST)" || true)
	if [ "$$cur" = "$$sha" ]; then
	  echo "update: current-$(HOST) is already $$sha"
	else
	  relink "$$sha" "current-$(HOST)"
	  if [ -n "$$cur" ]; then relink "$$cur" "previous-$(HOST)"; fi
	  echo "update: current-$(HOST) $${cur:-(none)} -> $$sha"
	fi
	if ! cmp -s Makefile "$$sha/deploy/Makefile"; then
	  echo "update: ./Makefile differs from $$sha/deploy/Makefile; review it, then run make self-update"
	fi

rollback:
	@$(PRELUDE)
	$(NEEDHOST)
	cur=$$(readlink "current-$(HOST)" || true)
	prev=$$(readlink "previous-$(HOST)" || true)
	if [ -z "$$prev" ] || [ ! -e "$$prev/.deployed-$(HOST)" ]; then
	  echo "rollback: no complete previous build for $(HOST) to return to" >&2
	  exit 1
	fi
	relink "$$prev" "current-$(HOST)"
	if [ -n "$$cur" ]; then relink "$$cur" "previous-$(HOST)"; fi
	echo "rollback: current-$(HOST) $${cur:-(none)} -> $$prev"

prune:
	@$(PRELUDE)
	$(TAKE_LOCK)
	keep=" "
	for l in current-* previous-*; do
	  if [ -L "$$l" ]; then keep="$$keep$$(readlink "$$l") "; fi
	done
	removed=0
	for d in */; do
	  d=$${d%/}
	  [[ $$d =~ ^[0-9a-f]{12}$$ ]] || continue
	  case "$$keep" in *" $$d "*) continue ;; esac
	  echo "prune: removing $$d"
	  rm -rf "$$d"
	  removed=$$((removed + 1))
	done
	rm -rf .clone-*
	echo "prune: removed $$removed build(s)"

self-update:
	@$(PRELUDE)
	$(NEEDHOST)
	src="current-$(HOST)/deploy/Makefile"
	[ -f "$$src" ] || { echo "self-update: no $$src (run make update first)" >&2; exit 1; }
	if cmp -s Makefile "$$src"; then
	  echo "self-update: ./Makefile already matches $$src"
	else
	  diff -u Makefile "$$src" || true
	  cp "$$src" .Makefile.new
	  mv -f .Makefile.new Makefile
	  echo "self-update: ./Makefile replaced from $$src"
	fi

usage:
	@cat <<'EOF'
	# This host's install, for a shell or a job script (bash):
	export HFU_ROOT=$(ROOT)/current-$${NCAR_HOST}
	source $${HFU_ROOT}/etc/config_env.sh      # loads the host's modules, activates its env
	# The scan scripts (scans/*.sh) source $${HFU_ROOT}/etc/config_env.sh when HFU_ROOT is set.
	# HFU_MPI must match the build's (the default, auto, is what update builds with).
	EOF
