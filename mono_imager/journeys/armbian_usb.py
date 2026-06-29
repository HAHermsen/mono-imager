"""
mono-imager journey: Armbian via USB

DIP switch: RIGHT (NOR) throughout — NOR U-Boot loads Armbian from eMMC via extlinux.

Steps:
  1. Mount USB stick
  2. Detect firmware file on USB
  3. Flash Armbian image
  4. Unmount USB stick
  5. Reboot device

Image detection: scans USB for armbian*.img.xz or armbian*.img (case-insensitive).
Original vendor filenames work directly — no renaming needed.

Author:  H.A. Hermsen
License: MIT
"""

__version__ = "v.0.9.9 RC1"
__author__  = "H.A. Hermsen"

import logging
from mono_imager.step_registry import register_step, StepContext
from mono_imager.spinner import with_spinner, Spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger
from mono_imager.journeys.usb_utils import find_image_on_usb, check_usb_size

logger = logging.getLogger(__name__)

OS       = "Armbian"
TRANSFER = "usb"


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["usb_mounted"], label="Mount USB stick")
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
                    "no Armbian image found — expected armbian*.img.xz or armbian*.img")
    ctx.set("firmware_source", path)
    ctx.set("firmware_format", fmt)
    return step(0, f"Firmware found on USB ({path})", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash Armbian image")
def step_flash_armbian(ctx: StepContext) -> bool:
    d = ctx.device
    source = ctx.get("firmware_source")
    fmt    = ctx.get("firmware_format", "img")

    if fmt == "img.xz":
        flash_script = (
            f"xz -dc {source} | "
            f"dd of={ctx.flash_target} bs=1M "
            f"> /tmp/mono_imager_flash.log 2>&1; sync; "
            f"cat /tmp/mono_imager_flash.log"
        )
        console_logger.info("Flashing Armbian from USB (xz | dd) — this takes several minutes...")
    else:
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
    try:
        with Spinner("Unmounting USB stick..."):
            ctx.device.send_command(f"umount {ctx.usb_mount} 2>&1; sync", timeout=15)
        return step(0, f"USB unmounted ({ctx.usb_mount})", True)
    except Exception as e:
        verbose(f"⚠ USB unmount warning: {e}", "warning")
        return step(0, "USB unmount", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["usb_unmounted"], produces=["rebooted"], label="Reboot device")
def step_reboot(ctx: StepContext) -> bool:
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
        console_logger.info("Rebooting device into Armbian...")
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent", True)
