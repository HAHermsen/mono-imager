"""
mono-imager journey: OPNsense via USB

Steps:
  1. Confirm DIP switch is RIGHT (NOR)
  2. Mount USB stick
  3. Verify firmware file on USB
  4. Flash OPNsense image (bzip2 -dck | dd bs=1M)
  5. Unmount USB stick
  6. Detect device MAC address
  7. Re-image eMMC firmware (firmware update)
  8. DIP flip to eMMC + power cycle

Author:  H.A. Hermsen
License: MIT
"""

__version__ = "0.9.5"
__author__  = "H.A. Hermsen"

import logging
from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger

logger = logging.getLogger(__name__)

OS       = "OPNsense"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the OPNsense .img.bz2 file:"
TRANSFER = "usb"


def _uboot_steps_opnsense_usb(device) -> bool:
    """
    U-Boot commands for OPNsense — runs between phase1_uboot() and phase1_recovery().
    Set env vars and saveenv BEFORE erasing eMMC — MMC env backend is primary,
    so we must write while MMC is intact, then erase after.
    """
    verbose("Setting U-Boot env for OPNsense...")
    try:
        device.send_command("setenv bootcmd_bak ${bootcmd}", timeout=10)
        device.send_command('setenv bootcmd "run opnsense || run recovery"', timeout=10)
        device.send_command(
            'setenv opnsense "mmc dev 0; load mmc 0:1 0x82000000 kernel.img; '
            'load mmc 0:1 0x88000000 dtb/mono-gateway-dk.dtb; booti 0x82000000 - 0x88000000"',
            timeout=10
        )
        device.send_command("saveenv", timeout=15)
        step(0, "U-Boot env set for OPNsense", True)
    except Exception as e:
        verbose(f"✗ U-Boot env set failed: {e}", "error")
        return False

    verbose("Erasing eMMC (OPNsense requirement)...")
    try:
        response = device.send_command("mmc erase 0 3b48000", timeout=120)
        if "error" in response.lower():
            verbose(f"✗ eMMC erase failed: {response[-100:]}", "error")
            return False
        step(0, "eMMC erase (mmc erase 0 3b48000)", True)
    except Exception as e:
        verbose(f"✗ eMMC erase exception: {e}", "error")
        return False

    return True

register_uboot_steps(OS, TRANSFER, _uboot_steps_opnsense_usb)


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["dip_confirmed_nor"], label="Confirm DIP switch is RIGHT (NOR)")
def step_confirm_dip_nor(ctx: StepContext) -> bool:
    console_logger.info("")
    console_logger.info("  ┌─────────────────────────────────────────────────┐")
    console_logger.info("  │  START HERE: DIP switch → RIGHT (NOR)            │")
    console_logger.info("  │                                                   │")
    console_logger.info("  │  OPNsense flashes and boots entirely from NOR.   │")
    console_logger.info("  │  Keep DIP RIGHT throughout — do NOT flip to LEFT. │")
    console_logger.info("  └─────────────────────────────────────────────────┘")
    console_logger.info("")
    try:
        input("  Confirm DIP is RIGHT and press ENTER to continue... ")
    except KeyboardInterrupt:
        return step(0, "DIP confirmed NOR", False, "interrupted")
    return step(0, "DIP confirmed NOR (RIGHT)", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["dip_confirmed_nor"], produces=["usb_mounted"], label="Mount USB stick")
