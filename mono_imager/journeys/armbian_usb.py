"""
mono-imager journey: Armbian via USB

Follows the official documented procedure (we-are-mono/docs
"Installing Armbian") — see armbian_lan.py, which owns the shared
DIP-flip/firmware-refresh step implementations this file imports.

Steps:
  1. Mount USB stick
  2. Detect firmware file on USB
  3. Flash Armbian image
  4. Unmount USB stick
  5. Flip DIP to eMMC and verify boot
  6. Refresh eMMC firmware (NOR round-trip)

Image detection: scans USB for armbian*.img.xz or armbian*.img (case-insensitive).
Original vendor filenames work directly — no renaming needed.

Author:  H.A. Hermsen
License: GPLv3
"""

import logging
from mono_imager.step_registry import register_step, StepContext
from mono_imager.spinner import with_spinner, Spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger
from mono_imager.journeys.usb_utils import find_image_on_usb, check_usb_size, mount_usb_stick
from mono_imager.journeys.armbian_lan import _flip_to_emmc_and_verify, _refresh_firmware_and_finish
from mono_imager import constants as C

logger = logging.getLogger(__name__)

OS       = "Armbian"
TRANSFER = "usb"


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["usb_mounted"], label="Mount USB stick")
def step_mount_usb(ctx: StepContext) -> bool:
    d = ctx.device
    try:
        # Mount whichever partition holds the image, not blindly sda1 (#26).
        with Spinner("Mounting USB stick..."):
            part, _has_image, detail = mount_usb_stick(d, ctx.usb_device, ctx.usb_mount, [OS])
        ok = part is not None
        if ok:
            check_usb_size(d, ctx.usb_mount)
        return step(0, f"USB mounted ({part or ctx.usb_device} -> {ctx.usb_mount})", ok,
                    detail[-C.LOG_TAIL_SHORT:] if not ok else "")
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

    response, err = with_spinner(d.run_script, flash_script, marker="flash_dd", exec_timeout=C.FLASH_TIMEOUT, message="Flashing Armbian")
    if err:
        return step(0, "Armbian flash executed", False, str(err))
    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower() or "failed" in response.lower()
    step(0, "Armbian flash executed", True)
    step(0, "dd records in/out confirmed", has_records, response[-C.LOG_TAIL:] if not has_records else "")
    step(0, "No errors", not has_error, response[-C.LOG_TAIL:] if has_error else "")
    return has_records and not has_error


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"], produces=["usb_unmounted"], label="Unmount USB stick")
def step_unmount_usb(ctx: StepContext) -> bool:
    try:
        with Spinner("Unmounting USB stick..."):
            ctx.device.send_command(f"umount {ctx.usb_mount} 2>&1; sync", timeout=C.UMOUNT_TIMEOUT)
        return step(0, f"USB unmounted ({ctx.usb_mount})", True)
    except Exception as e:
        verbose(f"⚠ USB unmount warning: {e}", "warning")
        return step(0, "USB unmount", True)


# Official procedure from here on — same as armbian_lan.py, shared via
# the imported helper functions (no network dependency of its own is
# registered here; the firmware-refresh step resolves the device
# network inline, fresh, only once it actually needs it).

@register_step(os=[OS], transfer=[TRANSFER], requires=["usb_unmounted"], produces=["emmc_boot_verified"], label="Flip DIP to eMMC and verify boot")
def step_flip_to_emmc_and_verify(ctx: StepContext) -> bool:
    verbose("=" * C.DIVIDER_WIDTH); verbose("Flip DIP to eMMC and verify boot"); verbose("=" * C.DIVIDER_WIDTH)
    return _flip_to_emmc_and_verify(ctx)


@register_step(os=[OS], transfer=[TRANSFER], requires=["emmc_boot_verified"], produces=["rebooted"], label="Refresh eMMC firmware (NOR round-trip)")
def step_refresh_firmware(ctx: StepContext) -> bool:
    verbose("=" * C.DIVIDER_WIDTH); verbose("Refresh eMMC firmware (NOR round-trip)"); verbose("=" * C.DIVIDER_WIDTH)
    return _refresh_firmware_and_finish(ctx)
