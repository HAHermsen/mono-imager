"""
mono-imager: USB-stick step bodies shared by every *_usb journey.

The mount, image-detection and unmount steps were identical in
armbian_usb.py, openwrt_usb.py and opnsense_usb.py apart from the OS
name and the "not found" hint. Each journey still registers its own
steps (same position, labels and requires/produces as before), but the
bodies come from these factories.

Leading underscore: journeys/__init__.py's auto-discovery skips this
file; it registers nothing itself.

Author:  H.A. Hermsen
License: GPLv3
"""

from collections.abc import Callable

from mono_imager import constants as C
from mono_imager.flash_orchestrator import step, verbose
from mono_imager.journeys.usb_utils import check_usb_size, find_image_on_usb, mount_usb_stick
from mono_imager.spinner import Spinner
from mono_imager.step_registry import StepContext

Step = Callable[[StepContext], bool]


def mount_usb_step(os_name: str) -> Step:
    """Mount whichever USB partition holds an `os_name` image (#26)."""
    def step_mount_usb(ctx: StepContext) -> bool:
        d = ctx.device
        try:
            with Spinner("Mounting USB stick..."):
                part, _has_image, detail = mount_usb_stick(d, ctx.usb_device, ctx.usb_mount, [os_name])
            ok = part is not None
            if ok:
                check_usb_size(d, ctx.usb_mount)
            return step(0, f"USB mounted ({part or ctx.usb_device} -> {ctx.usb_mount})", ok,
                        detail[-C.LOG_TAIL_SHORT:] if not ok else "")
        except Exception as e:
            return step(0, "USB mount", False, str(e))
    return step_mount_usb


def firmware_on_usb_step(os_name: str, not_found: str) -> Step:
    """Find the `os_name` image on the mounted stick; `not_found` is the failure hint."""
    def step_firmware_on_usb(ctx: StepContext) -> bool:
        path, fmt = find_image_on_usb(ctx.device, ctx.usb_mount, os_name)
        if not path:
            return step(0, "Firmware found on USB", False, not_found)
        ctx.set("firmware_source", path)
        ctx.set("firmware_format", fmt)
        return step(0, f"Firmware found on USB ({path})", True)
    return step_firmware_on_usb


def step_unmount_usb(ctx: StepContext) -> bool:
    """Unmount the stick; a failing umount is only a warning."""
    try:
        with Spinner("Unmounting USB stick..."):
            ctx.device.send_command(f"umount {ctx.usb_mount} 2>&1; sync", timeout=C.UMOUNT_TIMEOUT)
        return step(0, f"USB unmounted ({ctx.usb_mount})", True)
    except Exception as e:
        verbose(f"⚠ USB unmount warning: {e}", "warning")
        return step(0, "USB unmount", True)
