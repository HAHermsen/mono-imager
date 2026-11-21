#!/usr/bin/env python3
"""
mono-imager: eMMC snapshot / restore (#29) — host-side logic.

No hardware required. Covers:
  - chunk planning and restore range planning (same board, firmware-region
    skip, clone mode)
  - manifest build/validate, SHA256SUMS round trip, store verification
    (corrupted / missing files caught before anything is sent)
  - host compression round trip (zst when available, gz, raw)
  - snapshot server: chunked + Content-Length uploads, token check,
    ranged /image serving with hashes
  - label rules, OS guess, NOR-env advice
  - reach_recovery(): NOR chainload commands, DIP fallback when the
    U-Boot has no `sf`

Run: python tests/unit/test_snapshot.py
"""

import datetime
import hashlib
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import snapshot as S
from mono_imager import snapshot_orchestrator as SO
from mono_imager.snapshot_server import SnapshotSession, start_snapshot_server, _iter_body

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


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


work = Path(tempfile.mkdtemp(prefix="mi_snap_"))

# ----------------------------------------------------------------------------
section("Chunk planning")
c = S.plan_chunks(32 * 1024 ** 3 - 1024 ** 2 * 3 + 512)
check("covers the whole eMMC", sum(n for _, _, n in c) == 32 * 1024 ** 3 - 1024 ** 2 * 3 + 512)
check("1 GiB chunks, contiguous", all(off == i * S.CHUNK_BYTES for i, off, _ in c))
check("last chunk is the remainder", c[-1][2] == (32 * 1024 ** 3 - 1024 ** 2 * 3 + 512) - (len(c) - 1) * S.CHUNK_BYTES)
for bad in (0, 1000):
    try:
        S.plan_chunks(bad)
        check(f"rejects size {bad}", False)
    except ValueError:
        check(f"rejects size {bad}", True)
try:
    S.plan_chunks(64 * S.MIB, chunk_bytes=16 * S.MIB)
    check("rejects chunks smaller than the firmware region", False)
except ValueError:
    check("rejects chunks smaller than the firmware region", True)

# ----------------------------------------------------------------------------
section("Restore planning")
m = {"chunks": [{"index": 0, "offset": 0, "bytes": S.CHUNK_BYTES},
                {"index": 1, "offset": S.CHUNK_BYTES, "bytes": 5 * S.MIB}]}
full = S.plan_restore(m, skip_fw=False)
check("full restore = whole chunks", [(r["abs"], r["len"]) for r in full]
      == [(0, S.CHUNK_BYTES), (S.CHUNK_BYTES, 5 * S.MIB)])
skip = S.plan_restore(m, skip_fw=True)
check("skip_fw: 0..4 KiB + 32 MiB..end of chunk 0, then chunk 1",
      [(r["abs"], r["len"]) for r in skip]
      == [(0, 4096), (S.FW_END, S.CHUNK_BYTES - S.FW_END), (S.CHUNK_BYTES, 5 * S.MIB)])
check("skip_fw never touches 4 KiB..32 MiB",
      not any(r["abs"] < S.FW_END and r["abs"] + r["len"] > S.FW_START for r in skip))
check("range offsets are chunk-relative", skip[1]["off"] == S.FW_END and skip[1]["chunk"] == 0)

# ----------------------------------------------------------------------------
section("Labels, OS guess, identity")
LABEL_NOW = datetime.datetime(2026, 11, 21, 13, 54)   # fixed "now" for the label checks
check("default label: dump-<date>-<time>, no OS name",
      S.default_label(now=LABEL_NOW) == "dump-20261121-1354")
check("auto label: auto-dump-<date>-<time>",
      S.default_label(S.AUTO_LABEL_PREFIX + S.LABEL_BASE, now=LABEL_NOW) == "auto-dump-20261121-1354")
