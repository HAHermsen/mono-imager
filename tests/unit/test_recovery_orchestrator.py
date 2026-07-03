#!/usr/bin/env python3
"""
mono-imager: Unit tests for recovery_orchestrator.py pure-logic functions.

No hardware required. All device interactions are mocked.

What this tests:
  - detect_modern_firmware_tool()  — modern vs legacy path detection
  - get_device_mac()               — MAC parsing from ip addr output
  - run_firmware_update()          — success/failure/callback detection
  - _stream_command()              — auto-confirm and output streaming
  - check_internet_reachable()     — gateway + host ping logic
  - legacy_flash_emmc/nor()        — dd record detection, auth failure
  - verify_boot_source()           — NOR/eMMC boot marker detection

Run: python tests/unit/test_recovery_orchestrator.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

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


# ============================================================================
# detect_modern_firmware_tool()
# ============================================================================

print("=" * 60)
print("detect_modern_firmware_tool()")
print("=" * 60)

d = MagicMock()
d.run_script.side_effect = ["/usr/sbin/firmware\nRC=0", "root=/dev/mmcblk0 boot_medium=EMMC"]
check("modern device detected as True", rec.detect_modern_firmware_tool(d) is True)

d = MagicMock()
d.run_script.return_value = "RC=1"
check("legacy device detected as False", rec.detect_modern_firmware_tool(d) is False)

d = MagicMock()
d.run_script.side_effect = RuntimeError("serial broke")
check("connection failure returns None (not False)", rec.detect_modern_firmware_tool(d) is None)


# ============================================================================
# get_device_mac()
# ============================================================================

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


# ============================================================================
# Shared fake serial for streaming tests
# ============================================================================

class FakeSerial:
    """Minimal fake of pyserial Serial for read/write/reset tests."""
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.written = []

    @property
    def in_waiting(self):
        return len(self._data) - self._pos

    def read(self, n=1):
        out = self._data[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def write(self, data):
        self.written.append(data)
        return len(data)

    def reset_input_buffer(self):
        self._pos = 0


# ============================================================================
# run_firmware_update()
# ============================================================================

print()
print("=" * 60)
print("run_firmware_update()")
print("=" * 60)

d = MagicMock()
d.ser = FakeSerial(b"Downloading...\nVerifying...\nFlashing...\nDone.\n")
d.run_script.return_value = "RC=0"
check("success detected", rec.run_firmware_update(d, idle_timeout=0.3, max_total=5.0) is True)
# auto_confirm_response="yes" is always passed to _stream_command — it's sent when
# the confirm prompt appears. With no prompt in the output, "yes\r\n" is written
# but the device never sees it trigger (harmless). We only check it was NOT triggered
# as a confirmation, not that no bytes were written.
sent = b"".join(d.ser.written)
check("firmware update command was sent to device", b"firmware update --preserve-env\r\n" in sent)

d = MagicMock()
d.ser = FakeSerial(b"curl: connection refused\n")
d.run_script.return_value = "RC=1"
check("failure detected", rec.run_firmware_update(d, idle_timeout=0.3, max_total=5.0) is False)

collected = []
d = MagicMock()
d.ser = FakeSerial(b"some live progress\n")
d.run_script.return_value = "RC=0"
rec.run_firmware_update(d, on_output=lambda chunk: collected.append(chunk),
                         idle_timeout=0.3, max_total=5.0)
check("on_output callback received the live chunks", "".join(collected) == "some live progress\n")


# ============================================================================
# _stream_command()
# ============================================================================

print()
print("=" * 60)
print("_stream_command()")
print("=" * 60)

d = MagicMock()
d.ser = FakeSerial(b"Type 'yes' to proceed: \nok\n")
out = rec._stream_command(d, "dummy", idle_timeout=0.3, max_total=5.0,
                           auto_confirm_response="yes")
check("returns full streamed text", out == "Type 'yes' to proceed: \nok\n")
# auto_confirm is piped: "echo yes | dummy\r\n" — confirm via written bytes
sent = b"".join(d.ser.written)
check("auto-confirm piped into command", b"echo yes | dummy\r\n" in sent)


# ============================================================================
# check_internet_reachable()
# ============================================================================

print()
print("=" * 60)
print("check_internet_reachable()")
print("=" * 60)

d = MagicMock()
d.run_script.return_value = "RC=0"
check("gateway + host both reachable -> True",
      rec.check_internet_reachable(d, gateway="192.168.1.1", host="firmware.mono.si") is True)
check("pinged gateway then host (2 calls)", d.run_script.call_count == 2)
check("gateway pinged first", "192.168.1.1" in d.run_script.call_args_list[0][0][0])
check("host pinged second", "firmware.mono.si" in d.run_script.call_args_list[1][0][0])

d = MagicMock()
d.run_script.return_value = "RC=1"
check("gateway unreachable -> False (host never pinged)",
      rec.check_internet_reachable(d, gateway="192.168.1.1") is False and d.run_script.call_count == 1)

d = MagicMock()
d.run_script.side_effect = ["RC=0", "RC=1"]
check("gateway OK but host unreachable -> False",
      rec.check_internet_reachable(d, gateway="192.168.1.1", host="firmware.mono.si") is False)

d = MagicMock()
d.run_script.return_value = "RC=0"
check("no gateway given — only pings host",
      rec.check_internet_reachable(d, host="firmware.mono.si") is True)
check("only 1 call made (no gateway check)", d.run_script.call_count == 1)

d = MagicMock()
d.run_script.side_effect = RuntimeError("serial broke")
check("run_script raising -> False (no crash)",
      rec.check_internet_reachable(d, gateway="192.168.1.1") is False)


# ============================================================================
# try_dhcp()
# ============================================================================

print()
print("=" * 60)
print("try_dhcp()")
print("=" * 60)

d = MagicMock()
d.run_script.return_value = (
    "udhcpc: lease obtained\n"
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
    "    inet 10.0.0.69/24 scope global eth0\n"
    "---ROUTE---\n"
    "default via 10.0.0.1 dev eth0\n"
    "---DNS---\n"
    "nameserver 10.0.0.1\n"
    "nameserver 8.8.8.8\n"
)
lease = rec.try_dhcp(d)
check("lease IP parsed",      lease is not None and lease["ip"] == "10.0.0.69")
check("lease prefix parsed",  lease is not None and lease["prefix"] == "24")
check("lease gateway parsed", lease is not None and lease["gateway"] == "10.0.0.1")
check("first DNS parsed",     lease is not None and lease["dns"] == "10.0.0.1")

d = MagicMock()
d.run_script.return_value = "udhcpc: sending discover\nudhcpc: no lease, forking to background\n"
check("no lease -> None", rec.try_dhcp(d) is None)

d = MagicMock()
d.run_script.return_value = (
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
    "    inet 10.0.0.69/24 scope global eth0\n"
    "---ROUTE---\n"
    "---DNS---\n"
)
check("address but no default route -> None", rec.try_dhcp(d) is None)

d = MagicMock()
d.run_script.return_value = (
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
    "    inet 10.0.0.69/24 scope global eth0\n"
    "---ROUTE---\n"
    "default via 10.0.0.1 dev eth0\n"
    "---DNS---\n"
)
check("no resolv.conf entries -> dns is empty string (not an error)",
      rec.try_dhcp(d) == {"ip": "10.0.0.69", "prefix": "24", "gateway": "10.0.0.1", "dns": "", "iface": "eth0"})

d = MagicMock()
d.run_script.side_effect = RuntimeError("serial broke")
check("run_script raising -> None (no crash)", rec.try_dhcp(d) is None)


# ============================================================================
# legacy_flash_emmc() / legacy_flash_nor()
# ============================================================================

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


# ============================================================================
# verify_boot_source()
# ============================================================================

print()
print("=" * 60)
print("verify_boot_source()  (real marker text from hardware captures)")
print("=" * 60)

d = MagicMock()
d.ser = FakeSerial(b"U-Boot 2022.04\r\nINFO: some boot stuff\r\nINFO: RCW BOOT SRC is SD/EMMC\r\nmore\r\n")
check("eMMC boot source confirmed", rec.verify_boot_source(d, "EMMC", timeout=5) is True)

d = MagicMock()
d.ser = FakeSerial(b"U-Boot 2022.04\r\nINFO: RCW BOOT SRC is QSPI\r\n")
check("NOR boot source confirmed (QSPI marker)", rec.verify_boot_source(d, "NOR", timeout=5) is True)

d = MagicMock()
d.ser = FakeSerial(b"")
check("timeout correctly returns False", rec.verify_boot_source(d, "EMMC", timeout=1) is False)

try:
    rec.verify_boot_source(MagicMock(), "GARBAGE")
    check("invalid expected value raises ValueError", False)
except ValueError:
    check("invalid expected value raises ValueError", True)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