def step_mount_usb(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Mount USB stick"); verbose("=" * 60)
    d = ctx.device
    try:
        d.send_command(f"mkdir -p {ctx.usb_mount}", timeout=5)
        response = d.send_command(f"mount {ctx.usb_device}1 {ctx.usb_mount} 2>&1; echo RC=$?", timeout=15)
        ok = "RC=0" in response
        if not ok:
            response = d.send_command(f"mount {ctx.usb_device} {ctx.usb_mount} 2>&1; echo RC=$?", timeout=15)
            ok = "RC=0" in response
        return step(0, f"USB mounted ({ctx.usb_device} → {ctx.usb_mount})", ok, response[-100:] if not ok else "")
    except Exception as e:
        return step(0, "USB mount", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["usb_mounted"], produces=["firmware_ready"], label="Verify firmware file on USB")
def step_firmware_on_usb(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Verify firmware file on USB"); verbose("=" * 60)
    d = ctx.device
    fw_path = f"{ctx.usb_mount}/firmware.img"
    try:
        response = d.send_command(f"test -f {fw_path} && echo FOUND || echo MISSING", timeout=5)
        ok = "FOUND" in response
        if ok:
            ctx.set("firmware_source", fw_path)
        return step(0, f"Firmware found on USB ({fw_path})", ok, "file not found on USB stick" if not ok else "")
    except Exception as e:
        return step(0, "Firmware on USB", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash OPNsense image (bzip2 | dd)")
def step_flash_opnsense(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Flash OPNsense image"); verbose("=" * 60)
    d = ctx.device
    source = ctx.get("firmware_source")
    # Per docs: bzip2 -dck OPNsense-*.img.bz2 | dd of=/dev/mmcblk0 bs=1M
    flash_script = (
        f"bzip2 -dck {source} | "
        f"dd of={ctx.flash_target} bs=1M "
        f"> /tmp/mono_imager_flash.log 2>&1; sync; "
        f"cat /tmp/mono_imager_flash.log"
    )
    console_logger.info("Flashing OPNsense from USB — decompressing, this takes several minutes...")
    response, err = with_spinner(d.run_script, flash_script, marker="flash_dd", exec_timeout=1200, message="Flashing OPNsense (bzip2 | dd)")
    if err:
        return step(0, "OPNsense flash executed", False, str(err))
    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower() or "failed" in response.lower()
    step(0, "OPNsense flash executed", True)
    step(0, "dd records in/out confirmed", has_records, response[-200:] if not has_records else "")
    step(0, "No errors", not has_error, response[-200:] if has_error else "")
    return has_records and not has_error


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"], produces=["usb_unmounted"], label="Unmount USB stick")
def step_unmount_usb(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Unmount USB stick"); verbose("=" * 60)
    try:
        ctx.device.send_command(f"umount {ctx.usb_mount} 2>&1; sync", timeout=15)
        return step(0, f"USB unmounted ({ctx.usb_mount})", True)
    except Exception as e:
        verbose(f"⚠ USB unmount warning: {e}", "warning")
        return step(0, "USB unmount", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["device_mac_known"], label="Detect device MAC address")
def step_detect_mac(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Detect device MAC address"); verbose("=" * 60)
    d = ctx.device
    try:
        result = d.run_script("cat /sys/class/net/eth0/address 2>/dev/null || echo unknown", marker="get_mac", exec_timeout=5)
        mac = result.strip()
        if mac and mac != "unknown" and ":" in mac:
            ctx.device_mac = mac
            ctx.set("device_mac", mac)
            ctx.set("device_mac_known", mac)
            verbose(f"✓ MAC: {mac}")
            return step(0, f"Device MAC detected ({mac})", True)
    except Exception as e:
        verbose(f"⚠ MAC detection failed: {e}", "warning")
    ctx.set("device_mac_known", None)
    step(0, "Device MAC detected", False, "will prompt at re-image step")
    return True  # non-fatal


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed", "device_mac_known"], produces=["emmc_firmware_reimaged"], label="Re-image eMMC firmware (firmware update)")
def step_reimage_emmc_firmware(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Re-image eMMC firmware via 'firmware update'"); verbose("=" * 60)
    from mono_imager.recovery_orchestrator import run_firmware_update
    d = ctx.device
    try:
        ok = run_firmware_update(d)
        return step(0, "eMMC firmware re-image (firmware update)", ok)
    except Exception as e:
        return step(0, "eMMC firmware re-image (firmware update)", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["emmc_firmware_reimaged"], produces=["rebooted"], label="Reboot into OPNsense")
def step_reboot(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Reboot into OPNsense"); verbose("=" * 60)
    console_logger.info("")
    console_logger.info("✓ Rebooting — U-Boot will boot OPNsense from eMMC automatically.")
    console_logger.info("  DIP stays RIGHT (NOR). No action needed.")
    console_logger.info("")
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent — OPNsense booting from eMMC", True)
