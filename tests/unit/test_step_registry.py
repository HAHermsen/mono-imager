#!/usr/bin/env python3
"""
mono-imager: Unit tests for step_registry.py's FlowRunner.run() execution
engine and the U-Boot/staging-boot registries.

No hardware required. Registers small throwaway steps under unique
os_name/transfer pairs so scenarios can't interfere with each other or
with the real journeys (which this file deliberately does NOT import —
importing mono_imager.journeys would populate the shared, process-wide
_registry with every real step, which test_journey_resolution.py already
covers for ordering/isolation. This file isolates the FlowRunner.run()
LOOP itself: requires-gating, exception handling, produces-marking, and
stop-on-first-failure — none of which is exercised by list_journey()/
steps_for(), which only ever resolve order and never execute anything).

What this tests:
  - FlowRunner.run(): success path executes steps in resolved order
  - FlowRunner.run(): missing requires at runtime aborts without calling fn
  - FlowRunner.run(): a step raising is caught, treated as failure, no crash
  - FlowRunner.run(): produces are defaulted to True only if not already set
  - FlowRunner.run(): stops immediately on the first failing step
  - register_uboot_steps() / run_uboot_steps()
  - register_staging_boot() / get_staging_boot_methods()

Run: python tests/unit/test_step_registry.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.step_registry import (
    register_step, register_uboot_steps,
    register_staging_boot, get_staging_boot_methods,
    FlowRunner, StepContext,
)

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
# FlowRunner.run(): success path, order, produces-marking
# ============================================================================

print("=" * 60)
print("FlowRunner.run(): success path")
print("=" * 60)

OS_A = "_TestOS_Success"
call_order = []


@register_step(os=[OS_A], transfer=["lan"], requires=[], produces=["network_up"], label="bring up net")
def _step_a(ctx):
    call_order.append("a")
    return True


@register_step(os=[OS_A], transfer=["lan"], requires=["network_up"], produces=["flashed"], label="flash")
def _step_b(ctx):
    call_order.append("b")
    ctx.set("flashed", {"bytes_written": 12345})  # explicit value, not the True default
    return True


ctx = StepContext(os_name=OS_A, transfer="lan")
result = FlowRunner(OS_A, "lan", ctx).run()

check("run() returns True when every step passes", result is True)
check("steps executed in dependency order", call_order == ["a", "b"])
check("produces defaults to True when the step didn't set it itself", ctx.get("network_up") is True)
check("produces is NOT overwritten when the step already set an explicit value",
      ctx.get("flashed") == {"bytes_written": 12345})


# ============================================================================
# FlowRunner.run(): missing requires aborts without calling the step
# ============================================================================

print()
print("=" * 60)
print("FlowRunner.run(): missing requires at runtime")
print("=" * 60)

OS_B = "_TestOS_MissingRequires"
called = []


@register_step(os=[OS_B], transfer=["lan"], requires=["never_produced"], produces=["done"], label="needs ghost key")
def _step_c(ctx):
    called.append("c")
    return True


ctx = StepContext(os_name=OS_B, transfer="lan")
with patch("builtins.print"):
    result = FlowRunner(OS_B, "lan", ctx).run()

check("run() returns False when a requires key is never satisfied", result is False)
check("the step function itself is never called", called == [])


# ============================================================================
# FlowRunner.run(): a step raising is caught, not propagated
# ============================================================================

print()
print("=" * 60)
print("FlowRunner.run(): step raises an exception")
print("=" * 60)

OS_C = "_TestOS_Raises"
after_called = []


@register_step(os=[OS_C], transfer=["lan"], requires=[], produces=["x"], label="boom")
def _step_d(ctx):
    raise RuntimeError("device unplugged mid-step")


@register_step(os=[OS_C], transfer=["lan"], requires=["x"], produces=["y"], label="never runs")
def _step_e(ctx):
    after_called.append("e")
    return True


ctx = StepContext(os_name=OS_C, transfer="lan")
try:
    with patch("builtins.print"):
        result = FlowRunner(OS_C, "lan", ctx).run()
    check("run() does not propagate the exception — returns False instead", result is False)
except RuntimeError:
    check("run() does not propagate the exception — returns False instead", False)

check("a later step depending on the failed one's output never runs", after_called == [])


# ============================================================================
# FlowRunner.run(): stops on first failure, later independent steps skipped
# ============================================================================

print()
print("=" * 60)
print("FlowRunner.run(): stop-on-first-failure")
print("=" * 60)

OS_D = "_TestOS_StopOnFailure"
ran = []


@register_step(os=[OS_D], transfer=["lan"], requires=[], produces=["p1"], label="fails cleanly")
def _step_f(ctx):
    ran.append("f")
    return False


@register_step(os=[OS_D], transfer=["lan"], requires=[], produces=["p2"], label="independent — still skipped")
def _step_g(ctx):
    ran.append("g")
    return True


ctx = StepContext(os_name=OS_D, transfer="lan")
with patch("builtins.print"):
    result = FlowRunner(OS_D, "lan", ctx).run()

check("run() returns False", result is False)
check("step f ran", "f" in ran)
check("step g (registered after f, independent) never runs once f fails", "g" not in ran)


# ============================================================================
# register_uboot_steps() / run_uboot_steps()
# ============================================================================

print()
print("=" * 60)
print("register_uboot_steps() / run_uboot_steps()")
print("=" * 60)


class _FakeCtx:
    def __init__(self, device):
        self.device = device


class _FakeRunner:
    """Minimal stand-in exposing only what run_uboot_steps() reads."""
    def __init__(self, os_name, transfer, device):
        self.os_name = os_name
        self.transfer = transfer
        self.ctx = _FakeCtx(device)
    run_uboot_steps = FlowRunner.run_uboot_steps


fake_device = MagicMock()

r = _FakeRunner("_TestOS_NoUboot", "lan", fake_device)
check("no uboot steps registered -> True (nothing to do)", r.run_uboot_steps() is True)

register_uboot_steps("_TestOS_UbootOK", "lan", lambda d: d is fake_device)
r = _FakeRunner("_TestOS_UbootOK", "lan", fake_device)
check("registered fn returning True -> run_uboot_steps() True", r.run_uboot_steps() is True)

register_uboot_steps("_TestOS_UbootFail", "lan", lambda d: False)
r = _FakeRunner("_TestOS_UbootFail", "lan", fake_device)
check("registered fn returning False -> run_uboot_steps() False", r.run_uboot_steps() is False)


def _raising_uboot_fn(d):
    raise RuntimeError("u-boot command timed out")


register_uboot_steps("_TestOS_UbootRaises", "lan", _raising_uboot_fn)
r = _FakeRunner("_TestOS_UbootRaises", "lan", fake_device)
check("registered fn raising -> run_uboot_steps() False (no crash)", r.run_uboot_steps() is False)


# ============================================================================
# register_staging_boot() / get_staging_boot_methods()
# ============================================================================

print()
print("=" * 60)
print("register_staging_boot() / get_staging_boot_methods()")
print("=" * 60)

check("no registration -> defaults to boot_recovery/login_recovery",
      get_staging_boot_methods("_TestOS_NoStaging", "lan") ==
      {"boot_method": "boot_recovery", "login_method": "login_recovery"})

register_staging_boot("_TestOS_Staging", "usb",
                       boot_method="boot_linux_staging", login_method="login_staging")
check("registered override is returned verbatim",
      get_staging_boot_methods("_TestOS_Staging", "usb") ==
      {"boot_method": "boot_linux_staging", "login_method": "login_staging"})
check("a different transfer for the same OS is unaffected",
      get_staging_boot_methods("_TestOS_Staging", "lan") ==
      {"boot_method": "boot_recovery", "login_method": "login_recovery"})


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
