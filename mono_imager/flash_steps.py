#!/usr/bin/env python3
"""
mono-imager: Step-based flash architecture.

Each step is a reusable, OS-agnostic function. Orchestrators
(flash_opnsense_network, flash_openwrt_network, etc.) compose
steps into device-specific flows.

This separation ensures:
  - Steps are testable independently
  - No conditional branching in steps themselves
  - Adding new OS/path combos = new orchestrator, no step changes
  - Future USB path reuses same steps

Author:  H.A. Hermsen
Version: 0.7.0
License: MIT
"""

__version__ = "0.9.0"
__author__  = "H.A. Hermsen"

import sys
import logging
from pathlib import Path
from http.server import HTTPServer
from typing import Optional

from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner

# Import existing phase infrastructure for now (will be decomposed)
from mono_imager.flash_orchestrator import (
    reset_results, step, verbose, console_logger, file_logger,
    _FirmwareHandler, start_http_server, wait_for_report
)

logger = logging.getLogger(__name__)

# ============================================================================
# INDIVIDUAL STEP FUNCTIONS (OS-agnostic, reusable)
# ============================================================================

def step_uboot_interrupt(d: SerialDevice) -> bool:
    """
    Step 1: Connect to device, interrupt U-Boot autoboot, confirm prompt.
    Returns True if U-Boot prompt confirmed.
    """
    verbose("=" * 60)
    verbose("Step 1: Interrupt U-Boot autoboot")
    verbose("=" * 60)
    
    try:
        # Device should already be connected from bootstrap
        d.send_command("", wait_for_prompt=True, timeout=2)
        verbose("✓ U-Boot prompt confirmed", "debug")
    except Exception as e:
        verbose(f"✗ Failed to confirm U-Boot prompt: {e}", "error")
        return step(1, "U-Boot prompt confirmed", False, str(e))
    
    return step(1, "U-Boot prompt confirmed", True)


def step_erase_emmc(d: SerialDevice) -> bool:
    """
    Step 2: Erase eMMC via 'mmc erase 0 3b48000' (U-Boot command).
    Per Mono OPNsense docs: opnsense.mono.si/releases/26.1/
    Returns True if erase succeeds.
    """
    verbose("=" * 60)
    verbose("Step 2: Erase eMMC (OPNsense requirement)")
    verbose("=" * 60)
    
    erase_cmd = "mmc erase 0 3b48000"
    verbose(f"Running: {erase_cmd}")
    
    try:
        response = d.send_command(erase_cmd, timeout=60)
        # mmc erase returns to prompt on success; presence of "error" = failure
        success = "error" not in response.lower()
        
        if success:
            verbose("✓ eMMC erased successfully", "debug")
        else:
            verbose(f"✗ eMMC erase failed: {response}", "error")
        
        return step(2, "eMMC erase (mmc erase 0 3b48000)", success,
                   response[-100:] if not success else "")
    except Exception as e:
        verbose(f"✗ eMMC erase exception: {e}", "error")
        return step(2, "eMMC erase (mmc erase 0 3b48000)", False, str(e))


def step_boot_recovery(d: SerialDevice) -> bool:
    """
    Step 3: Boot into recovery Linux via 'run recovery' (U-Boot).
    Returns True if recovery shell prompt confirmed.
    """
    verbose("=" * 60)
    verbose("Step 3: Boot recovery Linux")
    verbose("=" * 60)
    
    try:
        verbose("Sending 'run recovery' command...")
        d.send_command("run recovery", wait_for_prompt=False, timeout=5)
        
        # Wait for recovery shell prompt
        import time
        time.sleep(2)
        
        if d.verify_recovery_shell(timeout=10):
            verbose("✓ Recovery shell confirmed", "debug")
            return step(3, "Recovery Linux booted", True)
        else:
            verbose("✗ Recovery shell prompt not found", "error")
            return step(3, "Recovery Linux booted", False,
                       "recovery shell prompt not confirmed")
    except Exception as e:
        verbose(f"✗ Boot recovery failed: {e}", "error")
        return step(3, "Recovery Linux booted", False, str(e))


