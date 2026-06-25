#!/usr/bin/env python3
"""
mono-imager: Journey Steps (v0.9.1)

All flash journey steps declared via @register_step.
Supports 6 journeys across 3 OS × 2 transfer methods:

  OPNsense  + network
  OPNsense  + usb
  OpenWRT   + network
  OpenWRT   + usb
  Armbian   + network
  Armbian   + usb

Steps shared across journeys are tagged with multiple os= / transfer= values.
Steps unique to one journey carry only that OS/transfer tag.
FlowRunner resolves the correct ordered sequence automatically.

Author:  H.A. Hermsen
Version: 0.9.1
License: MIT
"""

__version__ = "0.9.1"
__author__  = "H.A. Hermsen"

import subprocess
import logging
from pathlib import Path

from mono_imager.step_registry import register_step, StepContext, ALL_OS, ALL_TRANSFER
from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import (
    step, verbose, console_logger,
    start_http_server, wait_for_report
)

logger = logging.getLogger(__name__)

FIRMWARE_EMMC_URL = "https://firmware.mono.si/firmware-emmc-gateway-dk.bin"

# Flash targets per OS
_FLASH_TARGETS = {
    "OPNsense": "/dev/mmcblk0",
    "OpenWRT":  "/dev/mmcblk0p1",
    "Armbian":  "/dev/mmcblk0",
}

SUPPORTED_OS       = list(_FLASH_TARGETS.keys())
SUPPORTED_TRANSFER = ["network", "usb"]


# ============================================================================
# NETWORK STEPS
# ============================================================================