check("valid label", S.valid_label("vyos-prod"))
check("label with slash refused", not S.valid_label("../x"))
check("label .partial refused", not S.valid_label("x.partial"))
check("OS from bootcmd: openwrt", S.detect_os_label({"bootcmd": "run openwrt || run recovery"}) == "openwrt")
check("OS from bootcmd: vyos", S.detect_os_label({"bootcmd": "run vyos || run recovery"}) == "vyos")
check("OS from extlinux: armbian", S.detect_os_label({"bootcmd": "sysboot mmc 0:1 any 0x1 /extlinux/extlinux.conf"}) == "armbian")
check("OS unknown -> emmc", S.detect_os_label({}) == "emmc")
check("same board by MAC (case-insensitive)",
      S.same_board({"device": {"mac": "E8:F6:D7:00:17:DF"}}, {"mac": "e8:f6:d7:00:17:df"}))
check("other board", not S.same_board({"device": {"mac": "e8:f6:d7:00:17:df"}}, {"mac": "e8:f6:d7:00:17:e0"}))
check("no MAC never counts as same board", not S.same_board({"device": {"mac": ""}}, {"mac": ""}))

# ----------------------------------------------------------------------------
section("Compression round trip")
data = os.urandom(300_000) + b"\0" * 2_000_000
for comp in ("raw", "gz") + (("zst",) if S._zstd_backend() else ()):
    p = work / f"rt.{comp}"
    with S.open_writer(p, comp) as f:
        f.write(data[:1000])
        f.write(data[1000:])
    with S.open_reader(p, comp) as f:
        back = f.read()
    check(f"{comp}: identical after round trip", back == data)
if S._zstd_backend():
    # multi-frame zstd (what `zstd` on the device + concatenation produces)
    p = work / "mf.zst"
    for part in (data[:5000], data[5000:]):
        q = work / "part.zst"
        with S.open_writer(q, "zst") as f:
            f.write(part)
        with open(p, "ab") as out:
            out.write(q.read_bytes())
    with S.open_reader(p, "zst") as f:
        check("zst: multi-frame file reads across frames", f.read() == data)
check("host prefers zst when available", S.host_compression() == ("zst" if S._zstd_backend() else "gz"))

# ----------------------------------------------------------------------------
section("Manifest + SHA256SUMS + store verification")


def make_snapshot(folder: Path, disk: bytes, comp="gz", chunk=32 * S.MIB, mac="e8:f6:d7:00:17:df"):
    folder.mkdir(parents=True)
    chunks = []
    for i, off, n in S.plan_chunks(len(disk), chunk_bytes=chunk):
        name = S.chunk_file(i, comp)
        with S.open_writer(folder / name, comp) as f:
            f.write(disk[off:off + n])
        chunks.append({"index": i, "offset": off, "bytes": n,
                       "sha256": hashlib.sha256(disk[off:off + n]).hexdigest(),
                       "file": name, "compression": comp})
    man = S.build_manifest(label=folder.name, identity={"mac": mac}, emmc_bytes=len(disk),
                           chunks=chunks,
                           fw_sha256=hashlib.sha256(disk[S.FW_START:S.FW_END]).hexdigest(),
                           boot_sha256=hashlib.sha256(disk[:S.FW_START]).hexdigest(),
                           os_label="openwrt", boot_dip="EMMC", transfer="lan", tool_version="t",
                           has_env=True)
    man["chunk_bytes"] = chunk
    (folder / "manifest.json").write_text(S.manifest_json(man))
    (folder / "uboot_env.txt").write_text(S.format_env({"bootcmd": "run openwrt"}))
    sums = {p.name: S.sha256_file(p) for p in folder.iterdir() if p.name != "SHA256SUMS"}
    (folder / "SHA256SUMS").write_text(S.sha256sums_text(sums))
    return man


