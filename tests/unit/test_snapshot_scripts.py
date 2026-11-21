#!/usr/bin/env python3
"""
mono-imager: eMMC snapshot / restore (#29) — recovery-Linux scripts, EXECUTED.

No hardware required. The real journey functions (snapshot_lan,
restore_lan, snapshot_usb, restore_usb, usb_read_manifest) drive the
real device scripts, run by a fake device under /bin/sh and, when
installed, under BusyBox sh with ONLY BusyBox applets (+ curl, zstd) on
PATH. The "eMMC" is a plain file; LAN talks to the real snapshot server
with the real curl.

What this tests:
  - LAN snapshot: every chunk lands on the PC, read-back verified, both
    with sha256sum on the device and without it (hash on the PC)
  - LAN restore: target becomes byte-identical; skip_fw / clone mode
    leaves 4 KiB..32 MiB untouched; a tampered chunk is caught
  - USB snapshot to a "stick" folder (gzip, zstd, none), SHA256SUMS valid
    for `sha256sum -c`, manifest read back over the cat script
  - USB restore: identical target, firmware region skip, a corrupted file
    on the stick stops the restore BEFORE anything is written
  - every generated script parses (sh -n), incl. the probe
  - single-pass LAN snapshot (upload, then hash from the page cache; no tee,
    no FIFO): fsha with OpenSSL 3 / 1.1 output and without openssl; a wrong
    device hash and an upload cut mid-stream are refused without hanging; a
    broken tee on the device does not matter; NOR dump; progress text;
    fallbacks to read-back; "Thorough verify"
    asked for LAN only; auto-snapshot before a restore; serial-safe size

Skipped (not failed) on hosts without /bin/sh or curl.

Run: python tests/unit/test_snapshot_scripts.py
"""

import hashlib
import json
import struct
import zlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import contextlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import constants as C
from mono_imager import snapshot as S
from mono_imager import snapshot_orchestrator as SO
from mono_imager import inventory as I
from mono_imager import uboot_env as E
from mono_imager.snapshot_server import SnapshotSession, start_snapshot_server

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


if not Path("/bin/sh").exists() or not shutil.which("curl"):
    print("SKIP: needs /bin/sh and curl")
    sys.exit(0)

BUSYBOX = shutil.which("busybox")
APPLETS = ["sh", "dd", "od", "strings", "date", "uname", "cut", "tr", "rm", "sync", "sleep", "cat", "grep", "printf", "basename",
           "dirname", "mkdir", "mv", "ls", "sort", "touch", "head", "tail", "gzip", "sha256sum",
           "df", "mount", "umount", "wc", "echo", "test", "[", "tee", "mkfifo"]
work = Path(tempfile.mkdtemp(prefix="mi_snap_sh_"))
CHUNK = 32 * S.MIB   # small chunks so a 40 MiB "eMMC" has two


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Ser:
    def write(self, b):
        return len(b)


class FakeDev:
    """run_script / launch_script executed locally by `shell`."""
    def __init__(self, shell, env):
        self.shell, self.env, self.ser, self.procs = shell, env, Ser(), []

    def run_script(self, body, marker=None, exec_timeout=60, **kw):
        r = subprocess.run(self.shell + ["-c", body], env=self.env, capture_output=True,
                           text=True, timeout=max(exec_timeout, 60))
        return r.stdout

    def launch_script(self, body, marker=None, **kw):
        self.procs.append(subprocess.Popen(self.shell + ["-c", body], env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        return "/tmp/x.sh"

    def disconnect(self):
        pass

    def wait(self):
        for p in self.procs:
            p.wait(timeout=300)
        self.procs = []


def shells():
    out = [("sh", ["/bin/sh"], dict(os.environ))]
    if BUSYBOX:
        bb = work / "bbbin"
        if not bb.exists():
            bb.mkdir()
            for a in APPLETS:
                (bb / a).symlink_to(BUSYBOX)
            for t in ("curl", "zstd"):
                if shutil.which(t):
                    (bb / t).symlink_to(shutil.which(t))
        out.append(("busybox", [str(bb / "sh")], {"PATH": str(bb), "HOME": str(work)}))
    return out


def gpt_bytes(disk_sectors, parts):
    """Valid primary (LBA 0..33) and backup (last LBA) GPT for a test disk."""
    ents = bytearray(128 * 128)
    for i, (f, l) in enumerate(parts):
        struct.pack_into("<16s16sQQQ72s", ents, i * 128, b"T" * 16, bytes([i + 1]) * 16, f, l, 0, b"")
    ents = bytes(ents)
    last = disk_sectors - 1

    def hdr(my, other, entries_lba):
        h = bytearray(struct.pack(S.GPT_HEADER_FMT, b"EFI PART", 0x10000, 92, 0, 0, my, other,
                                  34, last - 33, b"G" * 16, entries_lba, 128, 128, zlib.crc32(ents)))
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)))
        return bytes(h) + b"\0" * (512 - len(h))
    return b"\0" * 512 + hdr(1, last, 2) + ents, hdr(last, 1, last - 32)


