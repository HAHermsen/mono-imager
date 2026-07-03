"""
mono-imager journey: OpenWRT via USB

Steps:
  1. Device network ready
  2. Mount USB stick
  3. Detect firmware file on USB
  4. Flash OpenWRT image (dd)
  5. Unmount USB stick
  6. Firmware update (eMMC bootloader)
  7. Reboot device

Image detection: scans USB for openwrt*.bin.gz / openwrt*.bin / openwrt*.img (case-insensitive).
Sysupgrade .bin.gz format is handled on-device: extracts the 'root' ext4 member from the
inner tar and writes it directly to the flash target.

U-Boot pre-step:
  Shared with the LAN journey — ensures 'recovery' is defined and
  patches the eMMC U-Boot env so OpenWRT boots from DIP=LEFT.

Author:  H.A. Hermsen
License: GPLv3
"""

import logging
import re
from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner, Spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger
from mono_imager.journeys.openwrt_lan import _uboot_steps_openwrt_lan
from mono_imager.journeys.usb_utils import find_image_on_usb, check_usb_size
from mono_imager.journeys import _common  # noqa: F401 — registers "Device network ready" step

logger = logging.getLogger(__name__)

OS       = "OpenWRT"
TRANSFER = "usb"

register_uboot_steps(OS, TRANSFER, _uboot_steps_openwrt_lan)


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["usb_mounted"], label="Mount USB stick")
def step_mount_usb(ctx: StepContext) -> bool:
    d = ctx.device
    try:
        d.send_command(f"mkdir -p {ctx.usb_mount}", timeout=5)
        with Spinner("Mounting USB stick..."):
            response = d.send_command(
                f"mount {ctx.usb_device}1 {ctx.usb_mount} 2>&1; echo RC=$?", timeout=15
            )
            ok = "RC=0" in response
            if not ok:
                response = d.send_command(
                    f"mount {ctx.usb_device} {ctx.usb_mount} 2>&1; echo RC=$?", timeout=15
                )
                ok = "RC=0" in response
        if ok:
            check_usb_size(d, ctx.usb_mount)
        return step(0, f"USB mounted ({ctx.usb_device} -> {ctx.usb_mount})", ok,
                    response[-100:] if not ok else "")
    except Exception as e:
        return step(0, "USB mount", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["usb_mounted"], produces=["firmware_ready"], label="Detect firmware file on USB")
def step_firmware_on_usb(ctx: StepContext) -> bool:
    path, fmt = find_image_on_usb(ctx.device, ctx.usb_mount, OS)
    if not path:
        return step(0, "Firmware found on USB", False,
                    "no OpenWRT image found — expected openwrt*.bin.gz, openwrt*.bin, or openwrt*.img")
    ctx.set("firmware_source", path)
    ctx.set("firmware_format", fmt)
    return step(0, f"Firmware found on USB ({path})", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready", "emmc_partitioned"], produces=["os_flashed"], label="Flash OpenWRT image (dd)")
def step_flash_openwrt(ctx: StepContext) -> bool:
    d = ctx.device
    source = ctx.get("firmware_source")
    fmt    = ctx.get("firmware_format", "img")

    if fmt in ("bin.gz", "bin"):
        # OpenWRT sysupgrade: outer gzip wraps a tar with a 'root' member.
        # The 'root' member is itself gzip-compressed (double-compressed).
        # Extract root from tar → check first 2 bytes for gzip magic (1f8b) →
        # if gzip, pipe through a second zcat before dd; otherwise dd directly.
        decomp = "zcat" if fmt == "bin.gz" else "cat"
        flash_script = (
            f"SRC={source}; "
            f"TMP=/tmp/mono_rootfs_raw; "
            f"rm -f \"$TMP\"; "
            # Find root member dynamically: handles bare 'root', './root', and
            # 'PREFIX/root' layouts (e.g. sysupgrade-mono_gateway-dk/root).
            # BusyBox head requires -n 1, not -1.
            f"ROOT=$({decomp} \"$SRC\" | tar -t 2>/dev/null | grep -E '(^|/)root$' | head -n 1); "
            f"[ -n \"$ROOT\" ] && {decomp} \"$SRC\" | tar -xOf - \"$ROOT\" 2>/dev/null > \"$TMP\"; "
            f"if [ -s \"$TMP\" ]; then "
            # gzip -t is more reliable than od magic bytes on BusyBox
            f"  if gzip -t \"$TMP\" 2>/dev/null; then "
            f"    zcat \"$TMP\" | dd of={ctx.flash_target} bs=4096 "
            f"    > /tmp/mono_imager_flash.log 2>&1; "
            f"  else "
            f"    dd if=\"$TMP\" of={ctx.flash_target} bs=4096 "
            f"    > /tmp/mono_imager_flash.log 2>&1; "
            f"  fi; "
            f"  rm -f \"$TMP\"; "
            f"else "
            f"  {decomp} \"$SRC\" | dd of={ctx.flash_target} bs=4096 "
            f"  > /tmp/mono_imager_flash.log 2>&1; "
            f"fi; "
            f"sync; cat /tmp/mono_imager_flash.log"
        )
        console_logger.info("Flashing OpenWRT from USB (sysupgrade) — this takes several minutes...")
    elif fmt == "img.gz":
        flash_script = (
            f"zcat {source} | dd of={ctx.flash_target} bs=4096 "
            f"> /tmp/mono_imager_flash.log 2>&1; sync; "
            f"cat /tmp/mono_imager_flash.log"
        )
        console_logger.info("Flashing OpenWRT from USB (gz | dd) — this takes several minutes...")
    else:
        flash_script = (
            f"dd if={source} of={ctx.flash_target} bs=4096 "
            f"> /tmp/mono_imager_flash.log 2>&1; sync; "
            f"cat /tmp/mono_imager_flash.log"
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
    try:
        with Spinner("Unmounting USB stick..."):
            ctx.device.send_command(f"umount {ctx.usb_mount} 2>&1; sync", timeout=15)
        return step(0, f"USB unmounted ({ctx.usb_mount})", True)
    except Exception as e:
        verbose(f"⚠ USB unmount warning: {e}", "warning")
        return step(0, "USB unmount", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["usb_unmounted", "boot_configured"], produces=["rebooted"], label="Reboot device")
def step_reboot(ctx: StepContext) -> bool:
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
        console_logger.info("Rebooting device...")
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent", True)
