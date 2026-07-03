#!/usr/bin/env python3
"""
mono-imager: Unit tests for MonoImager._setup_recovery_network() (issue #9).

No hardware required. All device interactions are mocked.

What this tests:
  - _netmask_to_prefix()      — dotted mask / bare prefix parsing
  - _setup_recovery_network() — DHCP-first, manual fallback, session-cache
                                 reuse, and the manual-entry retry loop

Run: python tests/unit/test_device_network.py
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.tui import MonoImager, _netmask_to_prefix

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


def make_app():
    app = MonoImager()
    app.clear_screen = lambda: None
    return app


# Any run_script call in the happy path needs LOWER_UP (carrier check) and
# RC=0 (network-apply script) somewhere in its return value.
ETH_UP_OUTPUT = "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\nRC=0"


# ============================================================================
# _netmask_to_prefix()
# ============================================================================

print("=" * 60)
print("_netmask_to_prefix()")
print("=" * 60)

check("255.255.255.0 -> 24", _netmask_to_prefix("255.255.255.0") == "24")
check("255.255.0.0 -> 16",   _netmask_to_prefix("255.255.0.0") == "16")
check("bare prefix '24' -> '24'", _netmask_to_prefix("24") == "24")
check("empty string -> None", _netmask_to_prefix("") is None)
check("non-contiguous mask -> None", _netmask_to_prefix("255.0.255.0") is None)
check("garbage -> None", _netmask_to_prefix("not.an.ip.mask") is None)
check("prefix out of range -> None", _netmask_to_prefix("33") is None)


# ============================================================================
# _setup_recovery_network() — DHCP success (first time this session)
# ============================================================================

print()
print("=" * 60)
print("_setup_recovery_network(): DHCP succeeds")
print("=" * 60)

app = make_app()
d = MagicMock()
d.run_script.return_value = ETH_UP_OUTPUT
lease = {"ip": "10.0.0.69", "prefix": "24", "gateway": "10.0.0.1", "dns": "10.0.0.1"}

with patch("mono_imager.recovery_orchestrator.try_dhcp", return_value=lease), \
     patch("mono_imager.recovery_orchestrator.check_internet_reachable", return_value=True), \
     patch("builtins.input", side_effect=AssertionError("should not prompt on DHCP success")), \
     patch("builtins.print"):
    result = app._setup_recovery_network(d)

check("returns True", result is True)
check("device_net cached from lease", app.device_net == {**lease, "source": "dhcp"})


# ============================================================================
# _setup_recovery_network() — DHCP fails, manual entry succeeds first try
# ============================================================================

print()
print("=" * 60)
print("_setup_recovery_network(): DHCP fails -> manual entry succeeds")
print("=" * 60)

app = make_app()
d = MagicMock()
d.run_script.return_value = ETH_UP_OUTPUT

manual_inputs = iter([
    "192.168.1.50",      # device IP
    "255.255.255.0",     # subnet mask
    "192.168.1.1",       # gateway
    "1.1.1.1",           # dns
])

with patch("mono_imager.recovery_orchestrator.try_dhcp", return_value=None), \
     patch("mono_imager.recovery_orchestrator.check_internet_reachable", return_value=True), \
     patch("builtins.input", side_effect=lambda *_: next(manual_inputs)), \
     patch("builtins.print"):
    result = app._setup_recovery_network(d)

check("returns True", result is True)
check("device_net cached from manual entry",
      app.device_net == {"ip": "192.168.1.50", "prefix": "24", "gateway": "192.168.1.1",
                          "dns": "1.1.1.1", "source": "manual", "iface": "eth0"})


# ============================================================================
# _setup_recovery_network() — already resolved this session -> cache reuse
# ============================================================================

print()
print("=" * 60)
print("_setup_recovery_network(): cached device_net is reused, no prompting")
print("=" * 60)

app = make_app()
app.device_net = {"ip": "192.168.1.50", "prefix": "24", "gateway": "192.168.1.1",
                   "dns": "1.1.1.1", "source": "manual"}
d = MagicMock()
d.run_script.return_value = ETH_UP_OUTPUT

with patch("mono_imager.recovery_orchestrator.try_dhcp") as mock_dhcp, \
     patch("mono_imager.recovery_orchestrator.check_internet_reachable", return_value=True), \
     patch("builtins.input", side_effect=AssertionError("should not prompt when cached")), \
     patch("builtins.print"):
    result = app._setup_recovery_network(d)

check("returns True", result is True)
check("DHCP not attempted again", not mock_dhcp.called)


# ============================================================================
# _setup_recovery_network() — cached config went stale -> re-resolves
# ============================================================================

print()
print("=" * 60)
print("_setup_recovery_network(): stale cache falls back to DHCP re-attempt")
print("=" * 60)

app = make_app()
app.device_net = {"ip": "192.168.1.50", "prefix": "24", "gateway": "192.168.1.1",
                   "dns": "1.1.1.1", "source": "manual"}
d = MagicMock()
d.run_script.return_value = ETH_UP_OUTPUT
new_lease = {"ip": "10.0.0.5", "prefix": "24", "gateway": "10.0.0.1", "dns": ""}

with patch("mono_imager.recovery_orchestrator.try_dhcp", return_value=new_lease), \
     patch("mono_imager.recovery_orchestrator.check_internet_reachable", side_effect=[False, True]), \
     patch("builtins.print"):
    result = app._setup_recovery_network(d)

check("returns True after re-resolving", result is True)
check("device_net replaced with fresh DHCP lease", app.device_net == {**new_lease, "source": "dhcp"})


# ============================================================================
# _setup_recovery_network() — manual entry retry loop
# ============================================================================

print()
print("=" * 60)
print("_setup_recovery_network(): first manual attempt unreachable, retries, then succeeds")
print("=" * 60)

app = make_app()
d = MagicMock()
d.run_script.return_value = ETH_UP_OUTPUT

manual_inputs = iter([
    "192.168.1.50", "255.255.255.0", "192.168.1.1", "1.1.1.1",   # attempt 1
    "y",                                                          # retry?
    "192.168.1.51", "255.255.255.0", "192.168.1.1", "1.1.1.1",   # attempt 2
])

with patch("mono_imager.recovery_orchestrator.try_dhcp", return_value=None), \
     patch("mono_imager.recovery_orchestrator.check_internet_reachable", side_effect=[False, True]), \
     patch("builtins.input", side_effect=lambda *_: next(manual_inputs)), \
     patch("builtins.print"):
    result = app._setup_recovery_network(d)

check("returns True after retry", result is True)
check("second attempt's IP was cached", app.device_net["ip"] == "192.168.1.51")


# ============================================================================
# _setup_recovery_network() — user declines to retry -> aborts
# ============================================================================

print()
print("=" * 60)
print("_setup_recovery_network(): user declines retry -> returns False")
print("=" * 60)

app = make_app()
d = MagicMock()
d.run_script.return_value = ETH_UP_OUTPUT

manual_inputs = iter([
    "192.168.1.50", "255.255.255.0", "192.168.1.1", "1.1.1.1",   # attempt 1
    "n",                                                          # decline retry
])

with patch("mono_imager.recovery_orchestrator.try_dhcp", return_value=None), \
     patch("mono_imager.recovery_orchestrator.check_internet_reachable", return_value=False), \
     patch("builtins.input", side_effect=lambda *_: next(manual_inputs)), \
     patch("builtins.print"):
    result = app._setup_recovery_network(d)

check("returns False", result is False)
check("device_net left unset", app.device_net is None)


# ============================================================================
# _startup_network_setup() — runs at launch, before the main menu
# ============================================================================

print()
print("=" * 60)
print("_startup_network_setup(): no port found -> exits cleanly (nothing the tool can do)")
print("=" * 60)

app = make_app()
with patch.object(app, "_select_port", return_value=None), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    try:
        app._startup_network_setup()
        check("exits via SystemExit when no port found", False)
    except SystemExit as e:
        check("exits via SystemExit when no port found", e.code == 0)

check("device_net left unset when no port found", app.device_net is None)


print()
print("=" * 60)
print("_startup_network_setup(): bootstrap fails -> postponed, no crash")
print("=" * 60)

app = make_app()
with patch.object(app, "_select_port", return_value="COM5"), \
     patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=None), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app._startup_network_setup()

check("device_net left unset when bootstrap fails", app.device_net is None)


print()
print("=" * 60)
print("_startup_network_setup(): success -> resolves device_net and disconnects")
print("=" * 60)

app = make_app()
d = MagicMock()

def fake_setup_recovery_network(dev):
    app.device_net = {"ip": "10.0.0.69", "prefix": "24", "gateway": "10.0.0.1",
                       "dns": "10.0.0.1", "source": "dhcp"}
    return True

with patch.object(app, "_select_port", return_value="COM5"), \
     patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch.object(app, "_setup_recovery_network", side_effect=fake_setup_recovery_network) as mock_setup, \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app._startup_network_setup()

check("device_net resolved via _setup_recovery_network", app.device_net is not None
      and app.device_net["source"] == "dhcp")
check("_setup_recovery_network was called with the bootstrapped device", mock_setup.called
      and mock_setup.call_args[0][0] is d)
check("device disconnected afterward", d.disconnect.called)


# ============================================================================
# get_journey() forwards device_net to every journey unconditionally
# ============================================================================

print()
print("=" * 60)
print("get_journey(): device_net is forwarded into StepContext regardless of OS/transfer")
print("=" * 60)

from mono_imager.journeys import get_journey

net = {"ip": "10.0.0.69", "prefix": "24", "gateway": "10.0.0.1", "dns": "", "source": "dhcp"}
journey = get_journey("OpenWRT", "usb", device=MagicMock(), device_net=net)
check("ctx.device_net matches what was passed in", journey.ctx.device_net == net)

journey_none = get_journey("OpenWRT", "usb", device=MagicMock())
check("device_net defaults to None when not resolved yet", journey_none.ctx.device_net is None)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
