"""
mono-imager: recovery_orchestrator.py — the device helpers' error and
edge branches that test_recovery_orchestrator.py leaves out: firmware-tool
detection, MAC, internet check, output streaming, clock sync, tool caps,
the firmware command and manual download, the boot-source check, U-Boot
env stash/restore and the legacy curl flashes.

The device is a fake whose run_script() answers per marker (or raises),
with a scripted serial line for the streaming and boot-source readers.
Log and console lines are captured, so the user-facing messages are checked.

Run: python tests/unit/test_recovery_tools.py
"""

import contextlib
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import recovery_orchestrator as R

MAC = "e8:f6:d7:00:17:df"
BAD_MAC = "not-a-mac"
MTD_ENV = "/dev/mtd3"
DONE = "Firmware update complete"
QUICK_IDLE_S = 0.5                 # streaming: silence that ends the read in these tests
LONG_IDLE_S = 5.0                  # streaming: long enough that only the done marker can end it
BOOT_WAIT_S = 0.2                  # verify_boot_source timeout when nothing useful arrives
ERR = RuntimeError("serial timeout")

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


class Logs(logging.Handler):
    """Collects every mono_imager log/console line while active."""
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def has(self, text):
        return any(text in line for line in self.lines)


@contextlib.contextmanager
def captured():
    h, root = Logs(), logging.getLogger("mono_imager")
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        yield h
    finally:
        root.removeHandler(h)
        root.setLevel(old)


def device(**answers):
    """run_script(marker=m) -> answers[m] (an Exception is raised); unknown markers raise."""
    d = MagicMock()

    def run_script(script, marker=None, **kw):
        a = answers.get(marker, ERR)
        if isinstance(a, Exception):
            raise a
        return a(script) if callable(a) else a
    d.run_script.side_effect = run_script
    return d


class Line:
    """Serial line: in_waiting/read() serve `data`, then silence; read errors on demand."""
    def __init__(self, data=b"", error=None):
        self.data, self.error, self.written = bytearray(data), error, []

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, n):
        if not self.data and self.error:
            raise self.error
        out, self.data = bytes(self.data[:n]), self.data[n:]
        return out

    def reset_input_buffer(self):
        pass

    def write(self, b):
        self.written.append(b)


# ----------------------------------------------------------------------------
section("detect_modern_firmware_tool")
check("no RC= in the reply -> None (couldn't tell)",
      R.detect_modern_firmware_tool(device(detect_fw_tool="garbage")) is None)
with captured() as log:
    res = R.detect_modern_firmware_tool(device(detect_fw_tool="/usr/bin/firmware\nRC=0", check_cmdline=ERR))
check("cmdline unreadable -> treated as no boot_medium -> False", res is False)
check("reason shown on the console (old U-Boot)", log.has("boot_medium= absent"))
res = R.detect_modern_firmware_tool(device(detect_fw_tool="/usr/bin/firmware\nRC=0",
                                           check_cmdline="console=ttyS0 boot_medium=qspi"))
check("tool + boot_medium= -> True", res is True)


# ----------------------------------------------------------------------------
section("get_device_mac / check_internet_reachable / sync_device_clock / firmware_tool_caps")
check("MAC: run_script fails -> None", R.get_device_mac(device()) is None)
with captured() as log:
    res = R.check_internet_reachable(device(check_wan_ping="RC=0", check_dns=ERR))
check("DNS check cannot run -> False, explained", res is False and log.has("Could not run the DNS check"))
with captured() as log:
    res = R.sync_device_clock(device(sync_device_clock=ERR))
check("NTP cannot run -> False, continues with current clock",
      res is False and log.has("NTP sync could not run"))
check("tool caps: help unreadable -> assume modern tool", R.firmware_tool_caps(device()) ==
      {"preserve_env": True, "from": True})


# ----------------------------------------------------------------------------
section("_stream_command")
d = MagicMock()
d.ser = Line(b"Downloading...")
with captured() as log:
    out = R._stream_command(d, "firmware update", idle_timeout=LONG_IDLE_S, max_total=-1)
check("hard ceiling reached -> stops, logged", out == "" and log.has("hit hard ceiling"))

d.ser = Line(f"step 1\n{DONE}\nroot@recovery:~# ".encode())
seen: list[str] = []
out = R._stream_command(d, "firmware update", idle_timeout=LONG_IDLE_S, auto_confirm_response="yes",
                        on_output=seen.append, done_markers=[DONE])
check("done marker -> returns early with the output", DONE in out and "".join(seen) == out)
check("auto-confirm pipes the answer in", d.ser.written[0] == b"echo yes | firmware update\r\n")

d.ser = Line(b"")
out = R._stream_command(d, "true", idle_timeout=QUICK_IDLE_S)
check("silence -> idle timeout ends it, nothing read", out == "")


