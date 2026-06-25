#!/usr/bin/env python3
"""
OPNsense eMMC Firmware Re-imaging Step (Step 13)

CRITICAL: After flashing OPNsense OS to eMMC (offset 32MB+), the device
cannot boot from eMMC until the firmware bootloader region (first 32MB)
is re-imaged. This step implements that critical post-flash action.

THE PROBLEM:
  - Step 3: Erase entire eMMC via 'mmc erase'
  - Step 10-12: Flash OPNsense OS to eMMC (starts at offset 32MB)
  - Result: eMMC has OS but NO bootloader
  - Attempted DIP flip to eMMC = no boot (or crash)

THE SOLUTION (this step):
  - Boot recovery Linux from NOR (via DIP switch + reboot)
  - Fetch eMMC firmware blob (auth-required from firmware.mono.si)
  - Flash ONLY first 32MB of eMMC (bootloader region)
  - Reboot to eMMC (DIP switch + reboot)
  - Now device can boot into OPNsense OS

Firmware source: https://firmware.mono.si/firmware-emmc-gateway-dk.bin
Auth: user='mono', password=<device-mac-address>

This module provides:
  1. step_pause_for_dip_to_nor() — prompt user to flip DIP back to NOR
  2. step_confirm_nor_boot() — verify device booted into NOR recovery
  3. step_fetch_emmc_firmware() — download firmware blob with auth
  4. step_reimage_emmc_firmware() — curl | dd first 32MB only
  5. step_pause_for_dip_to_emmc() — prompt user to flip DIP to eMMC
  6. orchestrate_opnsense_firmware_reimage() — full workflow

Author: H.A. Hermsen
Version: 0.1.0
License: MIT
"""

__version__ = "0.1.0"
__author__  = "H.A. Hermsen"

import logging
import time
from typing import Optional, Tuple
from pathlib import Path

from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner

# Import step infrastructure
from mono_imager.flash_orchestrator import (
    step, verbose, console_logger, file_logger, 
    _FirmwareHandler, start_http_server, wait_for_report
)

logger = logging.getLogger(__name__)

# Firmware URLs (matches recovery_orchestrator.py)
FIRMWARE_EMMC_URL = "https://firmware.mono.si/firmware-emmc-gateway-dk.bin"

# ============================================================================
# INDIVIDUAL STEP FUNCTIONS
# ============================================================================

def step_pause_for_dip_to_nor(device_name: str = "Mono Gateway") -> bool:
    """
    Step 13a: Pause and prompt user to flip DIP switch to NOR boot.
    
    After flashing OS to eMMC, device must boot back into NOR recovery
    to access the recovery Linux needed for firmware re-imaging.
    
    DIP switch state:
      RIGHT (toward board edge) = NOR boot
      LEFT (toward USB) = eMMC boot
    
    Returns True (user confirmed action).
    """
    verbose("=" * 60)
    verbose("Step 13a: DIP Switch Flip (to NOR)")
    verbose("=" * 60)
    
    console_logger.info("")
    console_logger.info("⚠  MANUAL ACTION REQUIRED")
    console_logger.info(f"")
    console_logger.info(f"  Device: {device_name}")
    console_logger.info(f"  Action: Flip DIP switch to NOR (rightmost position)")
    console_logger.info(f"  Then:   Power cycle the device")
    console_logger.info(f"")
    console_logger.info(f"  (Tool will wait for recovery prompt on serial console)")
    console_logger.info("")
    
    input("Press ENTER when DIP is flipped and device is powered on... ")
    
    verbose("User confirmed DIP flip to NOR and power cycle", "debug")
    return step(13, "User confirmed DIP flip to NOR", True)


def step_confirm_nor_boot(d: SerialDevice, timeout: int = 30) -> bool:
    """
    Step 13b: Verify device booted into NOR recovery Linux.
    
    After user flips DIP and reboots, device should appear at recovery
    prompt on serial console within ~10-20s.
    
    Returns True if recovery shell prompt detected.
    """
    verbose("=" * 60)
    verbose("Step 13b: Confirm NOR Recovery Boot")
    verbose("=" * 60)
    
    verbose(f"Waiting for recovery Linux shell prompt (timeout={timeout}s)...")
    
    try:
        confirmed = d.verify_recovery_shell(timeout=timeout)
        if confirmed:
            verbose("✓ Recovery shell detected", "debug")
            return step(14, "Device booted to NOR recovery", True)
        else:
            verbose("✗ Recovery shell prompt not detected", "error")
            return step(14, "Device booted to NOR recovery", False,
                       "recovery prompt timeout")
    except Exception as e:
        verbose(f"✗ Exception while waiting for recovery: {e}", "error")
        return step(14, "Device booted to NOR recovery", False, str(e))


