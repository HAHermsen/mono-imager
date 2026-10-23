#!/usr/bin/env python3
"""
mono-imager: Unit tests for the VyOS journeys' PC-side helpers (#28).

No hardware required. Uses tests/unit/fixtures/vyos_hybrid.img.gz, a
miniature hybrid ISO built exactly like the VyOS LS1046A build makes the
real one (make_vyos_hybrid.sh: real mkdosfs -F 32, mtools, mkimage, the
build's own boot.cmd, the same MBR).

What this tests:
  - read_mbr() / hybrid_layout()      — partition table of the hybrid ISO
  - read_fat32_file()                 — boot.scr / DTB out of the FAT32 part
  - parse_uboot_script()              — mkimage script image -> text
  - adapt_boot_script_for_emmc()      — usb 0:2 -> mmc 0:2, + toram, refusals
  - stage_partition_table()           — v2's eMMC sector-0 entries
  - check_write_report()              — every failure reason
  - ask_vyos_password()               — match / mismatch / give up
  - _pick_emmc()                      — never the USB stick
  - menu label / notice / final DIP / confirmation-box wiring

Run: python tests/unit/test_vyos_host.py
"""

import gzip
import io
import os
import shutil
import struct
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.journeys import _vyos as V

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


FIX = Path(__file__).resolve().parent / "fixtures"
tmpdir = Path(tempfile.mkdtemp(prefix="mi_vyos_"))
iso = tmpdir / "vyos-test-LS1046A-arm64.iso"
with gzip.open(FIX / "vyos_hybrid.img.gz") as g, open(iso, "wb") as o:
    shutil.copyfileobj(g, o)
iso_size = iso.stat().st_size


print("=" * 60)
print("read_mbr() / hybrid_layout() on a build-identical hybrid ISO")
print("=" * 60)
entries = V.read_mbr(iso)
check("4 MBR entries read", len(entries) == 4)
layout, why = V.hybrid_layout(iso, iso_size)
check("layout accepted", layout is not None and why == "")
check("p1 = ISO9660 data (0x17) from sector 0", layout["p1"]["type"] == 0x17 and layout["p1"]["start"] == 0)
check("p2 = FAT32 (0x0C) right after the 1 MiB-padded ISO", layout["p2"]["type"] == 0x0C and layout["p2"]["start"] == 2048)

plain = tmpdir / "plain.iso"
plain.write_bytes(b"\x00" * 4096)
lay2, why2 = V.hybrid_layout(plain, 4096)
check("no MBR -> refused with a reason", lay2 is None and "no MBR" in why2)

bad = tmpdir / "bad.iso"
data = bytearray(iso.read_bytes()[:512])
struct.pack_into("<B", data, 446 + 16 + 4, 0x83)   # p2 not FAT32
bad.write_bytes(bytes(data) + b"\x00" * (iso_size - 512))
lay3, why3 = V.hybrid_layout(bad, iso_size)
check("p2 not FAT32 -> refused", lay3 is None and "FAT32" in why3)
lay4, why4 = V.hybrid_layout(iso, iso_size - 1024 * 1024)
check("partition past end of file -> refused", lay4 is None and "past the end" in why4)


print()
print("=" * 60)
print("read_fat32_file() / parse_uboot_script()")
print("=" * 60)
p2_off = layout["p2"]["start"] * V.SECTOR
scr = V.read_fat32_file(iso, p2_off, "BOOT.SCR")
check("boot.scr read from FAT32 equals mkimage's output", scr == (FIX / "vyos_boot.scr").read_bytes())
check("lower-case name works too", V.read_fat32_file(iso, p2_off, "boot.scr") == scr)
check("DTB in the root read", V.read_fat32_file(iso, p2_off, "MONO-GW.DTB") == b"DTB")
try:
    V.read_fat32_file(iso, p2_off, "NOPE.TXT")
    check("missing file raises", False)
except ValueError:
    check("missing file raises", True)
try:
    V.read_fat32_file(iso, 0, "BOOT.SCR")
    check("not-a-FAT32 offset raises", False)
except ValueError:
    check("not-a-FAT32 offset raises", True)

text = V.parse_uboot_script(scr)
check("script text = the build's boot.cmd", text.strip() == (FIX / "vyos_boot.cmd").read_text().strip())
try:
    V.parse_uboot_script(b"\x00" * 100)
    check("non-mkimage data raises", False)
except ValueError:
    check("non-mkimage data raises", True)


