#!/usr/bin/env python3
"""
mono-imager: Unit tests for flash_orchestrator.py — the core phase
implementations and their pure-logic helpers.

No hardware required. All device interactions are mocked.

What this tests:
  - reset_results()/step()/print_report()  — result tracking & auto-numbering
  - parse_active_eth_iface()                — LOWER_UP eth* detection
  - _FirmwareHandler report store            — wait_for_report()/peek_report()
  - start_http_server()                      — reports cleared, OSError -> None
  - phase1_uboot()                           — port detection, connect, autoboot
                                                 interrupt gating (incl. already-
                                                 at-prompt skip)
  - phase1_recovery()                        — boot/login gating, custom
                                                 staging-boot method names
  - phase1_bootstrap()                       — short-circuits on phase1_uboot
                                                 failure without calling
                                                 phase1_recovery()
  - phase3_flash()                           — step09/10/11/12 gating,
                                                 buffered vs. streaming script
                                                 selection at FLASH_SIZE_CAP
  - phase4_postflash()                       — sends reboot, always True

NOTE: parse_uboot_env()/capture_uboot_env()/restore_uboot_env() are covered
in test_uboot_env.py, not duplicated here.

Run: python tests/unit/test_flash_orchestrator.py
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

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


# ============================================================================
# reset_results() / step() / print_report()
# ============================================================================

print("=" * 60)
print("reset_results() / step() / print_report()")
print("=" * 60)

core.reset_results()
check("results starts empty", core.results == [])

n1 = core.step(0, "first auto-numbered step", True)
n2 = core.step(0, "second auto-numbered step", True)
check("step() returns the passed value", n1 is True and n2 is True)
check("auto-numbering starts at 1 and increments", core.results[0][0] == 1 and core.results[1][0] == 2)

core.step(99, "explicit step number", True)
check("explicit step number is used as-is", core.results[2][0] == 99)

core.reset_results()
check("reset_results() clears accumulated results", core.results == [])
n3 = core.step(0, "after reset", True)
check("auto-numbering restarts at 1 after reset_results()", core.results[0][0] == 1)

core.reset_results()
core.step(0, "ok step", True)
core.step(0, "bad step", False, "reason here")
check("print_report() returns False when any step failed", core.print_report() is False)

core.reset_results()
core.step(0, "ok step 1", True)
core.step(0, "ok step 2", True)
check("print_report() returns True when all steps passed", core.print_report() is True)

core.reset_results()
check("print_report() on empty results -> True (0/0 passed == total)", core.print_report() is True)


# ============================================================================
# parse_active_eth_iface()
# ============================================================================

print()
print("=" * 60)
print("parse_active_eth_iface()")
print("=" * 60)

ip_link_up = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
    "2: eth0: <BROADCAST,MULTICAST> mtu 1500\n"
    "3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
)
check("first eth* iface with LOWER_UP is returned (loopback excluded)",
      core.parse_active_eth_iface(ip_link_up) == "eth1")

ip_link_down = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
    "2: eth0: <BROADCAST,MULTICAST> mtu 1500\n"
)
check("no eth* iface with LOWER_UP -> None", core.parse_active_eth_iface(ip_link_down) is None)

check("empty input -> None", core.parse_active_eth_iface("") is None)

ip_link_multi = (
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
    "3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
)
check("first matching iface wins when several have LOWER_UP",
      core.parse_active_eth_iface(ip_link_multi) == "eth0")


# ============================================================================
# parse_active_eth_ifaces()  (multi-cable / complex topology, issue #19)
# ============================================================================

print()
print("=" * 60)
print("parse_active_eth_ifaces()")
print("=" * 60)

_multi = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
    "3: eth1: <BROADCAST,MULTICAST> mtu 1500\n"
    "4: eth2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
)
check("returns every live eth* (loopback excluded), in order",
      core.parse_active_eth_ifaces(_multi) == ["eth0", "eth2"])

check("single live port -> one-element list",
      core.parse_active_eth_ifaces("3: eth1: <UP,LOWER_UP> mtu 1500\n") == ["eth1"])

check("no live port -> empty list",
      core.parse_active_eth_ifaces("2: eth0: <BROADCAST> mtu 1500\n") == [])

check("empty input -> empty list", core.parse_active_eth_ifaces("") == [])

# The singular helper must stay consistent: first of the list, or None.
check("singular helper returns first of the multi list",
      core.parse_active_eth_iface(_multi) == "eth0")


# ============================================================================
# _FirmwareHandler report store: wait_for_report() / peek_report()
# ============================================================================

print()
print("=" * 60)
print("wait_for_report() / peek_report()")
print("=" * 60)

with core._FirmwareHandler._reports_lock:
    core._FirmwareHandler._reports.clear()

core._FirmwareHandler._reports["09"] = "200"
check("wait_for_report() returns and consumes an already-present value",
      core.wait_for_report("09", timeout=1.0) == "200")
check("wait_for_report() consumes — second call finds nothing and times out",
      core.wait_for_report("09", timeout=0.2) is None)

core._FirmwareHandler._reports["progress"] = "42%"
check("peek_report() returns without consuming", core.peek_report("progress") == "42%")
check("peek_report() does not consume — value still present", core.peek_report("progress") == "42%")

check("wait_for_report() on missing key times out -> None",
      core.wait_for_report("missing", timeout=0.2) is None)
check("peek_report() on missing key -> None", core.peek_report("nope") is None)

with core._FirmwareHandler._reports_lock:
    core._FirmwareHandler._reports.clear()


# ============================================================================
# start_http_server()
# ============================================================================

print()
print("=" * 60)
print("start_http_server()")
print("=" * 60)

core._FirmwareHandler._reports["stale"] = "leftover-from-a-previous-attempt"


class FakeHTTPServer:
    def __init__(self, addr, handler):
        self.addr = addr
        self.handler = handler

    def serve_forever(self):
        pass


with patch("mono_imager.flash_orchestrator.HTTPServer", FakeHTTPServer):
    server = core.start_http_server("127.0.0.1", 8080, Path("dummy.img"))

check("returns the constructed server on success", isinstance(server, FakeHTTPServer))
check("stale reports are cleared on server start", "stale" not in core._FirmwareHandler._reports)


def raising_http_server(addr, handler):
    raise OSError("port already in use")


with patch("mono_imager.flash_orchestrator.HTTPServer", raising_http_server):
    server = core.start_http_server("127.0.0.1", 8080, Path("dummy.img"))

check("OSError during bind -> None (no crash)", server is None)


# ============================================================================
# phase1_uboot()
# ============================================================================

print()
print("=" * 60)
print("phase1_uboot()")
print("=" * 60)


def make_fake_device(connect=True, already_at_prompt=False, autoboot=True):
    d = MagicMock()
    d.connect.return_value = connect
    d.probe_uboot_prompt.return_value = already_at_prompt
    d.wait_for_autoboot.return_value = autoboot
    d.send_command.return_value = "baudrate=115200\n"  # capture_uboot_env() input
    return d


known_port = MagicMock(device="COM5")

with patch("mono_imager.flash_orchestrator.detect_serial_ports", return_value=([], [])):
    result = core.phase1_uboot("COM5")
check("port not found in detected list -> None", result is None)

with patch("mono_imager.flash_orchestrator.detect_serial_ports", side_effect=RuntimeError("no pyserial")):
    result = core.phase1_uboot("COM5")
check("detect_serial_ports raising -> None (no crash)", result is None)

fake_dev = make_fake_device(connect=False)
with patch("mono_imager.flash_orchestrator.detect_serial_ports", return_value=([known_port], [])), \
     patch("mono_imager.flash_orchestrator.SerialDevice", return_value=fake_dev):
    result = core.phase1_uboot("COM5")
check("connect() failing -> None", result is None)

fake_dev = make_fake_device(connect=True, already_at_prompt=True)
with patch("mono_imager.flash_orchestrator.detect_serial_ports", return_value=([known_port], [])), \
     patch("mono_imager.flash_orchestrator.SerialDevice", return_value=fake_dev):
    result = core.phase1_uboot("COM5")
check("already at U-Boot prompt -> success without needing wait_for_autoboot",
      result is fake_dev)
check("wait_for_autoboot() not called when already at prompt", not fake_dev.wait_for_autoboot.called)
check("U-Boot env snapshot captured onto the device object",
      getattr(fake_dev, "captured_uboot_env", None) == {"baudrate": "115200"})

fake_dev = make_fake_device(connect=True, already_at_prompt=False, autoboot=True)
with patch("mono_imager.flash_orchestrator.detect_serial_ports", return_value=([known_port], [])), \
     patch("mono_imager.flash_orchestrator.SerialDevice", return_value=fake_dev), \
     patch("builtins.print"):
    result = core.phase1_uboot("COM5")
check("not at prompt, autoboot interrupt succeeds -> success", result is fake_dev)

fake_dev = make_fake_device(connect=True, already_at_prompt=False, autoboot=False)
with patch("mono_imager.flash_orchestrator.detect_serial_ports", return_value=([known_port], [])), \
     patch("mono_imager.flash_orchestrator.SerialDevice", return_value=fake_dev), \
     patch("builtins.print"):
    result = core.phase1_uboot("COM5")
check("autoboot interrupt failing -> None", result is None)
check("device disconnected after autoboot interrupt failure", fake_dev.disconnect.called)


# ============================================================================
# phase1_recovery()
# ============================================================================

print()
print("=" * 60)
print("phase1_recovery()")
print("=" * 60)

d = MagicMock()
d.boot_recovery.return_value = True
d.login_recovery.return_value = True
result = core.phase1_recovery(d)
check("default boot_recovery/login_recovery success -> returns the device", result is d)

d = MagicMock()
d.boot_recovery.return_value = False
result = core.phase1_recovery(d)
check("boot_recovery() failing -> None", result is None)
check("device disconnected on boot failure", d.disconnect.called)

d = MagicMock()
d.boot_recovery.return_value = True
d.login_recovery.return_value = False
result = core.phase1_recovery(d)
check("login_recovery() failing -> None", result is None)
check("device disconnected on login failure", d.disconnect.called)

d = MagicMock()
d.boot_linux_staging.return_value = True
d.login_staging.return_value = True
result = core.phase1_recovery(d, boot_method="boot_linux_staging", login_method="login_staging")
check("custom staging boot/login method names are used", result is d)
check("boot_linux_staging() was called (not boot_recovery)", d.boot_linux_staging.called)
check("login_staging() was called (not login_recovery)", d.login_staging.called)

d = MagicMock()
d.boot_recovery.side_effect = RuntimeError("serial broke")
result = core.phase1_recovery(d)
check("boot_recovery() raising -> None (no crash)", result is None)


# ============================================================================
# phase1_bootstrap()
# ============================================================================

print()
print("=" * 60)
print("phase1_bootstrap()")
print("=" * 60)

with patch("mono_imager.flash_orchestrator.phase1_uboot", return_value=None) as mock_uboot, \
     patch("mono_imager.flash_orchestrator.phase1_recovery") as mock_recovery:
    result = core.phase1_bootstrap("COM5")
check("phase1_uboot() failing short-circuits -> None", result is None)
check("phase1_recovery() never called when phase1_uboot() fails", not mock_recovery.called)

sentinel_device = MagicMock()
sentinel_result = MagicMock()
with patch("mono_imager.flash_orchestrator.phase1_uboot", return_value=sentinel_device), \
     patch("mono_imager.flash_orchestrator.phase1_recovery", return_value=sentinel_result) as mock_recovery:
    result = core.phase1_bootstrap("COM5")
check("phase1_recovery() called with phase1_uboot()'s device",
      mock_recovery.call_args[0][0] is sentinel_device)
check("phase1_bootstrap() returns phase1_recovery()'s result", result is sentinel_result)


# ============================================================================
# phase3_flash()
# ============================================================================

print()
print("=" * 60)
print("phase3_flash()")
print("=" * 60)

GOOD_DD_OUTPUT = "7811+0 records in\n7811+0 records out\nRC=0"

d = MagicMock()
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value=None):
    result = core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0")
check("step09 report never arrives -> False", result is False)

d = MagicMock()
d.launch_script.side_effect = RuntimeError("serial broke")
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="200"), \
     patch.object(core, "with_spinner", return_value=(GOOD_DD_OUTPUT, None)), \
     patch("builtins.print"):
    result = core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0")
check("launch_script() raising for step09 doesn't crash — step09 still resolved via wait_for_report",
      result is True)

d = MagicMock()
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="404"), \
     patch.object(core, "with_spinner", return_value=(GOOD_DD_OUTPUT, None)):
    result = core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0")
check("non-200 status reported for step09 -> False", result is False)

d = MagicMock()
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="200"), \
     patch.object(core, "with_spinner", return_value=(None, RuntimeError("dd hung"))), \
     patch("builtins.print"):
    result = core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0")
check("run_script() raising during flash -> False", result is False)

d = MagicMock()
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="200"), \
     patch.object(core, "with_spinner", return_value=("no useful markers here", None)):
    result = core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0")
check("dd output missing 'records in/out' -> False", result is False)

d = MagicMock()
bad_output = "7811+0 records in\n7811+0 records out\ncurl: (22) error: unauthorized\nRC=1"
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="200"), \
     patch.object(core, "with_spinner", return_value=(bad_output, None)):
    result = core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0")
check("dd output containing 'error' -> False despite records present", result is False)

d = MagicMock()
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="200"), \
     patch.object(core, "with_spinner", return_value=(GOOD_DD_OUTPUT, None)):
    result = core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0")
check("full success path -> True", result is True)

# --- streaming vs. buffered script selection at FLASH_SIZE_CAP -------------

captured_calls = []


def capturing_with_spinner(fn, *args, message="", **kwargs):
    captured_calls.append(args[0] if args else None)
    return GOOD_DD_OUTPUT, None


# FLASH_SIZE_CAP is a local inside phase3_flash (≈80% of the confirmed 3.8GB
# recovery-Linux tmpfs root), not a module constant — recomputed here to
# probe the exact boundary rather than duplicating a magic number blindly.
FLASH_SIZE_CAP = int(3.8 * 1024**3 * 0.8)

d = MagicMock()
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="200"), \
     patch.object(core, "with_spinner", side_effect=capturing_with_spinner):
    core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0", firmware_size=FLASH_SIZE_CAP)
check("at exactly FLASH_SIZE_CAP -> buffered (download-then-dd) script",
      "bs=4096" in captured_calls[-1] and " | dd" not in captured_calls[-1])

d = MagicMock()
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="200"), \
     patch.object(core, "with_spinner", side_effect=capturing_with_spinner):
    core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0", firmware_size=FLASH_SIZE_CAP + 1)
check("just over FLASH_SIZE_CAP -> streaming (curl | dd) script",
      "bs=1M" in captured_calls[-1] and " | " in captured_calls[-1])

d = MagicMock()
with patch("mono_imager.flash_orchestrator.wait_for_report", return_value="200"), \
     patch.object(core, "with_spinner", side_effect=capturing_with_spinner):
    core.phase3_flash(d, "192.168.1.50", 8080, "/dev/mmcblk0", firmware_size=0)
check("firmware_size=0 (unknown) -> buffered script, not streaming",
      "bs=4096" in captured_calls[-1])


# ============================================================================
# phase4_postflash()
# ============================================================================

print()
print("=" * 60)
print("phase4_postflash()")
print("=" * 60)

d = MagicMock()
result = core.phase4_postflash(d)
check("always returns True", result is True)
check("sends reboot without waiting for a prompt",
      d.send_command.call_args == (("reboot",), {"wait_for_prompt": False, "timeout": 5}))


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