disk = bytearray(os.urandom(40 * S.MIB + 1536))
disk[512:520] = b"EFI PART"
disk = bytes(disk)
snap = work / "store" / "openwrt-1"
man = make_snapshot(snap, disk)
check("manifest validates", S.validate_manifest(man) == [])
ok, why, m2 = S.verify_store_snapshot(snap)
check("store snapshot verifies", ok and m2["label"] == "openwrt-1")
check("GPT detected in chunk 0", S.snapshot_has_gpt(snap, man))
check("list_snapshots finds it", [n for n, _ in S.list_snapshots(work / "store")] == ["openwrt-1"])
(work / "store" / "half.partial").mkdir()
check("list_snapshots ignores .partial", len(S.list_snapshots(work / "store")) == 1)
check("SHA256SUMS parse round trip",
      S.parse_sha256sums(S.sha256sums_text({"a": "0" * 64, "b c": "f" * 64})) == {"a": "0" * 64, "b c": "f" * 64})
check("env file round trip", S.parse_env_file(S.format_env({"a": "x=y", "b": ""})) == {"a": "x=y", "b": ""})

bad = dict(man, chunks=man["chunks"][:1])
check("manifest with a missing chunk is refused", S.validate_manifest(bad) != [])
bad2 = json.loads(json.dumps(man)); bad2["chunks"][0]["compression"] = "lz4"
check("unknown compression refused", S.validate_manifest(bad2) != [])
bad3 = json.loads(json.dumps(man)); bad3["format"] = 99
check("unknown format refused", S.validate_manifest(bad3) != [])

