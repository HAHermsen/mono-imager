#!/usr/bin/env python3
"""
mono-imager: Mocked smoke tests for the OpenWRT/Armbian official-procedure
journeys.

No hardware required. Runs each rewritten journey's full FlowRunner.run()
sequence end-to-end against a mocked SerialDevice and mocked network/
firmware primitives, verifying:
  - the journey completes (run() returns True)
  - every step actually gets recorded in flash_orchestrator.results —
    the tracker print_report()/tui.py's flash_success depend on. A step
    that returns True/False without calling step() is invisible to that
    tracker, which is exactly the bug found via real-hardware testing
    (a failed DIP-flip/verify step wasn't recorded, so the run still
    reported full success). This test would have caught that.
  - the recorded step count is at least the resolved step count (some
    steps call step() more than once for sub-checks, e.g. the flash
    steps record "flash executed"/"records confirmed"/"no errors"
    separately)

Covers: openwrt_lan, openwrt_usb, armbian_lan, armbian_usb — the four
journeys rewritten to follow the official DIP-flip/firmware-refresh
procedure, including Armbian's NOR-round-trip firmware refresh and its
two manual DIP-flip confirmations. OPNsense is unchanged and already
covered by test_journey_resolution.py's step-sequence checks.

Run: python tests/unit/test_official_journeys_smoke.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.journeys import get_journey
from mono_imager import flash_orchestrator

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


def _wait_for_report_side_effect(marker, timeout=None):
    """Serves both the reachability check (marker '06') and the flash
    step's record-count regex (marker '07') with one mock function."""
    return {
        "06": "200",
        "07": "5000+0 records in\n5000+0 records out\n",
    }.get(marker, "")


def _send_command_side_effect(cmd, *args, **kwargs):
    # fdisk / mount / firmware-update commands in these journeys all end
    # with "echo RC=$?" — answering all of them the same way is enough,
    # none of these steps otherwise inspect send_command's return value.
    if "RC=$?" in cmd:
        return "ok\nRC=0"
    return ""


def _make_mock_device():
    d = MagicMock()
    d.send_command.side_effect = _send_command_side_effect
    d.launch_script.return_value = None
    d.run_script.return_value = "5000+0 records in\n5000+0 records out\n"
    d.wait_for_autoboot.return_value = True
    d.boot_recovery.return_value = True
    d.login_recovery.return_value = True
    return d


DEVICE_NET = {
    "ip": "192.168.168.222", "prefix": "24", "gateway": "192.168.168.1",
    "dns": "", "iface": "eth0", "source": "dhcp",
}


def _run_journey(os_name, transfer, **kwargs):
    device = _make_mock_device()
    runner = get_journey(
        os_name=os_name, transfer=transfer, device=device,
        host_ip="192.168.168.84", http_port=8080,
        firmware_path=Path(f"/tmp/fake_{os_name.lower()}.img"),
        device_net=DEVICE_NET,
        **kwargs,
    )
    n_steps = len(runner.steps_for())
    ok = runner.run()
    results = list(flash_orchestrator.results)
    return ok, results, n_steps


def test_openwrt_lan():
    print("\n--- OpenWRT + lan ---")
    with patch("mono_imager.journeys.openwrt_lan.start_http_server", return_value=MagicMock()), \
         patch("mono_imager.journeys.openwrt_lan.wait_for_report", side_effect=_wait_for_report_side_effect), \
         patch("builtins.input", return_value=""):
        ok, results, n_steps = _run_journey("OpenWRT", "lan")
    check("OpenWRT+lan: run() returns True", ok)
    check("OpenWRT+lan: all recorded results passed", bool(results) and all(r[2] for r in results))
    check(f"OpenWRT+lan: results cover all {n_steps} resolved steps", len(results) >= n_steps)


def test_openwrt_usb():
    print("\n--- OpenWRT + usb ---")
    with patch("mono_imager.journeys.openwrt_usb.find_image_on_usb",
               return_value=("/mnt/usb/openwrt.img", "img")), \
         patch("builtins.input", return_value=""):
        ok, results, n_steps = _run_journey(
            "OpenWRT", "usb", usb_device="/dev/sda", usb_mount="/mnt/usb"
        )
    check("OpenWRT+usb: run() returns True", ok)
    check("OpenWRT+usb: all recorded results passed", bool(results) and all(r[2] for r in results))
    check(f"OpenWRT+usb: results cover all {n_steps} resolved steps", len(results) >= n_steps)


def test_armbian_lan():
    print("\n--- Armbian + lan ---")
    with patch("mono_imager.journeys.armbian_lan.start_http_server", return_value=MagicMock()), \
         patch("mono_imager.journeys.armbian_lan.wait_for_report", side_effect=_wait_for_report_side_effect), \
         patch("mono_imager.recovery_orchestrator.run_firmware_update", return_value=True), \
         patch("mono_imager.device_net.RecoveryNetwork") as MockNet, \
         patch("builtins.input", return_value=""):
        MockNet.return_value.resolve.return_value = True
        ok, results, n_steps = _run_journey("Armbian", "lan")
    check("Armbian+lan: run() returns True", ok)
    check("Armbian+lan: all recorded results passed", bool(results) and all(r[2] for r in results))
    check(f"Armbian+lan: results cover all {n_steps} resolved steps", len(results) >= n_steps)
    # The two DIP-flip steps specifically must each show up in the
    # tracked results — this is exactly what the real-hardware bug
    # missed (an automated verify step whose failure never got recorded).
    descs = [r[1] for r in results]
    check("Armbian+lan: 'DIP flipped to eMMC' step recorded",
          any("DIP flipped to eMMC" in d for d in descs))
    check("Armbian+lan: final 'flip DIP to eMMC' step recorded",
          any("flip DIP to eMMC" in d for d in descs))


def test_armbian_usb():
    print("\n--- Armbian + usb ---")
    with patch("mono_imager.journeys.armbian_usb.find_image_on_usb",
               return_value=("/mnt/usb/armbian.img.xz", "img.xz")), \
         patch("mono_imager.recovery_orchestrator.run_firmware_update", return_value=True), \
         patch("mono_imager.device_net.RecoveryNetwork") as MockNet, \
         patch("builtins.input", return_value=""):
        MockNet.return_value.resolve.return_value = True
        ok, results, n_steps = _run_journey(
            "Armbian", "usb", usb_device="/dev/sda", usb_mount="/mnt/usb"
        )
    check("Armbian+usb: run() returns True", ok)
    check("Armbian+usb: all recorded results passed", bool(results) and all(r[2] for r in results))
    check(f"Armbian+usb: results cover all {n_steps} resolved steps", len(results) >= n_steps)


test_openwrt_lan()
test_openwrt_usb()
test_armbian_lan()
test_armbian_usb()

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