# ----------------------------------------------------------------------------
section("_run_firmware_cmd / _manual_download / run_firmware_update")
for what, stream, rc, want, logged in (
        ("Aborted printed", "Type 'yes' to proceed\nAborted.", "RC=0", False, "printed 'Aborted.'"),
        ("ERROR: printed", "ERROR: Cannot detect boot medium", "RC=0", False, "printed 'ERROR:'"),
        ("exit code unreadable", "flashing...", ERR, False, "did not report RC=0"),
        ("completed", DONE, ERR, True, "")):
    with patch.object(R, "_stream_command", return_value=stream), captured() as log:
        ok, out = R._run_firmware_cmd(device(firmware_update_rc=rc), "firmware update", None, 1, 1)
    check(f"{what} -> success {want}", ok is want and out == stream and (not logged or log.has(logged)))
check("manual download: run_script fails -> False", R._manual_download(device(), "emmc") is False)

with patch.object(R, "sync_device_clock"), \
     patch.object(R, "firmware_tool_caps", return_value={"preserve_env": False, "from": True}), \
     patch.object(R, "_run_firmware_cmd", return_value=(True, DONE)) as cmd, captured() as log:
    ok = R.run_firmware_update(device(detect_boot_source=ERR), preserve_env=False)
check("boot source unreadable -> assumes eMMC target, still flashes", ok is True and log.has("could not detect boot source"))
check("old tool + 'reset env' asked -> told it cannot wipe, no flag", log.has("cannot wipe the U-Boot env")
      and cmd.call_args.args[1] == "firmware update")


# ----------------------------------------------------------------------------
section("stash_uboot_env / restore_uboot_env_region")
check("stash: run_script fails -> None", R.stash_uboot_env(device(), "emmc") is None)
scripts: list[str] = []


def restore_reply(reply):
    return lambda script: (scripts.append(script), reply)[1]


guarded = device(restore_uboot_env=restore_reply("RESTORE_OK"))
check("restore NOR: env device must be an MTD -> False, nothing written",
      R.restore_uboot_env_region(guarded, "qspi", "/dev/sda1") is False and not guarded.run_script.called)
ok = R.restore_uboot_env_region(device(restore_uboot_env=restore_reply("RESTORE_OK")), "qspi", MTD_ENV)
check("restore NOR: flashcp to that MTD -> True", ok is True and f"flashcp {R.UBOOT_ENV_STASH} {MTD_ENV}" in scripts[-1])
with captured() as log:
    ok = R.restore_uboot_env_region(device(restore_uboot_env=restore_reply("RESTORE_FAIL")), "emmc", "/dev/mmcblk0")
check("restore eMMC: dd at the env offset; failure -> False, logged",
      ok is False and f"seek={R.UBOOT_ENV_EMMC_OFFSET}" in scripts[-1] and log.has("restore_uboot_env_region: failed"))
check("restore: run_script fails -> False", R.restore_uboot_env_region(device(), "emmc", "/dev/mmcblk0") is False)


# ----------------------------------------------------------------------------
section("verify_boot_source")
d = MagicMock()
d.send_command.side_effect = OSError("device went away")
d.ser = Line(b"rcK: umount...\nU-Boot 2024.04\nRCW BOOT SRC is SD/EMMC\n")
check("reboot command errors (expected) -> still listens, eMMC confirmed", R.verify_boot_source(d, "EMMC") is True)
d.ser = Line(b"U-Boot 2024.04\nRCW BOOT SRC is SD/EMMC\n", error=OSError("read failed"))
with captured() as log:
    res = R.verify_boot_source(d, "NOR", timeout=BOOT_WAIT_S)
check("U-Boot seen, other source, then a read error -> False, said so",
      res is False and log.has("U-Boot detected but did not see"))
d.ser = Line(b"rcK: stopping services\n")
with captured() as log:
    res = R.verify_boot_source(d, "EMMC", timeout=BOOT_WAIT_S)
check("no U-Boot output at all -> False, said so", res is False and log.has("Did not detect U-Boot output"))
try:
    R.verify_boot_source(d, "USB")
    check("unknown boot source -> ValueError", False)
except ValueError:
    check("unknown boot source -> ValueError", True)


# ----------------------------------------------------------------------------
section("legacy_flash_emmc / legacy_flash_nor")
check("eMMC: run_script fails -> False", R.legacy_flash_emmc(device(), MAC) is False)
with captured() as log:
    res = R.legacy_flash_emmc(device(legacy_emmc="curl: (22) 401\nRC=22"), MAC)
check("eMMC: no RC=0 / dd records -> False, logged", res is False and log.has("legacy eMMC flash did not confirm"))
check("eMMC: RC=0 but dd reported nothing -> False",
      R.legacy_flash_emmc(device(legacy_emmc="curl done\nRC=0"), MAC) is False)
check("eMMC: dd success -> True", R.legacy_flash_emmc(device(legacy_emmc="64+0 records out\nRC=0"), MAC) is True)
check("NOR: invalid MAC -> False, nothing run", R.legacy_flash_nor(device(), BAD_MAC) is False)
check("NOR: run_script fails -> False", R.legacy_flash_nor(device(), MAC) is False)
with captured() as log:
    res = R.legacy_flash_nor(device(legacy_nor="flashcp: error\nRC=1"), MAC)
check("NOR: no RC=0 -> False, logged", res is False and log.has("legacy NOR flash did not confirm"))
check("NOR: RC=0 -> True", R.legacy_flash_nor(device(legacy_nor="Writing data: 100%\nRC=0"), MAC) is True)


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