corrupt = work / "store2" / "c"
make_snapshot(corrupt, disk)
f = corrupt / man["chunks"][1]["file"]
b = bytearray(f.read_bytes()); b[len(b) // 2] ^= 0xFF; f.write_bytes(bytes(b))
ok, why, _ = S.verify_store_snapshot(corrupt)
check("corrupted chunk caught by SHA256SUMS", not ok and "does not match" in why)
(corrupt / man["chunks"][1]["file"]).unlink()
ok, why, _ = S.verify_store_snapshot(corrupt)
check("missing chunk caught", not ok and "missing" in why)

# ----------------------------------------------------------------------------
section("Snapshot server: uploads, tokens, ranged serving")
sess = SnapshotSession()
port = free_port()
srv = start_snapshot_server("127.0.0.1", port, sess)
check("server starts", srv is not None)
target = work / "up"
target.mkdir()
sess.begin_snapshot(target, "gz")


def put(path, body: bytes, chunked: bool):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    if chunked:
        conn.putrequest("PUT", path)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        for i in range(0, len(body), 70_000):
            piece = body[i:i + 70_000]
            conn.send(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        conn.send(b"0\r\n\r\n")
    else:
        conn.request("PUT", path, body=body)
    r = conn.getresponse()
    r.read()
    conn.close()
    return r.status


payload = os.urandom(1_000_003)
check("chunked upload accepted", put(f"/upload?token={sess.token}&chunk=0", payload, True) == 200)
up = sess.wait("uploads", 0, 5)
check("upload size + SHA-256 recorded", up and up["bytes"] == len(payload)
      and up["sha256"] == hashlib.sha256(payload).hexdigest())
with S.open_reader(target / up["file"], "gz") as fh:
    check("upload stored compressed, decompresses to the same bytes", fh.read() == payload)
check("Content-Length verify accepted", put(f"/verify?token={sess.token}&tag=x", payload, False) == 200)
check("verify hash recorded", sess.wait("verify", "x", 5)["sha256"] == hashlib.sha256(payload).hexdigest())
check("wrong token refused", put("/upload?token=nope&chunk=1", b"x", False) == 403)
check("nothing written for a refused upload", 1 not in sess.uploads)

sess.begin_restore(snap, man)
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
conn.request("GET", f"/image?token={sess.token}&k=0&chunk=0&off={S.FW_END - 100}&len=100")
r = conn.getresponse()
body = r.read()
conn.close()
check("ranged /image returns the exact bytes", r.status == 200 and body == disk[S.FW_END - 100:S.FW_END])
sent = sess.wait("sent", 0, 5)
check("served range hash recorded", sent["sha256"] == hashlib.sha256(body).hexdigest() and not sent["error"])
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
conn.request("GET", f"/image?token={sess.token}&k=1&chunk=1&off=0&len={man['chunks'][1]['bytes']}")
r = conn.getresponse(); body = r.read(); conn.close()
check("full chunk served and checked against the manifest",
      body == disk[32 * S.MIB:] and sess.wait("sent", 1, 5).get("manifest_ok") is True)
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
conn.request("GET", f"/image?token={sess.token}&k=2&chunk=1&off=0&len={10 ** 12}")
r = conn.getresponse(); r.read(); conn.close()
check("range outside the chunk refused", r.status == 400)
srv.shutdown()

# chunked body parser alone
import io
raw = b"5\r\nhello\r\n6;ext=1\r\n world\r\n0\r\nX-T: 1\r\n\r\n"
check("_iter_body: chunked with extension + trailer",
      b"".join(_iter_body(io.BytesIO(raw), {"Transfer-Encoding": "chunked"})) == b"hello world")

# ----------------------------------------------------------------------------
section("od dump parsing, U-Boot env blobs")
import struct, zlib, subprocess
raw = os.urandom(1000) + b"\0" * 40
if shutil.which("od"):
    od = subprocess.run(["od", "-v", "-b"], input=raw, capture_output=True).stdout.decode()
    check("parse_od round trip (GNU od -v -b)", S.parse_od(od) == raw)
else:
    print("  SKIP: parse_od round trip (GNU od -v -b): no od found (add Git for Windows usr\\bin to PATH or run on Linux/macOS)")
if shutil.which("busybox"):
    bb = subprocess.run(["busybox", "od", "-v", "-b"], input=raw, capture_output=True).stdout.decode()
    check("parse_od round trip (BusyBox od -v -b)", S.parse_od(bb) == raw)
else:
    print("  SKIP: parse_od round trip (BusyBox od -v -b): no busybox found")
check("parse_od: gap -> None", S.parse_od("0000000 001 002\n0000020 003\n") is None)
check("no od -A/-t in device scripts (BusyBox lacks them)",
      not any(x in src for x in ("od -A", "od -t", "-tx1")
              for src in [Path(S.__file__).read_text(encoding="utf-8")]))


def env_blob(vars_, redundant=False, size=S.NOR_ENV_SIZE, bad=False):
    data = b"".join(f"{k}={v}".encode() + b"\0" for k, v in vars_.items()) + b"\0"
    head = 4 + (1 if redundant else 0)
    data = data + b"\xff" * (size - head - len(data))
    crc = zlib.crc32(data) ^ (1 if bad else 0)
    return struct.pack("<I", crc) + (b"\x01" if redundant else b"") + data


ev = {"bootcmd": "run opnsense || run recovery", "opnsense": "mmc dev 0; booti", "ethaddr": "e8:f6:d7:00:17:df"}
check("env blob parsed", S.parse_uboot_env_blob(env_blob(ev)) == ev)
check("redundant env blob parsed", S.parse_uboot_env_blob(env_blob(ev, redundant=True)) == ev)
check("bad CRC -> None", S.parse_uboot_env_blob(env_blob(ev, bad=True)) is None)

# ----------------------------------------------------------------------------
section("GPT health check")
SEC = S.SECTOR


def make_gpt(disk_sectors, parts, n_entries=128, backup=True, corrupt_hdr=False, at_end=True):
    """parts: [(first, last, name)] -> (head bytes of 34 sectors, tail sector)."""
    ents = bytearray(n_entries * 128)
    for i, (f, l, nm) in enumerate(parts):
        struct.pack_into("<16s16sQQQ72s", ents, i * 128, b"T" * 16, bytes([i + 1]) * 16, f, l, 0,
                         nm.encode("utf-16-le"))
    ents = bytes(ents)
    last = disk_sectors - 1
    alt = last if at_end else last - 1000

    def hdr(my, other, entries_lba):
        h = bytearray(struct.pack(S.GPT_HEADER_FMT, b"EFI PART", 0x10000, 92, 0, 0, my, other,
                                  34, last - 33, b"G" * 16, entries_lba, n_entries, 128,
                                  zlib.crc32(ents)))
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) ^ (1 if corrupt_hdr and my == 1 else 0))
        return bytes(h) + b"\0" * (SEC - len(h))
    head = b"\0" * SEC + hdr(1, alt, 2) + ents
    tail = hdr(alt, 1, alt - 32) if backup else b"\0" * SEC
    return head[:34 * SEC], tail


