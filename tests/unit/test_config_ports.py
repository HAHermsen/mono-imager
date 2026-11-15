"""
mono-imager: config.py — the parts test_config.py leaves out: where the
config file lives, and serial-port detection (known USB-UART chips first,
plus the two failure modes turned into readable RuntimeErrors).

pyserial's port list is patched, so no serial hardware is needed.

Run: python tests/unit/test_config_ports.py
"""

import builtins
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import serial.tools.list_ports

from mono_imager import config

CONFIG_PARTS = (".config", "mono-imager", "config.json")
FTDI = SimpleNamespace(device="COM5", description="USB Serial Port (FTDI FT232R)")
CP210X = SimpleNamespace(device="COM7", description="Silicon Labs CP210x USB to UART Bridge")
COM_PORT = SimpleNamespace(device="COM1", description="Communications Port (COM1)")
BLUETOOTH = SimpleNamespace(device="COM3", description="Standard Serial over Bluetooth link")
NO_DESCRIPTION = SimpleNamespace(device="COM9", description=None)
PYSERIAL_MODULES = ("serial", "serial.tools", "serial.tools.list_ports")

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


def section(title):
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def detect(ports=None, error=None):
    """detect_serial_ports() with a fake port list; returns (result, raised)."""
    side = error if error else None
    with patch.object(serial.tools.list_ports, "comports", return_value=ports or [], side_effect=side):
        try:
            return config.detect_serial_ports(), None
        except RuntimeError as e:
            return None, e


# ----------------------------------------------------------------------------
section("get_config_path")
p = config.get_config_path()
check("config lives in ~/.config/mono-imager/config.json",
      p == Path.home().joinpath(*CONFIG_PARTS) and p.parts[-len(CONFIG_PARTS):] == CONFIG_PARTS)


# ----------------------------------------------------------------------------
section("detect_serial_ports")
(known, other), err = detect([COM_PORT, FTDI, NO_DESCRIPTION, CP210X])
check("no error", err is None)
check("known USB-UART chips listed first, in order", known == [FTDI, CP210X])
check("everything else listed separately, incl. ports without a description", other == [COM_PORT, NO_DESCRIPTION])

(known, other), _ = detect([BLUETOOTH])
check("current behaviour: a Bluetooth 'Standard Serial' port matches the 'serial' keyword",
      known == [BLUETOOTH] and other == [])

(known, other), _ = detect([])
check("no ports -> two empty lists", known == [] and other == [])

_, err = detect(error=PermissionError("Access is denied"))
check("permission denied -> RuntimeError with the fix (dialout / port in use)",
      isinstance(err, RuntimeError) and "dialout" in str(err) and "Access is denied" in str(err)
      and isinstance(err.__cause__, PermissionError))

real_import = builtins.__import__


def no_pyserial(name, *args, **kwargs):
    if name in PYSERIAL_MODULES:
        raise ImportError(f"No module named {name!r}")
    return real_import(name, *args, **kwargs)


with patch.object(builtins, "__import__", no_pyserial):
    try:
        config.detect_serial_ports()
        err = None
    except RuntimeError as e:
        err = e
check("pyserial missing -> RuntimeError telling how to install it",
      isinstance(err, RuntimeError) and "pip install pyserial" in str(err))


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
