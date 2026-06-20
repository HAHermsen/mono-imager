#!/usr/bin/env python3
"""
mono-imager: Recovery orchestration logic.

Implements the documented Mono Gateway recovery/firmware-update
procedure (https://docs.mono.si/gateway-development-kit/flashing-firmware):

  Modern path (firmware has the `firmware` command):
    1. Boot recovery from NOR, run `firmware update` (flashes eMMC)
    2. User flips DIP to eMMC, reboots, tool verifies eMMC boot
    3. Boot recovery from eMMC, run `firmware update` (flashes NOR)
    4. User flips DIP back to NOR, reboots

  Legacy path (no `firmware` command — older devices in the wild):
    curl + dd (eMMC, with the documented skip=1 seek=1 4KB offset)
    curl + flashcp (NOR)
    This tool's policy: legacy devices are always brought up to the
    CURRENT firmware via the legacy download, never re-flashed with
    old firmware.

  Which path applies is DETECTED LIVE per device (`which firmware`)
  — there is no published version cutoff to gate on; devices in the
  wild may have either.

DIP-switch flips and the reboots that follow them are physical user
actions this tool cannot perform — those steps explicitly pause and
prompt, matching the "POWER CYCLE NOW" pattern used elsewhere.

This is a SEPARATE module from flash_orchestrator.py (and gets its
own isolated `results` list) rather than reusing its reporting state,
since mixing two different orchestrators' results in one shared list
is exactly the stale-state bug class fixed earlier this session.

Author:  H.A. Hermsen
Version: 0.5.0
License: MIT
"""

__version__ = "0.5.0"
__author__  = "H.A. Hermsen"

import re
import logging
from typing import Optional

from mono_imager.serial_device import SerialDevice

logger = logging.getLogger(__name__)
console_logger = logging.getLogger(__name__ + ".console")

# --- Result tracker (ISOLATED from flash_orchestrator.results — see
#     module docstring for why) -----------------------------------------

results: list[tuple[int, str, bool, str]] = []

def reset_results():
    """Clear accumulated step results before a new recovery attempt."""
    results.clear()

def step(num: int, description: str, passed: bool, reason: str = ""):
    mark = "✓" if passed else "✗"
    file_msg = f"Step {num:02d}: {'✓ PASS' if passed else '✗ FAIL'} — {description}"
    if reason:
        file_msg += f" ({reason})"
    logger.info(file_msg) if passed else logger.error(file_msg)
    console_logger.info(f"  {mark} {description}")
    results.append((num, description, passed, reason))
    return passed


# --- Firmware URLs (per documented "Manual flashing (legacy)" section) ------

LEGACY_EMMC_URL = "https://firmware.mono.si/firmware-emmc-gateway-dk.bin"
LEGACY_NOR_URL  = "https://firmware.mono.si/firmware-qspi-gateway-dk.bin"


# --- Detection ---------------------------------------------------------

def detect_modern_firmware_tool(d: SerialDevice) -> Optional[bool]:
    """
    Live-detect whether the device's CURRENT recovery Linux has the
    modern `firmware` command, via `which firmware` — there is no
    published firmware-version cutoff to gate on (checked the docs;
    none exists), so this must be checked live, per device, every
    time.

    Returns True if found, False if confirmed absent, None if the
    check itself failed (e.g. command didn't return cleanly) — None
    is NOT the same as False, and callers should treat it as
    "couldn't determine" rather than assuming legacy.
    """
    try:
        output = d.run_script("which firmware; echo RC=$?", marker="detect_fw_tool")
    except RuntimeError as e:
        logger.warning(f"detect_modern_firmware_tool: run_script failed: {e}")
        return None

    if "RC=0" in output and "firmware" in output:
        return True
    if "RC=" in output:
        return False
    return None


def get_device_mac(d: SerialDevice, interface: str = "eth0") -> Optional[str]:
    """
    Get the device's real MAC address from `ip a`, parsed — never
    assumed or asked of the user (avoids transcription errors). Tries
    the given interface first, falls back to the first link/ether seen
    if that specific interface isn't found.
    """
    try:
        output = d.run_script(f"ip addr show {interface} 2>/dev/null || ip addr", marker="get_mac")
    except RuntimeError as e:
        logger.warning(f"get_device_mac: run_script failed: {e}")
        return None

    match = re.search(r'link/ether\s+([0-9a-fA-F:]{17})', output)
    if match:
        return match.group(1).lower()
    return None


# --- Modern path: `firmware update` -------------------------------------