disk = 1000000
h, t = make_gpt(disk, [(2048, 4095, "boot"), (4096, 99999, "root")])
g = S.check_gpt(h, t, disk * SEC)
check("valid GPT -> ok, 2 partitions", g["status"] == "ok" and len(g["partitions"]) == 2)
h, t = make_gpt(disk, [(2048, 4095, "a")], at_end=False)
g = S.check_gpt(h, t, disk * SEC)
check("backup not at the disk end -> warn (sgdisk -e)", g["status"] == "warn"
      and any("sgdisk -e" in x[1] for x in g["findings"]))
h, t = make_gpt(disk, [(2048, 4095, "a")], corrupt_hdr=True)
check("primary header CRC wrong -> error", S.check_gpt(h, t, disk * SEC)["status"] == "error")
h, t = make_gpt(disk, [(2048, 9000, "a"), (8000, 12000, "b")])
check("overlapping partitions -> error", S.check_gpt(h, t, disk * SEC)["status"] == "error")
h, t = make_gpt(disk, [(2048, 4095, "a")])
hb = bytearray(h); hb[S.FW_START + 100] ^= 0xFF            # Mono firmware overwrote entries past 4 KiB
g = S.check_gpt(bytes(hb), t, disk * SEC)
check("entry CRC broken only past 4 KiB -> warn (Mono firmware region), partitions still read",
      g["status"] == "warn" and len(g["partitions"]) == 1)
hb = bytearray(h); hb[2 * SEC + 40] ^= 0xFF                # inside the first 4 KiB
check("entry CRC broken inside 4 KiB -> error", S.check_gpt(bytes(hb), t, disk * SEC)["status"] == "error")
check("no GPT -> none", S.check_gpt(b"\0" * 34 * SEC, b"\0" * SEC, disk * SEC)["status"] == "none")
check("partition past the usable area -> error",
      S.check_gpt(*make_gpt(disk, [(2048, disk + 5, "x")]), disk * SEC)["status"] == "error")

fs = S.parse_fs_check({"FS_1": "vfat 0", "FS_2": "ext 4", "FS_3": "other nocheck", "FS_4": "ext notool"})
check("fsck results: clean / errors / not checked",
      fs[1]["level"] == "ok" and fs[2]["level"] == "error" and fs[3]["level"] == "none"
      and fs[4]["level"] == "none")

# ----------------------------------------------------------------------------
section("U-Boot env regions: rebuild, boot closure")
from mono_imager import uboot_env as E, inventory as I
reg = E.parse(env_blob(ev))
blob2 = E.build(E.EnvRegion(dict(reg.vars, bootcmd="run openwrt || run recovery"), False, None, reg.size))
check("rebuilt region parses, same size, new value",
      len(blob2) == S.NOR_ENV_SIZE and E.parse(blob2).vars["bootcmd"] == "run openwrt || run recovery")
red = E.parse(env_blob(ev, redundant=True))
check("redundant layout + flag kept on rebuild",
      red.redundant and E.parse(E.build(red)).redundant and E.build(red)[4] == red.flag)
try:
    E.build(E.EnvRegion({"x": "y" * 9000}, False, None, S.NOR_ENV_SIZE))
    check("too big refused", False)
except ValueError:
    check("too big refused", True)
envx = {"bootcmd": "run openwrt || run recovery", "openwrt": "setenv bootargs ${bootargs_console} x; run emmc_load && booti ${kernel_addr_r} - ${fdt_addr_r}",
        "emmc_load": "ext4load mmc 0:1 ${kernel_addr_r} /boot/Image", "kernel_addr_r": "0x82000000",
        "fdt_addr_r": "0x88000000", "bootargs_console": "console=ttyS0,115200", "recovery": "sf probe; booti",
        "ethaddr": "e8:f6:d7:00:17:df"}
