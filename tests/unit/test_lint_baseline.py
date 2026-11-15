"""
mono-imager: lint baseline gate (refactor phase 0.3).

Runs `ruff check mono_imager` and `mypy mono_imager` and counts the findings
per rule / error code. The counts may go down, never up: a change that adds
a finding of any code fails here, and the code is named. Counts that went
down are reported; lock them in with --update and review `git diff`.

The counts depend on the tool versions, so the baseline records them and the
check is skipped (with a message) when ruff or mypy is missing or a different
version. Pinned versions: pyproject.toml, `pip install -e .[dev]`.
mypy checks for the Python in [tool.mypy] (pyproject.toml) and for Windows, on any
machine.

Run:     python tests/unit/test_lint_baseline.py
Update:  python tests/unit/test_lint_baseline.py --update
"""

import json
import subprocess
import sys
import tempfile
from collections import Counter
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGE = "mono_imager"
BASELINE = Path(__file__).resolve().parent / "fixtures" / "lint_baseline.json"
UPDATE_FLAG = "--update"
RUFF = "ruff"
MYPY = "mypy"
TOOLS = (RUFF, MYPY)
VERSIONS_KEY = "versions"
MYPY_ERROR = "error"
NO_CODE = "no-code"                 # a finding without a code (e.g. a syntax error)
TOOL_FAILED = 2                     # exit code of ruff and mypy when the tool itself fails
JSON_INDENT = 2
# ruff's EXE rules read the Unix executable bit, which Windows has not ("not enforced on
# Windows or WSL", ruff docs): their count depends on the machine, so they are left out.
MACHINE_DEPENDENT_PREFIXES = ("EXE",)

RUFF_CMD = [sys.executable, "-m", RUFF, "check", PACKAGE,
            "--output-format", "json", "--no-cache", "--exit-zero"]
# mypy checks for the OS the app runs on (Windows), whatever OS runs the test: the
# sys.platform branches it analyses, and so the counts, then match on every machine.
MYPY_PLATFORM = "win32"
MYPY_CMD = [sys.executable, "-m", MYPY, PACKAGE, "--output", "json", "--no-error-summary",
            "--platform", MYPY_PLATFORM]


def installed_versions() -> dict[str, str | None]:
    """tool -> installed version, or None when it is not installed."""
    found: dict[str, str | None] = {}
    for tool in TOOLS:
        try:
            found[tool] = metadata.version(tool)
        except metadata.PackageNotFoundError:
            found[tool] = None
    return found


def run_tool(cmd: list[str]) -> str:
    """Run a lint tool from the repo root (so pyproject.toml applies); return stdout."""
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                            check=False)
    if result.returncode >= TOOL_FAILED:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr}")
    return result.stdout


def ruff_counts() -> dict[str, int]:
    findings = json.loads(run_tool(RUFF_CMD))
    codes = (f["code"] or NO_CODE for f in findings)
    return dict(Counter(c for c in codes if not c.startswith(MACHINE_DEPENDENT_PREFIXES)))


def mypy_counts() -> dict[str, int]:
    with tempfile.TemporaryDirectory() as cache:       # no .mypy_cache left in the repo
        out = run_tool([*MYPY_CMD, "--cache-dir", cache])
    lines = (json.loads(line) for line in out.splitlines() if line.strip())
    return dict(Counter(d["code"] or NO_CODE for d in lines if d["severity"] == MYPY_ERROR))


def measure(versions: dict[str, str | None]) -> dict:
    return {VERSIONS_KEY: versions, RUFF: ruff_counts(), MYPY: mypy_counts()}


passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    if condition:
        print(f"  PASS: {label}")
        passed += 1
    else:
        print(f"  FAIL: {label}")
        failed += 1


def section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def compare(tool: str, base: dict[str, int], now: dict[str, int]) -> None:
    section(f"{tool}: no rule/error code has more findings than the baseline")
    print(f"  total: baseline {sum(base.values())}, now {sum(now.values())}")
    for code in sorted(set(base) | set(now)):
        was, count = base.get(code, 0), now.get(code, 0)
        check(f"{code}: {count} (baseline {was})", count <= was)
        if count < was:
            print(f"        went down: lock it in with {UPDATE_FLAG}")


versions = installed_versions()
if UPDATE_FLAG in sys.argv:
    if None in versions.values():
        sys.exit(f"cannot update: install the dev tools first ({versions})")
    BASELINE.write_text(json.dumps(measure(versions), indent=JSON_INDENT, sort_keys=True) + "\n")
    print(f"  wrote {BASELINE}")

baseline = json.loads(BASELINE.read_text())
if versions != baseline[VERSIONS_KEY]:
    print(f"SKIP: lint baseline needs {baseline[VERSIONS_KEY]}, installed {versions}.")
    print("      Install the pinned dev tools: pip install -e .[dev]")
    sys.exit(0)

current = measure(versions)
for tool in TOOLS:
    compare(tool, baseline[tool], current[tool])

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