def run_firmware_update(d: SerialDevice) -> bool:
    """
    Run the modern `firmware update` command and confirm it reported
    success. This command downloads, verifies, and flashes the OTHER
    medium than the one currently booted (per docs: auto-detects boot
    source, never overwrites what you're currently running from).

    Requires real internet access on the device's network — this is
    a hard, documented prerequisite for both paths, not something
    this tool can route around.
    """
    try:
        output = d.run_script(
            "firmware update; echo RC=$?",
            marker="firmware_update",
            exec_timeout=300,  # real download + flash, give it real time
        )
    except RuntimeError as e:
        logger.error(f"run_firmware_update: run_script failed: {e}")
        return False

    success = "RC=0" in output
    if not success:
        logger.error(f"firmware update did not report success — output:\n{output}")
    return success


def verify_boot_source(d: SerialDevice, expected: str, timeout: float = 60) -> bool:
    """
    After a reboot, confirm the device actually booted from the
    expected medium by watching for U-Boot's own confirmation line,
    exactly as the docs say to check manually (Step 5) and as
    confirmed in a real boot capture earlier this session:

        "RCW BOOT SRC is SD/EMMC"   (eMMC boot)
        "RCW BOOT SRC is QSPI"      (NOR boot — QSPI is the real
                                      flash interface name U-Boot
                                      uses, not "NOR")

    Reuses the same read-until-marker approach as
    capture_boot_diagnostics() / wait_for_autoboot(), rather than
    trusting the DIP switch position blindly (a user could flip it
    without rebooting, or flip the wrong way).

    Args:
        expected: "EMMC" or "NOR" (caller-facing naming) — mapped
            internally to the real U-Boot marker text above.
    """
    marker_text = {
        "EMMC": "RCW BOOT SRC is SD/EMMC",
        "NOR":  "RCW BOOT SRC is QSPI",
    }.get(expected.upper())

    if marker_text is None:
        raise ValueError(f"verify_boot_source: expected must be 'EMMC' or 'NOR', got {expected!r}")

    logger.info(f"Waiting for boot source confirmation ({marker_text!r})...")

    import time
    start = time.time()
    buffer = b""
    while time.time() - start < timeout:
        try:
            byte = d.ser.read(1)
            if byte:
                buffer += byte
                if marker_text.encode() in buffer:
                    return True
        except Exception:
            break
    logger.warning(f"Did not see {marker_text!r} within {timeout}s")
    return False


# --- Legacy path: curl + dd / flashcp -----------------------------------

def legacy_flash_emmc(d: SerialDevice, mac: str) -> bool:
    """
    Legacy eMMC flash exactly per the documented "Manual flashing
    (legacy)" procedure: curl with mono:{MAC} basic auth, then dd
    with the documented skip=1 seek=1 (skips the first 4KB / GPT
    region on both input and output, per the docs' own explanation).
    """
    cmd = (
        f"curl -u mono:{mac} -O {LEGACY_EMMC_URL} && "
        f"dd if=firmware-emmc-gateway-dk.bin of=/dev/mmcblk0 bs=4096 skip=1 seek=1; "
        f"echo RC=$?"
    )
    try:
        output = d.run_script(cmd, marker="legacy_emmc", exec_timeout=300)
    except RuntimeError as e:
        logger.error(f"legacy_flash_emmc: run_script failed: {e}")
        return False

    success = "RC=0" in output and ("records out" in output or "records in" in output)
    if not success:
        logger.error(f"legacy eMMC flash did not confirm success — output:\n{output}")
    return success


def legacy_flash_nor(d: SerialDevice, mac: str) -> bool:
    """
    Legacy NOR flash exactly per the documented procedure: curl with
    mono:{MAC} basic auth, then flashcp to /dev/mtd0.
    """
    cmd = (
        f"curl -u mono:{mac} -O {LEGACY_NOR_URL} && "
        f"flashcp -v firmware-qspi-gateway-dk.bin /dev/mtd0; "
        f"echo RC=$?"
    )
    try:
        output = d.run_script(cmd, marker="legacy_nor", exec_timeout=300)
    except RuntimeError as e:
        logger.error(f"legacy_flash_nor: run_script failed: {e}")
        return False

    success = "RC=0" in output
    if not success:
        logger.error(f"legacy NOR flash did not confirm success — output:\n{output}")
    return success


