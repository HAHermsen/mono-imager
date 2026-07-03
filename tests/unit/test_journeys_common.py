#!/usr/bin/env python3
"""
mono-imager: Unit tests for journeys/_common.py's _step_network_ready().

No hardware required.

_step_network_ready() is the requires=["network_up"] checkpoint shared by
every LAN journey and by OpenWRT/OPNsense-via-USB (see _common.py's module
docstring) — it does NOT resolve networking itself, it only confirms that
MonoImager._setup_recovery_network() already resolved ctx.device_net
before the journey started, and forwards ctx.device_ip from it.

What this tests:
  - ctx.device_net is None                 -> step fails, ctx.device_ip untouched
  - ctx.device_net has no usable "ip"       -> step fails (empty dict, empty ip)
  - ctx.device_net resolved, ctx.device_ip
    not yet set                             -> step passes, device_ip populated
  - ctx.device_ip already set               -> step passes, device_ip NOT overwritten

Run: python tests/unit/test_journeys_common.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.journeys._common import _step_network_ready
from mono_imager.step_registry import StepContext
from mono_imager import flash_orchestrator as core

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


DHCP_NET = {"ip": "10.0.0.69", "prefix": "24", "gateway": "10.0.0.1",
            "dns": "10.0.0.1", "source": "dhcp"}


# ============================================================================
# device_net unresolved -> fails
# ============================================================================

print("=" * 60)
print("_step_network_ready(): device_net not resolved")
print("=" * 60)

core.reset_results()
ctx = StepContext(device_net=None)
result = _step_network_ready(ctx)
check("device_net is None -> step fails", result is False)
check("ctx.device_ip left untouched", ctx.device_ip == "")
check("failure reason explains network was never resolved",
      "not resolved" in core.results[-1][3])

core.reset_results()
ctx = StepContext(device_net={})
check("device_net is an empty dict -> step fails", _step_network_ready(ctx) is False)

core.reset_results()
ctx = StepContext(device_net={"ip": "", "prefix": "24", "gateway": "10.0.0.1", "source": "dhcp"})
check("device_net present but ip is an empty string -> step fails", _step_network_ready(ctx) is False)


# ============================================================================
# device_net resolved -> passes, forwards device_ip
# ============================================================================

print()
print("=" * 60)
print("_step_network_ready(): device_net resolved")
print("=" * 60)

core.reset_results()
ctx = StepContext(device_net=DHCP_NET)
result = _step_network_ready(ctx)
check("resolved device_net -> step passes", result is True)
check("ctx.device_ip populated from device_net when previously unset", ctx.device_ip == "10.0.0.69")

core.reset_results()
ctx = StepContext(device_net=DHCP_NET, device_ip="192.168.1.50")
result = _step_network_ready(ctx)
check("already-set ctx.device_ip is preserved -> step still passes", result is True)
check("ctx.device_ip is NOT overwritten by device_net", ctx.device_ip == "192.168.1.50")

core.reset_results()
manual_net = {"ip": "192.168.1.60", "prefix": "24", "gateway": "192.168.1.1",
              "dns": "", "source": "manual"}
ctx = StepContext(device_net=manual_net)
result = _step_network_ready(ctx)
check("manual-source network with no DNS still passes (dns_note is optional)", result is True)
check("device_ip populated from manual network", ctx.device_ip == "192.168.1.60")


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
