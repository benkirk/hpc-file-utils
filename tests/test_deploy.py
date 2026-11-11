"""deploy/Makefile: the versioned shared install on glade, one checkout per commit with
one conda env per host.

The Makefile runs against a throwaway git repo whose `make conda-env` writes stub tools
into ./conda-env-<host>, so the rotation logic (check out, build per host, link per host,
roll back, prune, dry run) is exercised end to end in a second or two, with no conda and
no network.  It needs a GNU make with `.ONESHELL:` (3.82+); macOS ships 3.81, so these
tests look for `make` or `gmake` and skip when neither qualifies.  CI's Ubuntu make runs
them.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY_MAKEFILE = REPO / "deploy" / "Makefile"

# The stand-in for the repo's own `make conda-env`: per host, as the real one is
# (conda-env-$(NCAR_HOST)); fails when HFU_STUB_FAIL is set; records HFU_MPI.
STUB_MAKEFILE = """\
conda-env:
\ttest -z "$$HFU_STUB_FAIL"
\tmkdir -p conda-env-$(NCAR_HOST)/bin
\tprintf '#!/bin/sh\\nexit 0\\n' > conda-env-$(NCAR_HOST)/bin/python
\tfor t in walkstat walktar walksync; do cp conda-env-$(NCAR_HOST)/bin/python conda-env-$(NCAR_HOST)/bin/$$t; done
\tchmod +x conda-env-$(NCAR_HOST)/bin/*
\techo "$(HFU_MPI)" > conda-env-$(NCAR_HOST)/hfu_mpi
"""


def _oneshell_make():
    for cand in ("make", "gmake"):
        exe = shutil.which(cand)
        if not exe:
            continue
        probe = subprocess.run([exe, "-s", "-f", "-", "x"], capture_output=True, text=True,
                               input="x:\n\t@echo $(filter oneshell,$(.FEATURES))\n")
        if probe.stdout.strip() == "oneshell":
            return exe
    return None


GMAKE = _oneshell_make()
needs_gmake = pytest.mark.skipif(GMAKE is None, reason="no GNU make with .ONESHELL: (3.82+)")


# ---------------------------------------------------------------------------
# Static rules
# ---------------------------------------------------------------------------

def test_oneshell_is_guarded():
    """GNU make 3.81 ignores .ONESHELL: silently, which would split every recipe
    across shells: the Makefile must refuse it."""
    text = DEPLOY_MAKEFILE.read_text()
    assert ".ONESHELL:" in text
    assert "$(filter oneshell,$(.FEATURES))" in text
    assert "$(error" in text
    assert re.search(r"^\.SHELLFLAGS\s*:= -e", text, re.M)


def test_no_recipe_names_make_so_dry_runs_stay_dry():
    """GNU make executes any recipe line that references $(MAKE) even under -n."""
    recipes = [ln for ln in DEPLOY_MAKEFILE.read_text().splitlines() if ln.startswith("\t")]
    offenders = [ln for ln in recipes if re.search(r"\$[({]MAKE[)}]", ln)]
    assert not offenders, offenders


def test_defaults_carry_no_trailing_comment():
    """make keeps the whitespace before a same-line # comment, so a default like
    `BUILD := make   # note` would silently become 'make   '."""
    for line in DEPLOY_MAKEFILE.read_text().splitlines():
        if re.match(r"^\w+\s*[?:]?=", line):
            assert "#" not in line, line


# ---------------------------------------------------------------------------
# End to end against a throwaway repo
# ---------------------------------------------------------------------------

# CI runners have no git identity, so every commit the tests make names one.
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=GIT_ENV,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def site(tmp_path):
    """A source repo, a deploy ROOT holding a copy of the Makefile, and an environment
    with a stand-in `conda` on PATH.  make(host, *args) runs as that host."""
    src = tmp_path / "src"
    (src / "deploy").mkdir(parents=True)
    for d in ("hpc_file_utils", "sync", "tools"):
        (src / d).mkdir()
        (src / d / "__init__.py").write_text("")
    (src / "Makefile").write_text(STUB_MAKEFILE)
    shutil.copy(DEPLOY_MAKEFILE, src / "deploy" / "Makefile")
    _git(tmp_path, "init", "-q", "-b", "main", str(src))
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "one")

    root = tmp_path / "root"
    root.mkdir(mode=0o750)
    shutil.copy(DEPLOY_MAKEFILE, root / "Makefile")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "conda").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "conda").chmod(0o755)

    base = {k: v for k, v in os.environ.items()
            if k not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "NCAR_HOST", "HFU_MPI", "HFU_STUB_FAIL")}
    base["PATH"] = "%s%s%s" % (bindir, os.pathsep, base.get("PATH", ""))

    def make(host, *args, check=True, **env):
        e = dict(base, **env)
        if host:
            e["NCAR_HOST"] = host
        proc = subprocess.run([GMAKE, "-C", str(root), *args, "REPO=%s" % src],
                              capture_output=True, text=True, env=e)
        if check:
            assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc

    def commit(message):
        (src / "change.txt").write_text(message)
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", message)
        return _git(src, "rev-parse", "--short=12", "HEAD")

    sha1 = _git(src, "rev-parse", "--short=12", "HEAD")
    return {"src": src, "root": root, "make": make, "commit": commit, "sha1": sha1}


def _link(root, name):
    p = root / name
    return os.readlink(p) if p.is_symlink() else None


def _links(root, host):
    return _link(root, "current-" + host), _link(root, "previous-" + host)


def _builds(root):
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.is_symlink() and re.fullmatch(r"[0-9a-f]{12}", p.name))


def _snapshot(root):
    """Every path under root with its link target or size: what a dry run must leave
    byte-for-byte alone."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath, name)
            out.append((str(p.relative_to(root)),
                        os.readlink(p) if p.is_symlink() else (None if p.is_dir() else p.stat().st_size)))
    return sorted(out)


@needs_gmake
def test_update_builds_this_hosts_env_links_it_and_is_idempotent(site):
    root, make, sha1 = site["root"], site["make"], site["sha1"]
    out = make("derecho", "update").stdout
    assert "built %s for derecho" % sha1 in out
    assert _links(root, "derecho") == (sha1, None)
    assert (root / sha1 / ".checkout").exists() and (root / sha1 / ".deployed-derecho").exists()
    assert os.access(root / "current-derecho" / "conda-env-derecho" / "bin" / "walksync", os.X_OK)
    assert (root / sha1 / "conda-env-derecho" / "hfu_mpi").read_text().strip() == "auto"
    assert root.stat().st_mode & 0o777 == 0o755     # update opens the directory to users
    assert not list(root.glob(".clone-*"))

    again = make("derecho", "update").stdout
    assert "already built for derecho" in again and "current-derecho is already" in again
    assert _builds(root) == [sha1]


@needs_gmake
def test_the_second_host_shares_the_checkout_and_has_its_own_links(site):
    root, make, sha1 = site["root"], site["make"], site["sha1"]
    make("derecho", "update")
    out = make("casper", "update", "HFU_MPI=conda").stdout
    assert "checked out" not in out and "built %s for casper" % sha1 in out
    assert _builds(root) == [sha1]
    assert (root / sha1 / ".deployed-derecho").exists() and (root / sha1 / ".deployed-casper").exists()
    assert (root / sha1 / "conda-env-casper" / "hfu_mpi").read_text().strip() == "conda"
    assert _links(root, "derecho") == (sha1, None) and _links(root, "casper") == (sha1, None)


@needs_gmake
def test_one_hosts_update_and_rollback_leave_the_other_alone(site):
    root, make, sha1 = site["root"], site["make"], site["sha1"]
    make("derecho", "update")
    make("casper", "update")
    sha2 = site["commit"]("two")
    make("derecho", "update")
    assert _links(root, "derecho") == (sha2, sha1)
    assert _links(root, "casper") == (sha1, None)
    assert not (root / sha2 / ".deployed-casper").exists()

    make("derecho", "rollback")
    assert _links(root, "derecho") == (sha1, sha2) and _links(root, "casper") == (sha1, None)
    make("derecho", "rollback")
    assert _links(root, "derecho") == (sha2, sha1)

    proc = make("casper", "rollback", check=False)
    assert proc.returncode != 0 and "no complete previous build for casper" in proc.stderr


@needs_gmake
def test_prune_keeps_every_build_any_host_names(site):
    root, make, sha1 = site["root"], site["make"], site["sha1"]
    make("derecho", "update")
    make("casper", "update")
    sha2 = site["commit"]("two")
    make("derecho", "update")
    sha3 = site["commit"]("three")
    make("derecho", "update")
    make("casper", "update")                     # casper: current sha3, previous sha1
    sha4 = site["commit"]("four")
    make("derecho", "update")                    # derecho: current sha4, previous sha3
    assert _builds(root) == sorted([sha1, sha2, sha3, sha4])

    out = make("derecho", "prune").stdout
    assert "removing %s" % sha2 in out
    assert _builds(root) == sorted([sha1, sha3, sha4])
    assert _links(root, "derecho") == (sha4, sha3) and _links(root, "casper") == (sha3, sha1)


@needs_gmake
def test_a_failed_build_is_not_linked_and_a_rerun_finishes_it(site):
    root, make, sha1 = site["root"], site["make"], site["sha1"]
    make("derecho", "update")
    proc = make("casper", "update", check=False, HFU_STUB_FAIL="1")
    assert proc.returncode != 0
    assert not (root / sha1 / ".deployed-casper").exists() and _links(root, "casper") == (None, None)
    assert (root / sha1 / ".deployed-derecho").exists()          # the other host's build is untouched

    out = make("casper", "update").stdout
    assert "checked out" not in out and "built %s for casper" % sha1 in out
    assert _links(root, "casper") == (sha1, None)


@needs_gmake
def test_a_half_made_checkout_is_redone_but_one_with_a_stamp_is_refused(site):
    root, make, sha1 = site["root"], site["make"], site["sha1"]
    (root / sha1).mkdir()
    (root / sha1 / "debris").write_text("interrupted")
    make("derecho", "update")
    assert not (root / sha1 / "debris").exists() and (root / sha1 / ".checkout").exists()

    (root / sha1 / ".checkout").unlink()
    proc = make("casper", "update", check=False)
    assert proc.returncode != 0 and "refusing to touch it" in proc.stderr
    assert (root / sha1 / ".deployed-derecho").exists()


@needs_gmake
def test_dry_runs_change_nothing(site):
    root, make = site["root"], site["make"]
    make("derecho", "update")
    site["commit"]("two")
    before = _snapshot(root)
    for target in ("update", "prune", "rollback", "self-update"):
        make("derecho", "-n", target)
        make("casper", "-n", target)
    assert _snapshot(root) == before


@needs_gmake
def test_status_reports_each_host_and_makefile_drift(site):
    root, make, sha1 = site["root"], site["make"], site["sha1"]
    make("derecho", "update")
    out = make("derecho", "status").stdout
    assert "derecho   current %s, previous (none)" % sha1 in out
    assert "casper    current (none)" in out
    assert "%s  committed" % sha1 in out and "built for: derecho" in out
    assert "(named by current-derecho)" in out                   # once
    assert "differs" not in out

    (root / "Makefile").write_text((root / "Makefile").read_text() + "\n# local edit\n")
    assert "make self-update" in make("derecho", "status").stdout
    make("derecho", "self-update")
    assert (root / "Makefile").read_text() == DEPLOY_MAKEFILE.read_text()


@needs_gmake
def test_refuses_to_run_inside_a_checkout(site):
    (site["root"] / "pyproject.toml").write_text("")
    proc = site["make"]("derecho", "update", check=False)
    assert proc.returncode != 0 and "is a checkout, not the deploy directory" in proc.stderr
    assert not (site["root"] / "repo.git").exists()


@needs_gmake
def test_the_host_comes_from_ncar_host_never_from_an_environment_host(site):
    """derecho's login shells export HOST=derecho5 (the node): taken by a ?=, it built a
    'derecho5' env with conda-forge MPI (found in the first real deploy, 2026-11-11)."""
    root, make, sha1 = site["root"], site["make"], site["sha1"]
    out = make("derecho", "update", HOST="derecho5").stdout
    assert "built %s for derecho" % sha1 in out and "derecho5" not in out
    assert _links(root, "derecho") == (sha1, None) and not (root / "current-derecho5").exists()
    out = make("derecho", "status", HOST="derecho5").stdout
    assert "derecho   current %s" % sha1 in out
    make("derecho", "update", "HOST=casper")                     # the command line still chooses
    assert _links(root, "casper") == (sha1, None)


@needs_gmake
def test_update_needs_a_host(site):
    proc = site["make"](None, "update", check=False)
    assert proc.returncode != 0 and "NCAR_HOST is unset" in proc.stderr
    assert not (site["root"] / "repo.git").exists()


@needs_gmake
def test_usage_names_this_hosts_current():
    out = subprocess.run([GMAKE, "-s", "-f", str(DEPLOY_MAKEFILE), "usage", "ROOT=/opt/x"],
                         capture_output=True, text=True, check=True).stdout
    assert "export HFU_ROOT=/opt/x/current-${NCAR_HOST}" in out
    assert "source ${HFU_ROOT}/etc/config_env.sh" in out
