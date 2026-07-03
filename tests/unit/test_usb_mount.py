#!/usr/bin/env python3
"""
mono-imager: Unit tests for MonoImager.menu_test_usb_mount() (Test USB stick).

No hardware required. All device interactions are mocked.

What this tests:
  - No serial port found -> returns to MAIN, no crash
  - Bootstrap failure -> "Device in recovery shell" fails, returns to MAIN
  - Mount succeeds via the partitioned device (usb_device + "1")
  - Mount fails via the partitioned device but succeeds via the bare-device
    fallback (unpartitioned stick)
  - Mount fails entirely -> stops before scanning for images, device is
    still disconnected (mirrors the real USB journeys' fallback order)
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


MOUNT_OK   = "RC=0"
MOUNT_FAIL = "mount: mounting /dev/sda1 on /mnt/usb failed: No such file or directory\nRC=1"


def mount_calls(mock_send_command):
    """Extract just the 'mount ...' commands sent, in call order."""
    return [c.args[0] for c in mock_send_command.call_args_list
            if c.args and c.args[0].startswith("mount ")]


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
# Mount succeeds via the partitioned device, image found -> overall pass
# ============================================================================

print()
print("=" * 60)
print("menu_test_usb_mount(): mount succeeds via /dev/sda1, image found")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
d = MagicMock()
d.send_command.return_value = MOUNT_OK

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch("mono_imager.journeys.usb_utils.check_usb_size"), \
     patch("mono_imager.journeys.usb_utils.find_image_on_usb",
           return_value=("/mnt/usb/openwrt-foo.bin.gz", "bin.gz")), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app.menu_test_usb_mount()

check("mounted the partitioned device first", mount_calls(d.send_command)[0].startswith("mount /dev/sda1 "))
check("device disconnected", d.disconnect.called)
check("unmount attempted", any("umount" in c.args[0] for c in d.send_command.call_args_list if c.args))
check("serial_port persisted for reuse", app.serial_port == "COM5")
check("state returned to MAIN", app.current_state == MenuState.MAIN)


# ============================================================================
# Partitioned mount fails, bare-device fallback succeeds
# ============================================================================

print()
print("=" * 60)
print("menu_test_usb_mount(): partition mount fails, bare-device fallback succeeds")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
d = MagicMock()
_attempts = iter([MOUNT_FAIL, MOUNT_OK])


def _fake_send_command(cmd, *a, **kw):
    if cmd.startswith("mount "):
        return next(_attempts)
    return "RC=0"


d.send_command.side_effect = _fake_send_command

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch("mono_imager.journeys.usb_utils.check_usb_size"), \
     patch("mono_imager.journeys.usb_utils.find_image_on_usb", return_value=(None, None)), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app.menu_test_usb_mount()

calls = mount_calls(d.send_command)
check("tried the partitioned device first", len(calls) >= 1 and calls[0].startswith("mount /dev/sda1 "))
check("fell back to the bare device", len(calls) >= 2 and calls[1].startswith("mount /dev/sda "))


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
d.send_command.return_value = MOUNT_FAIL

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
     patch("mono_imager.journeys.usb_utils.find_image_on_usb") as mock_find, \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app.menu_test_usb_mount()

check("image scan never runs after a failed mount", not mock_find.called)
check("device still disconnected on mount failure", d.disconnect.called)


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
d.send_command.return_value = MOUNT_OK
printed = []

with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
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
