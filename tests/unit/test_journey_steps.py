#!/usr/bin/env python3
"""
mono-imager: every journey STEP function, called directly — success and
every failure branch.

No hardware required. The device is a MagicMock (or, for OpenWRT's U-Boot
'recovery' repair, a small U-Boot simulator with an env, a NOR flash map
and backup env blocks). Network report-backs (wait_for_report), the HTTP
server, USB helpers and `firmware update` are patched per test.

Complements test_official_journeys_smoke.py (whole happy-path runs) and
test_journey_resolution.py (step ordering).

What this tests:
  - OPNsense LAN/USB: U-Boot env + eMMC erase, DIP confirmation, flash
    (bzip2 | dd) report parsing, MAC detection, firmware re-image, reboot
  - OpenWRT LAN: U-Boot 'recovery' check / repair — valid kernel FIT,
    DTB-sized FIT, gzip kernel with/without kernel_comp_addr_r, backup
    env restore, NOR scan (FIT, DTB+FIT, DTB+gzip, gzip, uImage, nothing,
    sf probe failure); two-pass dd flash parsing; firmware update
  - OpenWRT/Armbian/OPNsense USB: mount, image detection, flash per
    format, unmount
  - Armbian LAN/USB: flash (raw, xz), live serial relay while waiting,
    the NOR round-trip's failure exits
  - shared LAN steps (_common), image/container checks, journey
    discovery, usb_utils size check and image search (the generated
    search script really runs under /bin/sh)

Run: python tests/unit/test_journey_steps.py
"""

import io
import os
import subprocess
import shutil as _sh_which

# a real POSIX sh: /bin/sh, or sh.exe from Git for Windows; None -> that section is skipped
SH = "/bin/sh" if os.path.exists("/bin/sh") else _sh_which.which("sh")
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import constants as C
from mono_imager import flash_orchestrator as FO
from mono_imager.step_registry import StepContext
from mono_imager import journeys as J
from mono_imager.journeys import _common, usb_utils as U
from mono_imager.journeys import opnsense_lan as OL, opnsense_usb as OU
from mono_imager.journeys import openwrt_lan as WL, openwrt_usb as WU
from mono_imager.journeys import armbian_lan as AL, armbian_usb as AU

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


def ctx(**kw):
    c = StepContext(device=kw.pop("device", MagicMock()), host_ip="192.168.168.84", http_port=8080,
                    firmware_path=Path(kw.pop("firmware_path", "/tmp/fw.img")), **kw)
    return c


def last(n=1):
    return FO.results[-n:]


def reasons():
    return " | ".join(r[3] for r in FO.results if not r[2])


DD_OK = "4096+0 records in\n4096+0 records out\n"
TWO_DD = "8+0 records in\n8+0 records out\n100+0 records in\n100+0 records out\n"


# ============================================================================
section("OPNsense: U-Boot env, then eMMC erase")
d = MagicMock()
d.send_command.return_value = "OK"
FO.reset_results()
check("env set + erase -> True", OL._uboot_steps_opnsense_lan(d) is True)
cmds = [c.args[0] for c in d.send_command.call_args_list]
check("bootcmd = 'run opnsense || run recovery'", 'setenv bootcmd "run opnsense || run recovery"' in cmds)
check("saveenv BEFORE the eMMC erase (MMC env backend)",
      cmds.index("saveenv") < next(i for i, c in enumerate(cmds) if c.startswith("mmc erase")))
check("erases the whole eMMC", f"mmc erase 0 {C.EMMC_ERASE_SECTORS_HEX}" in cmds)
d = MagicMock()
d.send_command.side_effect = RuntimeError("serial gone")
check("setenv fails -> False", OL._uboot_steps_opnsense_lan(d) is False)
d = MagicMock()
d.send_command.side_effect = lambda c, **k: "MMC erase: error -5" if c.startswith("mmc erase") else "OK"
check("erase reports an error -> False", OL._uboot_steps_opnsense_lan(d) is False)
d = MagicMock()


def erase_raises(c, **k):
    if c.startswith("mmc erase"):
        raise TimeoutError("erase hung")
    return "OK"


d.send_command.side_effect = erase_raises
check("erase raises -> False", OL._uboot_steps_opnsense_lan(d) is False)

