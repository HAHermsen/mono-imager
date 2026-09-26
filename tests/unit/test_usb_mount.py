#!/usr/bin/env python3
"""
mono-imager: Unit tests for MonoImager.menu_test_usb_mount() (Test USB stick).

No hardware required. All device interactions are mocked.

What this tests:
  - No serial port found -> returns to MAIN, no crash
  - Bootstrap failure -> "Device in recovery shell" fails, returns to MAIN
  - Mount goes through usb_utils.mount_usb_stick() (partition scan, #26)
    for all three OSes, and the mounted partition is shown in the check
  - GUID stick: sda2 reported, not the EFI partition sda1
  - Mount fails entirely -> stops before scanning for images, device is
    still disconnected (mirrors the real USB journeys)
  (the scan's shell logic itself: test_usb_partition_scan.py)
  - Image scan: at least one recognizable OS image -> overall pass
  - Image scan: no recognizable OS image -> overall fail (mount itself
    still succeeded — these are reported as separate checks)
  - Unmount is always attempted once mounted, even when nothing is found
  - self.serial_port is persisted after a successful run

Run: python tests/unit/test_usb_mount.py
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.tui import MonoImager, MenuState

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


def make_app():
    app = MonoImager()
    app.clear_screen = lambda: None
    app.print_header = lambda: None
    return app


MOUNT_SDA1 = ("/dev/sda1", True, "USB_MOUNTED:/dev/sda1:IMAGE")
MOUNT_SDA2 = ("/dev/sda2", True, "USB_MOUNTED:/dev/sda2:IMAGE")
MOUNT_FAIL = (None, False, "USB_MOUNT_FAILED")


# ============================================================================
# No serial port found
# ============================================================================

print("=" * 60)
print("menu_test_usb_mount(): no serial port found")
print("=" * 60)

app = make_app()
with patch.object(app, "_select_port", return_value=None), \
     patch("builtins.print"):
    app.menu_test_usb_mount()

check("returns to MAIN when no port found", app.current_state == MenuState.MAIN)


# ============================================================================
# Bootstrap failure
# ============================================================================

print()
print("=" * 60)
print("menu_test_usb_mount(): bootstrap fails")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=None), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app.menu_test_usb_mount()

check("returns to MAIN when bootstrap fails", app.current_state == MenuState.MAIN)



# ============================================================================
# Mount succeeds, image found -> overall pass
# ============================================================================

print()
print("=" * 60)
print("menu_test_usb_mount(): mount succeeds via /dev/sda1, image found")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
d = MagicMock()
d.send_command.return_value = "RC=0"

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch("mono_imager.journeys.usb_utils.mount_usb_stick", return_value=MOUNT_SDA1) as mock_mount, \
     patch("mono_imager.journeys.usb_utils.check_usb_size"), \
     patch("mono_imager.journeys.usb_utils.find_image_on_usb",
           return_value=("/mnt/usb/openwrt-foo.bin.gz", "bin.gz")), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app.menu_test_usb_mount()

check("mount goes through mount_usb_stick()", mock_mount.called)
check("scan covers all three OSes",
      mock_mount.called and sorted(mock_mount.call_args.args[3]) == ["Armbian", "OPNsense", "OpenWRT"])
check("device disconnected", d.disconnect.called)
check("unmount attempted", any("umount" in c.args[0] for c in d.send_command.call_args_list if c.args))
check("serial_port persisted for reuse", app.serial_port == "COM5")
check("state returned to MAIN", app.current_state == MenuState.MAIN)


# ============================================================================
# GUID stick: the data partition (sda2) is reported, not EFI (sda1) (#26)
# ============================================================================

print()
print("=" * 60)
print("menu_test_usb_mount(): GUID stick -> /dev/sda2 reported")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
d = MagicMock()
d.send_command.return_value = "RC=0"
printed = []

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch("mono_imager.journeys.usb_utils.mount_usb_stick", return_value=MOUNT_SDA2), \
     patch("mono_imager.journeys.usb_utils.check_usb_size"), \
     patch("mono_imager.journeys.usb_utils.find_image_on_usb",
           return_value=("/mnt/usb/OPNsense-x.img.bz2", "img.bz2")), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))), \
     patch("mono_imager.console.check", wraps=__import__("mono_imager.console", fromlist=["check"]).check) as mock_check:
    app.menu_test_usb_mount()

labels = [c.args[1] for c in mock_check.call_args_list]
check("mount check names /dev/sda2", any("/dev/sda2 -> /mnt/usb" in l for l in labels))


# ============================================================================
# Mount fails entirely -> stops before scanning, still disconnects
# ============================================================================

print()
print("=" * 60)
print("menu_test_usb_mount(): mount fails entirely")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
d = MagicMock()

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch("mono_imager.journeys.usb_utils.mount_usb_stick", return_value=MOUNT_FAIL), \
     patch("mono_imager.journeys.usb_utils.find_image_on_usb") as mock_find, \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app.menu_test_usb_mount()

check("image scan never runs after a failed mount", not mock_find.called)
check("device still disconnected on mount failure", d.disconnect.called)


# ============================================================================
# Mount raises -> treated as failed mount, still disconnects
# ============================================================================

print()
print("=" * 60)
print("menu_test_usb_mount(): mount raises")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
d = MagicMock()

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch("mono_imager.journeys.usb_utils.mount_usb_stick", side_effect=RuntimeError("serial gone")), \
     patch("mono_imager.journeys.usb_utils.find_image_on_usb") as mock_find, \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app.menu_test_usb_mount()

check("no image scan after mount exception", not mock_find.called)
check("device still disconnected after mount exception", d.disconnect.called)


# ============================================================================
# Mount succeeds, no recognizable image found -> overall fail
# ============================================================================

print()
print("=" * 60)
print("menu_test_usb_mount(): mount succeeds, no image found -> overall fail")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
d = MagicMock()
d.send_command.return_value = "RC=0"
printed = []

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch("mono_imager.journeys.usb_utils.mount_usb_stick",
           return_value=("/dev/sda1", False, "USB_MOUNTED:/dev/sda1:NOIMAGE")), \
     patch("mono_imager.journeys.usb_utils.check_usb_size"), \
     patch("mono_imager.journeys.usb_utils.find_image_on_usb", return_value=(None, None)), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
    app.menu_test_usb_mount()

summary = "\n".join(printed)
check("summary reports a failed check", "failed" in summary)
check("still disconnects even though nothing was found", d.disconnect.called)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
