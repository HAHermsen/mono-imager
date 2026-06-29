#!/usr/bin/env python3
"""
mono-imager test runner — unit + hardware tests.

Runs all tests in unit/ then hardware/. Destructive tests are
omitted on purpose and must be run manually.

Usage:
    python run_tests.py                   # unit only (no hardware)
    python run_tests.py --port COM5       # unit + hardware
    python run_tests.py --unit-only       # force unit only even if --port given

Each test file is run as a subprocess so failures are isolated —
one failing test does not prevent the rest from running.

Exit code: 0 if all run tests passed, 1 if any failed.
"""

import sys
import subprocess
import argparse
import time
from pathlib import Path

ROOT  = Path(__file__).resolve().parent
UNIT  = ROOT / "unit"
HW    = ROOT / "hardware"

# Hardware tests and the extra args they need beyond --port
# Order matters: serial_connect first (confirms basic connectivity),
# then detection, then inspection, then dryrun last (most involved).
HW_TESTS = [
    ("test_serial_connect.py",   ["--port", "{port}"]),
    ("test_serial_hotplug.py",   ["--port", "{port}", "--mode", "simulated"]),
    ("test_uboot_dump.py",       ["--port", "{port}", "--section", "all"]),
    ("test_recovery_detect.py",  ["--port", "{port}"]),
    ("test_emmc_inspect.py",     ["--port", "{port}"]),
    ("test_recovery_dryrun.py",  ["--port", "{port}"]),
]

GREEN  = "\x1b[32m"
RED    = "\x1b[31m"
YELLOW = "\x1b[33m"
RESET  = "\x1b[0m"
BOLD   = "\x1b[1m"


def run_test(script: Path, extra_args: list[str] = None) -> bool:
    """Run a single test script, stream its output, return True on exit 0."""
    cmd = [sys.executable, str(script)] + (extra_args or [])

    print(f"\n{BOLD}{'-' * 60}{RESET}")
    print(f"{BOLD}>  {script.name}{RESET}")
    print(f"{'-' * 60}")

    start  = time.time()
    result = subprocess.run(cmd, cwd=ROOT.parent)
    elapsed = time.time() - start

    ok = result.returncode == 0
    status = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"\n{status}  {script.name}  ({elapsed:.1f}s)")
    return ok


def main():
    parser = argparse.ArgumentParser(
        description="mono-imager test runner — unit + hardware (no destructive)"
    )
    parser.add_argument("--port", default=None,
                        help="Serial port for hardware tests (e.g. COM5). "
                             "Omit to run unit tests only.")
    parser.add_argument("--unit-only", action="store_true",
                        help="Run unit tests only, even if --port is provided.")
    args = parser.parse_args()

    results: list[tuple[str, bool]] = []

    # ── Unit tests ──────────────────────────────────────────────────
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  UNIT TESTS{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    for script in sorted(UNIT.glob("test_*.py")):
        ok = run_test(script)
        results.append((script.name, ok))

    # ── Hardware tests ───────────────────────────────────────────────
    if args.unit_only or not args.port:
        print(f"\n{YELLOW}Hardware tests skipped — pass --port COM5 to include them.{RESET}")
    else:
        print(f"\n{BOLD}{'=' * 60}{RESET}")
        print(f"{BOLD}  HARDWARE TESTS  (port: {args.port}){RESET}")
        print(f"{BOLD}{'=' * 60}{RESET}")

        for filename, template_args in HW_TESTS:
            script     = HW / filename
            extra_args = [a.replace("{port}", args.port) for a in template_args]
            ok         = run_test(script, extra_args)
            results.append((filename, ok))

    # ── Summary ──────────────────────────────────────────────────────
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total  = len(results)

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  SUMMARY{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    for name, ok in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {name}")

    print(f"\n  {passed}/{total} passed", end="")
    if failed:
        print(f"  —  {RED}{failed} failed{RESET}")
    else:
        print(f"  —  {GREEN}all passed{RESET}")

    if not args.port and not args.unit_only:
        print(f"\n  {YELLOW}Note: hardware tests were not run.{RESET}")
        print(f"  {YELLOW}Run with --port COM5 to include them.{RESET}")

    print()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
