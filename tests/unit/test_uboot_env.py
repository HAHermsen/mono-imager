#!/usr/bin/env python3
"""
mono-imager: Unit tests for U-Boot env capture/restore and the
already-at-prompt probe (issues #8, #12).

No hardware required. All device interactions are mocked.

What this tests:
  - parse_uboot_env()        — printenv text -> dict, "Environment size"
                                summary line excluded, malformed lines skipped
  - capture_uboot_env()      — wraps printenv + parse, None on failure
  - restore_uboot_env()      — replays setenv per captured var, 0 on empty backup
  - SerialDevice.probe_uboot_prompt() — "=>" detection without requiring
                                the "Hit any key to stop autoboot" wait

Run: python tests/unit/test_uboot_env.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import flash_orchestrator as core
from mono_imager.serial_device import SerialDevice

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
# parse_uboot_env()
# ============================================================================

print("=" * 60)
print("parse_uboot_env()")
print("=" * 60)

output = (
    "baudrate=115200\n"
    "bootcmd=run distro_bootcmd\n"
    "bootdelay=2\n"
    "ethaddr=e8:f6:d7:00:19:9c\n"
    "\n"
    "Environment size: 1234/65532 bytes\n"
)
env = core.parse_uboot_env(output)
check("baudrate parsed", env.get("baudrate") == "115200")
check("bootcmd parsed", env.get("bootcmd") == "run distro_bootcmd")
check("ethaddr parsed (value itself contains no '=')", env.get("ethaddr") == "e8:f6:d7:00:19:9c")
check("'Environment size' summary line excluded", "Environment size" not in env)
check("blank line produces no entry", "" not in env)
check("exactly 4 real vars parsed", len(env) == 4)

check("empty input -> empty dict", core.parse_uboot_env("") == {})
check("no '=' anywhere -> empty dict", core.parse_uboot_env("garbage\nmore garbage\n") == {})

# A value that itself contains '=' (e.g. a U-Boot command string) must
# still parse correctly — only the FIRST '=' should split key from value.
env2 = core.parse_uboot_env("opnsense=mmc dev 0; setenv foo=bar; booti 0x82000000\n")
check("value containing '=' preserved whole via partition() on first '='",
      env2.get("opnsense") == "mmc dev 0; setenv foo=bar; booti 0x82000000")


# ============================================================================
# capture_uboot_env()
# ============================================================================

print()
print("=" * 60)
print("capture_uboot_env()")
print("=" * 60)

d = MagicMock()
d.send_command.return_value = "baudrate=115200\nbootcmd=run distro_bootcmd\n"
result = core.capture_uboot_env(d)
check("captures parsed dict on success", result == {"baudrate": "115200", "bootcmd": "run distro_bootcmd"})
check("sent 'printenv' to the device", d.send_command.call_args[0][0] == "printenv")

d = MagicMock()
d.send_command.return_value = ""
check("empty printenv output -> None", core.capture_uboot_env(d) is None)

d = MagicMock()
d.send_command.side_effect = RuntimeError("serial broke")
check("send_command raising -> None (no crash)", core.capture_uboot_env(d) is None)


# ============================================================================
# restore_uboot_env()
# ============================================================================

print()
print("=" * 60)
print("restore_uboot_env()")
print("=" * 60)

d = MagicMock()
backup = {"bootcmd": "run distro_bootcmd", "baudrate": "115200"}
restored = core.restore_uboot_env(d, backup)
check("restores every captured var", restored == 2)
sent_cmds = [c.args[0] for c in d.send_command.call_args_list]
check("setenv sent for bootcmd", any(cmd.startswith("setenv bootcmd ") for cmd in sent_cmds))
check("setenv sent for baudrate", any(cmd.startswith("setenv baudrate ") for cmd in sent_cmds))
check("caller must call saveenv separately (not sent here)",
      not any("saveenv" in cmd for cmd in sent_cmds))

check("None backup -> 0, no calls made", core.restore_uboot_env(MagicMock(), None) == 0)
check("empty dict backup -> 0, no calls made", core.restore_uboot_env(MagicMock(), {}) == 0)

d = MagicMock()
d.send_command.side_effect = RuntimeError("serial broke")
check("individual setenv failure doesn't raise or stop the loop",
      core.restore_uboot_env(d, {"a": "1", "b": "2"}) == 0)


# ============================================================================
# SerialDevice.probe_uboot_prompt()
# ============================================================================

print()
print("=" * 60)
print("SerialDevice.probe_uboot_prompt()")
print("=" * 60)


class FakeSerial:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.is_open = True

    def reset_input_buffer(self):
        pass

    def write(self, data):
        return len(data)

    def read(self, n=1):
        out = self._data[self._pos:self._pos + n]
        self._pos += len(out)
        return out


d = SerialDevice("COM99")
d.ser = FakeSerial(b"\r\n=> ")
check("U-Boot prompt detected -> True", d.probe_uboot_prompt(timeout=1.0) is True)

d = SerialDevice("COM99")
d.ser = FakeSerial(b"")
check("no response -> False (falls back to normal power-cycle wait)",
      d.probe_uboot_prompt(timeout=0.3) is False)

d = SerialDevice("COM99")
d.ser = FakeSerial(b"root@recovery:~# ")
check("recovery shell prompt (past U-Boot) is NOT a false positive",
      d.probe_uboot_prompt(timeout=0.3) is False)

d = SerialDevice("COM99")
d.ser = None
check("no open connection -> False, no crash", d.probe_uboot_prompt(timeout=0.3) is False)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
