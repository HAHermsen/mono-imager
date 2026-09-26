#!/usr/bin/env python3
"""
mono-imager: Unit tests for the USB partition scan (#26).

No hardware required. The generated mount script is EXECUTED for real
under /bin/sh (POSIX), with `mount`/`umount` replaced by stub scripts on
PATH and /proc/partitions replaced by a fake file, so the shell logic
itself is exercised — not just the Python around it. Skipped (not
failed) on hosts without /bin/sh (e.g. plain Windows).

What this tests:
  - GUID stick (macOS default): sda1 = EFI (no image), sda2 = data -> sda2
  - MBR stick: image on sda1 -> sda1
  - Unpartitioned stick: no sdaN, bare sda holds image -> sda
  - Image on no partition: first mountable one left mounted, NOIMAGE
  - Nothing mountable -> USB_MOUNT_FAILED
  - Case-insensitive match (OPNsense-...-GATEWAY.img.bz2)
  - Other OS's image does not count as a hit
  - mount_usb_stick(): parses IMAGE / NOIMAGE / FAILED output
  - mount_usb_stick(): rejects unsafe device/mount paths without running

Run: python tests/unit/test_usb_partition_scan.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.journeys.usb_utils import build_mount_script, mount_usb_stick

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {label}")
        passed += 1
    else:
        print(f"  FAIL: {label}")
        failed += 1


MOUNT_STUB = """#!/bin/sh
# stub: "mount /dev/X <dir>" copies $FAKEDEV/X/* into <dir>; fails if X absent
src="$FAKEDEV/$(basename "$1")"
[ -d "$src" ] || exit 32
cp -R "$src"/. "$2"/ && touch "$2/.mounted_$(basename "$1")"
"""
UMOUNT_STUB = """#!/bin/sh
[ -d "$1" ] && rm -rf "$1"/* "$1"/.mounted_* 2>/dev/null
exit 0
"""


def run_scan(partitions, os_names=("OPNsense",)):
    """
    partitions: {"sda1": ["file", ...] or None (exists but not mountable)}
    Every key is listed in the fake /proc/partitions; only keys with a
    file list (possibly empty) are mountable. Returns (stdout, mountdir files).
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "bin").mkdir()
        (tmp / "dev").mkdir()
        (tmp / "mnt").mkdir()
        for name, body in (("mount", MOUNT_STUB), ("umount", UMOUNT_STUB)):
            p = tmp / "bin" / name
            p.write_text(body)
            p.chmod(0o755)
        lines = ["major minor  #blocks  name", "", "   8        0   62500000 sda"]
        for i, (part, files) in enumerate(partitions.items(), start=1):
            if part != "sda":
                lines.append(f"   8        {i}   1000 {part}")
            if files is not None:
                d = tmp / "dev" / part
                d.mkdir()
                for f in files:
                    (d / f).write_text("x")
        (tmp / "partitions").write_text("\n".join(lines) + "\n")

        script = build_mount_script("/dev/sda", str(tmp / "mnt"), list(os_names))
        script = script.replace("/proc/partitions", str(tmp / "partitions"))
        env = dict(os.environ, PATH=f"{tmp/'bin'}:{os.environ['PATH']}", FAKEDEV=str(tmp / "dev"))
        out = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, env=env, timeout=20)
        mounted = sorted(os.listdir(tmp / "mnt"))
        return out.stdout.strip(), mounted
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if not Path("/bin/sh").exists():
    print("SKIP: /bin/sh not available — shell-level tests skipped")
else:
    print("=" * 60)
    print("build_mount_script(): executed under /bin/sh")
    print("=" * 60)

    out, mnt = run_scan({"sda1": ["EFI"], "sda2": ["OPNsense-26.1.5-arm-aarch64-GATEWAY.img.bz2"]})
    check("GUID: picks sda2, not the EFI partition", out == "USB_MOUNTED:/dev/sda2:IMAGE")
    check("GUID: sda2 left mounted", ".mounted_sda2" in mnt and ".mounted_sda1" not in mnt)

    out, mnt = run_scan({"sda1": ["opnsense-foo.img"]})
    check("MBR: picks sda1", out == "USB_MOUNTED:/dev/sda1:IMAGE")

    out, mnt = run_scan({"sda": ["OPNsense-x.img.bz2"]})
    check("unpartitioned: falls back to bare sda", out == "USB_MOUNTED:/dev/sda:IMAGE")

    out, mnt = run_scan({"sda1": ["EFI"], "sda2": ["notes.txt"]})
    check("no image anywhere: first mountable left mounted", out == "USB_MOUNTED:/dev/sda1:NOIMAGE")
    check("no image anywhere: sda1 is what's mounted", ".mounted_sda1" in mnt and ".mounted_sda2" not in mnt)

    out, mnt = run_scan({"sda1": None, "sda2": None})
    check("nothing mountable -> USB_MOUNT_FAILED", out == "USB_MOUNT_FAILED")

    out, mnt = run_scan({"sda1": None, "sda2": ["OPNsense-x.img.bz2"]})
    check("unmountable sda1 is skipped", out == "USB_MOUNTED:/dev/sda2:IMAGE")

    out, mnt = run_scan({"sda1": ["openwrt-x.img.gz"], "sda2": ["OPNsense-x.img.bz2"]}, ("OPNsense",))
    check("other OS's image is not a hit", out == "USB_MOUNTED:/dev/sda2:IMAGE")

    out, mnt = run_scan({"sda1": ["EFI"], "sda2": ["Armbian_26.2.5_Gateway-dk.img.xz"]},
                        ("OPNsense", "OpenWRT", "Armbian"))
    check("multi-OS scan (diagnostics) finds Armbian on sda2", out == "USB_MOUNTED:/dev/sda2:IMAGE")

print()
print("=" * 60)
print("mount_usb_stick(): output parsing")
print("=" * 60)

d = MagicMock()
d.run_script.return_value = "sh /tmp/x.sh\r\nUSB_MOUNTED:/dev/sda2:IMAGE\r\nroot@recovery:~#"
with patch("mono_imager.journeys.usb_utils.verbose"):
    part, has, _ = mount_usb_stick(d, "/dev/sda", "/mnt/usb", ["OPNsense"])
check("IMAGE -> (/dev/sda2, True)", (part, has) == ("/dev/sda2", True))

d.run_script.return_value = "USB_MOUNTED:/dev/sda1:NOIMAGE\n"
with patch("mono_imager.journeys.usb_utils.verbose"):
    part, has, _ = mount_usb_stick(d, "/dev/sda", "/mnt/usb", ["OPNsense"])
check("NOIMAGE -> (/dev/sda1, False)", (part, has) == ("/dev/sda1", False))

d.run_script.return_value = "USB_MOUNT_FAILED\n"
part, has, _ = mount_usb_stick(d, "/dev/sda", "/mnt/usb", ["OPNsense"])
check("FAILED -> (None, False)", (part, has) == (None, False))

d.run_script.side_effect = RuntimeError("serial timeout")
part, has, detail = mount_usb_stick(d, "/dev/sda", "/mnt/usb", ["OPNsense"])
check("run_script error -> (None, False, message)", part is None and "serial timeout" in detail)

d = MagicMock()
part, has, _ = mount_usb_stick(d, "/dev/sda; rm -rf /", "/mnt/usb", ["OPNsense"])
check("unsafe device path rejected", part is None and not d.run_script.called)
part, has, _ = mount_usb_stick(d, "/dev/sda", "/mnt/usb $(reboot)", ["OPNsense"])
check("unsafe mount path rejected", part is None and not d.run_script.called)

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