# ============================================================================
section("OPNsense LAN + USB: DIP confirmation, MAC, re-image, reboot")
for mod, tag in ((OL, "LAN"), (OU, "USB")):
    with patch("builtins.input", return_value=""):
        check(f"{tag}: DIP confirmed -> True", mod.step_confirm_dip_nor(ctx()) is True)
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        check(f"{tag}: Ctrl-C at the DIP prompt -> False", mod.step_confirm_dip_nor(ctx()) is False
              and last()[0][3] == "interrupted")
    c = ctx(device_net={"iface": "eth1"})
    c.device.run_script.return_value = "e8:f6:d7:00:17:df\n"
    check(f"{tag}: MAC detected on the configured iface", mod.step_detect_mac(c) is True
          and c.device_mac == "e8:f6:d7:00:17:df" and c.get("device_mac_known") == "e8:f6:d7:00:17:df"
          and "/sys/class/net/eth1/address" in c.device.run_script.call_args.args[0])
    c = ctx()
    c.device.run_script.return_value = "unknown"
    check(f"{tag}: MAC unknown -> still True, marked unknown", mod.step_detect_mac(c) is True
          and c.get("device_mac_known") is None and "eth0" in c.device.run_script.call_args.args[0])
    c = ctx()
    c.device.run_script.side_effect = RuntimeError("x")
    check(f"{tag}: MAC read raises -> still True", mod.step_detect_mac(c) is True)
    for ret, want in ((True, True), (False, False)):
        with patch("mono_imager.recovery_orchestrator.run_firmware_update", return_value=ret):
            check(f"{tag}: firmware update {ret} -> {want}", mod.step_reimage_emmc_firmware(ctx()) is want)
    with patch("mono_imager.recovery_orchestrator.run_firmware_update", side_effect=RuntimeError("fw")):
        check(f"{tag}: firmware update raises -> False", mod.step_reimage_emmc_firmware(ctx()) is False)
    c = ctx()
    check(f"{tag}: reboot sent -> True", mod.step_reboot(c) is True
          and c.device.send_command.call_args.args[0] == "reboot")
    c = ctx()
    c.device.send_command.side_effect = RuntimeError("port reset by reboot")
    check(f"{tag}: reboot raising is only a warning -> True", mod.step_reboot(c) is True)

# ============================================================================
section("OPNsense LAN: flash (curl | bzip2 -dck | dd) report parsing")
for what, report, err, want in (("records, no error", DD_OK, None, True),
                                ("no dd records", "bzip2: (stdin) is not a bzip2 file.", None, False),
                                ("dd error", DD_OK + "dd: error writing: No space left", None, False),
                                ("no report", None, None, False),
                                ("spinner error", None, TimeoutError("t"), False)):
    c = ctx()
    c.set("firmware_source", "http://h/firmware.img")
    with patch.object(OL, "with_spinner", return_value=(report, err)):
        check(f"{what} -> {want}", OL.step_flash_opnsense(c) is want)
script = c.device.launch_script.call_args.args[0]
check("script streams curl | bzip2 -dck | dd to the flash target",
      "curl -sk http://h/firmware.img | bzip2 -dck | dd of=/dev/mmcblk0 bs=1M" in script
      and "report?step=07" in script)
c = ctx()
c.device.launch_script.side_effect = RuntimeError("wc mismatch")
check("launch fails -> False", OL.step_flash_opnsense(c) is False)

# ============================================================================
section("USB journeys: mount, image detection, unmount")
for mod, osn in ((OU, "OPNsense"), (WU, "OpenWRT"), (AU, "Armbian")):
    with patch.object(mod, "mount_usb_stick", return_value=("/dev/sda2", True, "")), \
         patch.object(mod, "check_usb_size") as cs:
        check(f"{osn}: mounted -> True, size checked", mod.step_mount_usb(ctx()) is True and cs.called)
    with patch.object(mod, "mount_usb_stick", return_value=(None, False, "no partitions\n")), \
         patch.object(mod, "check_usb_size") as cs:
        check(f"{osn}: nothing mounted -> False, no size check", mod.step_mount_usb(ctx()) is False
              and not cs.called and "no partitions" in last()[0][3])
    with patch.object(mod, "mount_usb_stick", side_effect=RuntimeError("serial")):
        check(f"{osn}: mount raises -> False", mod.step_mount_usb(ctx()) is False)
    c = ctx()
    with patch.object(mod, "find_image_on_usb", return_value=("/mnt/usb/x.img", "img")):
        check(f"{osn}: image found -> source + format stored", mod.step_firmware_on_usb(c) is True
              and c.get("firmware_source") == "/mnt/usb/x.img" and c.get("firmware_format") == "img")
    with patch.object(mod, "find_image_on_usb", return_value=(None, None)):
        check(f"{osn}: no image -> False", mod.step_firmware_on_usb(ctx()) is False)
    c = ctx()
    check(f"{osn}: unmount -> True", mod.step_unmount_usb(c) is True
          and c.device.send_command.call_args.args[0].startswith("umount /mnt/usb"))
    c = ctx()
    c.device.send_command.side_effect = RuntimeError("busy")
    check(f"{osn}: unmount raising is only a warning -> True", mod.step_unmount_usb(c) is True)

