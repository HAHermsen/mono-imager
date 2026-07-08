#!/usr/bin/env python3
"""
mono-imager: Unit tests for serial permission handling (issue #15).

No hardware required. serial.Serial is mocked.

What this tests:
  - _is_permission_error(): classify permission-denied vs other open errors
  - wait_for_port(): fail FAST on permission-denied instead of retrying the
    full timeout and reporting a misleading "did not appear"; loop-then-
    timeout on a genuinely absent port; succeed when the port opens.

Run: python tests/unit/test_serial_device.py
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import serial
from mono_imager.serial_device import SerialDevice, _is_permission_error

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


# ============================================================================
# _is_permission_error()
# ============================================================================

print("=" * 60)
print("_is_permission_error()")
print("=" * 60)

check("PermissionError -> True", _is_permission_error(PermissionError("nope")))

_e_msg = serial.SerialException(
    "could not open port /dev/ttyUSB0: [Errno 13] Permission denied"
)
check("SerialException with 'Permission denied' message -> True",
      _is_permission_error(_e_msg))

_e_errno = serial.SerialException("boom")
_e_errno.errno = 13
check("SerialException with errno 13 -> True", _is_permission_error(_e_errno))

check("SerialException 'No such file' -> False",
      not _is_permission_error(serial.SerialException("[Errno 2] No such file or directory")))

check("unrelated exception -> False", not _is_permission_error(ValueError("x")))


# ============================================================================
# wait_for_port(): permission-denied fails fast (#15)
# ============================================================================

print()
print("=" * 60)
print("wait_for_port() permission handling (#15)")
print("=" * 60)

# Permission denied must NOT be retried for the whole timeout — it returns
# immediately so the real cause is surfaced instead of "did not appear".
dev = SerialDevice("/dev/ttyTEST")
with patch("serial.Serial", side_effect=serial.SerialException("[Errno 13] Permission denied")), \
     patch("mono_imager.serial_device.verbose"):
    t0 = time.time()
    _ok = dev.wait_for_port(timeout=30)
    _elapsed = time.time() - t0
check("permission-denied -> returns False", _ok is False)
check("permission-denied -> returns fast (no 30s retry loop)", _elapsed < 1.0)

# A genuinely absent port keeps polling until the (short) timeout, then False.
dev = SerialDevice("/dev/ttyTEST")
with patch("serial.Serial", side_effect=serial.SerialException("[Errno 2] No such file or directory")), \
     patch("mono_imager.serial_device.verbose"):
    t0 = time.time()
    _ok = dev.wait_for_port(timeout=0.2)
    _elapsed = time.time() - t0
check("absent port -> False only after the timeout elapses", _ok is False and _elapsed >= 0.2)

# Port that opens cleanly -> True.
dev = SerialDevice("/dev/ttyTEST")
with patch("serial.Serial", return_value=MagicMock()), \
     patch("mono_imager.serial_device.verbose"):
    check("port opens -> returns True", dev.wait_for_port(timeout=5) is True)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