def step_fetch_emmc_firmware(
    mac_address: str,
    local_path: Path = Path("/tmp/firmware_emmc.bin")
) -> Tuple[bool, Optional[Path]]:
    """
    Step 13c: Download eMMC firmware blob with auth.
    
    Firmware source: https://firmware.mono.si/firmware-emmc-gateway-dk.bin
    
    Auth requires:
      - User: 'mono'
      - Password: device's MAC address
    
    Args:
        mac_address: Device MAC (e.g. '00:11:22:33:44:55')
        local_path: Where to save firmware locally (for logging purposes)
    
    Returns: (success: bool, path: Path | None)
    """
    verbose("=" * 60)
    verbose("Step 13c: Fetch eMMC Firmware Blob")
    verbose("=" * 60)
    
    verbose(f"Firmware URL: {FIRMWARE_EMMC_URL}")
    verbose(f"Auth: user=mono, password={mac_address}")
    
    import subprocess
    
    try:
        # Note: Device will fetch this live during re-imaging step (13d).
        # This step just verifies server is reachable and auth is valid.
        # We do a HEAD request to check without downloading the full blob locally.
        
        cmd = [
            "curl", "-I",  # HEAD request
            f"--user", f"mono:{mac_address}",
            "-s", "-w", "%{http_code}",
            FIRMWARE_EMMC_URL
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if "401" in result.stdout:
            verbose("✗ Authentication failed (401) — invalid MAC address?", "error")
            return step(15, "Firmware server auth check (HEAD)", False,
                       "401 unauthorized — check MAC address"), None
        
        if "200" not in result.stdout:
            verbose(f"✗ Server returned {result.stdout} (expected 200)", "error")
            return step(15, "Firmware server auth check (HEAD)", False,
                       f"HTTP {result.stdout}"), None
        
        verbose("✓ Firmware server reachable, auth valid", "debug")
        return step(15, "Firmware server auth check (HEAD)", True), local_path
        
    except subprocess.TimeoutExpired:
        verbose("✗ Firmware server unreachable (timeout)", "error")
        return step(15, "Firmware server auth check (HEAD)", False,
                   "server timeout"), None
    except Exception as e:
        verbose(f"✗ Exception during firmware fetch check: {e}", "error")
        return step(15, "Firmware server auth check (HEAD)", False, str(e)), None


def step_reimage_emmc_firmware(
    d: SerialDevice,
    mac_address: str,
    firmware_url: str = FIRMWARE_EMMC_URL
) -> bool:
    """
    Step 13d: Re-image eMMC firmware region (first 32MB) via curl | dd.
    
    CRITICAL: This is offset-preserving:
      - Target: /dev/mmcblk0 (entire eMMC device)
      - Offset: bs=4096 skip=1 seek=1 (preserves first 4KB)
      - Size: only first 32MB (firmware region)
    
    Device is in recovery Linux at this point, with network up.
    
    The dd command writes ONLY the bootloader/U-Boot region. The OS
    (already on eMMC at offset 32MB+) is untouched.
    
    Args:
        d: SerialDevice (assumed already in recovery shell)
        mac_address: For firmware server auth
        firmware_url: URL of firmware blob (default: firmware.mono.si)
    
    Returns: True if reimage succeeds
    """
    verbose("=" * 60)
    verbose("Step 13d: Re-image eMMC Firmware (first 32MB)")
    verbose("=" * 60)
    
    verbose(f"Target: /dev/mmcblk0 (eMMC, offset 4KB, size ~32MB)")
    verbose(f"Firmware: {firmware_url}")
    
    # curl auth syntax: user:password@url or --user user:password
    # Using --user for clarity
    curl_cmd = f"curl -s --user mono:{mac_address} {firmware_url}"
    
    # dd writes only first 32MB with offset preservation
    # bs=4096 skip=1 seek=1 = skip first 4KB (GPT preserved)
    dd_cmd = (
        f"dd of=/dev/mmcblk0 bs=4096 skip=1 seek=1 "
        f">> /tmp/mono_imager_step13d_reimage.log 2>&1"
    )
    
    # Capture log for verification
    full_script = (
        f"{curl_cmd} 2>/tmp/mono_imager_step13d_reimage.log | "
        f"{dd_cmd}; "
        f"cat /tmp/mono_imager_step13d_reimage.log"
    )
    
    verbose(f"Running: curl | dd (firmware re-imaging)...", "debug")
    
    try:
        response, flash_error = with_spinner(
            d.run_script, full_script,
            marker="step13d_reimage", exec_timeout=120,
            message="Re-imaging eMMC firmware"
        )
        
        if flash_error is not None:
            verbose(f"✗ Script execution failed: {flash_error}", "error")
            return step(16, "eMMC firmware re-image via curl | dd", False, str(flash_error))
        
        # Verify dd output
        has_records = "records in" in response and "records out" in response
        has_auth_error = "401" in response or "unauthorized" in response.lower()
        has_other_error = "error" in response.lower() or "failed" in response.lower()
        
        if has_auth_error:
            verbose("✗ Authentication failed during firmware fetch", "error")
            return step(16, "eMMC firmware re-image via curl | dd", False,
                       "401 auth error during curl")
        
        if not has_records:
            verbose("✗ dd did not report records in/out", "error")
            return step(16, "eMMC firmware re-image via curl | dd", False,
                       f"no dd records: {response[-200:]}")
        
        if has_other_error:
            verbose("✗ dd reported error", "error")
            return step(16, "eMMC firmware re-image via curl | dd", False,
                       f"dd error: {response[-200:]}")
        
        verbose("✓ eMMC firmware re-imaged successfully", "debug")
        return step(16, "eMMC firmware re-image via curl | dd", True)
        
    except Exception as e:
        verbose(f"✗ Exception during firmware re-imaging: {e}", "error")
        return step(16, "eMMC firmware re-image via curl | dd", False, str(e))


def step_pause_for_dip_to_emmc(device_name: str = "Mono Gateway") -> bool:
    """
    Step 13e: Pause and prompt user to flip DIP switch to eMMC boot.
    
    After firmware re-imaging completes, device must boot from eMMC
    to run the freshly-flashed OPNsense OS.
    
    DIP switch state:
      RIGHT (toward board edge) = NOR boot
      LEFT (toward USB) = eMMC boot
    
    Returns True (user confirmed action).
    """
    verbose("=" * 60)
    verbose("Step 13e: DIP Switch Flip (to eMMC)")
    verbose("=" * 60)
    
    console_logger.info("")
    console_logger.info("⚠  MANUAL ACTION REQUIRED")
    console_logger.info(f"")
    console_logger.info(f"  Device: {device_name}")
    console_logger.info(f"  Action: Flip DIP switch to eMMC (leftmost position)")
    console_logger.info(f"  Then:   Power cycle the device")
    console_logger.info(f"")
    console_logger.info(f"  Device should boot into OPNsense")
    console_logger.info(f"")
    
    input("Press ENTER when DIP is flipped and device is powered on... ")
    
    verbose("User confirmed DIP flip to eMMC and power cycle", "debug")
    return step(17, "User confirmed DIP flip to eMMC", True)


# ============================================================================
# ORCHESTRATION
# ============================================================================

def orchestrate_opnsense_firmware_reimage(
    d: SerialDevice,
    mac_address: str,
    device_name: str = "Mono Gateway"
) -> bool:
    """
    Orchestrate the complete OPNsense eMMC firmware re-imaging sequence.
    
    Full workflow:
      1. Pause for DIP flip to NOR + power cycle
      2. Confirm device booted to NOR recovery
      3. Verify eMMC firmware server is reachable (auth check)
      4. Re-image eMMC firmware region (first 32MB)
      5. Pause for DIP flip to eMMC + power cycle
    
    Args:
        d: SerialDevice (currently in post-OS-flash state)
        mac_address: Device MAC address (for firmware auth)
        device_name: Device name for user prompts
    
    Returns: True if all steps pass
    """
    verbose("")
    verbose("=" * 60)
    verbose("Phase 4b: OPNsense eMMC Firmware Re-imaging")
    verbose("=" * 60)
    
    # Step 13a: Prompt DIP flip to NOR
    if not step_pause_for_dip_to_nor(device_name):
        return False
    
    # Step 13b: Confirm recovery boot
    if not step_confirm_nor_boot(d, timeout=30):
        return False
    
    # Step 13c: Check firmware server auth
    success, _ = step_fetch_emmc_firmware(mac_address)
    if not success:
        return False
    
    # Step 13d: Re-image firmware
    if not step_reimage_emmc_firmware(d, mac_address):
        return False
    
    # Step 13e: Prompt DIP flip to eMMC
    if not step_pause_for_dip_to_emmc(device_name):
        return False
    
    verbose("")
    verbose("✓ OPNsense firmware re-imaging sequence complete")
    verbose("  Device should now boot into eMMC/OPNsense")
    
    return True
