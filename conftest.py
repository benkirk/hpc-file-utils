"""Root pytest configuration shared by every test suite.

- Collects every ``*_test.sh`` under testpaths as a test item (after hpc-usage-queries).
  Exit status 0 passes, 77 skips (the last stdout line is the reason), anything else fails.
  A header line ``# pytest-mark: site`` (or ``mpi``) adds those markers.
- Skips ``mpi`` tests when mpi4py/mpiexec are missing, and on derecho outside a PBS job
  (cray-mpich only launches inside PBS).
"""

import importlib.util
import os
import re
import shutil
import subprocess

import pytest

SKIP_STATUS = 77
MARK_RE = re.compile(r"^#\s*pytest-mark:\s*(.+?)\s*$")


# ---------------------------------------------------------------------------
# Shell-script test collection
# ---------------------------------------------------------------------------

class ShellScriptError(Exception):
    def __init__(self, result):
        self.result = result


class ShellScriptItem(pytest.Item):
    def runtest(self):
        result = subprocess.run(
            ["bash", str(self.path)],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.stdout.strip():
            self.add_report_section("call", "stdout", result.stdout.rstrip())
        if result.stderr.strip():
            self.add_report_section("call", "stderr", result.stderr.rstrip())
        if result.returncode == SKIP_STATUS:
            lines = result.stdout.strip().splitlines()
            pytest.skip(lines[-1] if lines else f"{self.path.name} exited {SKIP_STATUS}")
        if result.returncode != 0:
            raise ShellScriptError(result)

    def repr_failure(self, excinfo):
        if not isinstance(excinfo.value, ShellScriptError):
            return super().repr_failure(excinfo)
        r = excinfo.value.result
        lines = [f"Shell script failed (exit {r.returncode}): {self.path.name}"]
        if r.stdout.strip():
            lines += ["--- stdout ---", r.stdout.rstrip()]
        if r.stderr.strip():
            lines += ["--- stderr ---", r.stderr.rstrip()]
        return "\n".join(lines)

    def reportinfo(self):
        return self.path, None, f"shell: {self.path.name}"


class ShellScriptFile(pytest.File):
    def collect(self):
        item = ShellScriptItem.from_parent(self, name=self.path.name)
        with open(self.path, errors="replace") as f:
            for line in f:
                if not line.startswith("#"):
                    break
                m = MARK_RE.match(line)
                if m:
                    for name in m.group(1).replace(",", " ").split():
                        item.add_marker(name)
        yield item


def pytest_collect_file(parent, file_path):
    """Collect *_test.sh files as shell-script test items."""
    if file_path.suffix == ".sh" and file_path.name.endswith("_test.sh"):
        return ShellScriptFile.from_parent(parent, path=file_path)


# ---------------------------------------------------------------------------
# mpi marker: skip where we cannot launch
# ---------------------------------------------------------------------------

def mpi_unavailable_reason():
    if importlib.util.find_spec("mpi4py") is None:
        return "mpi4py is not installed"
    if not (os.environ.get("HFU_MPIEXEC") or shutil.which("mpiexec")):
        return "no mpiexec on PATH"
    if os.environ.get("NCAR_HOST") == "derecho" and not os.environ.get("PBS_JOBID"):
        return "cray-mpich only launches inside a PBS job (use make test-pbs)"
    return None


def pytest_collection_modifyitems(config, items):
    reason = mpi_unavailable_reason()
    if reason is None:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "mpi" in item.keywords:
            item.add_marker(skip)
