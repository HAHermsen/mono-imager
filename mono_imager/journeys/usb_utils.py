"""
Shared USB helpers for mono-imager USB flash journeys.

Images are located by lowercased filename pattern — original vendor-named
files work directly, no renaming needed:
    Armbian_26.2.5_Gateway-dk_resolute_current_6.12.49_minimal.img.xz
    openwrt-layerscape-armv8_64b-mono_gateway-dk-ext4-sysupgrade.bin.gz
    OPNsense-26.1.5-arm-aarch64-GATEWAY.img.bz2

Recommended minimum USB stick size: 16 GB.
Typical compressed sizes: Armbian ~400 MB, OpenWRT ~100 MB, OPNsense ~600 MB.
All three fit comfortably on a 16 GB stick with room to spare.
If the stick is smaller, flashing a single OS may still work, but you
cannot cache all images simultaneously.
"""

from mono_imager.flash_orchestrator import verbose

USB_MIN_GB = 16
USB_MIN_KB = USB_MIN_GB * 1024 * 1024

# (lowercase_prefix, lowercase_suffix, format_tag)
# More-specific extensions listed first so .img.xz matches before .img.
_PATTERNS = {
    "Armbian": [
        ("armbian", ".img.xz", "img.xz"),
        ("armbian", ".img",    "img"),
    ],
    "OpenWRT": [
        ("openwrt", ".bin.gz", "bin.gz"),
        ("openwrt", ".bin",    "bin"),
        ("openwrt", ".img.gz", "img.gz"),
        ("openwrt", ".img",    "img"),
    ],
    "OPNsense": [
        ("opnsense", ".img.bz2", "img.bz2"),
        ("opnsense", ".img",     "img"),
    ],
}


def check_usb_size(device, usb_mount: str) -> None:
    """
    Warn (non-fatal) if the mounted USB stick total capacity is below
    USB_MIN_GB.  A small stick may still work for a single OS image.
    """
    try:
        out = device.run_script(
            f"df -k {usb_mount} | awk 'NR==2 {{print $2}}'",
            marker="usb_size_check", exec_timeout=5,
        ).strip()
        kb = next(int(l) for l in out.splitlines() if l.strip().isdigit())
        gb = kb / (1024 * 1024)
        if kb < USB_MIN_KB:
            verbose(
                f"  ⚠ USB stick is {gb:.1f} GB — minimum recommended is {USB_MIN_GB} GB "
                "to cache all OS images. Flashing this single image may still work.",
                "warning",
            )
        else:
            verbose(f"  ✓ USB stick capacity: {gb:.1f} GB")
    except Exception as e:
        verbose(f"  ⚠ Could not read USB size: {e}", "warning")


def find_image_on_usb(device, usb_mount: str, os_name: str):
    """
    Scan the USB mount for the first file matching a known image pattern
    for os_name.  Matching is case-insensitive (basename lowercased before
    comparison).

    Returns (filepath, format_tag) on success, (None, None) if not found.
    format_tag is one of: 'img', 'img.xz', 'bin.gz', 'bin', 'img.gz', 'img.bz2'
    """
    patterns = _PATTERNS.get(os_name)
    if not patterns:
        verbose(f"  ⚠ No USB image patterns defined for '{os_name}'", "warning")
        return None, None

    # Build a POSIX sh case statement. Each matching clause prints
    # "TAG:FILEPATH" and sets found=1 to stop the for loop.
    clauses = "".join(
        f'    {pfx}*{sfx}) printf "%s\\n" "{tag}:$f"; found=1; break ;;\n'
        for pfx, sfx, tag in patterns
    )
    script = (
        "found=0\n"
        f"for f in {usb_mount}/*; do\n"
        '  [ -f "$f" ] || continue\n'
        "  b=$(basename \"$f\" | tr 'A-Z' 'a-z')\n"
        '  case "$b" in\n'
        f"{clauses}"
        "  esac\n"
        "done\n"
        '[ $found -eq 0 ] && printf "NOT_FOUND\\n"'
    )

    try:
        raw = device.run_script(script, marker="usb_find_image", exec_timeout=10)
    except Exception as e:
        verbose(f"  ⚠ USB image scan failed: {e}", "warning")
        return None, None

    valid_tags = {"img", "img.xz", "bin.gz", "bin", "img.gz", "img.bz2"}
    for line in raw.splitlines():
        line = line.strip()
        if line == "NOT_FOUND":
            break
        if ":" in line:
            tag, _, path = line.partition(":")
            if tag in valid_tags and path.startswith(usb_mount):
                verbose(f"  ✓ Found: {path} [{tag}]")
                return path, tag

    return None, None