# ============================================================================
section("OPNsense / Armbian USB: flash per image format")
for mod, osn, comp_fmt, comp_cmd in ((OU, "OPNsense", "img.bz2", "bzip2 -dc /mnt/usb/f |"),
                                     (AU, "Armbian", "img.xz", "xz -dc /mnt/usb/f |")):
    for fmt, needle in ((comp_fmt, comp_cmd), ("img", "dd if=/mnt/usb/f of=/dev/mmcblk0 bs=1M")):
        c = ctx()
        c.set("firmware_source", "/mnt/usb/f")
        c.set("firmware_format", fmt)
        c.device.run_script.return_value = DD_OK
        check(f"{osn} {fmt}: flashed -> True", mod.step_flash_opnsense(c) is True if mod is OU
              else mod.step_flash_armbian(c) is True)
        check(f"{osn} {fmt}: script uses '{needle.split()[0]}'", needle in c.device.run_script.call_args.args[0])
    fl = OU.step_flash_opnsense if mod is OU else AU.step_flash_armbian
    for what, out, want in (("dd error", DD_OK + "error", False), ("no records", "", False)):
        c = ctx()
        c.set("firmware_source", "/mnt/usb/f")
        c.device.run_script.return_value = out
        check(f"{osn}: {what} -> {want}", fl(c) is want)
    c = ctx()
    c.device.run_script.side_effect = RuntimeError("size mismatch")
    check(f"{osn}: run_script raises -> False", fl(c) is False)

# ============================================================================
section("OpenWRT USB: flash (two dd passes from a plain file)")
c = ctx()
c.set("firmware_source", "/mnt/usb/o.bin")
c.set("firmware_format", "img.xz")
check("unsupported format -> False, nothing run", WU.step_flash_openwrt(c) is False
      and not c.device.run_script.called and "unsupported format" in last()[0][3])
for fmt in ("img", "img.gz"):
    c = ctx()
    c.set("firmware_source", "/mnt/usb/o.img" + (".gz" if fmt == "img.gz" else ""))
    c.set("firmware_format", fmt)
    c.device.run_script.return_value = TWO_DD
    check(f"{fmt}: both passes -> True", WU.step_flash_openwrt(c) is True)
    s = c.device.run_script.call_args.args[0]
    if fmt == "img.gz":
        check("img.gz: gunzipped to a staged file, dd reads it, staged file removed",
              f'gunzip -c "/mnt/usb/o.img.gz" > {WU._STAGED_IMG}' in s and f'dd if="{WU._STAGED_IMG}"' in s
              and f"rm -f {WU._STAGED_IMG}; cat" in s)
    else:
        check("img: dd reads the stick directly, no staging", 'dd if="/mnt/usb/o.img"' in s and "gunzip" not in s)
    check(f"{fmt}: partition-table pass then bulk pass",
          s.index(WU.PT_DD_ARGS) < s.index(WU.BULK_DD_ARGS))
for what, out, err, want in (("one pass only", "8+0 records out\n", None, False),
                             ("dd error", TWO_DD + "dd: failed", None, False),
                             ("empty partition-table pass", "0+0 records out\n100+0 records out\n", None, False),
                             ("run_script raises", None, RuntimeError("x"), False)):
    c = ctx()
    c.set("firmware_source", "/mnt/usb/o.img")
    with patch.object(WU, "with_spinner", return_value=(out, err)):
        check(f"{what} -> False", WU.step_flash_openwrt(c) is want)

# ============================================================================
section("OpenWRT LAN: flash (download, then two dd passes) + firmware update")
for fw, gz in (("/tmp/openwrt-emmc.img.gz", True), ("/tmp/openwrt-emmc.img", False)):
    c = ctx(firmware_path=fw)
    c.set("firmware_source", "http://h/firmware.img")
    with patch.object(WL, "with_spinner", return_value=(TWO_DD, None)):
        check(f"{'gz' if gz else 'raw'}: both passes -> True", WL.step_flash_openwrt(c) is True)
    s = c.device.launch_script.call_args.args[0]
    check(f"{'gz' if gz else 'raw'}: downloaded to a local file first{', then gunzip' if gz else ''}",
          ("gunzip -f /tmp/mono_imager_openwrt.img.gz" in s) is gz and "curl -sk -o /tmp/mono_imager_openwrt.img" in s
          and "| dd" not in s)