def step_network_setup(d: SerialDevice, host_ip: str, device_ip: str) -> bool:
    """
    Step 4: Configure eth0 and assign device IP.
    Returns True if device IP is reachable from host.
    """
    verbose("=" * 60)
    verbose("Step 4: Network setup")
    verbose("=" * 60)
    
    try:
        verbose(f"Bringing up eth0...")
        d.send_command("ip link set eth0 up", timeout=10)
        
        verbose(f"Assigning device IP {device_ip}/24...")
        d.send_command(f"ip addr add {device_ip}/24 dev eth0", timeout=10)
        
        # Verify reachability using icmplib (cross-platform)
        try:
            from icmplib import ping
            result = ping(device_ip, count=1, timeout=5)
            reachable = result.is_alive
        except ImportError:
            verbose("⚠ icmplib not installed, skipping reachability check", "warning")
            reachable = True  # Assume reachable if icmplib unavailable
        except Exception as e:
            verbose(f"⚠ Reachability check failed: {e}", "warning")
            reachable = True  # Assume reachable on error
        
        if reachable:
            verbose(f"✓ Device {device_ip} reachable", "debug")
        else:
            verbose(f"✗ Device {device_ip} not reachable", "error")
        
        return step(4, f"Network up, device IP {device_ip} reachable", reachable)
    except Exception as e:
        verbose(f"✗ Network setup failed: {e}", "error")
        return step(4, "Network up", False, str(e))


def step_http_server_start(firmware_path: Path, host_ip: str, port: int) -> Optional[HTTPServer]:
    """
    Step 5: Start HTTP server on host to serve firmware.
    Returns HTTPServer object on success, None on failure.
    """
    verbose("=" * 60)
    verbose("Step 5: Start HTTP server")
    verbose("=" * 60)
    
    try:
        server = start_http_server(host_ip, port, firmware_path)
        if server:
            verbose(f"✓ HTTP server up on {host_ip}:{port}", "debug")
            return step(5, f"HTTP server up on {host_ip}:{port}", True) and server
        else:
            verbose(f"✗ Failed to start HTTP server", "error")
            step(5, f"HTTP server up on {host_ip}:{port}", False)
            return None
    except Exception as e:
        verbose(f"✗ HTTP server start failed: {e}", "error")
        step(5, f"HTTP server up on {host_ip}:{port}", False, str(e))
        return None


def step_firmware_reachable(d: SerialDevice, url: str, host_ip: str, port: int) -> bool:
    """
    Step 6: Verify device can reach firmware URL via HEAD request.
    Returns True if HTTP 200 received.
    """
    verbose("=" * 60)
    verbose("Step 6: Verify firmware source reachable")
    verbose("=" * 60)
    
    check_script = (
        f"curl -s -I -o /dev/null -w '%{{http_code}}' {url} "
        f"> /tmp/mono_imager_step06_code.txt; "
        f"curl -s -X POST --data-binary @/tmp/mono_imager_step06_code.txt "
        f"\"http://{host_ip}:{port}/report?step=06\" >/dev/null 2>&1"
    )
    
    try:
        remote_path = d.launch_script(check_script, marker="step06_reachable")
        verbose(f"✓ launch_script returned: {remote_path}", "debug")
    except Exception as e:
        verbose(f"✗ launch_script failed: {e}", "error")
        return step(6, f"Firmware source reachable ({url})", False, str(e))
    
    check = wait_for_report("06", timeout=20.0)
    reachable = check is not None and "200" in check
    
    debug_detail = f"HTTP status: {check}" if not reachable else ""
    return step(6, f"Firmware source reachable ({url})", reachable, debug_detail)


