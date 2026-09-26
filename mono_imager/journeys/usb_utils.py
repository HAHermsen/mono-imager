"""
Shared USB helpers for mono-imager USB flash journeys.

Images are located by lowercased filename pattern — original vendor-named
files work directly, no renaming needed:
    Armbian_26.2.5_Gateway-dk_resolute_current_6.12.49_minimal.img.xz
    openwrt-layerscape-armv8_64b-mono_gateway-dk-ext4-emmc.img.gz
    OPNsense-26.1.5-arm-aarch64-GATEWAY.img.bz2

Recommended minimum USB stick size: 16 GB.
Typical compressed sizes: Armbian ~400 MB, OpenWRT ~100 MB, OPNsense ~600 MB.
All three fit comfortably on a 16 GB stick with room to spare.
If the stick is smaller, flashing a single OS may still work, but you
cannot cache all images simultaneously.
"""

import re

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
        ("openwrt", ".img.gz", "img.gz"),
        ("openwrt", ".img",    "img"),
    ],
    "OPNsense": [
        ("opnsense", ".img.bz2", "img.bz2"),
        ("opnsense", ".img",     "img"),
    ],
}


_SAFE_MOUNT_PATH = re.compile(r'[A-Za-z0-9/_.-]+')
_SAFE_USB_DEVICE = re.compile(r'/dev/[a-z]+')


def build_mount_script(usb_device: str, usb_mount: str, os_names) -> str:
    """
    POSIX-sh script that mounts the USB partition holding an OS image (#26).

    Hardcoding `mount /dev/sda1` broke on GUID-partitioned sticks (macOS
    default): sda1 is the ~200 MB EFI System Partition — FAT, so it mounts
    fine, but the images live on sda2. Instead, every partition listed in
    /proc/partitions (sda1..sdaN, in order), then the bare disk
    (unpartitioned "superfloppy" sticks), is mounted in turn and checked for
    a file matching os_names' image patterns. The first hit stays mounted.
    If none has an image, the first mountable candidate is left mounted so
    the caller's image-detection step reports the precise "no image found"
    error instead of a misleading mount failure.

    Runs as ONE script on the device: each serial round-trip costs ~5 s, so
    looping per partition from Python would multiply the step's duration.
    /proc/partitions rather than lsblk/blkid, which BusyBox recovery images
    are not guaranteed to ship.

    Prints exactly one of:
        USB_MOUNTED:<dev>:IMAGE     image found on <dev>, left mounted
        USB_MOUNTED:<dev>:NOIMAGE   nothing matched, <dev> left mounted
        USB_MOUNT_FAILED            no candidate mounted at all
    """
    disk = usb_device.rsplit("/", 1)[-1]
    patterns = [f"{pfx}*{sfx}" for os_name in os_names
                for pfx, sfx, _tag in _PATTERNS.get(os_name, [])]
    case = "|".join(patterns) if patterns else "__mono_imager_no_pattern__"
    return (
        f'm="{usb_mount}"\n'
        'mkdir -p "$m"\n'
        'umount "$m" 2>/dev/null\n'
        'first=""\n'
        f"for p in $(awk '$4 ~ /^{disk}[0-9]+$/ {{print $4}}' /proc/partitions) {disk}; do\n"
        '  mount "/dev/$p" "$m" 2>/dev/null || continue\n'
        '  hit=0\n'
        '  for f in "$m"/*; do\n'
        '    [ -f "$f" ] || continue\n'
        "    b=$(basename \"$f\" | tr 'A-Z' 'a-z')\n"
        f'    case "$b" in {case}) hit=1; break ;; esac\n'
        '  done\n'
        '  if [ $hit -eq 1 ]; then echo "USB_MOUNTED:/dev/$p:IMAGE"; exit 0; fi\n'
        '  [ -z "$first" ] && first="$p"\n'
        '  umount "$m" 2>/dev/null\n'
        'done\n'
        'if [ -n "$first" ] && mount "/dev/$first" "$m" 2>/dev/null; then\n'
        '  echo "USB_MOUNTED:/dev/$first:NOIMAGE"; exit 0\n'
        'fi\n'
        'echo "USB_MOUNT_FAILED"\n'
    )


def mount_usb_stick(device, usb_device: str, usb_mount: str, os_names):
    """
    Mount the USB stick partition that holds an image for any of os_names
    (see build_mount_script() for why and how).

    Returns (partition, has_image, detail):
        partition  — e.g. "/dev/sda2", or None if nothing could be mounted
        has_image  — True if that partition holds a matching image
        detail     — raw device output (for the step's failure message)
    """
    if not _SAFE_MOUNT_PATH.fullmatch(usb_mount) or not _SAFE_USB_DEVICE.fullmatch(usb_device):
        return None, False, f"unexpected device/mount path: {usb_device!r} {usb_mount!r}"
    try:
        out = device.run_script(
            build_mount_script(usb_device, usb_mount, os_names),
            marker="usb_mount", exec_timeout=60,
        )
    except Exception as e:
        return None, False, str(e)
    for line in out.splitlines():
        m = re.search(r"USB_MOUNTED:(/dev/[a-z]+[0-9]*):(IMAGE|NOIMAGE)", line)
        if m:
            part, kind = m.group(1), m.group(2)
            if kind == "IMAGE":
                verbose(f"  ✓ Mounted {part} (contains a matching image)")
            else:
                verbose(f"  ⚠ No partition holds a matching image — mounted {part} anyway", "warning")
            return part, kind == "IMAGE", out
    return None, False, out


def check_usb_size(device, usb_mount: str) -> None:
    """
    Warn (non-fatal) if the mounted USB stick total capacity is below
    USB_MIN_GB.  A small stick may still work for a single OS image.
    """
    if not _SAFE_MOUNT_PATH.fullmatch(usb_mount):
        verbose(f"  ⚠ Unexpected mount path — skipping size check: {usb_mount!r}", "warning")
        return
    try:
        out = device.run_script(
            f'df -k "{usb_mount}" | awk \'NR==2 {{print $2}}\'',
            marker="usb_size_check", exec_timeout=5,
        ).strip()
        kb = next((int(l) for l in out.splitlines() if l.strip().isdigit()), None)
        if kb is None:
            verbose("  ⚠ Could not parse USB size from df output", "warning")
            return
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
    format_tag is one of: 'img', 'img.xz', 'img.gz', 'img.bz2'
    """
    patterns = _PATTERNS.get(os_name)
    if not patterns:
        verbose(f"  ⚠ No USB image patterns defined for '{os_name}'", "warning")
        return None, None

    if not _SAFE_MOUNT_PATH.fullmatch(usb_mount):
        verbose(f"  ⚠ Unexpected mount path: {usb_mount!r}", "warning")
        return None, None

    # Build a POSIX sh case statement. Each matching clause prints
    # "TAG:FILEPATH" and sets found=1 to stop the for loop.
    clauses = "".join(
        f'    {pfx}*{sfx}) printf "%s\\n" "{tag}:$f"; found=1; break ;;\n'
        for pfx, sfx, tag in patterns
    )
    script = (
        "found=0\n"
        f'for f in "{usb_mount}"/*; do\n'
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

    valid_tags = {"img", "img.xz", "img.gz", "img.bz2"}
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
