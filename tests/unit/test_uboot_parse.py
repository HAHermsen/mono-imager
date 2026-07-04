#!/usr/bin/env python3
"""
mono-imager: Unit tests for uboot_parse.py.

No hardware required — pure string parsing, no I/O.

What this tests:
  - parse_uboot_identity(): SoC/Model/DRAM/clock extraction, including
    the CPU-clock same-vs-different-per-core collapsing logic
  - parse_uboot_self_test(): [ OK ] and [FAIL] line capture, correct
    label/value splitting on multi-word labels (regression test for a
    bug where the first single space split "DDR4 Memory" into
    "DDR4"/"Memory ..." instead of treating the whole run of 2+ spaces
    as the label/value separator), and "Self-test starting" exclusion

Run: python tests/unit/test_uboot_parse.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.uboot_parse import parse_uboot_identity, parse_uboot_self_test

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


# ============================================================================
# parse_uboot_identity()
# ============================================================================

print("=" * 60)
print("parse_uboot_identity()")
print("=" * 60)

SAMPLE_BOOT = """SoC:  LS1046AE Rev1.0 (0x87070010)
Clock Configuration:
       CPU0(A72):1600 MHz  CPU1(A72):1600 MHz  CPU2(A72):1600 MHz
       CPU3(A72):1600 MHz
       Bus:      600  MHz  DDR:      2100 MT/s  FMAN:     700  MHz
Model: Mono Gateway Development Kit
DRAM:  7.9 GiB (DDR4, 64-bit, CL=16, ECC on)
[ OK ] Self-test starting
[ OK ] DDR4 Memory          Bank0: 1982 MB, Bank1: 6144 MB
[FAIL] Temperatures         CPU sensor not responding
[ OK ] USB PD controller    ID 0x25
Hit any key to stop autoboot
"""

identity = parse_uboot_identity(SAMPLE_BOOT)
check("SoC parsed", identity.get("SoC") == "LS1046AE Rev1.0 (0x87070010)")
check("Model parsed", identity.get("Model") == "Mono Gateway Development Kit")
check("DRAM parsed", identity.get("DRAM") == "7.9 GiB (DDR4, 64-bit, CL=16, ECC on)")
check("Bus clock parsed", identity.get("Bus clock") == "600 MHz")
check("DDR clock parsed", identity.get("DDR clock") == "2100 MT/s")
check("FMAN clock parsed", identity.get("FMAN clock") == "700 MHz")
check("CPU clock collapses identical cores",
      identity.get("CPU clock") == "1600 MHz (all cores)")

MIXED_CPU_BOOT = SAMPLE_BOOT.replace(
    "CPU3(A72):1600 MHz", "CPU3(A72):1400 MHz"
)
identity_mixed = parse_uboot_identity(MIXED_CPU_BOOT)
check("CPU clock lists each core when they differ",
      identity_mixed.get("CPU clock") == "1600 MHz, 1600 MHz, 1600 MHz, 1400 MHz")

check("Missing fields simply absent, not guessed",
      "SoC" not in parse_uboot_identity("no recognizable output here"))


# ============================================================================
# parse_uboot_self_test()
# ============================================================================

print()
print("=" * 60)
print("parse_uboot_self_test()")
print("=" * 60)

self_test = parse_uboot_self_test(SAMPLE_BOOT)
check("Correct number of entries (Self-test starting excluded)", len(self_test) == 3)
check("Multi-word label 'DDR4 Memory' split correctly (not truncated to 'DDR4')",
      self_test[0] == ("DDR4 Memory", "Bank0: 1982 MB, Bank1: 6144 MB", True))
check("[FAIL] line captured with passed=False",
      self_test[1] == ("Temperatures", "CPU sensor not responding", False))
check("Multi-word label 'USB PD controller' split correctly",
      self_test[2] == ("USB PD controller", "ID 0x25", True))
check("'Self-test starting' line excluded from results",
      not any(label.lower().startswith("self-test") for label, _, _ in self_test))

check("No self-test lines -> empty list", parse_uboot_self_test("nothing here") == [])

VALUE_ONLY_LABEL = "[ OK ] Some Flag\n"
label_only = parse_uboot_self_test(VALUE_ONLY_LABEL)
check("Label with no value -> value is ''",
      label_only == [("Some Flag", "", True)])


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