def step_flash_dd(d: SerialDevice, url: str, flash_target: str, firmware_size: int,
                   host_ip: str, port: int) -> bool:
    """
    Step 7: Flash via curl | dd or buffered download + dd.
    Auto-selects streaming (>3GB) or buffered (<3GB) based on size.
    Returns True if flash succeeds.
    """
    verbose("=" * 60)
    verbose("Step 7: Flash (curl | dd)")
    verbose("=" * 60)
    
    FLASH_SIZE_CAP = int(3.8 * 1024**3 * 0.8)  # ≈3.0GB, 80% of confirmed 3.8GB root cap
    use_streaming = firmware_size > 0 and firmware_size > FLASH_SIZE_CAP
    
    verbose(f"Flashing {flash_target} — this may take several minutes...")
    console_logger.info("Flashing firmware — this may take several minutes...")
    
    if use_streaming:
        verbose(
            f"Firmware size ({firmware_size / 1024**3:.2f} GB) exceeds the "
            f"{FLASH_SIZE_CAP / 1024**3:.2f} GB buffered-flash cap — using "
            "streaming mode (curl | dd direct to target).",
            "warning"
        )
        # For eMMC targets, skip first 4KB block (GPT partition table)
        skip_seek = ""
        if flash_target == "/dev/mmcblk0":
            skip_seek = "skip=1 seek=1 "
        
        flash_script = (
            f"curl -s {url} 2>/tmp/mono_imager_step07_flash.log | "
            f"dd {skip_seek}of={flash_target} bs=4096 "
            f">> /tmp/mono_imager_step07_flash.log 2>&1; "
            f"cat /tmp/mono_imager_step07_flash.log"
        )
    else:
        local_fw_path = "/tmp/mono_imager_firmware.img"
        # For eMMC targets, skip first 4KB block (GPT partition table) on both input and output
        # This preserves the eMMC partition table and OS partitions that start at 32MB+
        skip_seek = ""
        if flash_target == "/dev/mmcblk0":
            skip_seek = "skip=1 seek=1 "
        
        flash_script = (
            f"curl -s -o {local_fw_path} {url} "
            f"> /tmp/mono_imager_step07_flash.log 2>&1; "
            f"dd if={local_fw_path} {skip_seek}of={flash_target} bs=4096 "
            f">> /tmp/mono_imager_step07_flash.log 2>&1; "
            f"rm -f {local_fw_path}; "
            f"cat /tmp/mono_imager_step07_flash.log"
        )
    
    response, flash_error = with_spinner(
        d.run_script, flash_script,
        marker="step07_flash", exec_timeout=600,
        message="Flashing — this may take several minutes"
    )
    
    if flash_error is not None:
        verbose(f"✗ Flash script failed: {flash_error}", "error")
        return step(7, "curl | dd executed on device", False, str(flash_error))
    
    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower() or "failed" in response.lower()
    
    step(7, "curl | dd executed on device", True)
    step(8, "dd confirmed records in/out", has_records,
         f"output: {response[-200:]}" if not has_records else "")
    step(9, "No curl errors", not has_error,
         f"output: {response[-200:]}" if has_error else "")
    
    return has_records and not has_error


def step_pause_for_dip_flip(d: SerialDevice, os_name: str) -> bool:
    """
    Step 9b: Pause for DIP switch flip, then auto-reboot.
    Only shown for OPNsense (which requires eMMC boot).
    Returns True after reboot is sent.
    """
    if os_name != "OPNsense":
        return True  # Skip for other OSes
    
    verbose("=" * 60)
    verbose("Step 9b: DIP Switch Flip & Auto-Reboot")
    verbose("=" * 60)
    
    print()
    print("  ⚡ DIP SWITCH FLIP REQUIRED ⚡")
    print()
    print("  OPNsense requires the DIP switch set to LEFT (eMMC boot).")
    print("  The firmware has been written to eMMC, but the device is")
    print("  still running recovery from NOR flash.")
    print()
    print("  1. FLIP the DIP switch to LEFT (eMMC)")
    print("     (The switch is on the PCB, next to the Ethernet ports)")
    print()
    print("  2. Press Enter to confirm you've flipped it")
    print("     The device will automatically reboot.")
    print()
    
    try:
        input("  Press Enter to flip and reboot...")
        verbose("✓ User confirmed DIP switch flipped, sending reboot...", "debug")
        
        # Send reboot command
        try:
            d.send_command("reboot", wait_for_prompt=False, timeout=5)
            verbose("✓ Reboot command sent", "debug")
            console_logger.info("Device rebooting...")
        except Exception as e:
            verbose(f"⚠ Reboot command failed: {e}", "warning")
        
        return step(9, "DIP flip confirmed, reboot sent", True)
    except KeyboardInterrupt:
        verbose("⚠ User interrupted during DIP flip pause", "warning")
        return step(9, "DIP flip paused", False, "User interrupted")


