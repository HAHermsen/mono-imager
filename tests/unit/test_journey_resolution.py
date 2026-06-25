#!/usr/bin/env python3
"""
mono-imager: Unit tests for the step registry and journey resolution.

No hardware required. Tests the declarative @register_step system
introduced in v0.9.1.

What this tests:
  - All 6 journeys resolve to the correct ordered step sequence
  - Dependency ordering: requires/produces constraints are respected
  - OS isolation: OPNsense-only steps don't appear in OpenWRT/Armbian
  - Transfer isolation: network steps don't appear in USB journeys and vice versa
  - Shared steps: ALL_OS / ALL_TRANSFER steps appear in every applicable journey
  - Context field coverage: StepContext carries the right fields for each journey
  - No circular dependencies in any journey
  - Adding a new OS to _FLASH_TARGETS makes it appear in SUPPORTED_OS

Run: python tests/unit/test_journey_resolution.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.step_registry import list_journey, FlowRunner, StepContext
from mono_imager.journey_steps import SUPPORTED_OS, SUPPORTED_TRANSFER, get_journey, _FLASH_TARGETS

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
    ("OPNsense", "network"): [
        "Network setup (eth0)",
        "Start HTTP server",
        "Verify firmware reachable",
        "Erase eMMC (OPNsense requirement)",
        "Flash OS image (dd)",
        "Detect device MAC address",
        "DIP flip to NOR + power cycle",
        "Confirm NOR recovery boot",
        "Re-image eMMC firmware (first 32MB)",
        "DIP flip to eMMC + power cycle",
    ],
    ("OPNsense", "usb"): [
        "Mount USB stick",
        "Verify firmware file on USB",
        "Erase eMMC (OPNsense requirement)",
        "Flash OS image (dd)",
        "Unmount USB stick",
        "Detect device MAC address",
        "DIP flip to NOR + power cycle",
        "Confirm NOR recovery boot",
        "Re-image eMMC firmware (first 32MB)",
        "DIP flip to eMMC + power cycle",
    ],
    ("OpenWRT", "network"): [
        "Network setup (eth0)",
        "Start HTTP server",
        "Verify firmware reachable",
        "Flash OS image (dd)",
        "Reboot device",
    ],
    ("OpenWRT", "usb"): [
        "Mount USB stick",
        "Verify firmware file on USB",
        "Flash OS image (dd)",
        "Unmount USB stick",
        "Reboot device",
    ],
    ("Armbian", "network"): [
        "Network setup (eth0)",
        "Start HTTP server",
        "Verify firmware reachable",
        "Flash OS image (dd)",
        "Reboot device",
    ],
    ("Armbian", "usb"): [
        "Mount USB stick",
        "Verify firmware file on USB",
        "Flash OS image (dd)",
        "Unmount USB stick",
        "Reboot device",
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
        # Print diff for debugging
        for i, (got, exp) in enumerate(zip(resolved, expected_steps), 1):
            if got != exp:
                print(f"    Step {i}: got '{got}', expected '{exp}'")
        if len(resolved) != len(expected_steps):
            print(f"    Extra or missing steps: {resolved}")


# ============================================================================
# OS isolation: OPNsense-only steps absent from other journeys
# ============================================================================

print()
print("=" * 60)
print("OS isolation — OPNsense-only steps")
print("=" * 60)

OPNSENSE_ONLY = [
    "Erase eMMC (OPNsense requirement)",
    "Detect device MAC address",
    "DIP flip to NOR + power cycle",
    "Confirm NOR recovery boot",
    "Re-image eMMC firmware (first 32MB)",
    "DIP flip to eMMC + power cycle",
]

for os_name in ["OpenWRT", "Armbian"]:
    for transfer in SUPPORTED_TRANSFER:
        steps = list_journey(os_name, transfer)
        for opnsense_step in OPNSENSE_ONLY:
            check(
                f"'{opnsense_step}' absent from {os_name} + {transfer}",
                opnsense_step not in steps
            )


# ============================================================================
# Transfer isolation: network steps absent from USB journeys and vice versa
# ============================================================================

print()
print("=" * 60)
print("Transfer isolation — network vs USB steps")
print("=" * 60)

NETWORK_ONLY = ["Network setup (eth0)", "Start HTTP server", "Verify firmware reachable"]
USB_ONLY     = ["Mount USB stick", "Verify firmware file on USB", "Unmount USB stick"]

for os_name in SUPPORTED_OS:
    usb_steps     = list_journey(os_name, "usb")
    network_steps = list_journey(os_name, "network")

    for s in NETWORK_ONLY:
        check(f"'{s}' absent from {os_name} + usb",     s not in usb_steps)
    for s in USB_ONLY:
        check(f"'{s}' absent from {os_name} + network", s not in network_steps)


# ============================================================================
# Shared steps: flash step appears in every journey
# ============================================================================

print()
print("=" * 60)
print("Shared steps — Flash OS image present in all journeys")
print("=" * 60)

for os_name in SUPPORTED_OS:
    for transfer in SUPPORTED_TRANSFER:
        steps = list_journey(os_name, transfer)
        check(
            f"'Flash OS image (dd)' present in {os_name} + {transfer}",
            "Flash OS image (dd)" in steps
        )


# ============================================================================
# Dependency ordering: flash always after firmware_ready
# ============================================================================

print()
print("=" * 60)
print("Dependency ordering — flash comes after firmware_ready")
print("=" * 60)

FIRMWARE_READY_PRODUCERS = {
    "network": "Verify firmware reachable",
    "usb":     "Verify firmware file on USB",
}

for os_name in SUPPORTED_OS:
    for transfer in SUPPORTED_TRANSFER:
        steps = list_journey(os_name, transfer)
        producer = FIRMWARE_READY_PRODUCERS[transfer]
        if producer in steps and "Flash OS image (dd)" in steps:
            producer_idx = steps.index(producer)
            flash_idx    = steps.index("Flash OS image (dd)")
            check(
                f"{os_name} + {transfer}: firmware_ready before flash",
                producer_idx < flash_idx
            )


# ============================================================================
# OPNsense dependency chain: correct ordering of post-flash steps
# ============================================================================

print()
print("=" * 60)
print("Dependency ordering — OPNsense post-flash chain")
print("=" * 60)

OPNSENSE_CHAIN = [
    "Flash OS image (dd)",
    "DIP flip to NOR + power cycle",
    "Confirm NOR recovery boot",
    "Re-image eMMC firmware (first 32MB)",
    "DIP flip to eMMC + power cycle",
]

for transfer in SUPPORTED_TRANSFER:
    steps = list_journey("OPNsense", transfer)
    indices = [steps.index(s) for s in OPNSENSE_CHAIN if s in steps]
    check(
        f"OPNsense + {transfer}: post-flash chain is strictly ordered",
        indices == sorted(indices) and len(indices) == len(OPNSENSE_CHAIN)
    )


# ============================================================================
# SUPPORTED_OS and SUPPORTED_TRANSFER coverage
# ============================================================================

print()
print("=" * 60)
print("SUPPORTED_OS / SUPPORTED_TRANSFER coverage")
print("=" * 60)

check("OPNsense in SUPPORTED_OS",  "OPNsense" in SUPPORTED_OS)
check("OpenWRT in SUPPORTED_OS",   "OpenWRT"  in SUPPORTED_OS)
check("Armbian in SUPPORTED_OS",   "Armbian"  in SUPPORTED_OS)
check("network in SUPPORTED_TRANSFER", "network" in SUPPORTED_TRANSFER)
check("usb in SUPPORTED_TRANSFER",     "usb"     in SUPPORTED_TRANSFER)

check(
    "SUPPORTED_OS matches _FLASH_TARGETS keys",
    set(SUPPORTED_OS) == set(_FLASH_TARGETS.keys())
)


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
# StepContext fields populated by get_journey()
# ============================================================================

print()
print("=" * 60)
print("StepContext populated correctly by get_journey()")
print("=" * 60)

from unittest.mock import MagicMock

mock_device = MagicMock()
firmware    = Path("/tmp/firmware.img")

runner = get_journey(
    os_name       = "OPNsense",
    transfer      = "network",
    device        = mock_device,
    host_ip       = "192.168.168.84",
    device_ip     = "192.168.168.222",
    firmware_path = firmware,
    http_port     = 8080,
    device_mac    = "e8:f6:d7:00:19:9c",
)

ctx = runner.ctx
check("ctx.os_name == 'OPNsense'",              ctx.os_name == "OPNsense")
check("ctx.transfer == 'network'",              ctx.transfer == "network")
check("ctx.host_ip correct",                    ctx.host_ip == "192.168.168.84")
check("ctx.device_ip correct",                  ctx.device_ip == "192.168.168.222")
check("ctx.http_port correct",                  ctx.http_port == 8080)
check("ctx.device_mac correct",                 ctx.device_mac == "e8:f6:d7:00:19:9c")
check("ctx.firmware_path correct",              ctx.firmware_path == firmware)
check("ctx.flash_target is /dev/mmcblk0",       ctx.flash_target == "/dev/mmcblk0")
check("ctx.device is the mock",                 ctx.device is mock_device)

runner_usb = get_journey(
    os_name  = "OpenWRT",
    transfer = "usb",
    device   = mock_device,
    usb_device = "/dev/sdb",
    usb_mount  = "/mnt/stick",
)
ctx_usb = runner_usb.ctx
check("ctx.usb_device set correctly",           ctx_usb.usb_device == "/dev/sdb")
check("ctx.usb_mount set correctly",            ctx_usb.usb_mount == "/mnt/stick")
check("ctx.flash_target is /dev/mmcblk0p1",    ctx_usb.flash_target == "/dev/mmcblk0p1")


# ============================================================================
# No circular dependencies: all 6 journeys resolve without fallback
# ============================================================================

print()
print("=" * 60)
print("No circular dependencies")
print("=" * 60)

import logging
import io

for os_name in SUPPORTED_OS:
    for transfer in SUPPORTED_TRANSFER:
        # Capture any error log output from FlowRunner._resolve()
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logging.getLogger("mono_imager.step_registry").addHandler(handler)

        ctx      = StepContext(os_name=os_name, transfer=transfer)
        runner   = FlowRunner(os_name, transfer, ctx)
        resolved = runner.steps_for()

        logging.getLogger("mono_imager.step_registry").removeHandler(handler)
        log_output = log_capture.getvalue()

        check(
            f"{os_name} + {transfer}: no circular dependency detected",
            "circular dependency" not in log_output
        )


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