# --- Top-level recovery phases -------------------------------------------
#
# These functions are UI-AGNOSTIC, same separation of concerns as
# flash_orchestrator.py's phaseN_* functions: they do not call input()
# or block waiting for a keypress. Where a PHYSICAL user action is
# required (flipping the DIP switch), the function prints the
# instruction and then actively polls the device for the RESULT of
# that action (boot source confirmation) — same pattern as
# phase1_bootstrap's "POWER CYCLE NOW" + wait_for_autoboot(). The
# caller (tui.py) is responsible for any additional pacing/messaging
# around these calls, not for driving the wait itself.

def phase_modern_flash_emmc(d: SerialDevice) -> bool:
    """
    Modern path, step 1: from NOR-booted recovery, run `firmware
    update` to flash eMMC. Returns True on confirmed success.
    """
    console_logger.info("Running 'firmware update' to flash eMMC...")
    ok = step(1, "Flash eMMC via 'firmware update'", run_firmware_update(d))
    return ok


def phase_modern_verify_emmc_boot(d: SerialDevice, timeout: float = 90) -> bool:
    """
    Modern path, step 2: after the user flips the DIP switch to eMMC
    and reboots, confirm the device actually booted from eMMC by
    watching U-Boot's own confirmation line. Does NOT send the reboot
    itself or block on input — caller handles prompting the user to
    flip the switch and reboot; this just waits for and verifies the
    result once that happens.
    """
    ok = step(2, "Verify eMMC boot", verify_boot_source(d, "EMMC", timeout=timeout))
    return ok


def phase_modern_flash_nor(d: SerialDevice) -> bool:
    """
    Modern path, step 3: from eMMC-booted recovery, run `firmware
    update` again — it auto-targets NOR this time since eMMC is now
    the active boot source. Returns True on confirmed success.
    """
    console_logger.info("Running 'firmware update' to flash NOR...")
    ok = step(3, "Flash NOR via 'firmware update'", run_firmware_update(d))
    return ok


def phase_modern_verify_nor_boot(d: SerialDevice, timeout: float = 90) -> bool:
    """
    Modern path, step 4: after the user flips the DIP switch back to
    NOR and reboots, confirm the device actually booted from NOR.
    """
    ok = step(4, "Verify NOR boot (back to factory default)", verify_boot_source(d, "NOR", timeout=timeout))
    return ok


def phase_legacy_flash_emmc(d: SerialDevice) -> bool:
    """
    Legacy path, step 1: get the device's real MAC, then flash eMMC
    via curl+dd per the documented legacy procedure.
    """
    mac = get_device_mac(d)
    if mac is None:
        return step(1, "Flash eMMC (legacy curl+dd)", False, "could not determine device MAC address")
    console_logger.info(f"Device MAC: {mac}")
    console_logger.info("Downloading and flashing eMMC (legacy path)...")
    ok = step(1, "Flash eMMC (legacy curl+dd)", legacy_flash_emmc(d, mac))
    return ok


def phase_legacy_flash_nor(d: SerialDevice) -> bool:
    """
    Legacy path, step 2: same MAC, flash NOR via curl+flashcp.
    """
    mac = get_device_mac(d)
    if mac is None:
        return step(2, "Flash NOR (legacy curl+flashcp)", False, "could not determine device MAC address")
    console_logger.info(f"Device MAC: {mac}")
    console_logger.info("Downloading and flashing NOR (legacy path)...")
    ok = step(2, "Flash NOR (legacy curl+flashcp)", legacy_flash_nor(d, mac))
    return ok


def print_report() -> bool:
    """
    Summarize the recovery attempt's results — same OK/NOK verdict
    pattern as flash_orchestrator.py's print_report(), but recovery
    doesn't have its own dedicated log file, so this only logs via
    the standard logger/console_logger rather than referencing a
    log_file path.
    """
    logger.info("=" * 60)
    logger.info("Recovery Report")
    logger.info("=" * 60)
    passed = sum(1 for _, _, p, _ in results if p)
    total = len(results)
    for num, desc, p, reason in results:
        mark = "✓ PASS" if p else "✗ FAIL"
        line = f"  Step {num:02d}: {mark} — {desc}"
        if reason:
            line += f"\n           {reason}"
        logger.info(line)
    logger.info("-" * 60)
    verdict = "OK" if total > 0 and passed == total else "NOK"
    logger.info(f"Result: {verdict} ({passed}/{total} steps passed)")

    console_logger.info("")
    if verdict == "OK":
        console_logger.info("✓ Recovery completed successfully.")
    else:
        console_logger.info("✗ Recovery did not complete successfully.")
        failed = [desc for _, desc, p, _ in results if not p]
        for desc in failed:
            console_logger.info(f"  - {desc}")

    return verdict == "OK"

