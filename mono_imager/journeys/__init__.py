"""
mono-imager: Journey auto-discovery

Scans this package for journey modules and imports them all.
Each module registers its own steps via @register_step on import.

To add a new journey: drop a new .py file here. That's it.
To remove a journey: delete the file. It disappears from the TUI.

Files starting with _ are skipped (e.g. _common.py).
"""


import importlib
import pkgutil
from pathlib import Path

# Flash targets per OS — single source of truth used by journeys and tui.py
_FLASH_TARGETS = {
    "OPNsense": "/dev/mmcblk0",
    "OpenWRT":  "/dev/mmcblk0p1",
    "Armbian":  "/dev/mmcblk0",
}

# --- Image / OS format guard --------------------------------------------
# Each journey feeds its downloaded image to a specific decompressor before
# dd (opnsense: bzip2 always; openwrt: gunzip on .gz else raw; armbian: xz
# on .xz else raw). Flashing an image whose real container does not match
# that pipeline "succeeds" but produces an unbootable device (e.g. a bzip2
# OPNsense image sent through OpenWRT's raw dd). These helpers catch that.
_CONTAINER_MAGIC = {
    "bzip2": b"BZh",
    "gzip":  b"\x1f\x8b",
    "xz":    b"\xfd7zXZ\x00",
    "zip":   b"PK\x03\x04",
}


def detect_container(path) -> str:
    """Compression container of `path` from its magic bytes:
    'bzip2' | 'gzip' | 'xz' | 'zip' | 'raw' (raw = none recognised)."""
    try:
        with open(path, "rb") as f:
            head = f.read(6)
    except OSError:
        return "raw"
    for name, magic in _CONTAINER_MAGIC.items():
        if head.startswith(magic):
            return name
    return "raw"


def expected_container(os_name: str, path) -> str:
    """The container the chosen OS journey WILL decompress before dd
    (mirrors the per-OS flash steps). 'raw' means it is written as-is."""
    name = str(path).lower()
    if os_name == "OPNsense":
        return "bzip2"
    if os_name == "OpenWRT":
        return "gzip" if name.endswith(".gz") else "raw"
    if os_name == "Armbian":
        return "xz" if name.endswith(".xz") else "raw"
    return "raw"


def check_image_matches_os(os_name: str, path):
    """Return (ok, reason). ok=True when the image's real container matches
    what the chosen OS journey will do with it. Guards against the
    wrong-journey botch (bzip2 OPNsense image via OpenWRT raw dd, etc.)."""
    expected = expected_container(os_name, path)
    actual = detect_container(path)
    if expected == actual:
        return True, ""
    if expected == "raw":
        reason = (
            f"{os_name} expects a raw/uncompressed image here, but this file "
            f"looks {actual}-compressed. It would be written as-is and will "
            f"not boot. Decompress it, or use the extension the journey "
            f"decompresses (OpenWRT: .gz, Armbian: .xz)."
        )
    else:
        seen = "uncompressed" if actual == "raw" else actual
        reason = (
            f"{os_name} expects a {expected}-compressed image, but this file "
            f"looks {seen}. Use the correct image (OPNsense: .img.bz2)."
        )
    return False, reason

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
    device_net           = None,
):
    """
    Build a FlowRunner for the given OS + transfer method.
    Call .run() on the returned object to execute the journey.

    device_net carries the session's resolved device network (DHCP or
    manual — see MonoImager.device_net) and is forwarded to every
    journey unconditionally. Whether a given journey's steps actually
    read it is up to that journey.
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
        device_net    = device_net,
    )
    return FlowRunner(os_name, transfer, ctx)
