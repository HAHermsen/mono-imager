"""
mono-imager journey: OPNsense via USB

Steps:
  1. Device network ready
  2. Confirm DIP switch is RIGHT (NOR)
  3. Mount USB stick
  4. Verify firmware file on USB
  5. Flash OPNsense image (bzip2 -dck | dd bs=1M)
  6. Unmount USB stick
  7. Detect device MAC address
  8. Re-image eMMC firmware (firmware update)
  9. Reboot into OPNsense (DIP stays RIGHT / NOR)

Author:  H.A. Hermsen
License: GPLv3
"""

import logging

from mono_imager import constants as C
from mono_imager.flash_orchestrator import console_logger, step
from mono_imager.journeys import _common  # noqa: F401 — registers "Device network ready" step
from mono_imager.journeys import _usb_steps as _usb
from mono_imager.journeys import opnsense_lan as _lan
from mono_imager.spinner import with_spinner
from mono_imager.step_registry import StepContext, register_step, register_uboot_steps

logger = logging.getLogger(__name__)

OS       = "OPNsense"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the OPNsense .img.bz2 file:"
TRANSFER = "usb"


register_uboot_steps(OS, TRANSFER, _lan._uboot_steps_opnsense_lan)


step_confirm_dip_nor = register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["dip_confirmed_nor"], label="Confirm DIP switch is RIGHT (NOR)")(
    _lan.step_confirm_dip_nor)


step_mount_usb = register_step(os=[OS], transfer=[TRANSFER], requires=["dip_confirmed_nor"], produces=["usb_mounted"], label="Mount USB stick")(
    _usb.mount_usb_step(OS))


step_firmware_on_usb = register_step(os=[OS], transfer=[TRANSFER], requires=["usb_mounted"], produces=["firmware_ready"], label="Detect firmware file on USB")(
    _usb.firmware_on_usb_step(
        OS, "no OPNsense image found — expected opnsense*.img.bz2 or opnsense*.img"))


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
    response, err = with_spinner(d.run_script, flash_script, marker="flash_dd", exec_timeout=C.FLASH_TIMEOUT_SLOW, message="Flashing OPNsense (bzip2 | dd)")
    if err:
        return step(0, "OPNsense flash executed", False, str(err))
    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower() or "failed" in response.lower()
    step(0, "OPNsense flash executed", True)
    step(0, "dd records in/out confirmed", has_records, response[-C.LOG_TAIL:] if not has_records else "")
    step(0, "No errors", not has_error, response[-C.LOG_TAIL:] if has_error else "")
    return has_records and not has_error


step_unmount_usb = register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"], produces=["usb_unmounted"], label="Unmount USB stick")(
    _usb.step_unmount_usb)


step_detect_mac = register_step(os=[OS], transfer=[TRANSFER], requires=["network_up"], produces=["device_mac_known"], label="Detect device MAC address")(
    _lan.step_detect_mac)


step_reimage_emmc_firmware = register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed", "device_mac_known", "network_up"], produces=["emmc_firmware_reimaged"], label="Re-image eMMC firmware (firmware update)")(
    _lan.step_reimage_emmc_firmware)


step_reboot = register_step(os=[OS], transfer=[TRANSFER], requires=["emmc_firmware_reimaged"], produces=["rebooted"], label="Reboot into OPNsense")(
    _lan.step_reboot)
