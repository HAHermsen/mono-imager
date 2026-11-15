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

from mono_imager import constants as C
from mono_imager.flash_orchestrator import console_logger, step, verbose
from mono_imager.journeys import _usb_steps as _usb
from mono_imager.journeys.armbian_lan import _flip_to_emmc_and_verify, _refresh_firmware_and_finish
from mono_imager.spinner import with_spinner
from mono_imager.step_registry import StepContext, register_step

logger = logging.getLogger(__name__)

OS       = "Armbian"
TRANSFER = "usb"


step_mount_usb = register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["usb_mounted"], label="Mount USB stick")(
    _usb.mount_usb_step(OS))


step_firmware_on_usb = register_step(os=[OS], transfer=[TRANSFER], requires=["usb_mounted"], produces=["firmware_ready"], label="Detect firmware file on USB")(
    _usb.firmware_on_usb_step(
        OS, "no Armbian image found — expected armbian*.img.xz or armbian*.img"))


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


step_unmount_usb = register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"], produces=["usb_unmounted"], label="Unmount USB stick")(
    _usb.step_unmount_usb)


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