def make_disk(path: Path, size: int):
    b = bytearray(os.urandom(size // 2)) + bytearray(size - size // 2)   # half random, half zeros
    head, tail = gpt_bytes(size // 512, [(2048, size // 512 - 100)])
    b[:len(head)] = head
    b[-512:] = tail
    path.write_bytes(bytes(b))
    return bytes(b)


def env_blob(vars_):
    data = b"".join(f"{k}={v}".encode() + b"\0" for k, v in vars_.items()) + b"\0"
    data += b"\xff" * (S.NOR_ENV_SIZE - 4 - len(data))
    return struct.pack("<I", zlib.crc32(data)) + data


def cap(label, probe, **kw):
    return SO.Capture(label=label, probe=probe, identity=ident, env=env, os_label="openwrt",
                      boot_dip="EMMC", **kw)


def probe_for(size, have):
    return {"emmc_bytes": size, "have": set(have), "mac": "e8:f6:d7:00:17:df",
            "serial": "MT-1", "model": "Mono", "usb_present": True, "nor_present": False,
            "emmc_mounted": False, "boot_medium": "qspi", "ok": True}


ident = {"mac": "e8:f6:d7:00:17:df", "serial": "MT-1", "model": "Mono", "uboot_version": "U-Boot"}
env = {"bootcmd": "run openwrt || run recovery"}

for name, shell, envv in shells():
    section(f"[{name}] script syntax")
    scripts = {
        "probe": S.build_probe_script(),
        "lan snapshot": S.build_lan_snapshot_script(base_url="http://h:1", token="t",
                                                    chunks=[(0, 0, CHUNK)], have_sha=False, nor=True),
        "lan snapshot (single pass)": S.build_lan_snapshot_script(base_url="http://h:1", token="t",
                                                    chunks=[(0, 0, CHUNK)], have_sha=True, nor=True,
                                                    single_pass=True),
        "lan restore": S.build_lan_restore_script(base_url="http://h:1", token="t",
                                                  ranges=[{"chunk": 0, "off": 0, "len": 4096, "abs": 0}],
                                                  have_sha=True),
        "usb mount": S.build_usb_mount_script(mode="snapshot"),
        "gpt fix": S.build_gpt_fix_script(),
        "fs check": S.build_fs_check_script(),
        "nor env dump": S.build_nor_env_dump_script(),
        "nor env write": S.build_nor_env_write_script("/tmp/x", "0" * 64),
        "nor env save (usb)": S.build_usb_nor_env_save_script(label="x"),
        "dump": S.build_dump_script("/dev/mmcblk0", 0, 34),
    }
    for k, body in scripts.items():
        r = subprocess.run(shell + ["-n", "-c", body], env=envv, capture_output=True, text=True)
        check(f"{k} parses", r.returncode == 0)
    out = subprocess.run(shell + ["-c", S.build_probe_script(dev=str(work / "none"))], env=envv,
                         capture_output=True, text=True).stdout
    check("probe runs to the end", S.parse_probe(out)["ok"])

    # ------------------------------------------------------------------------
    for have_sha, extra in ((True, ()), (False, ()), (False, ("openssl",)),
                            (True, ("openssl",))):
        single = have_sha or bool(extra)
        section(f"[{name}] LAN snapshot + restore (device sha256sum: {have_sha}, single pass: {extra})")
        tag = f"{have_sha}_{'_'.join(extra) or 'two'}"
        emmc = work / f"emmc_{name}_{tag}.img"
        size = 40 * S.MIB + 1536
        disk = make_disk(emmc, size)
        store = work / f"store_{name}_{tag}"
        store.mkdir()
        port = free_port()
        sess = SnapshotSession()
        srv = start_snapshot_server("127.0.0.1", port, sess)
        base = f"http://127.0.0.1:{port}"
        dev = FakeDev(shell, envv)
        have = ({"sha256sum"} if have_sha else set()) | set(extra)
        with patch.object(S, "CHUNK_BYTES", CHUNK), patch.object(S, "EMMC_DEV", str(emmc)), \
             patch("builtins.print"):
            real_lan = S.build_lan_snapshot_script
            with patch.object(S, "build_lan_snapshot_script",
                              lambda **kw: (seen.append(kw.get("single_pass")),
                                            real_lan(**dict(kw, dev=str(emmc))))[1]):
                seen = []
                m = SO.snapshot_lan(dev, sess, base, store, cap("snap1", probe_for(size, have)))
        dev.wait()
        check(f"single pass used: {single}", seen == [single])
        check("snapshot published with a manifest", m is not None and (store / "snap1" / "manifest.json").is_file())
        ok, why, mm = S.verify_store_snapshot(store / "snap1")
        check("published snapshot verifies (SHA256SUMS)", ok)
        check("two chunks, hashes = the disk", m and [c["sha256"] for c in m["chunks"]] ==
              [hashlib.sha256(disk[:CHUNK]).hexdigest(), hashlib.sha256(disk[CHUNK:]).hexdigest()])
        check("firmware region hash recorded",
              m and m["emmc"]["fw_region"]["sha256"] == hashlib.sha256(disk[S.FW_START:S.FW_END]).hexdigest())
        check("U-Boot env stored", (store / "snap1" / "uboot_env.txt").read_text().startswith("bootcmd="))
        check("no .partial left behind", not (store / "snap1.partial").exists())

        # restore to a bigger, different target
        tgt = work / f"tgt_{name}_{tag}.img"
        tgt.write_bytes(b"\xAA" * (size + 8 * S.MIB))
        real_rst = S.build_lan_restore_script
        for skip_fw in (False, True):
            tgt.write_bytes(b"\xAA" * (size + 8 * S.MIB))
            ranges = S.plan_restore(mm, skip_fw)
            with patch.object(S, "build_lan_restore_script",
                              lambda **kw: real_rst(**dict(kw, dev=str(tgt)))), patch("builtins.print"):
                ok, why = SO.restore_lan(dev, sess, base, store / "snap1", mm, ranges, have_sha)
            dev.wait()
            t = tgt.read_bytes()
            if skip_fw:
                check(f"skip_fw restore OK ({why})", ok)
                check("skip_fw: firmware region untouched", t[S.FW_START:S.FW_END] == b"\xAA" * (S.FW_END - S.FW_START))
                check("skip_fw: everything else identical", t[:S.FW_START] == disk[:S.FW_START]
                      and t[S.FW_END:size] == disk[S.FW_END:])
            else:
                check(f"full restore OK ({why})", ok)
                check("full restore: byte-identical", t[:size] == disk)
            check("bytes past the image end untouched", t[size:] == b"\xAA" * (8 * S.MIB))

        # tampered chunk (after the PC-side check) -> caught by the manifest hash
        f = store / "snap1" / mm["chunks"][1]["file"]
        orig = f.read_bytes()
        with S.open_reader(f, mm["chunks"][1]["compression"]) as fh:
            raw = bytearray(fh.read())
        raw[100] ^= 0xFF
        with S.open_writer(f, mm["chunks"][1]["compression"]) as fh:
            fh.write(bytes(raw))
        with patch.object(S, "build_lan_restore_script",
                          lambda **kw: real_rst(**dict(kw, dev=str(tgt)))), patch("builtins.print"):
            ok, why = SO.restore_lan(dev, sess, base, store / "snap1", mm, S.plan_restore(mm, False), have_sha)
        dev.wait()
        check("tampered chunk caught during restore", not ok and "manifest" in why)
        f.write_bytes(orig)
        srv.shutdown()

    # ------------------------------------------------------------------------
    comps = [("gz", {"gzip"}), ("raw", set())]
    if shutil.which("zstd"):
        comps.insert(0, ("zst", {"zstd"}))
    for comp, extra in comps:
        section(f"[{name}] USB snapshot + restore ({comp})")
        emmc = work / f"uemmc_{name}_{comp}.img"
        size = 40 * S.MIB + 1536
        disk = make_disk(emmc, size)
        stick = work / f"stick_{name}_{comp}"
        (stick / S.USB_ROOT).mkdir(parents=True)
        dev = FakeDev(shell, envv)
        have = {"sha256sum", "head", "tail", "gzip1"} | extra
        p = probe_for(size, have)
        with patch.object(S, "CHUNK_BYTES", CHUNK), patch.object(SO, "USB_POLL", 0.2), \
             patch.object(S, "EMMC_DEV", str(emmc)), patch.object(S, "USB_MOUNT", str(stick)), \
             patch("builtins.print"):
            rs, rf = S.build_usb_snapshot_script, S.build_usb_finalize_script
            with patch.object(S, "build_usb_snapshot_script", lambda **kw: rs(**dict(kw, dev=str(emmc), mount=str(stick)))), \
                 patch.object(S, "build_usb_finalize_script", lambda **kw: rf(**dict(kw, mount=str(stick)))):
                m = SO.snapshot_usb(dev, cap("u1", p))
        d_snap = stick / S.USB_ROOT / "u1"
        check("snapshot published on the stick", m is not None and (d_snap / "manifest.json").is_file())
        check(f"device chose {comp}", m and m["chunks"][0]["compression"] == comp)
        r = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=d_snap, capture_output=True)
        check("SHA256SUMS on the stick checks out", r.returncode == 0)
        check("chunk hashes = the disk", m and m["chunks"][1]["sha256"] == hashlib.sha256(disk[CHUNK:]).hexdigest())
        rc = S.build_usb_cat_script
        with patch.object(S, "build_usb_cat_script", lambda **kw: rc(**dict(kw, mount=str(stick)))):
            m2, env2 = SO.usb_read_manifest(dev, "u1")
        check("manifest read back via the cat script", m2 == m)
        check("env read back", env2 == env)
        mounted = S.parse_usb_mount(subprocess.run(
            shell + ["-c", "echo USB_PART=/dev/sda1\necho USB_FREE_KB=99\necho SNAP_0=u1\necho SNAP_1=zz\necho MOUNT=done\n"],
            env=envv, capture_output=True, text=True).stdout)
        check("mount listing parsed in order", mounted["snapshots"] == ["u1", "zz"] and mounted["free_kb"] == 99)

        tgt = work / f"utgt_{name}_{comp}.img"
        rr = S.build_usb_restore_script
        for skip_fw in (False, True):
            tgt.write_bytes(b"\x55" * (size + 4 * S.MIB))
            with patch.object(SO, "USB_POLL", 0.2), patch("builtins.print"), \
                 patch.object(S, "build_usb_restore_script",
                              lambda **kw: rr(**dict(kw, dev=str(tgt), mount=str(stick)))):
                ok, why = SO.restore_usb(dev, "u1", m2, S.plan_restore(m2, skip_fw))
            t = tgt.read_bytes()
            check(f"USB restore OK (skip_fw={skip_fw}) {why}", ok)
            if skip_fw:
                check("firmware region untouched", t[S.FW_START:S.FW_END] == b"\x55" * (S.FW_END - S.FW_START))
                check("rest identical", t[:S.FW_START] == disk[:S.FW_START] and t[S.FW_END:size] == disk[S.FW_END:])
            else:
                check("byte-identical", t[:size] == disk)

        # corrupt a file on the stick -> nothing written
        f = d_snap / m2["chunks"][1]["file"]
        b = bytearray(f.read_bytes()); b[len(b) // 3] ^= 0x01; f.write_bytes(bytes(b))
        tgt.write_bytes(b"\x55" * (size + 4 * S.MIB))
        with patch.object(SO, "USB_POLL", 0.2), patch("builtins.print"), \
             patch.object(S, "build_usb_restore_script",
                          lambda **kw: rr(**dict(kw, dev=str(tgt), mount=str(stick)))):
            ok, why = SO.restore_usb(dev, "u1", m2, S.plan_restore(m2, False))
        check("corrupted stick file: restore refused", not ok and "SHA256SUMS" in why)
        check("corrupted stick file: target untouched", tgt.read_bytes() == b"\x55" * (size + 4 * S.MIB))

        # label already taken -> finalize refuses to overwrite
        (stick / S.USB_ROOT / "u1.partial").mkdir()
        out = dev.run_script(S.build_usb_finalize_script(label="u1", manifest_text="{}", text_files={},
                                                         mount=str(stick)))
        check("existing snapshot never overwritten", "FINAL=exists" in out and (d_snap / "emmc.000.img" if comp == "raw" else d_snap / m2["chunks"][0]["file"]).exists())


# ----------------------------------------------------------------------------
# Single-pass LAN snapshot: each chunk read ONCE and hashed on the device while
# it streams (tee into a FIFO). Edge cases, under every shell.
# Named limits and sizes for these tests (CLAUDE.md section 2: no magic numbers).
SP_DISK_BYTES = 40 * S.MIB + 1536     # fake eMMC: two chunks, the last one short (sector-sized tail)
SP_DISK_CHUNKS = 2                    # chunks in SP_DISK_BYTES at CHUNK size
SP_NOR_BYTES = 2 * S.MIB              # fake NOR flash for the read-only dump
SHA256_HEX_LEN = 64                   # hex digits in a SHA-256 digest
STUB_MODE = 0o755                     # stub tools must be executable
CUT_AFTER_BYTES = S.MIB               # the fake curl cuts an upload after this many bytes
CURL_RC_RECV_ERROR = 56               # curl exit code: failure receiving network data
CUT_SETTLE_TIMEOUT_S = 10             # snapshot_lan's upload-settle wait, shortened for the cut test
CUT_CHUNK_TIMEOUT_S = 60              # snapshot_lan's per-chunk report wait, shortened for the cut test
FAIL_FAST_S = 30                      # a cut upload must be refused within this time
DEVICE_EXIT_TIMEOUT_S = FAIL_FAST_S   # wait for the device script to exit (proves no hang)
REASON_PREVIEW_CHARS = 60             # failure reason shown in a check's label
LOCALHOST = "127.0.0.1"
EMMC_32GB_BYTES = 31_268_536_320      # a representative 32 GB eMMC
EMMC_32GB_CHUNKS = 30                 # chunks such an eMMC is split into
SERIAL_SCRIPT_MAX_BYTES = 4096        # largest safe device script (a 13 KB one broke on hardware)
SINGLE_PASS_MAX_GROWTH_BYTES = 256    # single pass may add at most this much over the read-back script
SINGLE_PASS_HASH_STEP = "| fsha)"      # the single-pass hash read, once per loop body
SIZE_TEST_BASE_URL = "http://192.168.168.89:8080"   # realistic URL length for the size check
TOKEN_HEX_LEN = 32                    # snapshot session token length (hex digits)
CHUNK_NUMBERS = range(1, SP_DISK_CHUNKS + 1)        # 1-based chunk numbers in progress text
NORF = work / "sp_nor.bin"
NORF.write_bytes(os.urandom(SP_NOR_BYTES))
REAL_CURL = shutil.which("curl")
SP_ALL = {"sha256sum", "openssl"}
SSL_STUB = {   # fake openssl: real hash, OpenSSL 3 / 1.1 output; or a wrong hash
    "ssl3": '#!/bin/sh\nh=$(sha256sum | cut -d" " -f1)\necho "SHA2-256(stdin)= $h"\n',
    "ssl1": '#!/bin/sh\nh=$(sha256sum | cut -d" " -f1)\necho "(stdin)= $h"\n',
    "sslbad": '#!/bin/sh\ncat >/dev/null\necho "SHA2-256(stdin)= ' + "0" * SHA256_HEX_LEN + '"\n',
}
BROKEN_TOOL = "#!/bin/sh\nexit 1\n"   # stub: a tool that is present but fails at once
# fake curl: an upload gets cut after CUT_AFTER_BYTES (curl rc CURL_RC_RECV_ERROR), everything else is real
CUT_CURL = (f'#!/bin/sh\ncase "$*" in\n  *upload*) head -c {CUT_AFTER_BYTES} | {REAL_CURL} "$@"; '
            f'exit {CURL_RC_RECV_ERROR};;\n'
            'esac\nexec ' + REAL_CURL + ' "$@"\n')


def stub_dir(tag, files):
    d = work / f"stub_{tag}"
    d.mkdir(exist_ok=True)
    for n, body in files.items():
        (d / n).write_text(body)
        (d / n).chmod(STUB_MODE)
    return d


def with_path(envv, d):
    return dict(envv, PATH=f"{d}:{envv['PATH']}")


def chunk_hashes(disk):
    return [hashlib.sha256(disk[:CHUNK]).hexdigest(), hashlib.sha256(disk[CHUNK:]).hexdigest()]


def lan_snapshot(shell, envv, have, label, *, thorough=False, nor_dump=False, **so_patches):
    """snapshot_lan end to end on a fresh 40 MiB "eMMC". Returns (manifest, disk, store, rec):
    rec = single_pass flags passed to the script builder, spinner texts, steps, _say lines,
    elapsed seconds, and whether the device script exited (no hang)."""
    size = SP_DISK_BYTES
    emmc = work / f"emmc_{label}.img"
    disk = make_disk(emmc, size)
    store = work / f"store_{label}"
    store.mkdir()
    port = free_port()
    sess = SnapshotSession()
    srv = start_snapshot_server(LOCALHOST, port, sess)
    dev = FakeDev(shell, envv)
    rec = {"single": [], "spin": [], "steps": [], "say": []}
    real_lan, real_spin, real_step = S.build_lan_snapshot_script, SO.with_spinner, SO.step

    def lan(**kw):
        rec["single"].append(kw.get("single_pass"))
        return real_lan(**dict(kw, dev=str(emmc)))

    def spin(fn, *a, message="", **kw):
        rec["spin"].append(message)
        return real_spin(fn, *a, message=message, **kw)

    def stp(n, text, ok, reason=""):
        rec["steps"].append((text, ok, reason))
        return real_step(n, text, ok, reason)

    patches = [patch.object(S, "CHUNK_BYTES", CHUNK), patch.object(S, "EMMC_DEV", str(emmc)),
               patch.object(S, "NOR_DEV", str(NORF)), patch.object(S, "build_lan_snapshot_script", lan),
               patch.object(SO, "with_spinner", spin), patch.object(SO, "step", stp),
               patch.object(SO, "_say", lambda m="": rec["say"].append(m)), patch("builtins.print")]
    patches += [patch.object(SO, k, v) for k, v in so_patches.items()]
    t0 = time.time()
    with contextlib.ExitStack() as es:
        for pt in patches:
            es.enter_context(pt)
        m = SO.snapshot_lan(dev, sess, f"http://{LOCALHOST}:{port}", store,
                            cap(label, probe_for(size, have), thorough=thorough, nor_dump=nor_dump))
    rec["elapsed"] = time.time() - t0
    rec["exited"] = True
    for pr in dev.procs:
        try:
            pr.wait(timeout=DEVICE_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            rec["exited"] = False
            pr.kill()
    srv.shutdown()
    return m, disk, store, rec


def nothing_published(store, label):
    return not (store / label).exists() and not (store / f"{label}.partial").exists()


def reason(rec):
    return " | ".join(r for _, ok, r in rec["steps"] if not ok)


for name, shell, envv in shells():
    section(f"[{name}] single pass: fsha (OpenSSL 3 / 1.1 output, no openssl)")
    want = hashlib.sha256(b"abc").hexdigest()
    variants = [("OpenSSL 3 output", with_path(envv, stub_dir("ssl3", {"openssl": SSL_STUB["ssl3"]}))),
                ("OpenSSL 1.1 output", with_path(envv, stub_dir("ssl1", {"openssl": SSL_STUB["ssl1"]})))]
    if name == "sh":
        if shutil.which("openssl"):
            variants.append(("real openssl", envv))
        nossl = work / "nossl_bin"
        if not nossl.exists():
            nossl.mkdir()
            for tool in ("sha256sum", "cut", "tr"):
                (nossl / tool).symlink_to(shutil.which(tool))
        variants.append(("no openssl -> sha256sum", dict(envv, PATH=str(nossl))))
    else:
        variants.append(("no openssl -> sha256sum", envv))   # BusyBox PATH has no openssl
    for what, e in variants:
        r = subprocess.run(shell + ["-c", S._RD_FN + "printf abc | fsha\n"], env=e,
                           capture_output=True, text=True)
        check(f"fsha, {what}: bare hex digest", r.stdout.strip() == want)

    section(f"[{name}] single pass: normal run with NOR dump, progress text")
    m, disk, store, rec = lan_snapshot(shell, envv, SP_ALL, f"spnor_{name}", nor_dump=True)
    check("single pass used", rec["single"] == [True])
    check("snapshot published, hashes = the disk", m is not None and [c["sha256"] for c in m["chunks"]] == chunk_hashes(disk))
    ok, _, _ = S.verify_store_snapshot(store / f"spnor_{name}")
    check("published snapshot verifies (SHA256SUMS)", ok)
    check("NOR dump after the single-pass loop: stored, hash recorded",
          m is not None and m["nor_dump"]["sha256"] == hashlib.sha256(NORF.read_bytes()).hexdigest()
          and (store / f"spnor_{name}" / "nor.bin").read_bytes() == NORF.read_bytes())
    check("spinner says 'hashing'", rec["spin"] == [f"Chunk {i}/{SP_DISK_CHUNKS}: eMMC -> PC, hashing" for i in CHUNK_NUMBERS])
    check("steps say 'captured and verified'",
          [s for s, ok, _ in rec["steps"] if s.startswith("Chunk")] ==
          [f"Chunk {i}/{SP_DISK_CHUNKS} captured and verified" for i in CHUNK_NUMBERS])
    check("no fallback warning", not any("missing on the device" in s for s in rec["say"]))

    section(f"[{name}] single pass: wrong device hash is refused")
    e = with_path(envv, stub_dir("sslbad", {"openssl": SSL_STUB["sslbad"]}))
    m, disk, store, rec = lan_snapshot(shell, e, SP_ALL, f"bad_{name}")
    check("single pass used", rec["single"] == [True])
    check("snapshot refused", m is None)
    check("reason: device hash does not match", "device hash does not match" in reason(rec))
    check("nothing published, no .partial", nothing_published(store, f"bad_{name}"))
    check("device script exited", rec["exited"])

    section(f"[{name}] single pass: upload cut mid-stream (no hang)")
    e = with_path(envv, stub_dir(f"cut_{name}", {"curl": CUT_CURL}))
    m, disk, store, rec = lan_snapshot(shell, e, SP_ALL, f"cut_{name}",
                                       UPLOAD_SETTLE_TIMEOUT=CUT_SETTLE_TIMEOUT_S, CHUNK_TIMEOUT=CUT_CHUNK_TIMEOUT_S)
    check("snapshot refused", m is None)
    check(f"reason names the short transfer ({reason(rec)[:REASON_PREVIEW_CHARS]})",
          "transfer failed" in reason(rec) and f"rc={CURL_RC_RECV_ERROR}" in reason(rec)
          and f"{CUT_AFTER_BYTES} of {CHUNK}" in reason(rec))
    check("device script exited (curl/fsha did not hang)", rec["exited"])
    check(f"failed fast ({rec['elapsed']:.1f}s < {FAIL_FAST_S}s)", rec["elapsed"] < FAIL_FAST_S)
    check("nothing published, no .partial", nothing_published(store, f"cut_{name}"))

    section(f"[{name}] single pass: no tee / FIFO (both stalled chunk 0 on hardware)")
    e = with_path(envv, stub_dir("brokentools", {"tee": BROKEN_TOOL, "mkfifo": BROKEN_TOOL}))
    m, disk, store, rec = lan_snapshot(shell, e, SP_ALL, f"notee_{name}")
    check("broken tee + mkfifo on the device: single pass used, snapshot OK, hashes = the disk",
          rec["single"] == [True] and m is not None
          and [c["sha256"] for c in m["chunks"]] == chunk_hashes(disk))
    check("device script exited", rec["exited"])

    section(f"[{name}] mode selection: thorough + fallbacks to read-back")
    m, disk, store, rec = lan_snapshot(shell, envv, SP_ALL, f"thor_{name}", thorough=True)
    check("thorough: read-back used although single pass is possible", rec["single"] == [False])
    check("thorough: snapshot OK, hashes = the disk", m is not None and [c["sha256"] for c in m["chunks"]] == chunk_hashes(disk))
    check("thorough: spinner says 'read back'", rec["spin"] == [f"Chunk {i}/{SP_DISK_CHUNKS}: eMMC -> PC, read back" for i in CHUNK_NUMBERS])
    check("thorough: steps say 'captured and read-back verified'",
          [s for s, ok, _ in rec["steps"] if s.startswith("Chunk")] ==
          [f"Chunk {i}/{SP_DISK_CHUNKS} captured and read-back verified" for i in CHUNK_NUMBERS])
    check("thorough: no fallback warning (it was asked for)", not any("missing on the device" in s for s in rec["say"]))
    m, disk, store, rec = lan_snapshot(shell, envv, set(), f"no_openssl_{name}")
    check("no openssl + sha256sum: falls back to read-back, with a warning",
          rec["single"] == [False] and any("missing on the device" in s for s in rec["say"]))
    check("no openssl + sha256sum: snapshot still OK",
          m is not None and [c["sha256"] for c in m["chunks"]] == chunk_hashes(disk))
    m, disk, store, rec = lan_snapshot(shell, envv, {"sha256sum"}, f"noossl_{name}")
    check("sha256sum only (no openssl): single pass still used",
          rec["single"] == [True] and m is not None and [c["sha256"] for c in m["chunks"]] == chunk_hashes(disk))


# ----------------------------------------------------------------------------
section(f"single pass: device script stays serial-safe ({EMMC_32GB_CHUNKS} chunks, 32 GB eMMC)")
ch = S.plan_chunks(EMMC_32GB_BYTES)
check(f"32 GB eMMC = {len(ch)} chunks", len(ch) == EMMC_32GB_CHUNKS)
built = {}
for sp in (False, True):
    for hs in (True, False):
        for nor in (False, True):
            built[(sp, hs, nor)] = S.build_lan_snapshot_script(
                base_url=SIZE_TEST_BASE_URL, token="0" * TOKEN_HEX_LEN, chunks=ch,
                have_sha=hs, nor=nor, single_pass=sp)
big = max(len(b.encode()) for b in built.values())
check(f"every variant <= {SERIAL_SCRIPT_MAX_BYTES} B (largest {big} B; a 13 KB script broke on hardware)",
      big <= SERIAL_SCRIPT_MAX_BYTES)
grow = len(built[(True, True, True)].encode()) - len(built[(False, True, True)].encode())
check(f"single pass adds < {SINGLE_PASS_MAX_GROWTH_BYTES} B over read-back ({grow} B)",
      grow < SINGLE_PASS_MAX_GROWTH_BYTES)
longest = {k: max(len(x) for x in b.splitlines()) for k, b in built.items()}
check(f"single pass: longest line not longer than read-back's ({longest[(True, True, True)]} chars)",
      longest[(True, True, True)] <= longest[(False, True, True)])
check("single pass script = one loop over the plan file, not unrolled per chunk",
      built[(True, True, False)].count(SINGLE_PASS_HASH_STEP) == 1)
check("single pass script uses no tee and no FIFO",
      not any(w in built[(True, True, False)].split() for w in ("tee", "mkfifo")))


# ----------------------------------------------------------------------------
section("[sh] read-only filesystem check (real e2fsck when installed)")
if shutil.which("mkfs.ext4") and shutil.which("e2fsck"):
    fsd = work / "fsdev"
    fsd.mkdir()
    part = fsd / "mmcblk0p1"
    part.write_bytes(b"\0" * (8 * S.MIB))
    subprocess.run(["mkfs.ext4", "-q", "-F", str(part)], check=True, capture_output=True)
    (fsd / "mmcblk0p2").write_bytes(b"\0" * S.MIB)
    parts_file = work / "partitions"
    parts_file.write_text("major minor  #blocks  name\n 179 0 100 mmcblk0\n 179 1 8192 mmcblk0p1\n"
                          " 179 2 1024 mmcblk0p2\n")
    script = S.build_fs_check_script(partitions=str(parts_file), dev_dir=str(fsd))
    before = part.read_bytes()
    out = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                         env=dict(os.environ, PATH=os.environ["PATH"] + ":/usr/sbin:/sbin")).stdout
    fs = S.parse_fs_check(S.parse_kv(out))
    check("clean ext4 -> ok, unknown partition -> not checked",
          fs.get(1, {}).get("level") == "ok" and fs.get(2, {}).get("type") == "other")
    check("the check never writes", part.read_bytes() == before)
    b = bytearray(before); b[1024 + 0x58: 1024 + 0x60] = b"\xff" * 8; part.write_bytes(bytes(b))  # break superblock
    out = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                         env=dict(os.environ, PATH=os.environ["PATH"] + ":/usr/sbin:/sbin")).stdout
    check("damaged ext4 -> error", S.parse_fs_check(S.parse_kv(out)).get(1, {}).get("level") == "error")
else:
    print("  SKIP: mkfs.ext4/e2fsck not installed")

# ----------------------------------------------------------------------------
section("[sh] inventory script (fake sysfs, NOR + eMMC images with U-Boot strings)")
inv_root = work / "inv"
(inv_root / "net" / "eth0").mkdir(parents=True)
(inv_root / "net" / "eth0" / "address").write_text("e8:f6:d7:00:17:df\n")
(inv_root / "net" / "eth3").mkdir()
(inv_root / "net" / "eth3" / "address").write_text("e8:f6:d7:00:17:e2\n")
(inv_root / "emmcdev").mkdir()
(inv_root / "emmcdev" / "life_time").write_text("0x01 0x02\n")
(inv_root / "emmcdev" / "name").write_text("DG4032\n")
(inv_root / "proc_mtd").write_text('dev:    size   erasesize  name\nmtd0: 04000000 00010000 "nor"\n')
nor_img = bytearray(4 * S.MIB); nor_img[S.MIB:S.MIB + 60] = b"\0U-Boot 2025.04-mono (Jun 01 2025 - 12:00:00 +0000)\0".ljust(60, b"\0")
(inv_root / "mtd0").write_bytes(bytes(nor_img))
emmc_img = bytearray(33 * S.MIB); emmc_img[5 * S.MIB:5 * S.MIB + 60] = b"\0U-Boot 2025.10-mono (Oct 01 2025 - 12:00:00 +0000)\0".ljust(60, b"\0")
(inv_root / "emmc").write_bytes(bytes(emmc_img))
script = I.build_inventory_script(dev=str(inv_root / "emmc"), emmc_sysfs=str(inv_root / "emmcdev"),
                                  net_sysfs=str(inv_root / "net"), proc_mtd=str(inv_root / "proc_mtd"),
                                  mtd_dev_dir=str(inv_root))
for name, shell_, envv_ in shells():
    out = subprocess.run(shell_ + ["-c", script], env=envv_, capture_output=True, text=True).stdout
    inv = I.parse_inventory(S.parse_kv(out), ntp_synced=False)
    check(f"[{name}] U-Boot versions found in NOR and the eMMC firmware region",
          I.nor_uboot(inv).startswith("U-Boot 2025.04") and I.emmc_uboot(inv).startswith("U-Boot 2025.10"))
    check(f"[{name}] MTD table, eMMC wear, all MACs, date",
          inv["mtd"] and inv["emmc"].get("life_time") == "0x01 0x02"
          and inv["macs"] == {"eth0": "e8:f6:d7:00:17:df", "eth3": "e8:f6:d7:00:17:e2"}
          and inv["date_utc"].endswith("Z") and inv["complete"])

# ----------------------------------------------------------------------------
# Whole journeys (run_snapshot / run_restore) on /bin/sh: prompts, auto-
# snapshot, firmware-region skip, clone mode, rollback after a bad write.
section("[sh] journeys end to end (LAN + USB)")
shell, envv = ["/bin/sh"], dict(os.environ)
size = 40 * S.MIB + 1536

# fake NOR env MTD partition (sysfs name + device file) and a flashcp stub
mtd_sys, mtd_dev, stub = work / "sys_mtd", work / "dev_mtd", work / "stubbin"
(mtd_sys / "mtd3").mkdir(parents=True)
(mtd_sys / "mtd3" / "name").write_text("uboot-env\n")
mtd_dev.mkdir()
stub.mkdir()
(stub / "flashcp").write_text('#!/bin/sh\ncp "$1" "$2"\n')
(stub / "flashcp").chmod(0o755)
envv["PATH"] = f"{stub}:{envv['PATH']}"
nor_env_dev = mtd_dev / "mtd3"
ENV_A = env_blob({"bootcmd": "run openwrt || run recovery", "ethaddr": "e8:f6:d7:00:17:df"})
ENV_B = env_blob({"bootcmd": "run opnsense || run recovery", "ethaddr": "e8:f6:d7:00:17:df"})
nor_env_dev.write_bytes(ENV_A)


asked = []   # prompts of the last journey (run_journey clears it)


def answers(*a):
    it = iter(a)

    def ask(prompt):
        asked.append(prompt)
        return next(it)
    return ask


real_inv_script = I.build_inventory_script


def run_journey(fn, emmc, probe_mac, inputs, have_extra=(), **kw):
    asked.clear()
    dev = FakeDev(shell, envv)
    net = work / f"net_{probe_mac.replace(':', '')}" / "eth0"
    net.mkdir(parents=True, exist_ok=True)
    (net / "address").write_text(probe_mac + "\n")
    pr = probe_for(size, {"sha256sum", "head", "tail", "gzip", "gzip1", "od", "flashcp"} | set(have_extra))
    pr["mac"] = probe_mac
    port = free_port()
    with patch.object(S, "CHUNK_BYTES", CHUNK), patch.object(S, "EMMC_DEV", str(emmc)), \
         patch.object(SO, "USB_POLL", 0.2), patch.object(S, "MTD_SYSFS", str(mtd_sys)), \
         patch.object(S, "MTD_DEV_DIR", str(mtd_dev)), \
         patch.object(I, "build_inventory_script",
                      lambda **k: real_inv_script(dev=str(emmc), net_sysfs=str(net.parent),
                                                  proc_mtd=str(work / "no_mtd"))), \
         patch.object(SO, "reach_recovery", lambda *a, **k: (dev, {"flipped": False, "env": env,
                                                                  "uboot_dip": "EMMC"})), \
         patch.object(SO, "probe_device", lambda d: pr), patch("builtins.print"):
        ok = fn(port=port, host_ip="127.0.0.1", http_port=port, setup_network=lambda d: True,
                ask=answers(*inputs), **kw)
    dev.wait()
    return ok, dev


emmc = work / "j_emmc.img"
disk = make_disk(emmc, size)
store = work / "j_store"
ok, _ = run_journey(SO.run_snapshot, emmc, "e8:f6:d7:00:17:df",
                    ["1", "prod", ""], transfer="lan", store=store)
check("run_snapshot (LAN) succeeds", ok and (store / "prod" / "manifest.json").is_file())
check("LAN snapshot asks 'Thorough verify'", any("Thorough verify" in q for q in asked))
mprod = json.loads((store / "prod" / "manifest.json").read_text())
check("manifest records DIP and OS", mprod["boot_dip"] == "EMMC" and mprod["os_label"] == "openwrt")
check("NOR U-Boot env saved with the snapshot (raw + readable)",
      (store / "prod" / "nor_env.bin").read_bytes() == ENV_A
      and mprod["nor_env"]["sha256"] == hashlib.sha256(ENV_A).hexdigest()
      and "bootcmd=run openwrt" in (store / "prod" / "nor_env.txt").read_text())
check("health check recorded: GPT valid", mprod["health"]["status"] == "ok"
      and mprod["health"]["gpt"]["status"] == "ok" and len(mprod["health"]["gpt"]["partitions"]) == 1)
ok_p, _, _ = S.verify_store_snapshot(store / "prod")
check("NOR env files covered by SHA256SUMS", ok_p and "nor_env.bin" in
      S.parse_sha256sums((store / "prod" / "SHA256SUMS").read_text()))

# a broken GPT -> the user is asked; "n" cancels the snapshot
bad = bytearray(disk); bad[512 + 20] ^= 0xFF; emmc.write_bytes(bytes(bad))
ok, _ = run_journey(SO.run_snapshot, emmc, "e8:f6:d7:00:17:df", ["1", "n"], transfer="lan", store=store)
check("GPT error -> asked, declined -> no snapshot", not ok and len(S.list_snapshots(store)) == 1)
emmc.write_bytes(disk)

# change the eMMC (another OS), then restore 'prod' with auto-snapshot
other = bytearray(disk); other[S.FW_END + 10:S.FW_END + 5000] = os.urandom(4990); emmc.write_bytes(bytes(other))
ok, _ = run_journey(SO.run_restore, emmc, "e8:f6:d7:00:17:df", ["1", "yes"], transfer="lan",
                    store=store, folder=store / "prod", manifest=mprod, auto_snapshot=True)
check("run_restore (LAN) succeeds", ok)
check("eMMC is back to 'prod'", emmc.read_bytes() == disk)
autos = [n for n, m in S.list_snapshots(store) if m.get("auto")]
check("auto-snapshot of the replaced eMMC was kept", len(autos) == 1)
ok_a, _, ma = S.verify_store_snapshot(store / autos[0])
check("auto-snapshot is complete and verified", ok_a and ma["chunks"][1]["sha256"] ==
      hashlib.sha256(bytes(other[CHUNK:])).hexdigest())

# clone mode onto "another board" with a bigger eMMC: firmware region kept, firmware update run
big = work / "j_big.img"
big.write_bytes(b"\x11" * (size + 2 * S.MIB))
calls = []
with patch("mono_imager.recovery_orchestrator.run_firmware_update",
           lambda d, **k: calls.append(1) or True):
    real_size = size
    size = size + 2 * S.MIB
    ok, _ = run_journey(SO.run_restore, big, "e8:f6:d7:00:99:99", ["1", "y", "yes"], transfer="lan",
                        store=store, folder=store / "prod", manifest=mprod, auto_snapshot=False)
    size = real_size
b = big.read_bytes()
check("clone restore succeeds", ok)
check("clone: firmware region of the target kept", b[S.FW_START:S.FW_END] == b"\x11" * (S.FW_END - S.FW_START))
check("clone: OS data cloned", b[:S.FW_START] == disk[:S.FW_START] and b[S.FW_END:size] == disk[S.FW_END:])
check("clone: firmware update provisioned the board", calls == [1])

ok, _ = run_journey(SO.run_restore, big, "e8:f6:d7:00:99:99", ["1", "n"], transfer="lan",
                    store=store, folder=store / "prod", manifest=mprod, auto_snapshot=False)
check("clone declined -> nothing written, journey fails", not ok)

# optional read-only NOR dump
norf = work / "j_nor.bin"
norf.write_bytes(os.urandom(2 * S.MIB))
sess = SnapshotSession(); port = free_port()
srv = start_snapshot_server("127.0.0.1", port, sess)
dev = FakeDev(shell, envv)
emmc.write_bytes(disk)
with patch.object(S, "CHUNK_BYTES", CHUNK), patch.object(S, "EMMC_DEV", str(emmc)), \
     patch.object(S, "NOR_DEV", str(norf)), patch("builtins.print"):
    mn = SO.snapshot_lan(dev, sess, f"http://127.0.0.1:{port}", store,
                         cap("withnor", probe_for(size, {"sha256sum"}), nor_dump=True))
dev.wait(); srv.shutdown()
check("NOR dump stored and listed in the manifest", mn and mn["nor_dump"]["sha256"] ==
      hashlib.sha256(norf.read_bytes()).hexdigest() and (store / "withnor" / "nor.bin").is_file())
check("NOR dump is never part of a restore plan",
      all(r["chunk"] in {c["index"] for c in mn["chunks"]} for r in S.plan_restore(mn, False)))
emmc.write_bytes(bytes(other))

# a bad write, then roll back to the auto-snapshot
emmc.write_bytes(bytes(other))
real_write = SO._write
state = {"n": 0}


def flaky(d, transfer, sess, base_url, folder, label, manifest, ranges, have):
    state["n"] += 1
    if state["n"] == 1:
        emmc_b = bytearray(emmc.read_bytes()); emmc_b[S.FW_END:S.FW_END + 100] = b"X" * 100
        emmc.write_bytes(bytes(emmc_b))   # half-written eMMC
        return False, "simulated re-hash mismatch"
    return real_write(d, transfer, sess, base_url, folder, label, manifest, ranges, have)


with patch.object(SO, "_write", flaky):
    ok, _ = run_journey(SO.run_restore, emmc, "e8:f6:d7:00:17:df", ["1", "yes", "b"], transfer="lan",
                        store=store, folder=store / "prod", manifest=mprod, auto_snapshot=True)
check("bad write -> rollback to the auto-snapshot restores the previous eMMC",
      emmc.read_bytes() == bytes(other) and state["n"] == 2)
check("journey still reports the failed restore", not ok)

# NOR env differs -> offered after the write; "y" writes it, "n" leaves NOR alone
emmc.write_bytes(bytes(other)); nor_env_dev.write_bytes(ENV_B)
ok, _ = run_journey(SO.run_restore, emmc, "e8:f6:d7:00:17:df", ["1", "yes", "n"], transfer="lan",
                    store=store, folder=store / "prod", manifest=mprod, auto_snapshot=False)
check("NOR env declined -> NOR untouched", ok and nor_env_dev.read_bytes() == ENV_B)
emmc.write_bytes(bytes(other))
ok, _ = run_journey(SO.run_restore, emmc, "e8:f6:d7:00:17:df", ["1", "yes", "y"], transfer="lan",
                    store=store, folder=store / "prod", manifest=mprod, auto_snapshot=False)
check("NOR env accepted -> written and read back", ok and nor_env_dev.read_bytes() == ENV_A
      and emmc.read_bytes() == disk)

# boot whatever the DIP position: eMMC env (OpenWRT) copied into NOR's env on opt-in
ENV_EMMC = env_blob({"bootcmd": "run openwrt || run recovery",
                     "openwrt": "run emmc_load && booti ${kernel_addr_r} - ${fdt_addr_r}",
                     "emmc_load": "ext4load mmc 0:1 ${kernel_addr_r} /Image",
                     "kernel_addr_r": "0x82000000", "fdt_addr_r": "0x88000000",
                     "recovery": "EMMC-RECOVERY"})
ENV_NOR = env_blob({"bootcmd": "run opnsense || run recovery", "recovery": "NOR-RECOVERY",
                    "ethaddr": "e8:f6:d7:00:17:df", "kernel_addr_r": "0x82000000"})
dual = bytearray(disk); dual[C.UBOOT_ENV_EMMC_OFFSET:C.UBOOT_ENV_EMMC_OFFSET + len(ENV_EMMC)] = ENV_EMMC
emmc.write_bytes(bytes(dual)); nor_env_dev.write_bytes(ENV_NOR)
ok, _ = run_journey(SO.run_snapshot, emmc, "e8:f6:d7:00:17:df", ["1", "dual", ""], transfer="lan", store=store)
mdual = json.loads((store / "dual" / "manifest.json").read_text())
check("snapshot keeps both env sets + inventory.txt",
      ok and (store / "dual" / "emmc_env.txt").is_file() and (store / "dual" / "inventory.txt").is_file()
      and "bootcmd=run openwrt" in (store / "dual" / "emmc_env.txt").read_text()
      and mdual["inventory"]["macs"] == {"eth0": "e8:f6:d7:00:17:df"})
emmc.write_bytes(bytes(other))
ok, _ = run_journey(SO.run_restore, emmc, "e8:f6:d7:00:17:df", ["1", "yes", "y"], transfer="lan",
                    store=store, folder=store / "dual", manifest=mdual, auto_snapshot=False)
nv = E.parse(nor_env_dev.read_bytes())
check("dual boot: OS boot vars copied into the NOR env, NOR keeps its own recovery + ethaddr",
      ok and nv and nv.vars["bootcmd"] == "run openwrt || run recovery" and "emmc_load" in nv.vars
      and nv.vars["recovery"] == "NOR-RECOVERY" and nv.vars["ethaddr"] == "e8:f6:d7:00:17:df")
check("dual boot: eMMC restored byte-identical", emmc.read_bytes() == bytes(dual))
nor_env_dev.write_bytes(ENV_A)

# USB journey: snapshot to the stick, then restore it
stick = work / "j_stick"
(stick / S.USB_ROOT).mkdir(parents=True)
emmc.write_bytes(disk)
mount_out = {"part": "/dev/sda1", "fs": "vfat", "free_kb": 10 ** 9, "snapshots": [], "ok": True}
with patch.object(S, "USB_MOUNT", str(stick)), patch.object(SO, "usb_mount", lambda d, mode: dict(mount_out)):
    ok, _ = run_journey(SO.run_snapshot, emmc, "e8:f6:d7:00:17:df", ["2", "usbprod", ""],
                        transfer="usb", store=store)
    check("run_snapshot (USB) succeeds", ok and (stick / S.USB_ROOT / "usbprod" / "SHA256SUMS").is_file())
    check("USB snapshot does not ask 'Thorough verify' (LAN only)", not any("Thorough" in q for q in asked))
    check("USB: NOR env saved on the stick and in SHA256SUMS",
          (stick / S.USB_ROOT / "usbprod" / "nor_env.bin").read_bytes() == nor_env_dev.read_bytes()
          and subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=stick / S.USB_ROOT / "usbprod",
                             capture_output=True).returncode == 0)
    emmc.write_bytes(bytes(other))
    mount_out["snapshots"] = ["usbprod"]
    ok, _ = run_journey(SO.run_restore, emmc, "e8:f6:d7:00:17:df", ["1", "1", "yes"], transfer="usb",
                        store=store, auto_snapshot=True)
    check("run_restore (USB) succeeds", ok and emmc.read_bytes() == disk)
    check("USB auto-snapshot kept on the stick",
          any(p.name.startswith("auto-") for p in (stick / S.USB_ROOT).iterdir()))


# ----------------------------------------------------------------------------
section("[sh] journeys: 'Thorough verify' answer, auto-snapshot uses single pass")
seen = []
real_lan_j = S.build_lan_snapshot_script


def rec_lan(**kw):
    seen.append(kw.get("single_pass"))
    return real_lan_j(**kw)


nor_env_dev.write_bytes(ENV_A)
emmc.write_bytes(disk)
for ans, single in (("", True), ("y", False), ("Y", False)):
    seen.clear()
    lab = f"j_sp_{'thorough' if ans else 'fast'}_{len(S.list_snapshots(store))}"
    with patch.object(S, "build_lan_snapshot_script", rec_lan):
        ok, _ = run_journey(SO.run_snapshot, emmc, "e8:f6:d7:00:17:df", ["1", lab, ans], transfer="lan",
                            store=store, have_extra=("openssl",))
    ok_v, _, mj = S.verify_store_snapshot(store / lab)
    check(f"answer {ans!r}: single pass {single}, snapshot OK and verified",
          ok and seen == [single] and ok_v and [c["sha256"] for c in mj["chunks"]] == chunk_hashes(disk))

emmc.write_bytes(bytes(other))
seen.clear()
with patch.object(S, "build_lan_snapshot_script", rec_lan):
    ok, _ = run_journey(SO.run_restore, emmc, "e8:f6:d7:00:17:df", ["1", "yes"], transfer="lan",
                        store=store, folder=store / "prod", manifest=mprod, auto_snapshot=True,
                        have_extra=("openssl",))
check("restore with auto-snapshot succeeds, eMMC = 'prod'", ok and emmc.read_bytes() == disk)
check("auto-snapshot used single pass", seen == [True])
check("auto-snapshot did not ask 'Thorough verify'", not any("Thorough" in q for q in asked))
autos = sorted(n for n, m in S.list_snapshots(store) if m.get("auto"))
ok_a, _, ma = S.verify_store_snapshot(store / autos[-1])
check("that auto-snapshot holds the replaced eMMC, verified",
      ok_a and [c["sha256"] for c in ma["chunks"]] == chunk_hashes(bytes(other)))

shutil.rmtree(work, ignore_errors=True)
print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
