"""
mono-imager journey: OpenWRT via USB

Steps:
  1. Device network ready
  2. Mount USB stick
  3. Detect firmware file on USB
  4. Flash OpenWRT image (dd) — two dd passes, same doc-matched scheme
     as openwrt_lan.py: bs=512 count=8 (partition table) then
     bs=1M skip=32 seek=32 (rest of image). No local staging needed
     here (the image is already a file on the mounted USB stick), so
     both passes just read it a second time via zcat/cat.
  5. Unmount USB stick
  6. Firmware update (eMMC bootloader)

No automated final DIP-flip/boot-verify step — see openwrt_lan.py's
module docstring. tui.py's own end-of-flash screen already prints the
"flip DIP to eMMC, power-cycle" instruction whenever flash_success is
True, so this journey ends after the firmware update.

Image detection: scans USB for openwrt*.img / openwrt*.img.gz
(case-insensitive). Sysupgrade .bin.gz support was DROPPED — see the
"Revision note" in openwrt_lan.py's module docstring; that format has
no partition table and doesn't fit the whole-disk model both OpenWRT
journeys now use.

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
                    "no OpenWRT image found — expected openwrt*.img.gz or openwrt*.img")
    ctx.set("firmware_source", path)
    ctx.set("firmware_format", fmt)
    return step(0, f"Firmware found on USB ({path})", True)


_STAGED_IMG = "/tmp/mono_imager_openwrt_usb.img"


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash OpenWRT image (dd)")
def step_flash_openwrt(ctx: StepContext) -> bool:
    """
    Decompresses to a local plain file first (if needed), then runs
    both dd passes reading directly from that file — no zcat|dd or
    cat|dd pipes into dd. Same reasoning as openwrt_lan.py's flash
    step: a pipe can hand dd short reads, which for the exact-4KiB
    partition-table pass (bs=512 count=8) is a correctness bug on
    BusyBox dd, not just cosmetic — see that module's docstring for
    the real-hardware finding this is based on. Reading a real local
    file has no such short-read risk.
    """
    d = ctx.device
    source = ctx.get("firmware_source")
    fmt    = ctx.get("firmware_format", "img")

    if fmt not in ("img", "img.gz"):
        return step(0, "OpenWRT flash executed", False,
                    f"unsupported format '{fmt}' — only raw -emmc.img / -emmc.img.gz accepted "
                    "(sysupgrade .bin/.bin.gz is no longer supported)")

    if fmt == "img.gz":
        prep = f'rm -f {_STAGED_IMG}; gunzip -c "{source}" > {_STAGED_IMG} 2>/tmp/mono_imager_dl.log; '
        img_path = _STAGED_IMG
    else:
        prep = ""
        img_path = source  # already a real file on the USB stick — dd reads it directly

    console_logger.info("Flashing OpenWRT — writing both dd passes...")
    script = (
        f"{prep}"
        f'{{ dd if="{img_path}" of={ctx.flash_target} bs=512 count=8; '
        f'dd if="{img_path}" of={ctx.flash_target} bs=1M skip=32 seek=32; }} '
        f"> /tmp/mono_imager_flash.log 2>&1; "
        f"sync; "
        + (f"rm -f {_STAGED_IMG}; " if fmt == "img.gz" else "")
        + "cat /tmp/mono_imager_flash.log"
    )
    response, err = with_spinner(
        d.run_script, script, marker="flash_dd",
        exec_timeout=600, message="Flashing OpenWRT"
    )
    if err:
        return step(0, "OpenWRT flash (dd, both passes)", False, str(err))

    matches = re.findall(r"(\d+)\+(\d+)\s+records out", response or "")
    has_error = "error" in (response or "").lower() or "failed" in (response or "").lower()

    if len(matches) < 2:
        return step(0, "OpenWRT flash (dd, both passes)", False,
                    f"expected 2 'records out' lines, got {len(matches)}: {(response or '')[-300:]}")

    pt_bytes = int(matches[0][0]) * 512 + int(matches[0][1])
    bulk_bytes = int(matches[1][0]) * 1024 * 1024 + int(matches[1][1])

    step(0, "OpenWRT partition table (dd bs=512 count=8)", pt_bytes > 0,
         response[-200:] if pt_bytes == 0 else "")
    ok = pt_bytes > 0 and bulk_bytes > 0 and not has_error
    step(0, f"OpenWRT image (dd bs=1M skip=32 seek=32, {bulk_bytes // 1024 // 1024} MB)",
         ok, response[-200:] if not ok else "")
    return ok


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"], produces=["usb_unmounted"], label="Unmount USB stick")
def step_unmount_usb(ctx: StepContext) -> bool:
    try:
        with Spinner("Unmounting USB stick..."):
            ctx.device.send_command(f"umount {ctx.usb_mount} 2>&1; sync", timeout=15)
        return step(0, f"USB unmounted ({ctx.usb_mount})", True)
    except Exception as e:
        verbose(f"⚠ USB unmount warning: {e}", "warning")
        return step(0, "USB unmount", True)


