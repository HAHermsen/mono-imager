#!/usr/bin/env python3
"""
mono-imager: Unit tests for the firmware update fallback ladder (#24).

No hardware required. _stream_command is replaced by a scripted fake so
each `firmware update ...` invocation returns a chosen output; run_script
is dispatched by its marker.

What this tests:
  run_firmware_update():
    - NTP sync runs first and its result + device time reach the console
    - env flag: eMMC default none, NOR default --preserve-env, explicit
      preserve_env overrides both ways
    - older tool (help without --preserve-env) never gets the flag
    - TLS error -> NTP re-sync + exactly one retry
    - non-TLS error -> no retry, straight to manual download
    - manual download: curl -k with mono:$mac auth, .bin AND .bin.sig,
      then `firmware update --from DIR` with the same env flag
    - tool without --from -> no manual tier; failed download -> no --from
  stash_uboot_env(): QSPI/eMMC scripts EXECUTED under /bin/sh against a
    fake sysfs/device (skips mtdNro, copies exactly 0x2000 bytes)
  _run_medium_update() (menu flows):
    - env prompt defaults: eMMC No, NOR Yes
    - modern failure + user declines -> legacy never runs
    - modern failure + user accepts + preserve -> stash, legacy, restore
    - stash fails + user declines -> legacy never runs
    - legacy-only device -> legacy runs with the env choice

Run: python tests/unit/test_firmware_update_ladder.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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


OK_OUT   = "║  Target: emmc ║\n:: Firmware flashed ✓\n:: Firmware update complete. Reboot to use the new firmware.\n"
TLS_OUT  = ("curl: (60) SSL certificate problem: certificate is not yet valid\n"
            "ERROR: Failed to download firmware image\n")
OTHER_OUT = "curl: (22) The requested URL returned error: 404\nERROR: Failed to download firmware image\n"
HELP_NEW = "Usage: firmware <command> [options]\n  --from PATH  ...\n  --preserve-env  Back up ...\n"
HELP_OLD = "Usage: firmware <command> [options]\n  update   Verify and flash firmware\n"


class Fake:
    """Scripted device + stream. stream_outputs: outputs for successive
    `firmware update` calls. Records every command in order."""

    def __init__(self, boot="qspi", help_text=HELP_NEW, stream_outputs=(OK_OUT,),
                 ntp_ok=True, download_ok=True):
        self.boot = boot
        self.help_text = help_text
        self.stream_outputs = list(stream_outputs)
        self.ntp_ok = ntp_ok
        self.download_ok = download_ok
        self.events = []          # ("ntp"|"stream"|"download"|..., detail)
        self.d = MagicMock()
        self.d.run_script.side_effect = self.run_script

    def run_script(self, script, marker="", **kw):
        if marker == "sync_device_clock":
            self.events.append(("ntp", script))
            return ("RC=0\n" if self.ntp_ok else "ntpd: bad address\nRC=1\n") + \
                "DEVICE_TIME=2026-09-26 12:00:00 UTC\n"
        if marker == "detect_boot_source":
            return f"boot_medium={self.boot}\n"
        if marker == "fw_tool_caps":
            return self.help_text
        if marker == "manual_fw_download":
            self.events.append(("download", script))
            return "DL_RC=0\n" if self.download_ok else "curl: (22) 401\nDL_RC=22\n"
        if marker == "firmware_update_rc":
            return "RC=1\n"
        return ""

    def stream(self, d, command, **kw):
        self.events.append(("stream", command))
        return self.stream_outputs.pop(0) if self.stream_outputs else OTHER_OUT

    def streams(self):
        return [c for k, c in self.events if k == "stream"]

    def kinds(self):
        return [k for k, _ in self.events]


def run(fake, **kw):
    console = []
    with patch.object(rec, "_stream_command", side_effect=fake.stream), \
         patch.object(rec.console_logger, "info", side_effect=lambda m, *a: console.append(str(m))):
        ok = rec.run_firmware_update(fake.d, idle_timeout=0.1, max_total=1.0, **kw)
    return ok, "\n".join(console)


print("=" * 60)
print("run_firmware_update(): tier 1 + env flag")
print("=" * 60)

f = Fake(boot="qspi")
ok, out = run(f)
check("success", ok is True)
check("NTP runs before the first firmware update", f.kinds()[:2] == ["ntp", "stream"])
check("NTP result + device time on console", "Clock synced — device time 2026-09-26 12:00:00 UTC" in out)
check("eMMC target default: no --preserve-env", f.streams() == ["firmware update"])

f = Fake(boot="emmc")
run(f)
check("NOR target default: --preserve-env", f.streams() == ["firmware update --preserve-env"])

f = Fake(boot="qspi")
run(f, preserve_env=True)
check("eMMC + preserve_env=True: flag passed", f.streams() == ["firmware update --preserve-env"])

f = Fake(boot="emmc")
run(f, preserve_env=False)
check("NOR + preserve_env=False: no flag", f.streams() == ["firmware update"])

f = Fake(boot="emmc", ntp_ok=False)
ok, out = run(f)
check("NTP failure is shown, update still attempted", "NTP sync failed" in out and ok is True)

f = Fake(boot="emmc", help_text=HELP_OLD)
ok, out = run(f)
check("older tool: --preserve-env never sent", f.streams() == ["firmware update"])
check("older tool: user told env is preserved anyway", "always preserves" in out)

f = Fake(boot="emmc", help_text="garbage / no usage text")
run(f)
check("unreadable help: keep assuming modern tool", f.streams() == ["firmware update --preserve-env"])

print()
print("=" * 60)
print("run_firmware_update(): TLS retry + manual download")
print("=" * 60)

f = Fake(boot="emmc", stream_outputs=[TLS_OUT, OK_OUT])
ok, out = run(f)
check("TLS error -> retry succeeds", ok is True)
check("TLS error -> NTP re-synced before retry", f.kinds() == ["ntp", "stream", "ntp", "stream"])
check("TLS retry keeps the env flag", f.streams() == ["firmware update --preserve-env"] * 2)
check("WARN shown for TLS error", "WARN: TLS/certificate error" in out)

f = Fake(boot="emmc", stream_outputs=[TLS_OUT, TLS_OUT, OK_OUT])
ok, out = run(f)
dl = [c for k, c in f.events if k == "download"]
check("2x TLS -> manual download -> success", ok is True and len(dl) == 1)
check("download uses curl -k with mono:$mac auth", dl and 'curl -kfsS -u "mono:$mac"' in dl[0])
check("download fetches the qspi .bin", dl and "https://firmware.mono.si/firmware-qspi-gateway-dk.bin " in dl[0])
check("download fetches the .bin.sig", dl and "https://firmware.mono.si/firmware-qspi-gateway-dk.bin.sig" in dl[0])
check("MAC detected like the official tool", dl and "ip -o link show | grep -m1 'ether'" in dl[0])
check("then firmware update --from DIR, same env flag",
      f.streams()[-1] == f"firmware update --from {rec.MANUAL_FW_DIR} --preserve-env")

f = Fake(boot="qspi", stream_outputs=[OTHER_OUT, OK_OUT])
ok, _ = run(f)
check("non-TLS error: no retry, no second NTP", f.kinds() == ["ntp", "stream", "download", "stream"] and ok)
check("eMMC target downloads the emmc .bin",
      "firmware-emmc-gateway-dk.bin" in [c for k, c in f.events if k == "download"][0])

f = Fake(boot="emmc", help_text="Usage: firmware\n  --preserve-env\n", stream_outputs=[OTHER_OUT])
ok, out = run(f)
check("tool without --from: no manual tier, fails", ok is False and "download" not in f.kinds())

f = Fake(boot="emmc", stream_outputs=[OTHER_OUT], download_ok=False)
ok, _ = run(f)
check("failed download: no --from run, fails", ok is False and len(f.streams()) == 1)

print()
print("=" * 60)
print("stash_uboot_env(): scripts executed under /bin/sh")
print("=" * 60)


def run_stash(target):
    tmp = Path(tempfile.mkdtemp())
    try:
        sysfs = tmp / "mtd"
        dev = tmp / "dev"
        dev.mkdir()
        for n, label in (("mtd0", "rcw-bl2"), ("mtd0ro", "rcw-bl2"),
                         ("mtd3", "uboot-env"), ("mtd3ro", "uboot-env")):
            (sysfs / n).mkdir(parents=True)
            (sysfs / n / "name").write_text(label + "\n")
        env_bytes = os.urandom(0x10000)
        (dev / "mtd3").write_bytes(env_bytes)
        emmc = os.urandom(0x300000 + 0x4000)
        (dev / "mmcblk0").write_bytes(emmc)
        stash = tmp / "stash.bin"
        captured = {}

        def fake_run_script(script, marker="", **kw):
            s = (script.replace("/sys/class/mtd", str(sysfs))
                       .replace(rec.UBOOT_ENV_STASH, str(stash))
                       .replace('e="/dev/', f'e="{dev}/')
                       .replace("if=/dev/mmcblk0", f"if={dev}/mmcblk0"))
            r = subprocess.run(["/bin/sh", "-c", s], capture_output=True, text=True, timeout=20)
            captured["out"] = r.stdout
            # map the fake path back to what the device would print
            return r.stdout.replace(f"{dev}/", "/dev/")

        d = MagicMock()
        d.run_script.side_effect = fake_run_script
        res = rec.stash_uboot_env(d, target)
        data = stash.read_bytes() if stash.exists() else b""
        expected = env_bytes[:0x2000] if target == "qspi" else emmc[0x300000:0x302000]
        return res, data == expected
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if not Path("/bin/sh").exists():
    print("SKIP: /bin/sh not available")
else:
    res, same = run_stash("qspi")
    check("QSPI: finds uboot-env as /dev/mtd3 (not mtd3ro)", res == "/dev/mtd3")
    check("QSPI: stash = first 0x2000 bytes of the env partition", same)
    res, same = run_stash("emmc")
    check("eMMC: env device /dev/mmcblk0", res == "/dev/mmcblk0")
    check("eMMC: stash = 0x2000 bytes at offset 0x300000", same)

d = MagicMock()
d.run_script.return_value = "STASH_FAIL\n"
check("stash failure -> None", rec.stash_uboot_env(d, "qspi") is None)
check("restore refuses a non-mtd device for qspi",
      rec.restore_uboot_env_region(MagicMock(), "qspi", "/dev/sda; reboot") is False)

print()
print("=" * 60)
print("_run_medium_update(): prompts and legacy consent")
print("=" * 60)


def run_flow(medium, answers, is_modern=True, modern_ok=False, stash="/dev/mtd3", legacy_ok=True):
    """answers: successive input() replies. Returns dict of what happened."""
    seen = {"modern": [], "legacy": [], "stash": 0, "prompts": []}
    answers = list(answers)

    def fake_input(prompt=""):
        seen["prompts"].append(prompt)
        return answers.pop(0) if answers else ""

    def fake_modern(d, on_output=None, preserve_env=None):
        seen["modern"].append(preserve_env)
        return modern_ok

    def fake_legacy(d, env_dev=None):
        seen["legacy"].append(env_dev)
        return legacy_ok

    def fake_stash(d, target):
        seen["stash"] += 1
        return stash

    phase = "phase_modern_flash_emmc" if medium == "emmc" else "phase_modern_flash_nor"
    lphase = "phase_legacy_flash_emmc" if medium == "emmc" else "phase_legacy_flash_nor"
    with patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=MagicMock()), \
         patch.object(rec, "detect_modern_firmware_tool", return_value=is_modern), \
         patch.object(rec, phase, side_effect=fake_modern), \
         patch.object(rec, lphase, side_effect=fake_legacy), \
         patch.object(rec, "stash_uboot_env", side_effect=fake_stash), \
         patch.object(rec, "print_report", return_value=True), \
         patch.object(rec.console_logger, "info"), \
         patch("builtins.input", side_effect=fake_input), \
         patch("builtins.print"):
        rec._run_medium_update(medium, "COM5", setup_network=lambda d: True)
    return seen


s = run_flow("emmc", [""], modern_ok=True)
check("eMMC: empty answer -> preserve=False", s["modern"] == [False])
s = run_flow("nor", [""], modern_ok=True)
check("NOR: empty answer -> preserve=True", s["modern"] == [True])
check("modern success: no legacy prompt", len(s["prompts"]) == 1 and s["legacy"] == [])

s = run_flow("nor", ["", ""], modern_ok=False)
check("modern fails + default answer (No) -> legacy never runs", s["legacy"] == [] and s["stash"] == 0)

s = run_flow("nor", ["", "y"], modern_ok=False)
check("modern fails + yes + preserve -> env stashed", s["stash"] == 1)
check("... and legacy runs with the stash device", s["legacy"] == ["/dev/mtd3"])

s = run_flow("emmc", ["", "y"], modern_ok=False)
check("preserve=No -> no stash, legacy runs without restore", s["stash"] == 0 and s["legacy"] == [None])

s = run_flow("nor", ["", "y", ""], modern_ok=False, stash=None)
check("stash fails + default (No) -> legacy never runs", s["legacy"] == [])
s = run_flow("nor", ["", "y", "y"], modern_ok=False, stash=None)
check("stash fails + explicit yes -> legacy runs, env lost", s["legacy"] == [None])

s = run_flow("nor", [""], is_modern=False)
check("legacy-only device: env prompt, stash, legacy", s["stash"] == 1 and s["legacy"] == ["/dev/mtd3"]
      and s["modern"] == [])

print()
print("=" * 60)
print("_legacy_flash_with_env(): restore after flash")
print("=" * 60)
with patch.object(rec, "restore_uboot_env_region", return_value=True) as r, \
     patch.object(rec.console_logger, "info"):
    ok = rec._legacy_flash_with_env(MagicMock(), "qspi", lambda: True, "/dev/mtd3")
check("flash OK + env_dev -> restore called", ok and r.called and r.call_args.args[1:] == ("qspi", "/dev/mtd3"))
with patch.object(rec, "restore_uboot_env_region") as r:
    rec._legacy_flash_with_env(MagicMock(), "qspi", lambda: False, "/dev/mtd3")
check("flash failed -> no restore attempted", not r.called)

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
