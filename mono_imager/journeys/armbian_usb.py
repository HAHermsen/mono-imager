"""
mono-imager journey: Armbian via USB

Steps:
  1. Mount USB stick
  2. Verify firmware file on USB
  3. Flash Armbian image (dd bs=1M + sync)
  4. Unmount USB stick
  5. Reboot device

Author:  H.A. Hermsen
License: MIT
"""

__version__ = "0.9.5"
__author__  = "H.A. Hermsen"

import logging
from mono_imager.step_registry import register_step, StepContext
from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger

logger = logging.getLogger(__name__)

OS       = "Armbian"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the Armbian .img file:"
TRANSFER = "usb"


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["usb_mounted"], label="Mount USB stick")
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


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash Armbian image (dd bs=1M)")
def step_flash_armbian(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Flash Armbian image"); verbose("=" * 60)
    d = ctx.device
    source = ctx.get("firmware_source")
    flash_script = (
        f"dd if={source} of={ctx.flash_target} bs=1M "
        f"> /tmp/mono_imager_flash.log 2>&1; sync; "
        f"cat /tmp/mono_imager_flash.log"
    )
    console_logger.info("Flashing Armbian from USB — this takes several minutes...")
    response, err = with_spinner(d.run_script, flash_script, marker="flash_dd", exec_timeout=600, message="Flashing Armbian")
    if err:
        return step(0, "Armbian flash executed", False, str(err))
    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower() or "failed" in response.lower()
    step(0, "Armbian flash executed", True)
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


@register_step(os=[OS], transfer=[TRANSFER], requires=["usb_unmounted"], produces=["rebooted"], label="Reboot device")
def step_reboot(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Reboot device"); verbose("=" * 60)
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
        console_logger.info("Rebooting device into Armbian...")
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent", True)