print()
print("=" * 60)
print("adapt_boot_script_for_emmc()")
print("=" * 60)
lines = V.adapt_boot_script_for_emmc(text)
check("starts with mmc rescan", lines[0] == "mmc rescan")
check("ends with the booti line", lines[-1].startswith("booti ${kernel_addr_r} ${ramdisk_addr_r}:${filesize}"))
check("no usb commands left", not any("usb start" in l or "usb stop" in l or " usb " in l for l in lines))
check("3 fatloads from mmc 0:2", sum(1 for l in lines if l.startswith("fatload mmc 0:2 ")) == 3)
check("initrd loaded last (booti uses ${filesize})",
      [l for l in lines if l.startswith("fatload")][-1].endswith("live/initrd.img"))
ba = [l for l in lines if l.startswith("setenv bootargs ")]
check("bootargs kept from the ISO + toram", len(ba) == 1 and ba[0].endswith(" toram")
      and "boot=live" in ba[0] and "nopersistence" in ba[0])
check("ramdisk_addr_r setenv kept", "setenv ramdisk_addr_r 0xa4000000" in lines)
check("toram not doubled", V.adapt_boot_script_for_emmc(text.replace("usbcore.autosuspend=-1", "usbcore.autosuspend=-1 toram"))
      == lines)
for label, src in [
    ("if/then refused", text + "\nif true; then echo x; fi\n"),
    ("unknown command refused", text + "\nsetexpr x 1\n"),
    ("no booti refused", "\n".join(l for l in text.splitlines() if not l.startswith("booti"))),
    ("no bootargs refused", "\n".join(l for l in text.splitlines() if not l.startswith("setenv bootargs"))),
    ("over-long line refused", text.replace("toram", "").replace("usbcore.autosuspend=-1", "x" * 600)),
]:
    try:
        V.adapt_boot_script_for_emmc(src)
        check(label, False)
    except ValueError:
        check(label, True)


print()
print("=" * 60)
print("stage_partition_table()")
print("=" * 60)
pt = V.stage_partition_table(layout)
check("66 bytes ending in 55 AA", len(pt) == 66 and pt[-2:] == b"\x55\xaa")
mbr = bytearray(512)
mbr[446:512] = pt
img = tmpdir / "mbr.bin"
img.write_bytes(bytes(mbr))
e = V.read_mbr(img)
check("p1 at 64 MiB, ISO length", e[0]["start"] == V.STAGE_BASE_LBA and e[0]["sectors"] == layout["p1"]["sectors"]
      and e[0]["type"] == 0x17)
check("p2 = FAT32 at 64 MiB + ISO offset", e[1]["start"] == V.STAGE_BASE_LBA + 2048 and e[1]["type"] == 0x0C
      and e[1]["sectors"] == layout["p2"]["sectors"])
check("entries 3/4 empty", e[2]["type"] == 0 and e[3]["type"] == 0)
check("64 MiB base is past Mono's 32 MiB eMMC firmware region", V.STAGE_BASE_LBA * 512 == 64 * 1024 * 1024)
check("printf_octal round-trips", bytes(int(x, 8) for x in V.printf_octal(pt).split("\\")[1:]) == pt)


print()
print("=" * 60)
print("build_write_script() guards / check_write_report()")
print("=" * 60)
try:
    V.build_write_script(url="u", dev="/dev/sda", iso_bytes=1000, iso_sha256="x", report_url="r")
    check("non-sector-sized ISO refused", False)
except ValueError:
    check("non-sector-sized ISO refused", True)
try:
    V.build_write_script(url="u", dev="/dev/sda", iso_bytes=512, iso_sha256="x", report_url="r", seek_lba=7)
    check("non-MiB seek refused", False)
except ValueError:
    check("non-MiB seek refused", True)

good = {"DL_RC": "0", "SIZE": "1024", "SHA": "abc", "DD_RC": "0", "VERIFY": "OK"}
check("good report passes", V.check_write_report(good, 1024, "abc") == (True, ""))
check("no sha256sum on device still passes", V.check_write_report({**good, "SHA": "unavailable"}, 1024, "abc")[0])
cases = [
    ("curl failure", {**good, "DL_RC": "7"}, "download"),
    ("short download", {**good, "SIZE": "10"}, "incomplete"),
    ("sha mismatch", {**good, "SHA": "zzz"}, "SHA-256"),
    ("skipped write", {**good, "WRITE": "SKIPPED"}, "nothing was written"),
    ("dd failure", {**good, "DD_RC": "1"}, "dd failed"),
    ("read-back mismatch", {**good, "VERIFY": "BAD"}, "read-back"),
]
for label, kv, word in cases:
    ok, why = V.check_write_report(kv, 1024, "abc")
    check(f"{label} -> fails ({word})", not ok and word in why)
