#!/usr/bin/env python3
"""
mono-imager: the VyOS journeys' recovery-Linux scripts, EXECUTED (#28).

No hardware required. build_write_script() and build_stick_probe_script()
run for real — under /bin/sh, and under BusyBox sh with ONLY BusyBox
applets on PATH (what recovery Linux has) when busybox is installed.
`curl` is a stub (download = copy the "PC" file, POST = save the report),
`blockdev` a no-op stub; the "devices" are plain files.

What this tests:
  - v1: ISO lands on the "stick" byte-for-byte, report passes check_write_report()
  - nothing is written when the download is short or its SHA-256 differs
  - no sha256sum on the device -> SHA=unavailable, still written + read back
  - v2: first 4 KiB = new partition table (old GPT header gone), the
        4 KiB-64 MiB region (Mono eMMC firmware) untouched, ISO at 64 MiB,
        partition table readable by read_mbr() and pointing at it
  - a corrupted read-back is caught (VERIFY=BAD)
  - stick probe: present / absent / mounted

Skipped (not failed) on hosts without /bin/sh (e.g. plain Windows).

Run: python tests/unit/test_vyos_scripts.py
"""

import gzip
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
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


if not Path("/bin/sh").exists():
    print("SKIP: no /bin/sh on this host")
    sys.exit(0)

CURL_STUB = """#!/bin/sh
# download: curl -sk -o FILE URL   -> copy $SRC_FILE (optionally truncated / corrupted)
# report:   curl -sk -X POST --data-binary @FILE URL -> copy FILE to $REPORT_OUT
if [ "$2" = "-o" ]; then
  cp "$SRC_FILE" "$3"
  [ -n "$TRUNC_TO" ] && dd if="$SRC_FILE" of="$3" bs=512 count="$TRUNC_TO" 2>/dev/null
  [ -n "$FLIP_BYTE" ] && printf 'Z' | dd of="$3" bs=1 seek="$FLIP_BYTE" conv=notrunc 2>/dev/null
  exit 0
fi
if [ "$3" = "POST" ]; then
  cp "${5#@}" "$REPORT_OUT"
  exit 0
fi
exit 2
"""
BLOCKDEV_STUB = "#!/bin/sh\nexit 0\n"
BB_APPLETS = ["dd", "cmp", "cut", "tr", "wc", "rm", "sync", "sleep", "cat", "grep",
              "cp", "printf", "basename", "mkdir"]

BUSYBOX = shutil.which("busybox")
FIX = Path(__file__).resolve().parent / "fixtures"
work = Path(tempfile.mkdtemp(prefix="mi_vyos_sh_"))
iso = work / "vyos.iso"
with gzip.open(FIX / "vyos_hybrid.img.gz") as g, open(iso, "wb") as o:
    shutil.copyfileobj(g, o)
iso_bytes = iso.stat().st_size
iso_sha = hashlib.sha256(iso.read_bytes()).hexdigest()


def make_bin(variant: str, with_sha: bool = True) -> Path:
    b = work / f"bin_{variant}_{int(with_sha)}"
    if b.exists():
        return b
    b.mkdir()
    for name, body in (("curl", CURL_STUB), ("blockdev", BLOCKDEV_STUB)):
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    if variant == "busybox":
        for a in BB_APPLETS + (["sha256sum"] if with_sha else []):
            os.symlink(BUSYBOX, b / a)
    return b


def run(script: str, variant: str = "sh", with_sha: bool = True, env_extra=None):
    report = work / "report.txt"
    report.unlink(missing_ok=True)
    env = {"SRC_FILE": str(iso), "REPORT_OUT": str(report), "HOME": str(work)}
    b = make_bin(variant, with_sha)
    if variant == "busybox":
        env["PATH"] = str(b)
        cmd = [BUSYBOX, "sh", "-c", script]
    else:
        env["PATH"] = f"{b}:/usr/bin:/bin"
        if not with_sha:
            # hide sha256sum: a PATH without /usr/bin, with the tools copied in
            nosha = work / "nosha"
            if not nosha.exists():
                nosha.mkdir()
                for t in ["dd", "cmp", "cut", "tr", "wc", "rm", "sync", "sleep", "cat", "grep", "cp"]:
                    src = shutil.which(t)
                    if src:
                        os.symlink(src, nosha / t)
            env["PATH"] = f"{b}:{nosha}"
        cmd = ["/bin/sh", "-c", script]
    env.update(env_extra or {})
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    rep = report.read_text() if report.exists() else ""
    return p, V.parse_kv(rep), rep