for what, out, err in (("one pass only", "8+0 records out\n", None),
                       ("no space", TWO_DD + "No space left on device", None),
                       ("partition table empty", "0+0 records out\n100+0 records out\n", None),
                       ("no report", None, None), ("spinner error", None, TimeoutError("t"))):
    c = ctx()
    with patch.object(WL, "with_spinner", return_value=(out, err)):
        check(f"{what} -> False", WL.step_flash_openwrt(c) is False)
c = ctx()
c.device.launch_script.side_effect = RuntimeError("x")
check("launch fails -> False", WL.step_flash_openwrt(c) is False)
for resp, err, want in (("ok\nRC=0", None, True), ("Cannot detect boot medium\nRC=1", None, False),
                        (None, TimeoutError("fw hung"), False)):
    with patch.object(WL, "with_spinner", return_value=(resp, err)):
        check(f"firmware update {resp or err!r} -> {want}", WL.step_firmware_update(ctx()) is want)
check("_md_be32 parses U-Boot md.b output", WL._md_be32("82000004: 00 5a 1c 3e    .Z.>") == 0x005a1c3e)
for bad in ("garbage", "82000004: zz"):
    try:
        WL._md_be32(bad)
        check(f"_md_be32({bad!r}) raises", False)
    except (ValueError, IndexError):
        check(f"_md_be32({bad!r}) raises", True)
with patch.object(_common, "start_http_server", return_value=MagicMock()):
    check("LAN wrapper: HTTP server step delegates to _common", WL.step_http_server_start(ctx()) is True)
with patch.object(_common, "with_spinner", return_value=("200", None)):
    check("LAN wrapper: reachability step delegates to _common", WL.step_firmware_reachable(ctx()) is True)


# ============================================================================
section("OpenWRT: U-Boot 'recovery' check / repair (U-Boot simulator)")
MB = C.MIB


class UBoot:
    """U-Boot over serial: env, NOR images by offset, backup env blocks."""

    def __init__(self, env=None, nor=None, backups=None, fail=()):
        self.env, self.nor, self.backups = dict(env or {}), dict(nor or {}), dict(backups or {})
        self.fail, self.cmds, self.loaded = tuple(fail), [], None

    def __call__(self, cmd, timeout=None, **kw):
        self.cmds.append(cmd)
        for f in self.fail:
            if f in cmd:
                raise RuntimeError(f"boom: {f}")
        if cmd.startswith("printenv "):
            k = cmd.split()[1]
            return f"{k}={self.env[k]}" if k in self.env else f'## Error: "{k}" not defined'
        if cmd.startswith("setenv "):
            p = cmd.split(None, 2)
            if len(p) == 2:
                self.env.pop(p[1], None)
            else:
                self.env[p[1]] = p[2]
            return ""
        if cmd.startswith("sf read "):
            self.loaded = int(cmd.split()[3], 16)
            return "SF: 256 bytes @ ... Read: OK"
        if cmd.startswith("env import"):
            if self.loaded in self.backups:
                self.env.update(self.backups[self.loaded])
                return ""
            return "## Error: bad CRC, import failed"
        if cmd.startswith("md.b "):
            addr = cmd.split()[1]
            img = self.nor.get(self.loaded, ("blank",))
            if addr == C.UBOOT_LOAD_ADDR:
                magic = {"fit": C.FIT_MAGIC_HEX, "gzip": C.GZIP_MAGIC_HEX + " 08 00",
                         "uimage": C.UIMAGE_MAGIC_HEX}.get(img[0], "ff ff ff ff")
                return f"{addr[2:]}: {magic}    ...."
            if addr == WL._FIT_SIZE_FIELD_ADDR:
                if img[0] == "fit" and img[1] == "garbage":
                    return "md: bad address"
                sz = img[1] if img[0] == "fit" else 0
                return f"{addr[2:]}: " + " ".join(f"{b:02x}" for b in sz.to_bytes(4, "big")) + "    ...."
        if cmd == "saveenv":
            return "Saving Environment to SPI Flash... OK"
        return ""

    def sent(self, prefix):
        return [c for c in self.cmds if c.startswith(prefix)]


