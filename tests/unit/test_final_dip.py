#!/usr/bin/env python3
"""
mono-imager: Unit tests for the journey-specific final DIP instruction (#25).

No hardware required.

What this tests:
  - final_dip(): OPNsense -> NOR, OpenWRT/Armbian -> EMMC, unknown -> EMMC
  - menu_done(): prints RIGHT (NOR) for OPNsense and never LEFT (eMMC)
  - menu_done(): prints LEFT (eMMC) for OpenWRT and Armbian (unchanged)
  - menu_done(): failed flash prints no DIP instruction at all

Run: python tests/unit/test_final_dip.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.journeys import final_dip, DIP_LABELS
from mono_imager.tui import MonoImager

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {label}")
        passed += 1
    else:
        print(f"  FAIL: {label}")
        failed += 1


def done_output(os_name, success=True):
    """Run menu_done() and return everything it printed as one string."""
    app = MonoImager()
    app.os_name = os_name
    app.flash_success = success
    app.log_file = None
    lines = []
    with patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(str(x) for x in a))), \
         patch("builtins.input", return_value=""):
        app.menu_done()
    return "\n".join(lines)


print("=" * 60)
print("final_dip()")
print("=" * 60)
check("OPNsense -> NOR", final_dip("OPNsense") == "NOR")
check("OpenWRT -> EMMC", final_dip("OpenWRT") == "EMMC")
check("Armbian -> EMMC", final_dip("Armbian") == "EMMC")
check("unknown OS -> EMMC (previous default)", final_dip("VyOS") == "EMMC")
check("None -> EMMC", final_dip(None) == "EMMC")
check("labels cover both positions", set(DIP_LABELS) == {"NOR", "EMMC"})

print()
print("=" * 60)
print("menu_done()")
print("=" * 60)
out = done_output("OPNsense")
check("OPNsense: tells user RIGHT (NOR)", "Move the DIP switch to: RIGHT (NOR)" in out)
check("OPNsense: never tells user LEFT (eMMC)", "LEFT (eMMC)" not in out)

for os_name in ("OpenWRT", "Armbian"):
    out = done_output(os_name)
    check(f"{os_name}: tells user LEFT (eMMC)", "Move the DIP switch to: LEFT (eMMC)" in out)

out = done_output("OPNsense", success=False)
check("failed flash: no DIP instruction", "Move the DIP switch" not in out)

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