def step_reboot(d: SerialDevice) -> bool:
    """
    Step 10: Reboot device and disconnect serial.
    Returns True (reboot is always sent, result depends on new firmware).
    """
    verbose("=" * 60)
    verbose("Step 10: Post-Flash (Reboot)")
    verbose("=" * 60)
    
    try:
        verbose("Sending reboot command...")
        d.send_command("reboot", wait_for_prompt=False, timeout=5)
        verbose("✓ Reboot sent", "debug")
        console_logger.info("Rebooting device...")
    except Exception as e:
        verbose(f"⚠ Reboot command failed: {e}", "warning")
    
    return step(10, "Reboot sent", True)


# ============================================================================
# OS-SPECIFIC ORCHESTRATORS (compose steps into flows)
# ============================================================================

def flash_opnsense_network(d: SerialDevice, host_ip: str, device_ip: str,
                            firmware_path: Path, port: int = 8080) -> bool:
    """
    OPNsense + Network flash flow.
    
    Steps (erase already done in phase1_bootstrap):
      3. Boot recovery (already done in phase1)
      4. Network setup
      5. HTTP server
      6. Firmware reachable
      7. Flash dd (OS to eMMC offset 32MB+)
      8-9. Phase 4b: eMMC firmware re-imaging (NEW)
           - Pause for DIP flip to NOR
           - Confirm NOR recovery boot
           - Verify firmware server auth
           - Flash eMMC firmware (first 32MB)
           - Pause for DIP flip to eMMC
    """
    verbose("=" * 80)
    verbose("ORCHESTRATOR: OPNsense + Network")
    verbose("=" * 80)
    reset_results()
    
    firmware_size = firmware_path.stat().st_size
    url = f"http://{host_ip}:{port}/firmware.img"
    flash_target = "/dev/mmcblk0"  # OPNsense: whole disk (offset 32MB+)
    
    # Erase already done in phase1_bootstrap, skip step_uboot_interrupt and step_erase_emmc
    # Device is already in recovery shell
    
    if not step_network_setup(d, host_ip, device_ip):
        return False
    
    if not step_http_server_start(firmware_path, host_ip, port):
        return False
    
    if not step_firmware_reachable(d, url, host_ip, port):
        return False
    
    if not step_flash_dd(d, url, flash_target, firmware_size, host_ip, port):
        return False
    
    # Phase 4b: OPNsense eMMC firmware re-imaging (NEW)
    # This handles the critical missing step: re-image first 32MB of eMMC
    # so device can boot from eMMC after OS flash.
    # Requires user to physically flip DIP switch twice (NOR -> eMMC).
    verbose("")
    verbose("=" * 80)
    verbose("PHASE 4b: OPNsense eMMC Firmware Re-imaging")
    verbose("=" * 80)
    
    # Import here to avoid circular dependency and keep module isolated
    from mono_imager.opnsense_firmware_reimage import (
        orchestrate_opnsense_firmware_reimage
    )
    
    # Get device MAC address - try to extract from device or use default
    # In a real scenario, this would come from device detection
    mac_address = "00:11:22:33:44:55"  # Placeholder; in practice, detect from device
    
    try:
        # Try to get MAC from device via ifconfig or similar
        mac_result = d.run_script("cat /sys/class/net/eth0/address 2>/dev/null || echo 'unknown'",
                                 marker="get_mac", exec_timeout=5)
        if mac_result and mac_result[0] != "unknown":
            mac_address = mac_result[0].strip()
            verbose(f"Detected device MAC: {mac_address}", "debug")
    except Exception as e:
        verbose(f"⚠ Could not detect MAC address: {e}", "warning")
        verbose(f"  Using placeholder: {mac_address}", "warning")
    
    if not orchestrate_opnsense_firmware_reimage(d, mac_address):
        return False
    
    return True