def run_uboot(sim):
    d = MagicMock()
    d.send_command.side_effect = sim
    FO.reset_results()
    ok = WL._uboot_steps_openwrt_lan(d)
    return ok, (FO.results[-1][1] if FO.results else "")


def booted_for_openwrt(sim):
    return (sim.env.get("bootcmd") == "'run openwrt || run recovery'" and "openwrt" in sim.env
            and "saveenv" in sim.cmds and any(f"boot_medium={C.BOOT_MEDIUM_NOR}" in c for c in sim.cmds))


SF_REC = f"sf probe 0; sf read {C.UBOOT_LOAD_ADDR} 0xa00000 0x2000000; bootm {C.UBOOT_LOAD_ADDR}"
KC = {"kernel_comp_addr_r": "0x90000000"}
cases = [
    ("plain 'recovery' (no sf read) -> confirmed", {"recovery": "run boot_nor"}, {}, {}, (), True, "confirmed"),
    ("'sf read' with no offset -> confirmed", {"recovery": "sf read"}, {}, {}, (), True, "confirmed"),
    ("'sf read' -> kernel FIT (>= 5 MiB) -> confirmed", {"recovery": SF_REC}, {0xa00000: ("fit", 20 * MB)}, {}, (),
     True, "confirmed"),
    ("'sf read' -> FIT size unreadable -> validation skipped, confirmed", {"recovery": SF_REC},
     {0xa00000: ("fit", "garbage")}, {}, (), True, "confirmed"),
    ("'sf read' -> gzip kernel + kernel_comp_addr_r -> confirmed", dict(recovery=SF_REC, **KC),
     {0xa00000: ("gzip",)}, {}, (), True, "confirmed"),
    ("'sf read' -> unknown magic -> confirmed", {"recovery": SF_REC}, {0xa00000: ("uimage",)}, {}, (), True, "confirmed"),
    ("'sf read' validation raises -> skipped, confirmed", {"recovery": SF_REC}, {}, {}, ("sf probe",), True, "confirmed"),
    ("'sf read' -> DTB-sized FIT -> cleared, restored from backup env 0x310000",
     {"recovery": SF_REC}, {0xa00000: ("fit", 38 * 1024)}, {0x310000: {"recovery": "run nor_boot"}}, (),
     True, "restored from NOR backup (0x310000)"),
    ("'sf read' -> gzip but kernel_comp_addr_r missing -> cleared, restored from backup",
     {"recovery": SF_REC}, {0xa00000: ("gzip",)}, {0x320000: {"recovery": "x"}}, (), True, "restored from NOR backup (0x320000)"),
    ("printenv always raises -> backups can't be verified, NOR empty -> False", {}, {},
     {0x3E0000: {"recovery": "y"}}, ("printenv recovery",), False, None),
    ("missing, no backups -> NOR scan: kernel FIT alone -> bootm", {}, {0x600000: ("fit", 30 * MB)}, {}, (),
     True, "reconstructed — NOR 0x600000"),
    ("missing -> NOR scan: DTB then kernel FIT -> bootm with DTB", {},
     {0x500000: ("fit", 38 * 1024), 0x900000: ("fit", 30 * MB)}, {}, (), True, "reconstructed — NOR 0x900000"),
    ("missing -> NOR scan: DTB then gzip kernel -> booti with DTB", {},
     {0x500000: ("fit", 38 * 1024), 0xa00000: ("gzip",)}, {}, (), True, "reconstructed — NOR 0xa00000"),
    ("missing -> NOR scan: gzip kernel, no DTB -> booti", {}, {0xa00000: ("gzip",)}, {}, (), True,
     "reconstructed — NOR 0xa00000"),
    ("missing -> NOR scan: legacy uImage -> bootm", {}, {0x800000: ("uimage",)}, {}, (), True, "reconstructed — NOR 0x800000"),
    ("missing -> NOR scan finds nothing -> False", {}, {}, {}, (), False, "recovery image not found"),
]
for what, env, nor, backups, fail, want, title in cases:
    sim = UBoot(env, nor, backups, fail)
    ok, t = run_uboot(sim)
    if title is None:   # printenv broken everywhere: backup can't be verified, scan finds nothing
        check(what, ok is False)
        continue
    check(what, ok is want and title in t)
    if want:
        check("  bootcmd/openwrt set, saved, NOR bootargs", booted_for_openwrt(sim))
    if "kernel FIT alone" in what:
        check("  recovery = sf read + bootm (no DTB)", sim.env["recovery"] ==
              f'"sf probe 0;sf read {C.UBOOT_LOAD_ADDR} 0x600000 {C.NOR_KERNEL_LEN};bootm {C.UBOOT_LOAD_ADDR}"')
    if "DTB then kernel FIT" in what:
        check("  DTB paired, fdt_high set", f"sf read {C.UBOOT_DTB_ADDR} 0x500000" in sim.env["recovery"]
              and f"bootm {C.UBOOT_LOAD_ADDR} - {C.UBOOT_DTB_ADDR}" in sim.env["recovery"]
              and sim.env.get("fdt_high") == C.UBOOT_FDT_HIGH_NO_RELOC)
    if "gzip" in what and "NOR scan" in what:
        check("  booti + kernel_comp vars set", "booti" in sim.env["recovery"]
              and sim.env.get("kernel_comp_addr_r") == C.UBOOT_KERNEL_COMP_ADDR)
    if "nothing" in what:
        offs = range(WL.NOR_SCAN_START, WL.NOR_SCAN_END, WL.NOR_SCAN_STEP)
        check(f"  all {len(offs)} NOR offsets probed, every backup slot tried",
              len(sim.sent(f"sf read {C.UBOOT_LOAD_ADDR} 0x")) >= len(offs) + 4 and len(sim.sent("env import")) == 4)
    if "DTB-sized" in what:
        check("  the bad pointer was cleared first", "setenv recovery" in sim.cmds)
