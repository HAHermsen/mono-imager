#!/usr/bin/env python3
"""
mono-imager: sanity-check recovery_orchestrator.py's pure-logic
functions against realistic mocked data — NO hardware required.

This is the cmd.exe-friendly way to re-run the same checks done
during development: just a normal .py file, no inline quoting needed.

Usage:
    py test_sanity_recovery_orchestrator.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager import recovery_orchestrator as rec

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


print("=" * 60)
print("detect_modern_firmware_tool()")
print("=" * 60)

d = MagicMock()
d.run_script.return_value = "/usr/sbin/firmware\nRC=0"
check("modern device detected as True", rec.detect_modern_firmware_tool(d) is True)

d = MagicMock()
d.run_script.return_value = "RC=1"
check("legacy device detected as False", rec.detect_modern_firmware_tool(d) is False)

d = MagicMock()
d.run_script.side_effect = RuntimeError("serial broke")
check("connection failure returns None (not False)", rec.detect_modern_firmware_tool(d) is None)


print()
print("=" * 60)
print("get_device_mac()")
print("=" * 60)

d = MagicMock()
d.run_script.return_value = (
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
    "    link/ether e8:f6:d7:00:19:9c brd ff:ff:ff:ff:ff:ff\n"
    "    inet 10.0.0.69/24 scope global eth0"
)
mac = rec.get_device_mac(d, "eth0")
check(f"MAC parsed correctly (got {mac})", mac == "e8:f6:d7:00:19:9c")

d = MagicMock()
d.run_script.return_value = "no useful output here"
check("no-match returns None", rec.get_device_mac(d) is None)


print()
print("=" * 60)
print("run_firmware_update()")
print("=" * 60)

d = MagicMock()
d.run_script.return_value = "Downloading...\nVerifying...\nFlashing...\nDone.\nRC=0"
check("success detected", rec.run_firmware_update(d) is True)

d = MagicMock()
d.run_script.return_value = "curl: connection refused\nRC=1"
check("failure detected", rec.run_firmware_update(d) is False)


print()
print("=" * 60)
print("legacy_flash_emmc() / legacy_flash_nor()")
print("=" * 60)

d = MagicMock()
d.run_script.return_value = "7811+0 records in\n7811+0 records out\nRC=0"
check("legacy eMMC success detected", rec.legacy_flash_emmc(d, "e8:f6:d7:00:19:9c") is True)

d = MagicMock()
d.run_script.return_value = "curl: (22) The requested URL returned error: 401\nRC=1"
check("legacy eMMC auth failure detected", rec.legacy_flash_emmc(d, "badmac") is False)

d = MagicMock()
d.run_script.return_value = "Erasing blocks: 100%\nWriting data: 100%\nRC=0"
check("legacy NOR success detected", rec.legacy_flash_nor(d, "e8:f6:d7:00:19:9c") is True)


print()
print("=" * 60)
print("verify_boot_source()  (real marker text from confirmed hardware capture)")
print("=" * 60)

class FakeSerial:
    def __init__(self, data: bytes):
        self._iter = iter(data)
    def read(self, n=1):
        try:
            return bytes([next(self._iter)])
        except StopIteration:
            return b""

d = MagicMock()
d.ser = FakeSerial(b"INFO: some boot stuff\r\nINFO: RCW BOOT SRC is SD/EMMC\r\nmore\r\n")
check("eMMC boot source confirmed", rec.verify_boot_source(d, "EMMC", timeout=5) is True)

d = MagicMock()
d.ser = FakeSerial(b"INFO: RCW BOOT SRC is QSPI\r\n")
check("NOR boot source confirmed (QSPI marker)", rec.verify_boot_source(d, "NOR", timeout=5) is True)

d = MagicMock()
d.ser = FakeSerial(b"")
check("timeout correctly returns False", rec.verify_boot_source(d, "EMMC", timeout=1) is False)

try:
    rec.verify_boot_source(MagicMock(), "GARBAGE")
    check("invalid expected value raises ValueError", False)
except ValueError:
    check("invalid expected value raises ValueError", True)


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
