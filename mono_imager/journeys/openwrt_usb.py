"""
mono-imager journey: OpenWRT via USB

Steps:
  1. Mount USB stick
  2. Verify firmware file on USB
  3. Flash OpenWRT image (dd)
  4. Unmount USB stick
  5. Reboot device

U-Boot pre-step:
  Shared with the LAN journey — ensures 'recovery' is defined and
  patches the eMMC U-Boot env so OpenWRT boots from DIP=LEFT.

Author:  H.A. Hermsen
License: MIT
"""

__version__ = "0.9.5"
__author__  = "H.A. Hermsen"

import logging
import re
from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger
from mono_imager.journeys.openwrt_lan import _uboot_steps_openwrt_lan

logger = logging.getLogger(__name__)

OS       = "OpenWRT"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the OpenWRT .img or .bin.gz file:"
TRANSFER = "usb"

# Reuse the same U-Boot pre-step as the LAN journey: checks/restores the
# 'recovery' NOR env variable and patches the eMMC env bootcmd for OpenWRT.
register_uboot_steps(OS, TRANSFER, _uboot_steps_openwrt_lan)


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["usb_mounted"], label="Mount USB stick")
def step_mount_usb(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Mount USB stick"); verbose("=" * 60)
    d = ctx.device
    try:
        d.send_command(f"mkdir -p {ctx.usb_mount}", timeout=5)
        response = d.send_command(
            f"mount {ctx.usb_device}1 {ctx.usb_mount} 2>&1; echo RC=$?", timeout=15
        )
        ok = "RC=0" in response
        if not ok:
            response = d.send_command(
                f"mount {ctx.usb_device} {ctx.usb_mount} 2>&1; echo RC=$?", timeout=15
            )
            ok = "RC=0" in response
        return step(0, f"USB mounted ({ctx.usb_device} → {ctx.usb_mount})", ok,
                    response[-100:] if not ok else "")
    except Exception as e:
        return step(0, "USB mount", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["usb_mounted"], produces=["firmware_ready"], label="Verify firmware file on USB")
def step_firmware_on_usb(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Verify firmware file on USB"); verbose("=" * 60)
    d = ctx.device
    fw_path = f"{ctx.usb_mount}/firmware.img"
    try:
        response = d.send_command(
            f"test -f {fw_path} && echo FOUND || echo MISSING", timeout=5
        )
        ok = "FOUND" in response
        if ok:
            ctx.set("firmware_source", fw_path)
        return step(0, f"Firmware found on USB ({fw_path})", ok,
                    "file not found on USB stick" if not ok else "")
    except Exception as e:
        return step(0, "Firmware on USB", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash OpenWRT image (dd)")
def step_flash_openwrt(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Flash OpenWRT image"); verbose("=" * 60)
    d = ctx.device
    source = ctx.get("firmware_source")

    # OpenWRT sysupgrade.bin.gz is gzip(tar.gz) with a 'root' member that
    # holds the raw ext4 image.  dd'ing the archive directly would put a tar
    # stream on the partition.  Try extracting on-device first; fall back to
    # direct dd for plain raw images.
    flash_script = (
        f"SRC={source}; "
        f"TMP=/tmp/mono_rootfs.ext4; "
        f"rm -f \"$TMP\"; "
        f"zcat \"$SRC\" 2>/dev/null | tar -xzOf - root 2>/dev/null > \"$TMP\"; "
        f"[ -s \"$TMP\" ] || "
        f"zcat \"$SRC\" 2>/dev/null | tar -xzOf - ./root 2>/dev/null > \"$TMP\"; "
        f"if [ -s \"$TMP\" ]; then "
        f"  {{ dd if=\"$TMP\" of={ctx.flash_target} bs=4096; }} "
        f"  > /tmp/mono_imager_flash.log 2>&1; "
        f"  rm -f \"$TMP\"; "
        f"else "
        f"  {{ dd if=\"$SRC\" of={ctx.flash_target} bs=4096; }} "
        f"  > /tmp/mono_imager_flash.log 2>&1; "
        f"fi; "
        f"sync; cat /tmp/mono_imager_flash.log"
    )
    console_logger.info("Flashing OpenWRT from USB — this takes several minutes...")
    response, err = with_spinner(
        d.run_script, flash_script, marker="flash_dd",
        exec_timeout=600, message="Flashing OpenWRT"
    )
    if err:
        return step(0, "OpenWRT flash executed", False, str(err))

    m = re.search(r"(\d+)\+(\d+)\s+records out", response)
    if m:
        full_records, partial_records = int(m.group(1)), int(m.group(2))
        bytes_written = full_records * 4096 + partial_records
    else:
        full_records = partial_records = bytes_written = 0

    has_real_data = bytes_written > 0
    has_error     = "error" in response.lower() or "failed" in response.lower()

    step(0, "OpenWRT flash executed", True)
    step(0, f"dd wrote data ({bytes_written // 1024} KB)", has_real_data,
         response[-200:] if not has_real_data else "")
    step(0, "No errors", not has_error, response[-200:] if has_error else "")
    return has_real_data and not has_error


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
        console_logger.info("Rebooting device...")
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent", True)