sim = UBoot({}, {0x500000: ("fit", 38 * 1024), 0x600000: ("fit", "garbage"), 0x700000: ("fit", 40 * 1024),
               0x900000: ("fit", 30 * MB)}, {})
ok, t = run_uboot(sim)
check("scan: unreadable FIT size counts as small; the FIRST DTB is the one paired",
      ok is True and "0x900000" in t and f"sf read {C.UBOOT_DTB_ADDR} 0x500000" in sim.env["recovery"])
sim = UBoot({}, {0x600000: ("fit", 30 * MB)}, {}, fail=("sf probe",))
ok, t = run_uboot(sim)
check("missing, sf probe fails -> False before any scan", ok is False and "sf probe failed" in t)
sim = UBoot({}, {0x600000: ("fit", 30 * MB)}, {}, fail=(f"sf read {C.UBOOT_LOAD_ADDR} 0x400000",))
ok, t = run_uboot(sim)
check("a failing NOR offset is skipped, scan goes on", ok is True and "0x600000" in t)

# ============================================================================
section("Armbian LAN: flash (raw download, or streamed xz)")
for fw, xz in (("/tmp/Armbian.img.xz", True), ("/tmp/Armbian.img", False)):
    c = ctx(firmware_path=fw)
    c.set("firmware_source", "http://h/firmware.img")
    with patch.object(AL, "with_spinner", return_value=(DD_OK, None)):
        check(f"{'xz' if xz else 'raw'} -> True", AL.step_flash_armbian(c) is True)
    s = c.device.launch_script.call_args.args[0]
    check(f"{'xz: streamed curl | xz -dc | dd' if xz else 'raw: downloaded to a file, then dd'}",
          ("| xz -dc | dd of=/dev/mmcblk0" in s) if xz else ("curl -sk -o /tmp/mono_imager_firmware.img" in s))
for what, rep, err in (("no records", "curl: (7) failed", None), ("no report", None, None),
                       ("spinner error", None, TimeoutError("t"))):
    with patch.object(AL, "with_spinner", return_value=(rep, err)):
        check(f"{what} -> False", AL.step_flash_armbian(ctx()) is False)
c = ctx()
c.device.launch_script.side_effect = RuntimeError("x")
check("launch fails -> False", AL.step_flash_armbian(c) is False)

# ============================================================================
section("Armbian: live serial relay while the user flips the DIP")


class RelaySer:
    def __init__(self, chunks, fail_first=0):
        self.chunks, self.fail, self.reads = list(chunks), fail_first, 0

    @property
    def in_waiting(self):
        if self.fail:
            self.fail -= 1
            raise OSError("port dropped mid power-cycle")
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, n):
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""


out = io.StringIO()
c = ctx()
c.device.ser = RelaySer([b"Armbian 24 ", b"mono login: "], fail_first=2)
with patch("builtins.input", side_effect=lambda p: time.sleep(0.4) or ""), patch.object(AL.sys, "stdout", out), \
     patch.object(AL, "RELAY_POLL_SLEEP", 0.01):
    ok = AL.step_flip_to_emmc_and_verify(c)
