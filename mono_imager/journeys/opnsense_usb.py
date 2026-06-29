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
  8. Reboot into OPNsense (DIP stays RIGHT / NOR)

Author:  H.A. Hermsen
License: MIT
"""

__version__ = "v.0.9.9 RC1"
__author__  = "H.A. Hermsen"

import logging
from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner, Spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger
from mono_imager.journeys.usb_utils import find_image_on_usb, check_usb_size
from mono_imager.journeys.opnsense_lan import _uboot_steps_opnsense_lan

logger = logging.getLogger(__name__)

OS       = "OPNsense"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the OPNsense .img.bz2 file:"
TRANSFER = "usb"


register_uboot_steps(OS, TRANSFER, _uboot_steps_opnsense_lan)


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
    d = ctx.device
    try:
        d.send_command(f"mkdir -p {ctx.usb_mount}", timeout=5)
        with Spinner("Mounting USB stick..."):
            response = d.send_command(f"mount {ctx.usb_device}1 {ctx.usb_mount} 2>&1; echo RC=$?", timeout=15)
            ok = "RC=0" in response
            if not ok:
                response = d.send_command(f"mount {ctx.usb_device} {ctx.usb_mount} 2>&1; echo RC=$?", timeout=15)
                ok = "RC=0" in response
        if ok:
            check_usb_size(d, ctx.usb_mount)
        return step(0, f"USB mounted ({ctx.usb_device} -> {ctx.usb_mount})", ok, response[-100:] if not ok else "")
    except Exception as e:
        return step(0, "USB mount", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["usb_mounted"], produces=["firmware_ready"], label="Detect firmware file on USB")
def step_firmware_on_usb(ctx: StepContext) -> bool:
    path, fmt = find_image_on_usb(ctx.device, ctx.usb_mount, OS)
    if not path:
        return step(0, "Firmware found on USB", False,
                    "no OPNsense image found — expected opnsense*.img.bz2 or opnsense*.img")
    ctx.set("firmware_source", path)
    ctx.set("firmware_format", fmt)
    return step(0, f"Firmware found on USB ({path})", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash OPNsense image (bzip2 | dd)")
def step_flash_opnsense(ctx: StepContext) -> bool:
    d = ctx.device
    source = ctx.get("firmware_source")
    fmt    = ctx.get("firmware_format", "img.bz2")

    if fmt == "img.bz2":
        flash_script = (
            f"bzip2 -dc {source} | "
            f"dd of={ctx.flash_target} bs=1M "
            f"> /tmp/mono_imager_flash.log 2>&1; sync; "
            f"cat /tmp/mono_imager_flash.log"
        )
        console_logger.info("Flashing OPNsense from USB (bzip2 | dd) — this takes several minutes...")
    else:
        flash_script = (
            f"dd if={source} of={ctx.flash_target} bs=1M "
            f"> /tmp/mono_imager_flash.log 2>&1; sync; "
            f"cat /tmp/mono_imager_flash.log"
        )
        console_logger.info("Flashing OPNsense from USB — this takes several minutes...")
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
    try:
        with Spinner("Unmounting USB stick..."):
            ctx.device.send_command(f"umount {ctx.usb_mount} 2>&1; sync", timeout=15)
        return step(0, f"USB unmounted ({ctx.usb_mount})", True)
    except Exception as e:
        verbose(f"⚠ USB unmount warning: {e}", "warning")
        return step(0, "USB unmount", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["device_mac_known"], label="Detect device MAC address")
def step_detect_mac(ctx: StepContext) -> bool:
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
    from mono_imager.recovery_orchestrator import run_firmware_update
    d = ctx.device
    try:
        with Spinner("Re-imaging eMMC firmware (firmware update)..."):
            ok = run_firmware_update(d)
        return step(0, "eMMC firmware re-image (firmware update)", ok)
    except Exception as e:
        return step(0, "eMMC firmware re-image (firmware update)", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["emmc_firmware_reimaged"], produces=["rebooted"], label="Reboot into OPNsense")
def step_reboot(ctx: StepContext) -> bool:
    console_logger.info("")
    console_logger.info("✓ Rebooting — U-Boot will boot OPNsense from eMMC automatically.")
    console_logger.info("  DIP stays RIGHT (NOR). No action needed.")
    console_logger.info("")
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent — OPNsense booting from eMMC", True)