clo = E.boot_closure(envx)
check("closure follows run + ${var}, never recovery, never unrelated vars",
      set(clo) == {"bootcmd", "openwrt", "emmc_load", "kernel_addr_r", "fdt_addr_r", "bootargs_console"})
check("merge only lists what differs",
      E.merge_for_boot(dict(envx, bootcmd="run opnsense || run recovery", openwrt="old"), envx)
      == {"bootcmd": envx["bootcmd"], "openwrt": envx["openwrt"]})

# ----------------------------------------------------------------------------
section("Inventory parsing, U-Boot version compare, MAC match, clone warning")
kv = {"DATE": "2026-11-14T10:00:00Z", "KERNEL": "6.6.1", "MTD_0": "mtd0 04000000 00010000 \"nor\"",
      "UBOOT_NOR_MTD0": "U-Boot 2025.04-g1 (Jun 01 2025 - 12:00:00 +0000)",
      "UBOOT_EMMC": "U-Boot 2025.04-g2 (Jul 02 2025 - 08:00:00 +0000)",
      "EMMC_LIFE_TIME": "0x01 0x01", "MAC_ETH0": "E8:F6:D7:00:17:DF", "MAC_ETH1": "e8:f6:d7:00:17:e0",
      "MEM_KB": "8000000", "INVENTORY": "done"}
banner = "SoC:  LS1046AE Rev1.0 (0x87070010)\nModel: Mono Gateway Development Kit\n[ OK ] DDR4 Memory          Bank0: 1982 MB\n[FAIL] Temperatures         CPU sensor not responding\n"
inv = I.parse_inventory(kv, ntp_synced=True, boot_output=banner, probe={"model": "Mono", "serial": "MT-1", "emmc_bytes": 1})
check("inventory fields", inv["complete"] and inv["uboot"]["emmc"].startswith("U-Boot 2025.04")
      and inv["macs"] == {"eth0": "E8:F6:D7:00:17:DF", "eth1": "e8:f6:d7:00:17:e0"}
      and inv["emmc"]["life_time"] == "0x01 0x01")
check("boot banner + self-test captured (FAIL kept)",
      inv["boot"]["identity"].get("Model") == "Mono Gateway Development Kit"
      and [t["ok"] for t in inv["boot"]["self_test"]] == [True, False])
txt = I.format_inventory(inv, label="x", nor_env={"bootcmd": "a"}, emmc_env=None, tool_version="t")
check("inventory.txt readable", "NOR mtd0" in txt and "[FAIL] Temperatures" in txt and "bootcmd=a" in txt)
check("version compare: older / newer / same / unknown",
      I.compare_uboot("U-Boot 2025.04 (Jun 01 2025 - 12:00:00 +0000)", "U-Boot 2025.04 (Jul 02 2025 - 08:00:00 +0000)") == "older"
      and I.compare_uboot("U-Boot 2025.10", "U-Boot 2025.04") == "newer"
      and I.compare_uboot("U-Boot 2025.04 (Jun 01 2025 - 12:00:00 +0000)", "U-Boot 2025.04 (Jun 01 2025 - 12:00:00 +0000)") == "same"
      and I.compare_uboot("", "U-Boot 2025.04") == "unknown")