check("errors swallowed, boot output relayed, Enter continues", ok is True
      and "mono login:" in out.getvalue() and FO.results[-1][1].startswith("DIP flipped to eMMC"))
check("relay thread stopped after Enter", c.device.ser.reads == 2)

section("Armbian: NOR round-trip failure exits")


def refresh(autoboot=(True, None), boot=(True, None), login=(True, None), net=(True, None), fw=(True, None)):
    seq = iter([autoboot, boot, login, net, fw])
    with patch("builtins.input", return_value=""), patch.object(AL, "with_spinner", side_effect=lambda *a, **k: next(seq)), \
         patch("mono_imager.device_net.RecoveryNetwork"):
        FO.reset_results()
        return AL.step_refresh_firmware(ctx())


check("all good -> True", refresh() is True and "flip DIP to eMMC" in FO.results[-1][1])
check("autoboot not seen -> False", refresh(autoboot=(False, None)) is False and "autoboot" in FO.results[-1][1])
check("autoboot error -> False", refresh(autoboot=(None, TimeoutError("t"))) is False)
check("recovery boot fails -> False", refresh(boot=(False, None)) is False and "Recovery Linux" in FO.results[-1][1])
check("login fails -> False", refresh(login=(False, None)) is False and "recovery shell" in FO.results[-1][1])
check("network fails -> False", refresh(net=(False, None)) is False and "network" in FO.results[-1][1])
check("firmware update fails -> False", refresh(fw=(False, None)) is False and "refreshed" in FO.results[-1][1])
check("firmware update errors -> False", refresh(fw=(True, RuntimeError("x"))) is False)
with patch("builtins.input", return_value=""), patch.object(AL, "_pause_with_live_serial"):
    check("Armbian USB: flip step delegates", AU.step_flip_to_emmc_and_verify(ctx()) is True)
with patch.object(AU, "_refresh_firmware_and_finish", return_value=True) as rf:
    check("Armbian USB: refresh step delegates", AU.step_refresh_firmware(ctx()) is True and rf.called)

# ============================================================================
section("shared LAN steps (_common)")
c = ctx()
srv = MagicMock()
with patch.object(_common, "start_http_server", return_value=srv):
    check("HTTP server up -> stored in ctx", _common._step_http_server_start(c) is True and c.get("http_server") is srv)
with patch.object(_common, "start_http_server", return_value=None):
    check("HTTP server not started -> False", _common._step_http_server_start(ctx()) is False)
with patch.object(_common, "start_http_server", side_effect=OSError("port in use")):
    check("HTTP server raises -> False", _common._step_http_server_start(ctx()) is False and "port in use" in last()[0][3])
for rep, want in (("200", True), ("404", False), (None, False)):
    c = ctx()
    with patch.object(_common, "with_spinner", return_value=(rep, None)):
        check(f"reachability HTTP {rep} -> {want}", _common._step_firmware_reachable(c) is want)
check("firmware URL stored for the flash step", c.get("firmware_source") == "http://192.168.168.84:8080/firmware.img")
check("HEAD request (curl -I), result POSTed back", "curl -sk -I" in c.device.launch_script.call_args.args[0]
      and "report?step=06" in c.device.launch_script.call_args.args[0])
c = ctx()
c.device.launch_script.side_effect = RuntimeError("x")
check("reachability launch fails -> False", _common._step_firmware_reachable(c) is False)

# ============================================================================
section("image container checks (journeys/__init__)")
tmp = Path(tempfile.mkdtemp(prefix="mi_js_"))
files = {"a.img.bz2": b"BZh91AY", "b.img.gz": b"\x1f\x8b\x08", "c.img.xz": b"\xfd7zXZ\x00..", "d.zip": b"PK\x03\x04",
         "e.img": b"\x00" * 16, "f.img": b""}
for n, b in files.items():
    (tmp / n).write_bytes(b)
check("detect_container: bzip2/gzip/xz/zip/raw/empty",
      [J.detect_container(tmp / n) for n in files] == ["bzip2", "gzip", "xz", "zip", "raw", "raw"])
check("detect_container: unreadable path -> raw", J.detect_container(tmp / "missing.img") == "raw")
check("expected_container per OS", [J.expected_container(o, p) for o, p in (
    ("OPNsense", "x.img"), ("OpenWRT", "x.IMG.GZ"), ("OpenWRT", "x.img"), ("Armbian", "x.img.xz"),
    ("Armbian", "x.img"), ("VyOS", "x.iso"))] == ["bzip2", "gzip", "raw", "xz", "raw", "raw"])
