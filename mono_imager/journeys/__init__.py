"""
mono-imager: Journey auto-discovery

Scans this package for journey modules and imports them all.
Each module registers its own steps via @register_step on import.

To add a new journey: drop a new .py file here. That's it.
To remove a journey: delete the file. It disappears from the TUI.

Files starting with _ are skipped (e.g. _common.py).
"""


__version__ = "0.9.5"
__author__  = "H.A. Hermsen"

import importlib
import pkgutil
from pathlib import Path

# Flash targets per OS — single source of truth used by journeys and tui.py
_FLASH_TARGETS = {
    "OPNsense": "/dev/mmcblk0",
    "OpenWRT":  "/dev/mmcblk0p1",
    "Armbian":  "/dev/mmcblk0",
}

def _load_all_journeys():
    """Import all journey modules in this package."""
    package_dir = Path(__file__).parent
    for finder, name, ispkg in pkgutil.iter_modules([str(package_dir)]):
        if not name.startswith("_"):
            importlib.import_module(f"mono_imager.journeys.{name}")

_load_all_journeys()


def discovered_journeys() -> list[tuple[str, str]]:
    """
    Return sorted list of (os_name, transfer) pairs discovered from
    the registry — used by the TUI to build its OS/transfer menus
    dynamically rather than from a hardcoded list.
    """
    from mono_imager.step_registry import _registry, ALL_OS, ALL_TRANSFER

    seen = set()
    for descriptor in _registry:
        for os_name in descriptor.os:
            if os_name == ALL_OS:
                continue
            for transfer in descriptor.transfer:
                if transfer == ALL_TRANSFER:
                    continue
                seen.add((os_name, transfer))

    return sorted(seen)


def get_firmware_prompt(os_name: str, transfer: str) -> str:
    """
    Return the firmware file prompt string for the given OS+transfer.
    Defined in each journey file as FIRMWARE_PROMPT.
    Falls back to a generic prompt if not defined.
    """
    import sys
    module_name = f"mono_imager.journeys.{os_name.lower()}_{transfer}"
    mod = sys.modules.get(module_name)
    if mod and hasattr(mod, "FIRMWARE_PROMPT"):
        return mod.FIRMWARE_PROMPT
    return "Type the full path (or drag-n-drop) of the firmware file:"


def get_journey(
    os_name:       str,
    transfer:      str,
    device,
    host_ip:       str  = "",
    device_ip:     str  = "",
    firmware_path        = None,
    http_port:     int  = 8080,
    device_mac:    str  = "",
    flash_target:  str  = "",
    usb_device:    str  = "/dev/sda",
    usb_mount:     str  = "/mnt/usb",
):
    """
    Build a FlowRunner for the given OS + transfer method.
    Call .run() on the returned object to execute the journey.
    """
    from mono_imager.step_registry import FlowRunner, StepContext

    ctx = StepContext(
        device        = device,
        os_name       = os_name,
        transfer      = transfer,
        host_ip       = host_ip,
        device_ip     = device_ip,
        http_port     = http_port,
        device_mac    = device_mac,
        firmware_path = firmware_path,
        flash_target  = flash_target or _FLASH_TARGETS.get(os_name, "/dev/mmcblk0"),
        usb_device    = usb_device,
        usb_mount     = usb_mount,
    )
    return FlowRunner(os_name, transfer, ctx)