@register_step(
    os=[ALL_OS],
    transfer=["network"],
    requires=[],
    produces=["network_up"],
    label="Network setup (eth0)"
)
def step_network_setup(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Network setup")
    verbose("=" * 60)

    d = ctx.device
    try:
        d.send_command("ip link set eth0 up", timeout=10)
        d.send_command(f"ip addr add {ctx.device_ip}/24 dev eth0", timeout=10)

        try:
            from icmplib import ping
            reachable = ping(ctx.device_ip, count=1, timeout=5).is_alive
        except Exception:
            reachable = True  # assume reachable if icmplib unavailable

        return step(4, f"Network up ({ctx.device_ip})", reachable)
    except Exception as e:
        return step(4, "Network up", False, str(e))


@register_step(
    os=[ALL_OS],
    transfer=["network"],
    requires=["network_up"],
    produces=["http_server_up"],
    label="Start HTTP server"
)
def step_http_server_start(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Start HTTP server")
    verbose("=" * 60)

    try:
        server = start_http_server(ctx.host_ip, ctx.http_port, ctx.firmware_path)
        if server:
            ctx.set("http_server", server)
            return step(5, f"HTTP server up ({ctx.host_ip}:{ctx.http_port})", True)
        return step(5, "HTTP server start", False)
    except Exception as e:
        return step(5, "HTTP server start", False, str(e))


@register_step(
    os=[ALL_OS],
    transfer=["network"],
    requires=["network_up", "http_server_up"],
    produces=["firmware_ready"],
    label="Verify firmware reachable"
)
def step_firmware_reachable(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Verify firmware reachable")
    verbose("=" * 60)

    url = f"http://{ctx.host_ip}:{ctx.http_port}/firmware.img"
    ctx.set("firmware_source", url)

    check_script = (
        f"curl -s -I -o /dev/null -w '%{{http_code}}' {url} "
        f"> /tmp/mono_imager_step06_code.txt; "
        f"curl -s -X POST --data-binary @/tmp/mono_imager_step06_code.txt "
        f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=06\" >/dev/null 2>&1"
    )
    try:
        ctx.device.launch_script(check_script, marker="step06_reachable")
    except Exception as e:
        return step(6, f"Firmware reachable ({url})", False, str(e))

    check = wait_for_report("06", timeout=20.0)
    ok = check is not None and "200" in check
    return step(6, f"Firmware reachable ({url})", ok,
                f"HTTP {check}" if not ok else "")


# ============================================================================
# USB STEPS
# ============================================================================

@register_step(
    os=[ALL_OS],
    transfer=["usb"],
    requires=[],
    produces=["usb_mounted"],
    label="Mount USB stick"
)
def step_mount_usb(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Mount USB stick")
    verbose("=" * 60)

    d = ctx.device
    try:
        d.send_command(f"mkdir -p {ctx.usb_mount}", timeout=5)
        response = d.send_command(
            f"mount {ctx.usb_device}1 {ctx.usb_mount} 2>&1; echo RC=$?",
            timeout=15
        )
        ok = "RC=0" in response
        if not ok:
            # Try without partition suffix (e.g. some USB sticks have no partition table)
            response2 = d.send_command(
                f"mount {ctx.usb_device} {ctx.usb_mount} 2>&1; echo RC=$?",
                timeout=15
            )
            ok = "RC=0" in response2
        return step(4, f"USB mounted ({ctx.usb_device} → {ctx.usb_mount})", ok,
                   response[-100:] if not ok else "")
    except Exception as e:
        return step(4, "USB mount", False, str(e))


@register_step(
    os=[ALL_OS],
    transfer=["usb"],
    requires=["usb_mounted"],
    produces=["firmware_ready"],
    label="Verify firmware file on USB"
)
def step_firmware_on_usb(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Verify firmware file on USB")
    verbose("=" * 60)

    d = ctx.device
    # Firmware file must be named firmware.img on the USB stick root
    fw_path = f"{ctx.usb_mount}/firmware.img"
    try:
        response = d.send_command(
            f"test -f {fw_path} && echo FOUND || echo MISSING",
            timeout=5
        )
        ok = "FOUND" in response
        if ok:
            ctx.set("firmware_source", fw_path)
        return step(5, f"Firmware found on USB ({fw_path})", ok,
                   "file not found on USB stick" if not ok else "")
    except Exception as e:
        return step(5, "Firmware on USB", False, str(e))


# ============================================================================
# EMMC ERASE — OPNsense only, both transfer methods
# ============================================================================

@register_step(
    os=["OPNsense"],
    transfer=[ALL_TRANSFER],
    requires=[],
    produces=["emmc_erased"],
    label="Erase eMMC (OPNsense requirement)"
)
def step_erase_emmc(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Erase eMMC")
    verbose("=" * 60)

    d = ctx.device
    try:
        response = d.send_command("mmc erase 0 3b48000", timeout=60)
        ok = "error" not in response.lower()
        return step(2, "eMMC erase (mmc erase 0 3b48000)", ok,
                   response[-100:] if not ok else "")
    except Exception as e:
        return step(2, "eMMC erase", False, str(e))


# ============================================================================
# FLASH — all OS, both transfer methods
# Source comes from ctx.state["firmware_source"] (URL or local path)
# ============================================================================

@register_step(
    os=[ALL_OS],
    transfer=[ALL_TRANSFER],
    requires=["firmware_ready"],
    produces=["os_flashed"],
    label="Flash OS image (dd)"
)
def step_flash_dd(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Flash OS image")
    verbose("=" * 60)

    d            = ctx.device
    source       = ctx.get("firmware_source")   # URL (network) or path (usb)
    flash_target = ctx.flash_target
    is_network   = source and source.startswith("http")

    firmware_size = ctx.firmware_path.stat().st_size if ctx.firmware_path else 0
    FLASH_SIZE_CAP = int(3.8 * 1024**3 * 0.8)
    use_streaming  = is_network and firmware_size > 0 and firmware_size > FLASH_SIZE_CAP

    skip_seek = "skip=1 seek=1 " if flash_target == "/dev/mmcblk0" else ""

    console_logger.info("Flashing firmware — this may take several minutes...")

    if is_network and use_streaming:
        verbose(f"Streaming mode ({firmware_size/1024**3:.1f} GB image)", "warning")
        flash_script = (
            f"curl -s {source} 2>/tmp/mono_imager_flash.log | "
            f"dd {skip_seek}of={flash_target} bs=4096 "
            f">> /tmp/mono_imager_flash.log 2>&1; "
            f"cat /tmp/mono_imager_flash.log"
        )
    elif is_network:
        local_fw = "/tmp/mono_imager_firmware.img"
        flash_script = (
            f"curl -s -o {local_fw} {source} "
            f"> /tmp/mono_imager_flash.log 2>&1; "
            f"dd if={local_fw} {skip_seek}of={flash_target} bs=4096 "
            f">> /tmp/mono_imager_flash.log 2>&1; "
            f"rm -f {local_fw}; "
            f"cat /tmp/mono_imager_flash.log"
        )
    else:
        # USB: source is a local path on the device
        flash_script = (
            f"dd if={source} {skip_seek}of={flash_target} bs=4096 "
            f"> /tmp/mono_imager_flash.log 2>&1; "
            f"cat /tmp/mono_imager_flash.log"
        )

    response, err = with_spinner(
        d.run_script, flash_script,
        marker="flash_dd", exec_timeout=600,
        message="Flashing — this may take several minutes"
    )

    if err is not None:
        return step(7, "dd flash executed", False, str(err))

    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower() or "failed" in response.lower()

    step(7, "dd flash executed", True)
    step(8, "dd records in/out confirmed", has_records,
         response[-200:] if not has_records else "")
    step(9, "No dd/curl errors", not has_error,
         response[-200:] if has_error else "")

    return has_records and not has_error


# ============================================================================
# USB UNMOUNT — after flash on USB journeys
# ============================================================================

@register_step(
    os=[ALL_OS],
    transfer=["usb"],
    requires=["os_flashed"],
    produces=["usb_unmounted"],
    label="Unmount USB stick"
)
def step_unmount_usb(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Unmount USB stick")
    verbose("=" * 60)

    d = ctx.device
    try:
        d.send_command(f"umount {ctx.usb_mount} 2>&1; sync", timeout=15)
        verbose("✓ USB unmounted")
        return step(10, f"USB unmounted ({ctx.usb_mount})", True)
    except Exception as e:
        # Not fatal — log and continue
        verbose(f"⚠ USB unmount warning: {e}", "warning")
        return step(10, "USB unmount", True)  # non-fatal


# ============================================================================
# REBOOT — non-OPNsense, both transfer methods
# ============================================================================

@register_step(
    os=["OpenWRT", "Armbian"],
    transfer=[ALL_TRANSFER],
    requires=["os_flashed"],
    produces=["rebooted"],
    label="Reboot device"
)
def step_reboot(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Reboot device")
    verbose("=" * 60)

    d = ctx.device
    try:
        d.send_command("reboot", wait_for_prompt=False, timeout=5)
        console_logger.info("Rebooting device...")
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")

    return step(11, "Reboot sent", True)


# ============================================================================
# OPNSENSE: detect MAC — both transfer methods
# ============================================================================

@register_step(
    os=["OPNsense"],
    transfer=[ALL_TRANSFER],
    requires=["network_up"] if False else [],   # best-effort, not blocking
    produces=["device_mac_known"],
    label="Detect device MAC address"
)
def step_detect_mac(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Detect device MAC address")
    verbose("=" * 60)

    d = ctx.device
    try:
        result, _ = d.run_script(
            "cat /sys/class/net/eth0/address 2>/dev/null || echo unknown",
            marker="get_mac", exec_timeout=5
        )
        mac = result.strip()
        if mac and mac != "unknown" and ":" in mac:
            ctx.device_mac = mac
            ctx.set("device_mac_known", mac)
            verbose(f"✓ MAC: {mac}")
            return step(10, f"Device MAC detected ({mac})", True)
    except Exception as e:
        verbose(f"⚠ MAC detection failed: {e}", "warning")

    # Not fatal — firmware re-image step will prompt user if needed
    ctx.set("device_mac_known", None)
    return step(10, "Device MAC detected", False, "will prompt at re-image step")


# ============================================================================
# OPNSENSE: DIP flip to NOR → re-image → DIP flip to eMMC
# All three steps apply to both transfer methods
# ============================================================================

@register_step(
    os=["OPNsense"],
    transfer=[ALL_TRANSFER],
    requires=["os_flashed"],
    produces=["dip_at_nor"],
    label="DIP flip to NOR + power cycle"
)
def step_dip_to_nor(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("DIP Switch → NOR")
    verbose("=" * 60)

    console_logger.info("")
    console_logger.info("⚠  MANUAL ACTION REQUIRED")
    console_logger.info("  Flip DIP switch RIGHT (NOR boot), then power cycle.")
    console_logger.info("  Tool will wait for recovery shell on serial console.")
    console_logger.info("")
    try:
        input("  Press ENTER when DIP is RIGHT and device is powered on... ")
    except KeyboardInterrupt:
        return step(11, "DIP flip to NOR", False, "interrupted")
    return step(11, "DIP flip to NOR confirmed", True)


@register_step(
    os=["OPNsense"],
    transfer=[ALL_TRANSFER],
    requires=["dip_at_nor"],
    produces=["nor_recovery_booted"],
    label="Confirm NOR recovery boot"
)
def step_confirm_nor_boot(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Confirm NOR recovery boot")
    verbose("=" * 60)

    d = ctx.device
    try:
        confirmed = d.verify_recovery_shell(timeout=30)
        return step(12, "NOR recovery shell confirmed", confirmed,
                   "timeout" if not confirmed else "")
    except Exception as e:
        return step(12, "NOR recovery shell confirmed", False, str(e))


@register_step(
    os=["OPNsense"],
    transfer=[ALL_TRANSFER],
    requires=["nor_recovery_booted"],
    produces=["emmc_firmware_reimaged"],
    label="Re-image eMMC firmware (first 32MB)"
)
def step_reimage_emmc_firmware(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("Re-image eMMC firmware region (first 32MB)")
    verbose("=" * 60)

    mac = ctx.device_mac
    if not mac or ":" not in mac:
        verbose("⚠ MAC not in context — prompting user", "warning")
        try:
            mac = input("  Enter device MAC address (for firmware auth): ").strip()
        except KeyboardInterrupt:
            return step(13, "eMMC firmware re-image", False, "interrupted")
        if not mac:
            return step(13, "eMMC firmware re-image", False, "no MAC provided")
        ctx.device_mac = mac

    # Verify auth first (saves time on bad MAC)
    try:
        r = subprocess.run(
            ["curl", "-I", "--user", f"mono:{mac}", "-s", "-w", "%{http_code}",
             FIRMWARE_EMMC_URL],
            capture_output=True, text=True, timeout=10
        )
        if "401" in r.stdout:
            return step(13, "eMMC firmware server auth", False, "401 — check MAC")
        if "200" not in r.stdout:
            return step(13, "eMMC firmware server auth", False, f"HTTP {r.stdout}")
    except subprocess.TimeoutExpired:
        return step(13, "eMMC firmware server auth", False, "server timeout")
    except Exception as e:
        return step(13, "eMMC firmware server auth", False, str(e))

    d = ctx.device
    script = (
        f"curl -s --user mono:{mac} {FIRMWARE_EMMC_URL} "
        f"2>/tmp/mono_imager_reimage.log | "
        f"dd of=/dev/mmcblk0 bs=4096 skip=1 seek=1 "
        f">> /tmp/mono_imager_reimage.log 2>&1; "
        f"cat /tmp/mono_imager_reimage.log"
    )
    try:
        response, err = with_spinner(
            d.run_script, script,
            marker="reimage", exec_timeout=120,
            message="Re-imaging eMMC firmware"
        )
    except Exception as e:
        return step(13, "eMMC firmware re-image", False, str(e))

    if err:
        return step(13, "eMMC firmware re-image", False, str(err))
    if "401" in response or "unauthorized" in response.lower():
        return step(13, "eMMC firmware re-image", False, "auth failed on device")

    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower()
    ok = has_records and not has_error
    return step(13, "eMMC firmware re-image (curl | dd)", ok,
               response[-200:] if not ok else "")


@register_step(
    os=["OPNsense"],
    transfer=[ALL_TRANSFER],
    requires=["emmc_firmware_reimaged"],
    produces=["dip_at_emmc"],
    label="DIP flip to eMMC + power cycle"
)
def step_dip_to_emmc(ctx: StepContext) -> bool:
    verbose("=" * 60)
    verbose("DIP Switch → eMMC")
    verbose("=" * 60)

    console_logger.info("")
    console_logger.info("⚠  MANUAL ACTION REQUIRED")
    console_logger.info("  Flip DIP switch LEFT (eMMC boot), then power cycle.")
    console_logger.info("  Device will boot OPNsense.")
    console_logger.info("")
    try:
        input("  Press ENTER when DIP is LEFT and device is powered on... ")
    except KeyboardInterrupt:
        return step(14, "DIP flip to eMMC", False, "interrupted")

    console_logger.info("✓ Journey complete — device booting OPNsense from eMMC.")
    return step(14, "DIP flip to eMMC confirmed", True)


# ============================================================================
# JOURNEY BUILDER — entry point for tui.py
# ============================================================================

def get_journey(
    os_name:       str,
    transfer:      str,
    device,
    host_ip:       str  = "",
    device_ip:     str  = "",
    firmware_path: Path = None,
    http_port:     int  = 8080,
    device_mac:    str  = "",
    flash_target:  str  = "",
    usb_device:    str  = "/dev/sda",
    usb_mount:     str  = "/mnt/usb",
) -> "FlowRunner":
    """
    Build a FlowRunner for the given OS + transfer method.
    Call .run() on the returned object to execute the journey.
    """
    from mono_imager.step_registry import FlowRunner, StepContext

    ctx = StepContext(
        device        = device,
        os_name       = os_name,
        transfer      = transfer,
        host_ip       = host_ip,
        device_ip     = device_ip,
        http_port     = http_port,
        device_mac    = device_mac,
        firmware_path = firmware_path,
        flash_target  = flash_target or _FLASH_TARGETS.get(os_name, "/dev/mmcblk0"),
        usb_device    = usb_device,
        usb_mount     = usb_mount,
    )
    return FlowRunner(os_name, transfer, ctx)