def device_file(name: str, size: int, fill: bytes = b"\xab") -> Path:
    p = work / name
    with open(p, "wb") as f:
        f.write(fill * size)
    return p


variants = ["sh"] + (["busybox"] if BUSYBOX else [])
if not BUSYBOX:
    print("NOTE: busybox not installed — BusyBox variant skipped")

for variant in variants:
    print()
    print("=" * 60)
    print(f"[{variant}] v1: write the ISO to the USB stick")
    print("=" * 60)
    stick = device_file(f"stick_{variant}", iso_bytes + 8 * 1024 * 1024)
    script = V.build_write_script(url="http://pc:8080/firmware.img", dev=str(stick), iso_bytes=iso_bytes,
                                  iso_sha256=iso_sha, report_url="http://pc:8080/report?step=vyos_write")
    p, kv, rep = run(script, variant)
    check("script ran (rc 0)", p.returncode == 0)
    ok, why = V.check_write_report(kv, iso_bytes, iso_sha)
    check(f"report passes ({why or 'ok'})", ok)
    check("device computed the same SHA-256", kv.get("SHA") == iso_sha)
    data = stick.read_bytes()
    check("stick holds the ISO byte-for-byte", data[:iso_bytes] == iso.read_bytes())
    check("rest of the stick untouched", data[iso_bytes:] == b"\xab" * (len(data) - iso_bytes))
    check("RAM copy removed afterwards", not Path(V.ISO_TMP).exists())

    print()
    print(f"[{variant}] nothing is written when the download is wrong")
    stick2 = device_file(f"stick2_{variant}", iso_bytes)
    _, kv, _ = run(script.replace(str(stick), str(stick2)), variant, env_extra={"TRUNC_TO": "100"})
    check("short download -> WRITE=SKIPPED", kv.get("WRITE") == "SKIPPED")
    check("short download -> stick untouched", stick2.read_bytes() == b"\xab" * iso_bytes)
    check("short download -> report fails", not V.check_write_report(kv, iso_bytes, iso_sha)[0])
    _, kv, _ = run(script.replace(str(stick), str(stick2)), variant, env_extra={"FLIP_BYTE": "40000"})
    check("same size, different bytes -> WRITE=SKIPPED (SHA-256)", kv.get("WRITE") == "SKIPPED")
    check("...stick untouched", stick2.read_bytes() == b"\xab" * iso_bytes)

    print()
    print(f"[{variant}] no sha256sum on the device")
    if variant == "busybox":
        # this BusyBox build runs its own applets even when they are not on
        # PATH (standalone shell), so "no sha256sum" can't be simulated here
        print("  NOTE: covered by the /bin/sh variant only")
    else:
        stick3 = device_file(f"stick3_{variant}", iso_bytes)
        _, kv, _ = run(script.replace(str(stick), str(stick3)), variant, with_sha=False)
        check("SHA=unavailable reported", kv.get("SHA") == "unavailable")
        check("still written and read back OK",
              kv.get("VERIFY") == "OK" and stick3.read_bytes() == iso.read_bytes())
        check("report passes", V.check_write_report(kv, iso_bytes, iso_sha)[0])

    print()
    print(f"[{variant}] v2: stage on the 'eMMC'")
    layout, _ = V.hybrid_layout(iso, iso_bytes)
    pt = V.stage_partition_table(layout)
    emmc_size = 64 * 1024 * 1024 + iso_bytes + 1024 * 1024
    emmc = device_file(f"emmc_{variant}", emmc_size)
    with open(emmc, "r+b") as f:          # an old GPT header at LBA1, like OpenWRT leaves
        f.seek(512)
        f.write(b"EFI PART")
    s2 = V.build_write_script(url="http://pc:8080/firmware.img", dev=str(emmc), iso_bytes=iso_bytes,
                              iso_sha256=iso_sha, report_url="http://pc:8080/report?step=vyos_write",
                              seek_lba=V.STAGE_BASE_LBA, partition_table=pt,
                              expect_p2=str(emmc) + "p2")
    p, kv, rep = run(s2, variant)
    check("script ran (rc 0)", p.returncode == 0)
    check("partition table steps OK", kv.get("PT_ZERO_RC") == "0" and kv.get("PT_RC") == "0")
    check("dd + read-back OK", kv.get("DD_RC") == "0" and kv.get("VERIFY") == "OK")
    check("p2 check ran (a plain file is not a block device -> MISSING)", kv.get("P2") == "MISSING")
    ok, why = V.check_write_report(kv, iso_bytes, iso_sha, partition_table=True, expect_p2=False)
    check(f"report passes without the p2 check ({why or 'ok'})", ok)
    d = emmc.read_bytes()
    e = V.read_mbr(emmc)
    check("sector 0: p1 -> 64 MiB", e[0]["start"] == V.STAGE_BASE_LBA and e[0]["type"] == 0x17)
    check("sector 0: p2 -> FAT32 inside the staged ISO", e[1]["start"] == V.STAGE_BASE_LBA + 2048
          and e[1]["type"] == 0x0C)
    check("bytes 0-445 zeroed (no stale boot code)", d[:446] == b"\x00" * 446)
    check("old GPT header gone (LBA1-7 zeroed)", d[512:4096] == b"\x00" * 3584)
    check("4 KiB-64 MiB untouched (Mono eMMC firmware region)",
          d[4096:64 * 1024 * 1024] == b"\xab" * (64 * 1024 * 1024 - 4096))
    base = V.STAGE_BASE_LBA * 512
    check("ISO at 64 MiB byte-for-byte", d[base:base + iso_bytes] == iso.read_bytes())
    check("after the ISO untouched", d[base + iso_bytes:] == b"\xab" * (emmc_size - base - iso_bytes))
    # FAT32 reachable through the staged table, exactly where U-Boot will look (mmc 0:2)
    scr = V.read_fat32_file(emmc, e[1]["start"] * 512, "BOOT.SCR")
    check("boot.scr readable via the staged p2", scr == (FIX / "vyos_boot.scr").read_bytes())

    blk = next((f"/dev/{n}" for n in sorted(os.listdir("/dev"))
                if stat.S_ISBLK(os.stat(f"/dev/{n}").st_mode)), None) if os.path.isdir("/dev") else None
    if blk:
        _, kv, _ = run(s2.replace(f"[ -b {emmc}p2 ]", f"[ -b {blk} ]"), variant)
        check(f"p2 present (block device {blk}) -> P2=OK", kv.get("P2") == "OK")
    else:
        print("  NOTE: no block device in /dev — P2=OK branch not exercised")

    print()
    print(f"[{variant}] a corrupted read-back is caught")
    bad = s2.replace("| cmp -s - \"$f\"", "| { dd of=/dev/null bs=1 count=1 2>/dev/null; cat; } | cmp -s - \"$f\"")
    check("(test hook applied)", bad != s2)
    _, kv, _ = run(bad, variant)
    check("VERIFY=BAD", kv.get("VERIFY") == "BAD")
    check("report fails", not V.check_write_report(kv, iso_bytes, iso_sha, partition_table=True)[0])

    print()
    print(f"[{variant}] stick probe")
    probe = V.build_stick_probe_script(str(stick)).replace(f"[ -b {stick} ]", f"[ -f {stick} ]")
    sysblk = work / "sys"
    (sysblk / stick.name / "device").mkdir(parents=True, exist_ok=True)
    (sysblk / stick.name / "size").write_text("30310400\n")
    (sysblk / stick.name / "device" / "vendor").write_text("SanDisk \n")
    (sysblk / stick.name / "device" / "model").write_text("Cruzer  Blade   \n")
    mounts = work / "mounts"
    mounts.write_text("proc /proc proc rw 0 0\n")
    probe = probe.replace("/sys/block/", f"{sysblk}/").replace("/proc/mounts", str(mounts))
    p, kv, _ = run(probe, variant)
    out = V.parse_kv(p.stdout)
    check("present -> STICK=YES", out.get("STICK") == "YES")
    check("size in sectors", out.get("SECTORS") == "30310400")
    check("vendor + model", out.get("MODEL") == "SanDisk Cruzer Blade")
    check("not mounted", out.get("MOUNTED") == "NO")
    mounts.write_text(f"{stick} /mnt vfat rw 0 0\n")
    p, _, _ = run(probe, variant)
    check("mounted -> MOUNTED=YES", V.parse_kv(p.stdout).get("MOUNTED") == "YES")
    p, _, _ = run(probe.replace(f"[ -f {stick} ]", "[ -f /nonexistent ]"), variant)
    check("absent -> STICK=NO", V.parse_kv(p.stdout).get("STICK") == "NO")

shutil.rmtree(work, ignore_errors=True)
print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