def flash_openwrt_network(d: SerialDevice, host_ip: str, device_ip: str,
                           firmware_path: Path, port: int = 8080) -> bool:
    """
    OpenWRT + Network flash flow.
    
    Steps (device already in recovery shell from phase1):
      4. Network setup
      5. HTTP server
      6. Firmware reachable
      7. Flash dd (to partition 1)
      10. Reboot
    """
    verbose("=" * 80)
    verbose("ORCHESTRATOR: OpenWRT + Network")
    verbose("=" * 80)
    reset_results()
    
    firmware_size = firmware_path.stat().st_size
    url = f"http://{host_ip}:{port}/firmware.img"
    flash_target = "/dev/mmcblk0p1"  # OpenWRT: partition 1
    
    if not step_network_setup(d, host_ip, device_ip):
        return False
    
    if not step_http_server_start(firmware_path, host_ip, port):
        return False
    
    if not step_firmware_reachable(d, url, host_ip, port):
        return False
    
    if not step_flash_dd(d, url, flash_target, firmware_size, host_ip, port):
        return False
    
    if not step_reboot(d):
        return False
    
    return True


def flash_armbian_network(d: SerialDevice, host_ip: str, device_ip: str,
                           firmware_path: Path, port: int = 8080) -> bool:
    """
    Armbian + Network flash flow.
    
    Steps (device already in recovery shell from phase1):
      4. Network setup
      5. HTTP server
      6. Firmware reachable
      7. Flash dd (to whole disk)
      10. Reboot
    """
    verbose("=" * 80)
    verbose("ORCHESTRATOR: Armbian + Network")
    verbose("=" * 80)
    reset_results()
    
    firmware_size = firmware_path.stat().st_size
    url = f"http://{host_ip}:{port}/firmware.img"
    flash_target = "/dev/mmcblk0"  # Armbian: whole disk
    
    if not step_network_setup(d, host_ip, device_ip):
        return False
    
    if not step_http_server_start(firmware_path, host_ip, port):
        return False
    
    if not step_firmware_reachable(d, url, host_ip, port):
        return False
    
    if not step_flash_dd(d, url, flash_target, firmware_size, host_ip, port):
        return False
    
    if not step_reboot(d):
        return False
    
    return True


def flash_vyos_network(d: SerialDevice, host_ip: str, device_ip: str,
                        firmware_path: Path, port: int = 8080) -> bool:
    """
    VyOS + Network flash flow (same as Armbian for now).
    """
    return flash_armbian_network(d, host_ip, device_ip, firmware_path, port)


def flash_other_network(d: SerialDevice, host_ip: str, device_ip: str,
                         firmware_path: Path, flash_target: str,
                         port: int = 8080) -> bool:
    """
    Generic + Network flash flow (user specifies flash target).
    
    Steps (device already in recovery shell from phase1):
      4. Network setup
      5. HTTP server
      6. Firmware reachable
      7. Flash dd
      10. Reboot
    """
    verbose("=" * 80)
    verbose("ORCHESTRATOR: Other/Generic + Network")
    verbose("=" * 80)
    reset_results()
    
    firmware_size = firmware_path.stat().st_size
    url = f"http://{host_ip}:{port}/firmware.img"
    
    if not step_network_setup(d, host_ip, device_ip):
        return False
    
    if not step_http_server_start(firmware_path, host_ip, port):
        return False
    
    if not step_firmware_reachable(d, url, host_ip, port):
        return False
    
    if not step_flash_dd(d, url, flash_target, firmware_size, host_ip, port):
        return False
    
    if not step_reboot(d):
        return False
    
    return True


# ============================================================================
# ORCHESTRATOR DISPATCHER
# ============================================================================

ORCHESTRATORS = {
    ("OPNsense", "network"): flash_opnsense_network,
    ("OpenWRT", "network"):  flash_openwrt_network,
    ("Armbian", "network"):  flash_armbian_network,
    ("VyOS", "network"):     flash_vyos_network,
    ("Other", "network"):    flash_other_network,
}


def get_orchestrator(os_name: str, transfer_method: str):
    """
    Get the appropriate orchestrator function for the given OS and transfer method.
    Returns the function or None if not found.
    """
    key = (os_name, transfer_method)
    if key not in ORCHESTRATORS:
        verbose(f"⚠ No orchestrator for {os_name} + {transfer_method}", "warning")
        return None
    return ORCHESTRATORS[key]