ok, why = V.check_write_report({**good, "PT_ZERO_RC": "0", "PT_RC": "1"}, 1024, "abc", partition_table=True)
check("partition table write failure -> fails", not ok and "partition table" in why)
ok, why = V.check_write_report({**good, "PT_ZERO_RC": "0", "PT_RC": "0", "P2": "MISSING"}, 1024, "abc",
                               partition_table=True, expect_p2=True)
check("p2 not visible -> fails", not ok and "boot partition" in why)
check("parse_kv first wins, ignores noise", V.parse_kv("x\nA=1\nA=2\n 12+0 records\nB= b \n") == {"A": "1", "B": "b"})


print()
print("=" * 60)
print("ask_vyos_password() / _pick_emmc()")
print("=" * 60)


def asker(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


with redirect_stdout(io.StringIO()):
    check("match -> returned", V.ask_vyos_password(asker(["s3cret!", "s3cret!"])) == "s3cret!")
    check("mismatch then match", V.ask_vyos_password(asker(["a1", "b2", "c3", "c3"])) == "c3")
    check("empty then ok", V.ask_vyos_password(asker(["", "pw", "pw"])) == "pw")
    check("non-ASCII refused", V.ask_vyos_password(asker(["pä", "ok", "ok"])) == "ok")
    check("three failures -> ''", V.ask_vyos_password(asker(["a", "b", "c", "d", "e", "f"])) == "")
    buf = io.StringIO()
with redirect_stdout(buf):
    V.ask_vyos_password(asker(["vyos", "vyos"]))
check("default password warned about", "well-known default" in buf.getvalue())

listing = "The following disks were found:\nDrive: /dev/sda (15.6 GB)\nDrive: /dev/mmcblk0 (29.1 GB)\n"
check("eMMC picked, not the stick", V._pick_emmc(listing) == "/dev/mmcblk0")
check("boot partitions never picked", V._pick_emmc("Drive: /dev/mmcblk0boot0 (0.0 GB)\n") is None)
check("no eMMC -> None", V._pick_emmc("Drive: /dev/sda (15.6 GB)\n") is None)


print()
print("=" * 60)
print("tui/console wiring: menu label, notice, final DIP, confirmation box")
print("=" * 60)
from mono_imager.journeys import (discovered_journeys, get_transfer_label, get_journey_notice,
                                  get_firmware_prompt, final_dip, _FLASH_TARGETS)
from mono_imager.step_registry import list_journey
from mono_imager import console

js = discovered_journeys()
check("both VyOS journeys discovered", ("VyOS", "net") in js and ("VyOS", "netemmc") in js)
check("v1 listed before v2", js.index(("VyOS", "net")) < js.index(("VyOS", "netemmc")))
check("v2 label says EXPERIMENTAL", "EXPERIMENTAL" in get_transfer_label("VyOS", "netemmc"))
check("v1 label mentions the USB stick", "USB stick" in get_transfer_label("VyOS", "net"))
check("other journeys keep their plain label", get_transfer_label("OpenWRT", "lan") == "lan")
check("source notice shown for VyOS", "does NOT download, check or verify" in get_journey_notice("VyOS", "net"))
check("no notice for other journeys", get_journey_notice("OpenWRT", "lan") == "")
check("prompt asks for a local .iso", ".iso" in get_firmware_prompt("VyOS", "net"))
check("final DIP NOR", final_dip("VyOS") == "NOR")
check("flash target eMMC", _FLASH_TARGETS["VyOS"] == "/dev/mmcblk0")
check("tui gets the Ethernet box / network setup",
      all("Device network ready" in list_journey("VyOS", t) for t in ("net", "netemmc")))


def box(os_name):
    out = io.StringIO()
    with redirect_stdout(out):
        import builtins
        orig = console.read_line
        console.read_line = lambda p: "n"
        try:
            console.show_flash_confirmation(os_name=os_name, port="COM5", firmware_path="x",
                                            flash_target="/dev/mmcblk0", host_ip="1.2.3.4",
                                            device_ip="5.6.7.8")
        finally:
            console.read_line = orig
    return out.getvalue()


vb = box("VyOS")
check("VyOS box: keep DIP RIGHT (NOR)", "keep the DIP switch RIGHT" in vb and "VyOS     boots via NOR" in vb)
ob = box("OPNsense")
check("OPNsense box text unchanged", "| (NOR). OPNsense boots via NOR itself, which      |" in ob)
check("box lines stay the same width", len({len(l) for l in vb.splitlines() if l.startswith("  |")}) == 1)
wb = box("OpenWRT")
check("eMMC OSes still get the LEFT/RIGHT explanation", "LEFT  = eMMC" in wb)

shutil.rmtree(tmpdir, ignore_errors=True)
print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
