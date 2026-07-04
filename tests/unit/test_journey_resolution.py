#!/usr/bin/env python3
"""
mono-imager: Unit tests for the step registry and journey resolution.

No hardware required. Tests the declarative @register_step system.

What this tests:
  - All 6 journeys resolve to the correct ordered step sequence
  - Dependency ordering: requires/produces constraints are respected
  - OS isolation: OPNsense-only steps don't appear in OpenWRT/Armbian
  - Transfer isolation: lan steps don't appear in USB journeys and vice versa
  - Flash targets correct per OS
  - get_journey() populates StepContext correctly
  - No circular dependencies in any journey

Run: python tests/unit/test_journey_resolution.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import mono_imager.journeys  # triggers auto-discovery of all journey modules
from mono_imager.step_registry import list_journey, FlowRunner, StepContext
from mono_imager.journeys import discovered_journeys, get_journey, _FLASH_TARGETS

SUPPORTED_OS       = ["Armbian", "OPNsense", "OpenWRT"]
SUPPORTED_TRANSFER = ["lan", "usb"]

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
# Expected sequences per journey
# ============================================================================

EXPECTED = {
    ("OPNsense", "lan"): [
        "Device network ready",
        "Confirm DIP switch is RIGHT (NOR)",
        "Start HTTP server",
        "Verify firmware reachable",
        "Flash OPNsense image (bzip2 | dd)",
        "Detect device MAC address",
        "Re-image eMMC firmware (firmware update)",
        "Reboot into OPNsense",
    ],
    ("OPNsense", "usb"): [
        "Device network ready",
        "Confirm DIP switch is RIGHT (NOR)",
        "Mount USB stick",
        "Detect firmware file on USB",
        "Flash OPNsense image (bzip2 | dd)",
        "Unmount USB stick",
        "Detect device MAC address",
        "Re-image eMMC firmware (firmware update)",
        "Reboot into OPNsense",
    ],
    ("OpenWRT", "lan"): [
        "Device network ready",
        "Start HTTP server",
        "Verify firmware reachable",
        "Partition eMMC (fdisk)",
        "Flash OpenWRT image (dd)",
        "Firmware update (eMMC bootloader)",
    ],
    ("OpenWRT", "usb"): [
        "Device network ready",
        "Partition eMMC (fdisk)",
        "Mount USB stick",
        "Detect firmware file on USB",
        "Flash OpenWRT image (dd)",
        "Firmware update (eMMC bootloader)",
        "Unmount USB stick",
    ],
    ("Armbian", "lan"): [
        "Device network ready",
        "Start HTTP server",
        "Verify firmware reachable",
        "Flash Armbian image (dd bs=1M)",
        "Flip DIP to eMMC and verify boot",
        "Refresh eMMC firmware (NOR round-trip)",
    ],
    ("Armbian", "usb"): [
        "Mount USB stick",
        "Detect firmware file on USB",
        "Flash Armbian image",
        "Unmount USB stick",
        "Flip DIP to eMMC and verify boot",
        "Refresh eMMC firmware (NOR round-trip)",
    ],
}


# ============================================================================
# All 6 journeys: step count and exact sequence
# ============================================================================

print("=" * 60)
print("Journey resolution — step sequences")
print("=" * 60)

for (os_name, transfer), expected_steps in EXPECTED.items():
    resolved = list_journey(os_name, transfer)
    label_prefix = f"{os_name} + {transfer}"

    check(
        f"{label_prefix}: resolves to {len(expected_steps)} steps",
        len(resolved) == len(expected_steps)
    )
    check(
        f"{label_prefix}: correct sequence",
        resolved == expected_steps
    )
    if resolved != expected_steps:
        for i, (got, exp) in enumerate(zip(resolved, expected_steps), 1):
            if got != exp:
                print(f"    Step {i}: got '{got}', expected '{exp}'")
        if len(resolved) != len(expected_steps):
            print(f"    Full resolved: {resolved}")


# ============================================================================
# discovered_journeys() returns all 6
# ============================================================================

print()
print("=" * 60)
print("discovered_journeys()")
print("=" * 60)

discovered = discovered_journeys()
check("discovered_journeys returns 6 entries", len(discovered) == 6)
for os_name in SUPPORTED_OS:
    for transfer in SUPPORTED_TRANSFER:
        check(
            f"({os_name}, {transfer}) in discovered_journeys",
            (os_name, transfer) in discovered
        )


# ============================================================================
# OS isolation: OPNsense-only steps absent from other journeys
# ============================================================================

print()
print("=" * 60)
print("OS isolation — OPNsense-only steps")
print("=" * 60)

OPNSENSE_ONLY = [
    "Confirm DIP switch is RIGHT (NOR)",
    "Flash OPNsense image (bzip2 | dd)",
    "Re-image eMMC firmware (firmware update)",
    "Reboot into OPNsense",
]

for os_name in ["OpenWRT", "Armbian"]:
    for transfer in SUPPORTED_TRANSFER:
        steps = list_journey(os_name, transfer)
        for s in OPNSENSE_ONLY:
            check(f"'{s}' absent from {os_name} + {transfer}", s not in steps)


# ============================================================================
# Transfer isolation
# ============================================================================

print()
print("=" * 60)
print("Transfer isolation — lan vs USB steps")
print("=" * 60)

LAN_ONLY = ["Start HTTP server", "Verify firmware reachable"]
USB_ONLY = ["Mount USB stick", "Detect firmware file on USB", "Unmount USB stick"]

for os_name in SUPPORTED_OS:
    usb_steps = list_journey(os_name, "usb")
    lan_steps = list_journey(os_name, "lan")
    for s in LAN_ONLY:
        check(f"'{s}' absent from {os_name} + usb", s not in usb_steps)
    for s in USB_ONLY:
        check(f"'{s}' absent from {os_name} + lan", s not in lan_steps)

# "Device network ready" is present in every LAN journey (needed for the
# HTTP flash transfer) and in the USB journeys whose post-flash step needs
# real internet access (OpenWRT/OPNsense's firmware update) — but NOT in
# Armbian-via-USB, which never touches the network at all.
for os_name in SUPPORTED_OS:
    check(f"'Device network ready' present in {os_name} + lan",
          "Device network ready" in list_journey(os_name, "lan"))

check("'Device network ready' present in OpenWRT + usb",
      "Device network ready" in list_journey("OpenWRT", "usb"))
check("'Device network ready' present in OPNsense + usb",
      "Device network ready" in list_journey("OPNsense", "usb"))
check("'Device network ready' absent from Armbian + usb",
      "Device network ready" not in list_journey("Armbian", "usb"))


# ============================================================================
# Flash targets correct per OS
# ============================================================================

print()
print("=" * 60)
print("Flash targets per OS")
print("=" * 60)

EXPECTED_TARGETS = {
    "OPNsense": "/dev/mmcblk0",
    "OpenWRT":  "/dev/mmcblk0p1",
    "Armbian":  "/dev/mmcblk0",
}

for os_name, expected_target in EXPECTED_TARGETS.items():
    check(
        f"{os_name} flash target is '{expected_target}'",
        _FLASH_TARGETS.get(os_name) == expected_target
    )


# ============================================================================
# StepContext populated correctly by get_journey()
# ============================================================================

print()
print("=" * 60)
print("StepContext populated correctly by get_journey()")
print("=" * 60)

mock_device = MagicMock()
firmware = Path("/tmp/firmware.img.bz2")

runner = get_journey(
    os_name       = "OPNsense",
    transfer      = "lan",
    device        = mock_device,
    host_ip       = "192.168.168.84",
    device_ip     = "192.168.168.222",
    firmware_path = firmware,
    http_port     = 8080,
    device_mac    = "e8:f6:d7:00:19:9c",
)

ctx = runner.ctx
check("ctx.os_name == 'OPNsense'",         ctx.os_name == "OPNsense")
check("ctx.transfer == 'lan'",             ctx.transfer == "lan")
check("ctx.host_ip correct",               ctx.host_ip == "192.168.168.84")
check("ctx.device_ip correct",             ctx.device_ip == "192.168.168.222")
check("ctx.http_port correct",             ctx.http_port == 8080)
check("ctx.device_mac correct",            ctx.device_mac == "e8:f6:d7:00:19:9c")
check("ctx.firmware_path correct",         ctx.firmware_path == firmware)
check("ctx.flash_target is /dev/mmcblk0", ctx.flash_target == "/dev/mmcblk0")
check("ctx.device is the mock",            ctx.device is mock_device)

runner_usb = get_journey(
    os_name    = "OpenWRT",
    transfer   = "usb",
    device     = mock_device,
    usb_device = "/dev/sdb",
    usb_mount  = "/mnt/stick",
)
ctx_usb = runner_usb.ctx
check("ctx.usb_device set correctly",        ctx_usb.usb_device == "/dev/sdb")
check("ctx.usb_mount set correctly",         ctx_usb.usb_mount == "/mnt/stick")
check("ctx.flash_target is /dev/mmcblk0p1", ctx_usb.flash_target == "/dev/mmcblk0p1")


# ============================================================================
# No circular dependencies
# ============================================================================

print()
print("=" * 60)
print("No circular dependencies")
print("=" * 60)

import logging, io

for os_name in SUPPORTED_OS:
    for transfer in SUPPORTED_TRANSFER:
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logging.getLogger("mono_imager.step_registry").addHandler(handler)

        ctx    = StepContext(os_name=os_name, transfer=transfer)
        runner = FlowRunner(os_name, transfer, ctx)
        runner.steps_for()

        logging.getLogger("mono_imager.step_registry").removeHandler(handler)

        check(
            f"{os_name} + {transfer}: no circular dependency",
            "circular" not in log_capture.getvalue()
        )


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