m_inv = {"device": {"mac": "e8:f6:d7:00:17:df"}, "inventory": {"macs": {"eth0": "e8:f6:d7:00:17:df", "eth1": "e8:f6:d7:00:17:e0"}}}
check("all ports match -> same", S.mac_match(m_inv, {"mac": "x"}, {"eth0": "E8:F6:D7:00:17:DF", "eth1": "e8:f6:d7:00:17:e0"})[0] == "same")
check("some ports match -> partial", S.mac_match(m_inv, {}, {"eth0": "e8:f6:d7:00:17:df", "eth1": "aa:aa:aa:aa:aa:aa"})[0] == "partial")
check("no port matches -> other (clone)", S.mac_match(m_inv, {}, {"eth0": "aa:aa:aa:aa:aa:aa"})[0] == "other")
check("old snapshot without inventory -> primary MAC", S.mac_match({"device": {"mac": "aa"}}, {"mac": "AA"}, None)[0] == "same")
warn = "\n".join(SO.clone_warning({"os_label": "openwrt", "device": {}}, {"a"}, {"b"}))
check("clone warning: identity clash + OS-specific hint, no edits",
      "SSH host keys" in warn and "macaddr" in warn and "does NOT edit" in warn)
asked = []
ok = SO.firmware_gate({"uboot": {"nor": {"mtd0": "U-Boot 2025.04 (Jun 01 2025 - 12:00:00 +0000)"}}},
                      {"uboot": {"nor": {"mtd0": "U-Boot 2025.10 (Oct 01 2025 - 12:00:00 +0000)"}}},
                      clone=False, ask=lambda p: asked.append(p) or "s")
check("older NOR firmware -> asked, 's' stops the restore", not ok and len(asked) == 1)
ok = SO.firmware_gate({"uboot": {"nor": {"mtd0": "U-Boot 2025.10"}}}, {"uboot": {"nor": {"mtd0": "U-Boot 2025.04"}}},
                      clone=True, ask=lambda p: 1 / 0)
check("newer NOR firmware -> no question", ok)

# ----------------------------------------------------------------------------
section("NOR env advice (restore never writes NOR)")
mm = {"boot_dip": "NOR"}
env = {"bootcmd": "run opnsense || run recovery", "opnsense": "mmc dev 0; booti x"}
adv = SO.nor_env_advice(mm, env, {"bootcmd": "run openwrt"}, True)
check("advises when NOR bootcmd differs", any("setenv bootcmd 'run opnsense || run recovery'" in l for l in adv)
      and any("setenv opnsense" in l for l in adv))
check("silent when NOR bootcmd already matches", SO.nor_env_advice(mm, env, env, True) == [])
check("silent for eMMC-booting OSes", SO.nor_env_advice({"boot_dip": "EMMC"}, env, {}, True) == [])

# ----------------------------------------------------------------------------
section("reach_recovery: recovery into RAM without the DIP switch")


def fake_uboot(sf_ok=True, eat_first=0, nor_env=True, fixed=True, boot_ok=True):
    """U-Boot at the prompt. nor_env: NOR holds a valid env (env import works).
    fixed: DTB/kernel at the documented NOR offsets (on real firmware 1.7.5's
    test board 0x500000 was EMPTY). boot_ok: `run recovery` reaches Linux."""
    d = MagicMock()
    d.captured_uboot_env = {"bootcmd": "run openwrt || run recovery", "ver": "U-Boot 2025.04"}
    state = {"eat": eat_first, "imported": False}

    def send(cmd, timeout=None, **kw):
        if cmd and state["eat"]:
            # real hardware: U-Boot swallows the first character after the interrupt
            state["eat"] -= 1
            return f"Unknown command '{cmd[1:].split()[0]}' - try 'help'"
        if cmd == "sf probe 0":
            return "SF: Detected mt25qu512abb with page size 256 Bytes" if sf_ok else "Unknown command 'sf'"
        if cmd.startswith("env import -c"):
            if nor_env and cmd.endswith(S.NOR_ENV_SIZES[0]):
                state["imported"] = True
                return ""
            return "## Error: bad CRC, import failed"
        if cmd == "printenv recovery":
            return "recovery=sf probe 0; sf read ...; bootm" if state["imported"] else "recovery=run emmc_recovery"
        if cmd.startswith(f"md.b {S.RAM_DTB}"):
            return "90000000: d0 0d fe ed" if fixed else "90000000: 00 00 00 00"
        if cmd.startswith(f"md.b {S.RAM_KERNEL}"):
            return "82000000: 1f 8b"
        return ""
    d.send_command.side_effect = send
    d.boot_recovery.return_value = boot_ok
    d.login_recovery.return_value = True
    return d


