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
     patch("serial.Serial", return_value=MagicMock()), \
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
     patch("serial.Serial", return_value=MagicMock()), \
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
# _select_iface() / complex-topology rejection (issue #19)
# ============================================================================

print()
print("=" * 60)
print("complex topology: multiple live Ethernet ports (#19)")
print("=" * 60)

from mono_imager.device_net import RecoveryNetwork

# Direct: user picks port 2 of [eth0, eth3]; the rest are set link down.
rn = RecoveryNetwork()
d = MagicMock()
with patch("builtins.input", return_value="2"), patch("builtins.print"):
    _chosen = rn._select_iface(d, ["eth0", "eth3"])
check("_select_iface returns the user-chosen port", _chosen == "eth3")
_downcmd = d.run_script.call_args[0][0]
check("_select_iface sets a non-chosen port down (eth0)", "ip link set eth0 down" in _downcmd)
check("_select_iface sets eth1/eth2/eth4 down too",
      all(f"ip link set eth{n} down" in _downcmd for n in (1, 2, 4)))
check("_select_iface does NOT set the chosen port down", "set eth3 down" not in _downcmd)

# Abort with q -> None, nothing set down.
rn = RecoveryNetwork()
d = MagicMock()
with patch("builtins.input", return_value="q"), patch("builtins.print"):
    check("_select_iface returns None on abort", rn._select_iface(d, ["eth0", "eth1"]) is None)
check("_select_iface sets nothing down when aborted", not d.run_script.called)

# Invalid entries re-prompt until a valid one is given.
rn = RecoveryNetwork()
d = MagicMock()
_ins = iter(["9", "x", "1"])
with patch("builtins.input", side_effect=lambda *_a, **_k: next(_ins)), patch("builtins.print"):
    check("_select_iface re-prompts on invalid input then accepts",
          rn._select_iface(d, ["eth0", "eth1"]) == "eth0")

# Integration: resolve() must route through _select_iface when >1 port is live.
d = MagicMock()
d.run_script.return_value = (
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
    "3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\nRC=0"
)
_lease = {"ip": "192.168.1.50", "prefix": "24", "gateway": "192.168.1.1", "dns": "192.168.1.1"}
rn = RecoveryNetwork()
with patch.object(RecoveryNetwork, "_select_iface", return_value="eth0") as _mock_sel, \
     patch("mono_imager.recovery_orchestrator.try_dhcp", return_value=_lease), \
     patch("mono_imager.recovery_orchestrator.check_internet_reachable", return_value=True), \
     patch("builtins.print"):
    _ok = rn.resolve(d)
check("resolve() calls _select_iface when multiple ports are live", _mock_sel.called)
check("resolve() succeeds after the user picks a port", _ok is True)


# ============================================================================
# _prompt_manual(): accept IP/CIDR in the address field (issue #17)
# ============================================================================

print()
print("=" * 60)
print("manual entry: IP/CIDR parsing (#17)")
print("=" * 60)

def _responder(mapping):
    def _r(prompt, *_a, **_k):
        for key, val in mapping.items():
            if key in prompt:
                return val
        return ""
    return _r

# "10.1.9.32/24" in the IP field -> prefix from CIDR, mask prompt skipped.
# The mask responder returns /25's mask; if it were (wrongly) used the
# prefix would be "25", so prefix == "24" proves the CIDR won.
rn = RecoveryNetwork()
_map = {"Device IP": "10.1.9.32/24", "Subnet mask": "255.255.255.128",
        "Gateway": "10.1.9.1", "DNS": "10.1.8.8"}
with patch("builtins.input", side_effect=_responder(_map)) as _inp, patch("builtins.print"):
    _net = rn._prompt_manual()
check("IP/CIDR: IP stripped of the /CIDR", _net and _net["ip"] == "10.1.9.32")
check("IP/CIDR: prefix taken from CIDR (24), not the mask", _net and _net["prefix"] == "24")
check("IP/CIDR: subnet-mask prompt never shown",
      not any("Subnet mask" in c.args[0] for c in _inp.call_args_list))
check("IP/CIDR: gateway and DNS still captured",
      _net and _net["gateway"] == "10.1.9.1" and _net["dns"] == "10.1.8.8")

# Plain IP (no CIDR) still uses the subnet-mask prompt as before.
rn = RecoveryNetwork()
_map = {"Device IP": "10.1.9.32", "Subnet mask": "", "Gateway": "10.1.9.1", "DNS": "10.1.8.8"}
with patch("builtins.input", side_effect=_responder(_map)), patch("builtins.print"):
    _net = rn._prompt_manual()
check("plain IP: prefix from default mask (/24)",
      _net and _net["ip"] == "10.1.9.32" and _net["prefix"] == "24")

# Invalid CIDR is rejected.
rn = RecoveryNetwork()
with patch("builtins.input", side_effect=_responder({"Device IP": "10.1.9.32/99"})), patch("builtins.print"):
    check("invalid CIDR (/99) -> None", rn._prompt_manual() is None)

# "/24" with no address before the slash is rejected.
rn = RecoveryNetwork()
with patch("builtins.input", side_effect=_responder({"Device IP": "/24"})), patch("builtins.print"):
    check("'/24' with no IP -> None", rn._prompt_manual() is None)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