check("match: OPNsense .bz2 OK", J.check_image_matches_os("OPNsense", tmp / "a.img.bz2") == (True, ""))
ok, why = J.check_image_matches_os("OpenWRT", tmp / "a.img.bz2")
check("mismatch: bzip2 image through OpenWRT raw dd -> explained", not ok and "bzip2-compressed" in why)
ok, why = J.check_image_matches_os("OPNsense", tmp / "e.img")
check("mismatch: raw image where bzip2 is expected -> 'uncompressed'", not ok and "uncompressed" in why)
ok, why = J.check_image_matches_os("Armbian", tmp / "b.img.gz")
check("mismatch: gzip where raw is expected", not ok and "gzip-compressed" in why)
from mono_imager import step_registry as SR
fake = [SimpleNamespace(os=[SR.ALL_OS, "X"], transfer=["lan", SR.ALL_TRANSFER]),
        SimpleNamespace(os=["Y"], transfer=["usb"])]
with patch.object(SR, "_registry", fake):
    check("discovered_journeys skips ALL_OS / ALL_TRANSFER wildcards", J.discovered_journeys() == [("X", "lan"), ("Y", "usb")])

# ============================================================================
section("usb_utils: size check, image search")
said = []
with patch.object(U, "verbose", lambda m, *a, **k: said.append(m)):
    d = MagicMock()
    U.check_usb_size(d, "/mnt/usb; rm -rf /")
    check("size check: unsafe mount path -> skipped, nothing run", not d.run_script.called and "skipping" in said[-1])
    for out, want in (("noise\n7500000\n", "minimum recommended"), ("62000000", "capacity: 59.1 GB"),
                      ("df: no such", "Could not parse")):
        d.run_script.return_value = out
        U.check_usb_size(d, "/mnt/usb")
        check(f"size check {out.strip()[:12]!r} -> {want!r}", want in said[-1])
    d.run_script.side_effect = RuntimeError("x")
    U.check_usb_size(d, "/mnt/usb")
    check("size check raises -> warning only", "Could not read USB size" in said[-1])
    check("image search: unknown OS -> (None, None)", U.find_image_on_usb(MagicMock(), "/mnt/usb", "BeOS") == (None, None))
    check("image search: unsafe mount path -> (None, None)",
          U.find_image_on_usb(MagicMock(), "/mnt/$(reboot)", "Armbian") == (None, None))
    d = MagicMock()
    d.run_script.side_effect = RuntimeError("x")
    check("image search raises -> (None, None)", U.find_image_on_usb(d, "/mnt/usb", "Armbian") == (None, None))
    d = MagicMock()
    d.run_script.return_value = "zip:/mnt/usb/x.zip\nimg:/elsewhere/y.img\nNOT_FOUND\nimg:/mnt/usb/z.img"
    check("image search: bad tag / path outside the mount ignored, stops at NOT_FOUND",
          U.find_image_on_usb(d, "/mnt/usb", "Armbian") == (None, None))


if SH:
    class Sh:
        """run_script() that really runs the generated script under /bin/sh."""
        def run_script(self, script, **kw):
            return subprocess.run([SH, "-c", script], capture_output=True, text=True).stdout


    stick = tmp / "stick"
    stick.mkdir()
    (stick / "README.txt").write_text("x")
    (stick / "notes").mkdir()
    check("real search, nothing matching -> (None, None)", U.find_image_on_usb(Sh(), str(stick), "OpenWRT") == (None, None))
    (stick / "OpenWrt-24.10-EMMC.IMG.GZ").write_bytes(b"\x1f\x8b")
    (stick / "Armbian_24.5_mono.img.xz").write_bytes(b"\xfd7zXZ\x00")
    (stick / "opnsense-25.1.img.bz2").write_bytes(b"BZh")
    with patch.object(U, "verbose"):
        got = {o: U.find_image_on_usb(Sh(), str(stick), o) for o in ("OpenWRT", "Armbian", "OPNsense")}
    check("real search: case-insensitive match per OS, right format tag",
          got == {"OpenWRT": (f"{stick}/OpenWrt-24.10-EMMC.IMG.GZ", "img.gz"),
                  "Armbian": (f"{stick}/Armbian_24.5_mono.img.xz", "img.xz"),
                  "OPNsense": (f"{stick}/opnsense-25.1.img.bz2", "img.bz2")})
else:
    section("usb_utils: real image search under sh")
    print("  SKIP: no sh found (install Git for Windows or run on Linux/macOS)")


import shutil
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