def run(*devs, dip=SO.DIP_EMMC):
    asks = []
    with patch.object(SO, "phase1_uboot", side_effect=list(devs)), patch("builtins.print"):
        dev, info = SO.reach_recovery("COM1", dip, ask=lambda p: asks.append(p) or "")
    return dev, info, asks


d0 = fake_uboot()
dev, info, asks = run(d0)
cmds = [c.args[0] for c in d0.send_command.call_args_list]
check("first: this U-Boot's own recovery (the one with working Ethernet), no NOR commands",
      dev is d0 and info["recovery_medium"] == "emmc" and not asks
      and not any(c.startswith("sf ") or c.startswith("env import") for c in cmds))
check("never saveenv", not any("saveenv" in c for c in cmds))


def fails_first(d):
    d.boot_recovery.side_effect = [False, True]
    return d


d1 = fails_first(fake_uboot())
dev, info, asks = run(d1)
cmds = [c.args[0] for c in d1.send_command.call_args_list]
check("own recovery fails -> NOR env imported from NOR's env offset (CRC-checked)",
      any(c.startswith(f"sf read {S.RAM_KERNEL} {S.NOR_ENV_OFF}") for c in cmds)
      and any(c.startswith("env import -c") for c in cmds))
check("then NOR's own `run recovery`, boot_medium=qspi, no address guessing",
      d1.boot_recovery.call_args.kwargs.get("boot_medium") == "qspi"
      and not any("0x500000" in c for c in cmds) and info["recovery_medium"] == "qspi" and not asks)

d_fixed = fails_first(fake_uboot(nor_env=False, fixed=True))
dev, info, asks = run(d_fixed)
check("no NOR env but documented offsets valid -> NOR recovery", info["recovery_medium"] == "qspi" and not asks)

d_none = fake_uboot(nor_env=False, fixed=False)
d_none.boot_recovery.side_effect = [False]
d_nor = fake_uboot()
dev, info, asks = run(d_none, d_nor)
check("nothing boots (own recovery fails, no NOR env, 0x500000 empty) -> only then the DIP question",
      len(asks) == 1 and dev is d_nor and info["uboot_dip"] == SO.DIP_NOR)

d_eat = fake_uboot(eat_first=1)
d_eat.captured_uboot_env = None
with patch.object(SO, "phase1_uboot", return_value=d_eat), \
     patch.object(SO, "capture_uboot_env", return_value={"bootcmd": "run openwrt"}), patch("builtins.print"):
    dev, info = SO.reach_recovery("COM1", SO.DIP_EMMC, ask=lambda p: "")
check("first character swallowed by U-Boot -> re-synced and retried, no DIP flip",
      dev is d_eat and info["flipped"] is False and d_eat.boot_recovery.called)
check("empty env capture -> captured again on a fresh prompt", info["env"] == {"bootcmd": "run openwrt"})
cmds = [c.args[0] for c in d_eat.send_command.call_args_list]
check("prompt primed with an empty line before sf probe", cmds[0] == "" or "" in cmds[:2])

d_dead, d_nor = fake_uboot(sf_ok=False, boot_ok=False), fake_uboot()
dev, info, asks = run(d_dead, d_nor)
check("no sf and own recovery fails -> DIP question",
      len(asks) == 1 and info["flipped"] and dev is d_nor and info["uboot_dip"] == SO.DIP_NOR)

dev, info, asks = run(None, fake_uboot())
check("U-Boot not caught (blank eMMC / bootdelay=0) -> DIP fallback", dev is not None and info["flipped"])

dev, info, asks = run(fake_uboot(), dip=SO.DIP_NOR)
check("DIP already RIGHT -> NOR U-Boot's own recovery, no import needed",
      info["recovery_medium"] == "qspi" and not asks)

shutil.rmtree(work, ignore_errors=True)
print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
