"""
mono-imager: eMMC snapshot / restore — shared building blocks (#29).

Pure logic, no serial I/O: on-disk format (manifest.json + SHA256SUMS +
1 GiB chunk files), host-side compression, restore planning, and the
POSIX/BusyBox shell scripts that run in recovery Linux. The flows that
drive a real device live in snapshot_orchestrator.py; the HTTP side
(upload/serve) in snapshot_server.py.

Snapshot = the WHOLE eMMC, bit for bit, no partition parsing, no OS
assumptions (blind capture). It is stored as chunks so that:
  - a FAT32 USB stick works (every file stays far below 4 GiB),
  - each chunk is hashed and read-back verified on its own,
  - a restore can skip the firmware region (4 KiB .. 32 MiB) by range.

Snapshot folder layout (same for LAN and USB):

    <label>/manifest.json      identity, sizes, per-chunk SHA-256 (raw), health check
    <label>/SHA256SUMS         sha256 of every file in the folder
    <label>/uboot_env.txt      env of the U-Boot that was interrupted (info only)
    <label>/nor_env.bin        NOR U-Boot env region (8 KiB, read-only copy)
    <label>/nor_env.txt        the same, readable
    <label>/emmc.000.img.zst   chunk 0 (raw bytes 0 .. 1 GiB), compressed
    <label>/emmc.001.img.zst   ...
    <label>/nor.bin            optional read-only NOR dump (never restored)

Compression: LAN snapshots are compressed on the PC (the device only
streams raw dd output), so recovery Linux needs no zstd. USB snapshots
are compressed on the device with the best tool it has (zstd > gzip >
none). Every format can be restored over LAN; over USB the device needs
the matching decompressor.

Author:  H.A. Hermsen
License: GPLv3
"""

import datetime as _dt
import gzip
import hashlib
import io
import json
import re
import struct
import zlib
from pathlib import Path
from typing import Optional

from mono_imager import constants as C
from mono_imager import uboot_env

# --- Constants -----------------------------------------------------------------

FORMAT_VERSION = 1

EMMC_DEV = C.EMMC_DEV
NOR_DEV  = C.NOR_MTD_DEV
USB_DEV  = C.USB_DEV
USB_MOUNT = C.USB_MOUNT
USB_ROOT  = "mono-snapshots"         # folder on the stick holding snapshots

SECTOR      = C.SECTOR_BYTES
MIB         = C.MIB
CHUNK_BYTES = C.GIB                  # 1 GiB raw per chunk file (FAT32-safe)
FW_START    = C.EMMC_FW_START        # Mono eMMC firmware region (docs.mono.si):
FW_END      = C.EMMC_FW_END          #   4 KiB .. 32 MiB

# NOR layout (docs.mono.si "NOR Flash Memory Map"; magic confirmed on
# hardware by openwrt_lan.py's NOR scan): DTB at 5 MiB, gzip Image with
# built-in initramfs at 10 MiB. Used to chainload NOR recovery Linux
# from whatever U-Boot we interrupted (eMMC or NOR) — RAM only, no saveenv.
NOR_DTB_OFF    = C.NOR_DTB_OFFSET
NOR_DTB_LEN    = C.NOR_DTB_LEN
NOR_KERNEL_OFF = C.NOR_KERNEL_OFFSET
NOR_KERNEL_LEN = C.NOR_KERNEL_LEN
RAM_KERNEL     = C.UBOOT_LOAD_ADDR
RAM_DTB        = C.UBOOT_DTB_ADDR
NOR_ENV_OFF    = hex(C.NOR_ENV_OFFSET)              # NOR U-Boot env (docs: 3 MiB)
NOR_ENV_SIZES  = (hex(C.UBOOT_ENV_SIZE), "0x10000", "0x20000")   # tried in order, CRC-checked

COMPRESSIONS = ("zst", "gz", "raw")

LOG = "/tmp/mono_imager_snap.log"

ZSTD_LEVEL = 3                       # PC-side zstd: fast, good ratio on eMMC data
ZSTD_ALL_CORES = -1
GZIP_LEVEL = 1                       # PC-side gzip fallback: speed over ratio
SHA256_HEX_LEN = 64
GPT_SIGNATURE = b"EFI PART"          # at LBA 1
HAVE_PREFIX = "HAVE_"

# NOR U-Boot environment (read-only copy kept with every snapshot; written
# back only on an explicit opt-in during a same-board restore)
NOR_ENV_MTD_NAME = "uboot-env"       # MTD partition name (recovery Linux)
MTD_SYSFS = "/sys/class/mtd"         # where MTD partitions are listed with their names
MTD_DEV_DIR = "/dev"
NOR_ENV_SIZE = C.UBOOT_ENV_SIZE      # same region as `firmware update --preserve-env`
NOR_ENV_FILE = "nor_env.bin"
NOR_ENV_TXT = "nor_env.txt"          # the same, readable (info only)
UBOOT_ENV_TXT = "uboot_env.txt"      # env of the U-Boot that was interrupted
NOR_ENV_TMP = "/tmp/mono_imager_nor_env.bin"
ENV_UPLOAD_TMP = "/tmp/mono_imager_env_new.bin"
EMMC_ENV_SECTOR = C.UBOOT_ENV_EMMC_OFFSET // C.SECTOR_BYTES   # eMMC env region, in sectors
EMMC_ENV_SECTORS = C.UBOOT_ENV_SIZE // C.SECTOR_BYTES
PRINTF_CHUNK = 64                    # bytes per printf line when sending a blob over serial

# GPT (UEFI spec) — health check before a snapshot
GPT_HEADER_LBA = 1
GPT_HEADER_MIN_SIZE = 92
GPT_HEADER_FMT = "<8sIIIIQQQQ16sQIII"
GPT_STD_SECTORS = 34                 # LBA 0..33: MBR, header, 128 x 128 B entries
GPT_ENTRY_FMT = "<16s16sQQQ72s"
GPT_UNUSED_TYPE = b"\0" * 16
GPT_NAME_ENCODING = "utf-16-le"

# Filesystem magic (offsets inside a partition)
EXT_MAGIC_OFFSET = 1080              # superblock (1024) + s_magic (56)
EXT_MAGIC = b"\x53\xef"              # 0xEF53, little endian
FAT_LABEL_OFFSETS = (54, 82)         # "FAT12/16" / "FAT32" type strings
E2FSCK_CLEAN = 0                     # e2fsck -n: 0 clean, 4 = errors found, 8 = could not check
SNAP_PREFIX = "SNAP_"

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# --- Host-side compression -------------------------------------------------------

def _zstd_backend():
    """'stdlib' (Python 3.14+ compression.zstd), 'zstandard' (pip), or None."""
    try:
        import compression.zstd  # noqa: F401
        return "stdlib"
    except ImportError:
        pass
    try:
        import zstandard  # noqa: F401
        return "zstandard"
    except ImportError:
        return None


def host_compression() -> str:
    """Best compression the PC can write: 'zst' if available, else 'gz'."""
    return "zst" if _zstd_backend() else "gz"


class _ZstdWriter(io.RawIOBase):
    """Write-only file object compressing with the 'zstandard' package."""
    def __init__(self, path):
        import zstandard
        self._f = open(path, "wb")
        self._w = zstandard.ZstdCompressor(level=ZSTD_LEVEL, threads=ZSTD_ALL_CORES).stream_writer(self._f, closefd=False)

    def writable(self):
        return True

    def write(self, b):
        self._w.write(b)
        return len(b)

    def close(self):
        if not self.closed:
            self._w.flush(1)  # FLUSH_FRAME
            self._w.close()
            self._f.close()
        super().close()


def open_writer(path, compression: str):
    """Binary write handle that compresses with `compression`."""
    if compression == "raw":
        return open(path, "wb")
    if compression == "gz":
        return gzip.open(path, "wb", compresslevel=GZIP_LEVEL)
    if compression == "zst":
        backend = _zstd_backend()
        if backend == "stdlib":
            from compression import zstd
            return zstd.ZstdFile(path, "wb", level=ZSTD_LEVEL)
        if backend == "zstandard":
            return _ZstdWriter(path)
        raise RuntimeError("zstd is not available on this PC (pip install zstandard)")
    raise ValueError(f"unknown compression {compression!r}")


def open_reader(path, compression: str):
    """Binary read handle that decompresses `compression` (multi-frame/member safe)."""
    if compression == "raw":
        return open(path, "rb")
    if compression == "gz":
        return gzip.open(path, "rb")
    if compression == "zst":
        backend = _zstd_backend()
        if backend == "stdlib":
            from compression import zstd
            return zstd.ZstdFile(path, "rb")
        if backend == "zstandard":
            import zstandard
            return zstandard.ZstdDecompressor().stream_reader(
                open(path, "rb"), read_across_frames=True, closefd=True)
        raise RuntimeError("this snapshot is zstd-compressed but zstd is not available "
                           "on this PC (pip install zstandard)")
    raise ValueError(f"unknown compression {compression!r}")


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(MIB), b""):
            h.update(block)
    return h.hexdigest()


# --- Chunk / range planning --------------------------------------------------------

def plan_chunks(emmc_bytes: int, chunk_bytes: Optional[int] = None) -> list:
    """[(index, offset, length)] covering the whole eMMC."""
    chunk_bytes = chunk_bytes or CHUNK_BYTES
    if emmc_bytes <= 0 or emmc_bytes % SECTOR:
        raise ValueError(f"eMMC size {emmc_bytes} is not a positive multiple of {SECTOR}")
    if chunk_bytes < FW_END or chunk_bytes % MIB:
        raise ValueError("chunk size must be a whole number of MiB and >= 32 MiB")
    out, off, i = [], 0, 0
    while off < emmc_bytes:
        n = min(chunk_bytes, emmc_bytes - off)
        out.append((i, off, n))
        off += n
        i += 1
    return out


def chunk_file(index: int, compression: str) -> str:
    return f"emmc.{index:03d}.img" + ("" if compression == "raw" else f".{compression}")


def plan_restore(manifest: dict, skip_fw: bool) -> list:
    """
    Ranges to write, as dicts {chunk, off, len, abs} (off relative to the
    chunk, abs = absolute eMMC byte offset). skip_fw=True leaves the
    firmware region (4 KiB .. 32 MiB) untouched: chunk 0 becomes
    0..4 KiB + 32 MiB..end. Used for clone mode (always) and for a
    same-board restore whose firmware region is already identical.
    """
    ranges = []
    for c in manifest["chunks"]:
        i, off, n = c["index"], c["offset"], c["bytes"]
        if skip_fw and off == 0:
            ranges.append({"chunk": i, "off": 0, "len": FW_START, "abs": 0})
            if n > FW_END:
                ranges.append({"chunk": i, "off": FW_END, "len": n - FW_END, "abs": FW_END})
        else:
            ranges.append({"chunk": i, "off": 0, "len": n, "abs": off})
    return ranges


# --- Manifest / SHA256SUMS ---------------------------------------------------------

def default_label(os_label: str, now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    base = re.sub(r"[^a-z0-9]+", "-", (os_label or "emmc").lower()).strip("-") or "emmc"
    return f"{base}-{now:%Y%m%d-%H%M}"


def valid_label(label: str) -> bool:
    return bool(_LABEL_RE.match(label or "")) and not label.endswith(".partial")


def detect_os_label(env: Optional[dict]) -> str:
    """Display-only OS guess from the U-Boot env (bootcmd and friends)."""
    if not env:
        return "emmc"
    text = " ".join(f"{k}={v}" for k, v in env.items()).lower()
    boot = (env.get("bootcmd") or "").lower()
    for name in ("vyos", "opnsense", "openwrt", "armbian"):
        if name in boot:
            return name
    if "extlinux" in boot or "armbian" in text:
        return "armbian"
    for name in ("vyos", "opnsense", "openwrt"):
        if name in text:
            return name
    return "emmc"


def normalize_mac(mac: Optional[str]) -> str:
    return (mac or "").strip().lower()


def same_board(manifest: dict, identity: dict) -> bool:
    a = normalize_mac(manifest.get("device", {}).get("mac"))
    b = normalize_mac(identity.get("mac"))
    return bool(a) and a == b


def mac_match(manifest: dict, identity: dict, target_macs: Optional[dict]) -> tuple:
    """
    ("same" | "partial" | "other", snapshot MACs, this board's MACs).
    Compares every port's MAC recorded in the snapshot's inventory with the
    ones seen now; falls back to the primary MAC for snapshots without one.
    "partial" = some ports match, some do not (e.g. a port renamed) — still
    this board, but worth a look.
    """
    snap = {normalize_mac(m) for m in (((manifest.get("inventory") or {}).get("macs")) or {}).values()}
    here = {normalize_mac(m) for m in (target_macs or {}).values()}
    snap.discard("")
    here.discard("")
    if snap and here:
        common = snap & here
        if common and snap == here:
            return "same", snap, here
        if common:
            return "partial", snap, here
        return "other", snap, here
    return ("same" if same_board(manifest, identity) else "other"), snap, here


def build_manifest(*, label: str, identity: dict, emmc_bytes: int, chunks: list,
                   fw_sha256: str, boot_sha256: str, os_label: str, boot_dip: str,
                   transfer: str, tool_version: str, auto: bool = False,
                   nor: Optional[dict] = None, has_env: bool = False,
                   nor_env: Optional[dict] = None, health: Optional[dict] = None,
                   inventory: Optional[dict] = None,
                   now: Optional[_dt.datetime] = None) -> dict:
    """
    nor_env: {"file", "sha256", "bytes", "vars_file"} of the NOR U-Boot env
    region saved with the snapshot (None if it could not be read).
    health: result of the pre-snapshot GPT / filesystem check.
    """
    now = now or _dt.datetime.now().astimezone()
    return {
        "format": FORMAT_VERSION,
        "tool": f"mono-imager {tool_version}",
        "label": label,
        "created": now.isoformat(timespec="seconds"),
        "auto": bool(auto),
        "transfer": transfer,
        "os_label": os_label,
        "boot_dip": boot_dip,
        "device": {
            "mac": normalize_mac(identity.get("mac")),
            "serial": identity.get("serial", ""),
            "model": identity.get("model", ""),
            "uboot_version": identity.get("uboot_version", ""),
        },
        "emmc": {
            "bytes": emmc_bytes,
            "fw_region": {"start": FW_START, "end": FW_END, "sha256": fw_sha256},
            "boot_sector_sha256": boot_sha256,
        },
        "chunk_bytes": CHUNK_BYTES,
        "chunks": chunks,
        "uboot_env_file": UBOOT_ENV_TXT if has_env else None,
        "nor_env": nor_env,
        "nor_dump": nor,
        "health": health,
        "inventory": inventory,
    }


def validate_manifest(m: dict) -> list:
    """Problems with a manifest (empty list = usable)."""
    errs = []
    if m.get("format") != FORMAT_VERSION:
        errs.append(f"unsupported snapshot format {m.get('format')!r}")
    try:
        total = int(m["emmc"]["bytes"])
        chunks = m["chunks"]
    except (KeyError, TypeError, ValueError):
        return errs + ["manifest is missing emmc.bytes or chunks"]
    expected = 0
    for i, c in enumerate(chunks):
        if c.get("index") != i or c.get("offset") != expected:
            errs.append(f"chunk {i} is out of order")
            break
        if c.get("compression") not in COMPRESSIONS:
            errs.append(f"chunk {i}: unknown compression {c.get('compression')!r}")
        if not re.fullmatch(rf"[0-9a-f]{{{SHA256_HEX_LEN}}}", c.get("sha256", "")):
            errs.append(f"chunk {i}: missing SHA-256")
        if not re.fullmatch(r"emmc\.\d{3}\.img(\.(zst|gz))?", c.get("file", "")):
            errs.append(f"chunk {i}: bad file name {c.get('file')!r}")
        expected += int(c.get("bytes", 0))
    if expected != total:
        errs.append(f"chunks cover {expected} bytes, manifest says {total}")
    if not chunks or chunks[0].get("bytes", 0) < FW_END:
        errs.append("first chunk is smaller than the firmware region")
    return errs


def manifest_json(m: dict) -> str:
    return json.dumps(m, indent=1, sort_keys=False) + "\n"


def format_env(env: Optional[dict]) -> str:
    return "".join(f"{k}={v}\n" for k, v in sorted((env or {}).items()))


def parse_env_file(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def sha256sums_text(entries: dict) -> str:
    """{file: sha256} -> GNU/BusyBox `sha256sum -c` compatible text."""
    return "".join(f"{h}  {name}\n" for name, h in sorted(entries.items()))


def parse_sha256sums(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-f]{64}) [ *](.+)$", line.strip())
        if m:
            out[m.group(2)] = m.group(1)
    return out


# --- Host-side snapshot store (LAN) --------------------------------------------------

def default_store() -> Path:
    return Path.home() / "mono-imager-snapshots"


def list_snapshots(store: Path) -> list:
    """[(label, manifest)] of complete snapshots, newest first."""
    out = []
    if not store.is_dir():
        return out
    for d in store.iterdir():
        mf = d / "manifest.json"
        if not d.is_dir() or d.name.endswith(".partial") or not mf.is_file():
            continue
        try:
            out.append((d.name, json.loads(mf.read_text())))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda x: x[1].get("created", ""), reverse=True)
    return out


def verify_store_snapshot(folder: Path) -> tuple:
    """
    (ok, reason, manifest) — the PC-side check that runs before a single
    byte is sent: manifest parses and is consistent, every file listed in
    SHA256SUMS exists and matches, and every chunk is listed.
    """
    try:
        sums = parse_sha256sums((folder / "SHA256SUMS").read_text())
        m = json.loads((folder / "manifest.json").read_text())
    except (OSError, ValueError) as e:
        return False, f"cannot read snapshot: {e}", None
    errs = validate_manifest(m)
    if errs:
        return False, "; ".join(errs), m
    needed = [c["file"] for c in m["chunks"]] + ["manifest.json"]
    for name in needed:
        if name not in sums:
            return False, f"{name} is not listed in SHA256SUMS", m
    for name, h in sums.items():
        p = folder / name
        if not p.is_file():
            return False, f"{name} is missing", m
        if sha256_file(p) != h:
            return False, f"{name} does not match SHA256SUMS (corrupted?)", m
    return True, "", m


def snapshot_has_gpt(folder: Path, manifest: dict) -> bool:
    c = manifest["chunks"][0]
    with open_reader(folder / c["file"], c["compression"]) as f:
        head = f.read(SECTOR + len(GPT_SIGNATURE))
    return head[SECTOR:SECTOR + len(GPT_SIGNATURE)] == GPT_SIGNATURE


# --- Device-side shell (recovery Linux: BusyBox ash) ----------------------------------

def parse_kv(text: str) -> dict:
    """KEY=VALUE lines -> dict (first occurrence wins)."""
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    return out


def _q(s: str) -> str:
    """Single-quote for sh."""
    return "'" + str(s).replace("'", "'\\''") + "'"


# rd START LEN — print exactly LEN bytes of $DEV starting at START.
# Big aligned reads in 1 MiB blocks, the (sector-sized) tail in 512 B,
# so a range never reads past the image end on a bigger target eMMC.
_RD_FN = r"""rd() {
  _s=$1; _n=$2; _m=1048576
  if [ $((_s % _m)) -eq 0 ] && [ "$_n" -ge "$_m" ]; then
    _q=$((_n / _m)); _r=$((_n % _m))
    dd if="$DEV" bs=$_m skip=$((_s / _m)) count=$_q 2>/dev/null
    if [ "$_r" -gt 0 ]; then
      dd if="$DEV" bs=512 skip=$(((_s + _q * _m) / 512)) count=$((_r / 512)) 2>/dev/null
    fi
  else
    dd if="$DEV" bs=512 skip=$((_s / 512)) count=$((_n / 512)) 2>/dev/null
  fi
  return 0
}
dropc() { sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; return 0; }
sha() { sha256sum | cut -d' ' -f1; }
# fast SHA-256: openssl uses the CPU's SHA instructions (~10x sha256sum, LS1046A)
fsha() {
  if command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 | cut -d= -f2 | tr -d ' '
  else sha256sum | cut -d' ' -f1; fi
}
"""


def build_probe_script(dev: Optional[str] = None, usb_dev: Optional[str] = None) -> str:
    """Identity + capabilities of the device, as KEY=VALUE lines."""
    dev = dev or EMMC_DEV
    usb_dev = usb_dev or USB_DEV
    b = dev.split("/")[-1]
    tools = ["sha256sum", "zstd", "gzip", "curl", "sgdisk", "head", "tail",
             "mount", "df", "flashcp", "base64", "od", "e2fsck", "fsck.vfat", "dosfsck",
             "openssl", "tee", "mkfifo"]
    s = [
        f'if [ -b {dev} ]; then echo "EMMC_BYTES=$(( $(cat /sys/block/{b}/size) * 512 ))"; '
        'else echo "EMMC_BYTES=0"; fi',
        f'if grep -q "^{dev}" /proc/mounts; then echo "EMMC_MOUNTED=1"; else echo "EMMC_MOUNTED=0"; fi',
        'echo "MEM_KB=$(grep MemTotal /proc/meminfo | tr -s " " | cut -d" " -f2)"',
        'echo "BOOT_MEDIUM=$(grep -o "boot_medium=[a-z]*" /proc/cmdline | cut -d= -f2)"',
        'for i in eth0 eth1 eth2 eth3 eth4; do',
        '  if [ -r /sys/class/net/$i/address ]; then echo "MAC=$(cat /sys/class/net/$i/address)"; break; fi',
        'done',
        'e=/sys/bus/i2c/devices/3-0050/eeprom',
        '[ -r $e ] || { echo 24c32 0x50 > /sys/bus/i2c/devices/i2c-3/new_device; sleep 1; } 2>/dev/null',
        'if [ -r $e ]; then',
        '  echo "MODEL=$(dd if=$e bs=1 skip=8 count=32 2>/dev/null | tr -d "\\000")"',
        '  echo "SERIAL=$(dd if=$e bs=1 skip=40 count=24 2>/dev/null | tr -d "\\000")"',
        'fi',
        f'if [ -b {usb_dev} ]; then echo "USB_PRESENT=1"; else echo "USB_PRESENT=0"; fi',
        '[ -c /dev/mtd0 ] && echo "NOR_PRESENT=1"',
    ]
    for t in tools:
        s.append(f'command -v {t} >/dev/null 2>&1 && echo "HAVE_{t.upper().replace(".", "_")}=1"')
    s.append('echo x | gzip -1 >/dev/null 2>&1 && echo "HAVE_GZIP1=1"')
    s.append('echo "PROBE=done"')
    return "\n".join(s) + "\n"


def parse_probe(text: str) -> dict:
    kv = parse_kv(text)
    have = {k[len(HAVE_PREFIX):].lower() for k, v in kv.items() if k.startswith(HAVE_PREFIX) and v == "1"}
    try:
        emmc = int(kv.get("EMMC_BYTES", "0"))
    except ValueError:
        emmc = 0
    try:
        mem_kb = int(kv.get("MEM_KB", "0"))
    except ValueError:
        mem_kb = 0
    return {
        "ok": kv.get("PROBE") == "done",
        "emmc_bytes": emmc,
        "emmc_mounted": kv.get("EMMC_MOUNTED") == "1",
        "mem_kb": mem_kb,
        "boot_medium": kv.get("BOOT_MEDIUM", ""),
        "mac": normalize_mac(kv.get("MAC")),
        "model": kv.get("MODEL", ""),
        "serial": kv.get("SERIAL", ""),
        "usb_present": kv.get("USB_PRESENT") == "1",
        "nor_present": kv.get("NOR_PRESENT") == "1",
        "have": have,
    }


def device_compression(have: set) -> str:
    """Best compression the DEVICE can do for a USB snapshot."""
    if "zstd" in have:
        return "zst"
    if "gzip" in have:
        return "gz"
    return "raw"


def _comp_cmd(compression: str, have: set) -> str:
    if compression == "zst":
        return "zstd -q -c -3"
    if compression == "gz":
        return "gzip -1 -c" if "gzip1" in have else "gzip -c"
    return "cat"


def _decomp_cmd(compression: str) -> str:
    return {"zst": "zstd -q -d -c", "gz": "gzip -d -c", "raw": "cat"}[compression]


def can_decompress(compression: str, have: set) -> bool:
    return compression == "raw" or (compression == "zst" and "zstd" in have) \
        or (compression == "gz" and "gzip" in have)


def _report(url: str, body: str) -> str:
    """POST a small KEY=VALUE body to the PC's /report endpoint."""
    return (f"printf '%s\\n' {body} | curl -s -H 'Expect:' -X POST --data-binary @- "
            f"{_q(url)} >/dev/null 2>&1")


# -- LAN ------------------------------------------------------------------------------

PLAN = "/tmp/mono_imager_plan.txt"   # one line per chunk / range, read by a shell loop

# The scripts below are LOOPS over a short plan file, never one block per
# chunk: CONFIRMED ON HARDWARE, a 13 KB script (30 chunks unrolled) did not
# survive the serial heredoc write (run_script/launch_script's byte check
# failed). Plan + loop keeps every script around 2 KB, whatever the eMMC size.


def _plan(rows: list) -> list:
    """Lines writing `rows` (lists of fields) to PLAN with a quoted heredoc."""
    body = "\n".join(" ".join(str(x) for x in r) for r in rows)
    return [f"cat > {PLAN} <<'MI_PLAN_EOF'", body, "MI_PLAN_EOF"]


# wr START: dd parameters (bs, seek) for a write starting at START (aligned 1 MiB or 512 B)
_WR_FN = r"""wr() {
  if [ $(($1 % 1048576)) -eq 0 ]; then echo "bs=1048576 seek=$(($1 / 1048576))"
  else echo "bs=512 seek=$(($1 / 512))"; fi
}
"""

# LAN helpers — short lines: long lines wrap on the serial console and the
# wrapped echo garbles (see SerialDevice.run_script()).
#   put PATH       stdin -> PC (PUT $U/PATH)        rep STEP K=V...  report to the PC
_NET_FN = f"""put() {{ curl -s -S -H 'Expect:' -T - "$U/$1" -o /dev/null 2>>{LOG}; }}
rep() {{
  _st=$1; shift
  printf '%s\\n' "$@" | curl -s -H 'Expect:' -X POST --data-binary @- \\
    "$U/report?step=$_st" >/dev/null 2>&1
}}
"""


def build_lan_snapshot_script(*, base_url: str, token: str, chunks: list,
                              have_sha: bool, nor: bool, dev: Optional[str] = None,
                              single_pass: bool = False) -> str:
    """
    Stream every chunk RAW to the PC (the PC compresses + hashes), then
    re-read it from the eMMC with the page cache dropped (a real media
    read) and hash it: on the device when it has sha256sum, otherwise
    by streaming it to the PC's hash-only endpoint. Optional read-only
    NOR dump at the end. One report per chunk (step=snap_<i>).

    single_pass: read each chunk once and hash it on the device while it
    streams (tee into a FIFO) instead of a second media read. Proves the
    PC got exactly what was read; ~2x faster. Needs tee, mkfifo and
    openssl or sha256sum on the device (the caller checks).
    """
    dev = dev or EMMC_DEV
    s = [f"DEV={dev}", f"U={_q(base_url)}", f"T={_q('token=' + token)}", _RD_FN, _NET_FN]
    s += _plan([[i, off, n] for i, off, n in chunks])
    s += [f'rd {FW_START} {FW_END - FW_START} | put "verify?$T&tag=fw"',
          f'rd 0 {FW_START} | put "verify?$T&tag=boot"',
          "while read -r i off n; do"]
    if single_pass:
        s += ["  dropc; rm -f /tmp/mi_f /tmp/mi_v; mkfifo /tmp/mi_f",
              "  fsha < /tmp/mi_f > /tmp/mi_v &",
              '  rd $off $n | tee /tmp/mi_f | put "upload?$T&chunk=$i"; urc=$?',
              "  wait; vs=$(cat /tmp/mi_v)"]
    else:
        s += ['  rd $off $n | put "upload?$T&chunk=$i"; urc=$?',
              "  dropc"]
        if have_sha:
            s.append("  vs=$(rd $off $n | sha)")
        else:
            s.append('  rd $off $n | put "verify?$T&tag=c$i"; vs=host')
    s += ['  rep snap_$i "CHUNK=$i" "UP_RC=$urc" "VSHA=$vs"',
          f"done < {PLAN}"]
    if nor:
        s += [f'cat {NOR_DEV} | put "nor?$T"; nrc=$?',
              'rep snap_nor "NOR_RC=$nrc"']
    return "\n".join(s) + "\n"


def build_lan_hash_script(*, base_url: str, token: str, start: int, length: int,
                          tag: str, dev: Optional[str] = None) -> str:
    """Hash one eMMC range on the PC (e.g. the firmware region)."""
    dev = dev or EMMC_DEV
    return "\n".join([
        f"DEV={dev}", _RD_FN, "dropc",
        f"rd {start} {length} | curl -s -H 'Expect:' -T - "
        f"{_q(base_url + '/verify?token=' + token + '&tag=' + tag)} -o /dev/null",
        "rc=$?",
        _report(base_url + f"/report?step=hash_{tag}", '"RC=$rc"'),
    ]) + "\n"


def build_lan_restore_script(*, base_url: str, token: str, ranges: list,
                             have_sha: bool, dev: Optional[str] = None) -> str:
    """
    Write each range from the PC (served decompressed), then drop the page
    cache and re-read it from the eMMC to hash it (on the device, or on the
    PC when there is no sha256sum). Stops at the first transfer/dd error.
    One report per range (step=rst_<k>).
    """
    dev = dev or EMMC_DEV
    s = [f"DEV={dev}", f"U={_q(base_url)}", f"T={_q('token=' + token)}",
         _RD_FN, _WR_FN, _NET_FN]
    s += _plan([[k, r["chunk"], r["off"], r["len"], r["abs"]] for k, r in enumerate(ranges)])
    s += ["while read -r k c o n a; do",
          '  q="image?$T&k=$k&chunk=$c&off=$o&len=$n"',
          f'  {{ curl -s -S "$U/$q" 2>>{LOG}; echo $? > /tmp/mi_crc; }} |',
          f'    dd of="$DEV" $(wr $a) conv=notrunc 2>>{LOG}',
          "  drc=$?; crc=$(cat /tmp/mi_crc)",
          "  dropc"]
    if have_sha:
        s.append("  vs=$(rd $a $n | sha)")
    else:
        s.append('  rd $a $n | put "verify?$T&tag=r$k"; vs=host')
    s += ['  rep rst_$k "RANGE=$k" "CURL_RC=$crc" "DD_RC=$drc" "VSHA=$vs"',
          '  [ "$crc" = 0 ] && [ "$drc" = 0 ] || exit 1',
          f"done < {PLAN}"]
    return "\n".join(s) + "\n"


# -- USB ------------------------------------------------------------------------------

def build_usb_mount_script(*, mode: str, usb_dev: Optional[str] = None,
                           mount: Optional[str] = None) -> str:
    """
    Mount a partition of the stick at `mount`. mode='restore': the first
    one holding mono-snapshots/; mode='snapshot': that one too if present,
    else the first writable one. Lists complete snapshots (SNAP_<n>=label).
    """
    mount = mount or USB_MOUNT
    usb_dev = usb_dev or USB_DEV
    want_dir = f'[ -d {mount}/{USB_ROOT} ]'
    s = [
        f"mkdir -p {mount}",
        f"umount {mount} 2>/dev/null",
        f'n=$(basename {usb_dev})',
        'C=$(grep -o "$n[0-9][0-9]*$" /proc/partitions | sort -u; echo $n)',
        "found=",
        "for p in $C; do",
        f"  mount /dev/$p {mount} 2>/dev/null || continue",
        f"  if {want_dir}; then found=/dev/$p; break; fi",
        f"  umount {mount} 2>/dev/null",
        "done",
    ]
    if mode == "snapshot":
        s += [
            'if [ -z "$found" ]; then',
            "  for p in $C; do",
            f"    mount /dev/$p {mount} 2>/dev/null || continue",
            f"    if touch {mount}/.mi_w 2>/dev/null; then rm -f {mount}/.mi_w; found=/dev/$p; break; fi",
            f"    umount {mount} 2>/dev/null",
            "  done",
            "fi",
        ]
    s += [
        'if [ -n "$found" ]; then',
        '  echo "USB_PART=$found"',
        f'  echo "USB_FS=$(grep " {mount} " /proc/mounts | cut -d" " -f3)"',
        f'  echo "USB_FREE_KB=$(df -k {mount} | tail -n 1 | tr -s " " | cut -d" " -f4)"',
        "  i=0",
        f"  for m in {mount}/{USB_ROOT}/*/manifest.json; do",
        '    [ -f "$m" ] || continue',
        '    d=$(dirname "$m"); d=$(basename "$d")',
        '    case "$d" in *.partial) continue;; esac',
        '    echo "SNAP_$i=$d"; i=$((i + 1))',
        "  done",
        "else",
        '  echo "USB_PART="',
        "fi",
        'echo "MOUNT=done"',
    ]
    return "\n".join(s) + "\n"


def parse_usb_mount(text: str) -> dict:
    kv = parse_kv(text)
    snaps = [v for _, v in sorted((int(k[len(SNAP_PREFIX):]), v) for k, v in kv.items()
                                  if re.fullmatch(r"SNAP_\d+", k))]
    try:
        free_kb = int(kv.get("USB_FREE_KB", "0") or 0)
    except ValueError:
        free_kb = 0
    return {"part": kv.get("USB_PART", ""), "fs": kv.get("USB_FS", ""),
            "free_kb": free_kb, "snapshots": snaps, "ok": kv.get("MOUNT") == "done"}


def _bg(body: list, done_key: str) -> str:
    """Run body in the background (so the serial shell stays free for
    polling), always finishing with <done_key>=1 in the log."""
    inner = "\n".join(body)
    return (f": > {LOG}\n( ( set +e\n{inner}\n)\necho \"{done_key}=1\" >> {LOG}\n) "
            f">/dev/null 2>&1 &\necho STARTED=1\n")


def build_usb_snapshot_script(*, label: str, chunks: list, compression: str,
                              have: set, nor: bool, dev: Optional[str] = None,
                              mount: Optional[str] = None) -> str:
    """
    Background job: per chunk, eMMC -> compressor -> file on the stick,
    then (page cache dropped) hash the decompressed file AND a fresh eMMC
    read; both must match. Results go to the log as C_<i>=<rc> <h_file> <h_emmc>.
    """
    dev = dev or EMMC_DEV
    mount = mount or USB_MOUNT
    d = f"{mount}/{USB_ROOT}/{label}.partial"
    comp, dec = _comp_cmd(compression, have), _decomp_cmd(compression)
    pre = _plan([[i, off, n, chunk_file(i, compression)] for i, off, n in chunks])
    b = [f"DEV={dev}", _RD_FN, f"D={_q(d)}", 'rm -rf "$D"; mkdir -p "$D" || exit 1',
         f'echo "FW_SHA=$(rd {FW_START} {FW_END - FW_START} | sha)" >> {LOG}',
         f'echo "BOOT_SHA=$(rd 0 {FW_START} | sha)" >> {LOG}',
         "while read -r i off n f; do",
         f'  rd $off $n | {comp} > "$D/$f"; c=$?',
         "  dropc",
         f'  h1=$({dec} < "$D/$f" | sha); h2=$(rd $off $n | sha)',
         f'  echo "C_$i=$c $h1 $h2" >> {LOG}',
         f'  [ "$c" = 0 ] && [ "$h1" = "$h2" ] || {{ echo "FAILED=$i" >> {LOG}; exit 1; }}',
         f"done < {PLAN}"]
    if nor:
        b += [f'cat {NOR_DEV} > "$D"/nor.bin; echo "NOR=$? $(sha < "$D"/nor.bin)" >> {LOG}']
    b.append('sync')
    return "\n".join(pre) + "\n" + _bg(b, "SNAP_DONE")


def build_usb_finalize_script(*, label: str, manifest_text: str, text_files: dict,
                              mount: Optional[str] = None) -> str:
    """Write manifest + env text files, build SHA256SUMS over every file on
    the device, publish the folder."""
    mount = mount or USB_MOUNT
    base = f"{mount}/{USB_ROOT}"
    d, final = f"{base}/{label}.partial", f"{base}/{label}"
    s = [f"cd {_q(d)} || exit 1",
         "cat > manifest.json <<'MI_MANIFEST_EOF'", manifest_text.rstrip("\n"), "MI_MANIFEST_EOF"]
    for i, (name, text) in enumerate(sorted(text_files.items())):
        if text:
            tag = f"MI_TEXT_{i}_EOF"
            s += [f"cat > {_q(name)} <<'{tag}'", text.rstrip("\n"), tag]
    s += ['F=; for f in *; do [ "$f" = SHA256SUMS ] || F="$F $f"; done',
          "sha256sum $F > SHA256SUMS",
          "rc=$?", "sync", "cd /",
          f"if [ -e {_q(final)} ]; then echo FINAL=exists; "
          f"elif [ $rc = 0 ] && mv {_q(d)} {_q(final)}; then sync; echo FINAL=ok; fi",
          "echo FINAL_DONE=1"]
    return "\n".join(s) + "\n"


def build_usb_nor_env_save_script(*, label: str, mount: Optional[str] = None) -> str:
    """Copy the NOR env region (read-only) into the snapshot folder on the stick."""
    mount = mount or USB_MOUNT
    dst = f"{mount}/{USB_ROOT}/{label}.partial/{NOR_ENV_FILE}"
    return "\n".join([
        'e=""',
        f"for s in {MTD_SYSFS}/mtd[0-9]*; do",
        f'  [ "$(cat "$s/name" 2>/dev/null)" = "{NOR_ENV_MTD_NAME}" ] || continue',
        f'  e="{MTD_DEV_DIR}/$(basename "$s")"; break',
        "done",
        f'[ -n "$e" ] && dd if="$e" of={_q(dst)} bs={NOR_ENV_SIZE} count=1 2>/dev/null && sync '
        f'&& echo "NOR_ENV_SHA=$(sha256sum < {_q(dst)} | cut -d" " -f1)"',
        "echo NOR_ENV_SAVED=1",
    ]) + "\n"


def build_usb_discard_script(*, label: str, mount: Optional[str] = None) -> str:
    mount = mount or USB_MOUNT
    return f"rm -rf {_q(f'{mount}/{USB_ROOT}/{label}.partial')}; sync; echo DISCARDED=1\n"


def build_usb_cat_script(*, label: str, name: str, mount: Optional[str] = None) -> str:
    """Print one small text file of a USB snapshot, one KEY=VALUE line per
    source line (L<n>=...) so run_script()'s duplicate-line filter can't
    merge identical lines."""
    mount = mount or USB_MOUNT
    p = f"{mount}/{USB_ROOT}/{label}/{name}"
    return (f"if [ -f {_q(p)} ]; then n=0; while IFS= read -r line || [ -n \"$line\" ]; do "
            f"echo \"L$n=$line\"; n=$((n + 1)); done < {_q(p)}; fi; echo CAT_DONE=1\n")


def parse_cat(text: str) -> str:
    lines = {}
    for line in (text or "").splitlines():
        m = re.match(r"^L(\d+)=(.*)$", line)
        if m:
            lines[int(m.group(1))] = m.group(2)
    return "".join(lines[i] + "\n" for i in sorted(lines))


def build_usb_restore_script(*, label: str, manifest: dict, ranges: list,
                             dev: Optional[str] = None, mount: Optional[str] = None) -> str:
    """
    Background job: sha256sum -c the snapshot on the stick first (nothing is
    written if any file is off), then per range decompress -> cut -> dd, drop
    the page cache, and compare a fresh eMMC read with the expected hash
    (manifest for whole chunks, recomputed from the file for partial ones).
    """
    dev = dev or EMMC_DEV
    mount = mount or USB_MOUNT
    d = f"{mount}/{USB_ROOT}/{label}"
    by_index = {c["index"]: c for c in manifest["chunks"]}
    rows = []
    for k, r in enumerate(ranges):
        c = by_index[r["chunk"]]
        full = r["off"] == 0 and r["len"] == c["bytes"]
        rows.append([k, c["file"], c["compression"], r["off"], r["len"], r["abs"],
                     c["sha256"] if full else "-"])
    pre = _plan(rows)
    b = [f"DEV={dev}", _RD_FN, _WR_FN,
         'dec() { case $1 in zst) zstd -q -d -c;; gz) gzip -d -c;; *) cat;; esac; }',
         f"cd {_q(d)} || {{ echo 'SUMS=missing' >> {LOG}; exit 1; }}",
         f"if sha256sum -c SHA256SUMS >/dev/null 2>&1; then echo SUMS=ok >> {LOG}; "
         f"else echo SUMS=bad >> {LOG}; exit 1; fi",
         "src() { if [ \"$w\" = - ]; then dec $z < \"$f\" | tail -c +$((o + 1)) | head -c $n; "
         "else dec $z < \"$f\"; fi; }",
         "while read -r k f z o n a w; do",
         '  src | dd of="$DEV" $(wr $a) conv=notrunc 2>/dev/null; drc=$?',
         "  dropc",
         '  if [ "$w" = - ]; then want=$(src | sha); else want=$w; fi',
         "  got=$(rd $a $n | sha)",
         f'  echo "R_$k=$drc $want $got" >> {LOG}',
         f'  [ "$drc" = 0 ] && [ "$want" = "$got" ] || {{ echo "FAILED=$k" >> {LOG}; exit 1; }}',
         f"done < {PLAN}",
         "sync"]
    return "\n".join(pre) + "\n" + _bg(b, "RESTORE_DONE")


def build_log_script() -> str:
    return f"cat {LOG} 2>/dev/null; echo LOG_READ=1\n"


def parse_usb_chunk_results(kv: dict, prefix: str) -> dict:
    """{index: (rc, h_a, h_b)} from C_<i>= / R_<k>= log lines."""
    out = {}
    for k, v in kv.items():
        m = re.fullmatch(prefix + r"_(\d+)", k)
        if m:
            parts = v.split()
            if len(parts) == 3:   # rc, hash A, hash B
                out[int(m.group(1))] = (parts[0], parts[1], parts[2])
    return out


def build_gpt_fix_script(dev: Optional[str] = None) -> str:
    """After a clone to a bigger eMMC: move the backup GPT to the real end."""
    dev = dev or EMMC_DEV
    return "\n".join([
        f'if [ "$(dd if={dev} bs=512 skip=1 count=1 2>/dev/null | head -c 8)" = "EFI PART" ]; then',
        '  if command -v sgdisk >/dev/null 2>&1; then',
        f'    sgdisk -e {dev} >/dev/null 2>&1; echo "GPT_FIX=$?"',
        '  else echo "GPT_FIX=nosgdisk"; fi',
        'else echo "GPT_FIX=nogpt"; fi',
    ]) + "\n"


# --- Reading raw bytes over serial (od) ----------------------------------------------
# Small regions (GPT, NOR env) are read as `od -v -b` text: octal offset +
# octal bytes. CONFIRMED ON HARDWARE: recovery Linux's BusyBox od has no -A
# / -t ("od: invalid option -- 'A'"); -b and -v are the classic options every
# od has. Every line starts with its own offset, so run_script()'s
# duplicate-line filter can never merge two lines.

OD_ARGS = "-v -b"


def build_dump_script(src: str, skip: int, count: int, bs: int = SECTOR) -> str:
    """Octal dump of `count` blocks of `bs` bytes of `src`, starting at block `skip`."""
    return (f"dd if={src} bs={bs} skip={skip} count={count} 2>/dev/null | od {OD_ARGS}; "
            "echo DUMP_DONE=1\n")


def parse_od(text: str) -> Optional[bytes]:
    """Bytes from `od -v -b` output (octal), or None if it is not contiguous."""
    rows = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([0-7]{7,})((?: [0-7]{3})+)\s*$", line.strip())
        if m:
            rows[int(m.group(1), 8)] = bytes(int(b, 8) for b in m.group(2).split())
    out, pos = bytearray(), 0
    for off in sorted(rows):
        if off != pos:
            return None
        out += rows[off]
        pos += len(rows[off])
    return bytes(out)


# --- NOR U-Boot env -------------------------------------------------------------------

def build_nor_env_dump_script() -> str:
    """Find the NOR env MTD partition and hex-dump its env region (read-only)."""
    return "\n".join([
        'e=""',
        f"for s in {MTD_SYSFS}/mtd[0-9]*; do",
        f'  [ "$(cat "$s/name" 2>/dev/null)" = "{NOR_ENV_MTD_NAME}" ] || continue',
        f'  e="{MTD_DEV_DIR}/$(basename "$s")"; break',
        "done",
        'echo "NOR_ENV_DEV=$e"',
        f'[ -n "$e" ] && dd if="$e" bs={NOR_ENV_SIZE} count=1 2>/dev/null | od {OD_ARGS}',
        "echo DUMP_DONE=1",
    ]) + "\n"


def parse_uboot_env_blob(blob: Optional[bytes]) -> Optional[dict]:
    """Variables of a raw U-Boot env region, or None when its CRC does not match."""
    region = uboot_env.parse(blob)
    return region.vars if region else None


def build_emmc_env_dump_script(dev: Optional[str] = None) -> str:
    """Hex dump of the eMMC U-Boot env region (inside the firmware region)."""
    return build_dump_script(dev or EMMC_DEV, EMMC_ENV_SECTOR, EMMC_ENV_SECTORS)


UPLOAD_PIECE = 512                   # bytes of a blob per serial script (~2 KB of text)


def build_upload_blob_scripts(data: bytes, dst: str) -> list:
    """
    Scripts that recreate `data` as file `dst` on the device over serial
    (printf, octal escapes), each small enough for one serial heredoc.
    The last one prints UPLOAD_SHA=<sha256 of dst>.
    """
    scripts = []
    for start in range(0, len(data), UPLOAD_PIECE):
        piece = data[start:start + UPLOAD_PIECE]
        s = [f": > {dst}"] if start == 0 else []
        for i in range(0, len(piece), PRINTF_CHUNK):
            s.append(f"printf '{printf_octal(piece[i:i + PRINTF_CHUNK])}' >> {dst}")
        scripts.append("\n".join(s) + "\n")
    scripts.append(f'echo "UPLOAD_SHA=$(sha256sum < {dst} | cut -d" " -f1)"\n')
    return scripts


def build_emmc_env_write_script(src: str, want_sha: str, dev: Optional[str] = None) -> str:
    """Write a rebuilt env region to the eMMC env location, read back, compare."""
    dev = dev or EMMC_DEV
    return "\n".join([
        f'[ "$(sha256sum < {src} | cut -d" " -f1)" = "{want_sha}" ] || '
        '{ echo "EMMC_ENV_WRITE=badsource"; exit 0; }',
        f"dd if={src} of={dev} bs={SECTOR} seek={EMMC_ENV_SECTOR} count={EMMC_ENV_SECTORS} "
        "conv=notrunc 2>/dev/null && sync || { echo \"EMMC_ENV_WRITE=dd\"; exit 0; }",
        f'got=$(dd if={dev} bs={SECTOR} skip={EMMC_ENV_SECTOR} count={EMMC_ENV_SECTORS} 2>/dev/null '
        '| sha256sum | cut -d" " -f1)',
        f'if [ "$got" = "{want_sha}" ]; then echo "EMMC_ENV_WRITE=ok"; else echo "EMMC_ENV_WRITE=readback"; fi',
    ]) + "\n"


def printf_octal(data: bytes) -> str:
    """`data` as a printf format string of octal escapes (POSIX / BusyBox printf)."""
    return "".join(f"\\{b:03o}" for b in data)


def build_nor_env_write_script(src: str, want_sha: str) -> str:
    """
    Write a saved NOR env region back (opt-in only): check the source file,
    flashcp it onto the env MTD partition, read it back and compare.
    """
    return "\n".join([
        'e=""',
        f"for s in {MTD_SYSFS}/mtd[0-9]*; do",
        f'  [ "$(cat "$s/name" 2>/dev/null)" = "{NOR_ENV_MTD_NAME}" ] || continue',
        f'  e="{MTD_DEV_DIR}/$(basename "$s")"; break',
        "done",
        '[ -n "$e" ] || { echo "NOR_ENV_WRITE=nodev"; exit 0; }',
        f'[ "$(sha256sum < {src} | cut -d" " -f1)" = "{want_sha}" ] || '
        '{ echo "NOR_ENV_WRITE=badsource"; exit 0; }',
        f'flashcp {src} "$e" >/dev/null 2>&1 || {{ echo "NOR_ENV_WRITE=flashcp"; exit 0; }}',
        f'got=$(dd if="$e" bs={NOR_ENV_SIZE} count=1 2>/dev/null | sha256sum | cut -d" " -f1)',
        f'if [ "$got" = "{want_sha}" ]; then echo "NOR_ENV_WRITE=ok"; else echo "NOR_ENV_WRITE=readback"; fi',
    ]) + "\n"


# --- Health check before a snapshot: GPT + filesystems (warnings only) -----------------

def _gpt_header(raw: bytes) -> Optional[dict]:
    if len(raw) < GPT_HEADER_MIN_SIZE or raw[:len(GPT_SIGNATURE)] != GPT_SIGNATURE:
        return None
    f = struct.unpack_from(GPT_HEADER_FMT, raw, 0)
    h = dict(zip(("sig", "rev", "size", "crc", "_r", "my_lba", "alt_lba", "first", "last",
                  "guid", "entries_lba", "n_entries", "entry_size", "entries_crc"), f))
    if not GPT_HEADER_MIN_SIZE <= h["size"] <= SECTOR:
        h["crc_ok"] = False
        return h
    body = bytearray(raw[:h["size"]])
    body[16:20] = b"\0\0\0\0"                       # header CRC field is zeroed for the CRC
    h["crc_ok"] = (zlib.crc32(bytes(body)) & 0xFFFFFFFF) == h["crc"]
    return h


def check_gpt(head: bytes, tail: bytes, disk_bytes: int) -> dict:
    """
    GPT health from the first GPT_STD_SECTORS and the last sector of the disk.
    -> {"status": ok|warn|error|none, "findings": [(level, text)], "partitions": [...]}

    Mono-specific: an OS image whose own entry array runs past 4 KiB has
    that part overwritten by Mono's firmware region; the resulting entry
    CRC mismatch is reported as a warning (it still boots), never an error.
    """
    out = {"status": "ok", "findings": [], "partitions": []}

    def add(level, text):
        out["findings"].append((level, text))
        rank = {"ok": 0, "none": 0, "warn": 1, "error": 2}
        if rank[level] > rank[out["status"]]:
            out["status"] = level

    last_lba = disk_bytes // SECTOR - 1
    p = _gpt_header(head[GPT_HEADER_LBA * SECTOR:(GPT_HEADER_LBA + 1) * SECTOR])
    if p is None:
        out["status"] = "none"
        out["findings"].append(("none", "no GPT (MBR or no partition table) — not checked"))
        return out
    if not p["crc_ok"]:
        add("error", "primary GPT header CRC is wrong")
    if p["my_lba"] != GPT_HEADER_LBA:
        add("error", f"primary GPT header says it lives at LBA {p['my_lba']}")
    if p["last"] > last_lba or p["first"] > p["last"]:
        add("error", f"usable area LBA {p['first']}..{p['last']} does not fit the disk")
    # entries
    ent_start = p["entries_lba"] * SECTOR
    ent_len = p["n_entries"] * p["entry_size"]
    entries = head[ent_start:ent_start + ent_len]
    if p["entry_size"] < struct.calcsize(GPT_ENTRY_FMT) or len(entries) != ent_len:
        add("warn", "GPT entry array is outside the checked area — partitions not checked")
        entries = b""
    elif (zlib.crc32(entries) & 0xFFFFFFFF) != p["entries_crc"]:
        # Mono's firmware region starts at 4 KiB. When the entry array reaches
        # past it and that part is no longer blank, the firmware overwrote it
        # (Armbian / OPNsense + `firmware update`): expected, still boots.
        past_fw = entries[max(0, FW_START - ent_start):]
        if ent_start + ent_len > FW_START and any(past_fw):
            add("warn", "GPT entry CRC mismatch: the entry array reaches into Mono's firmware "
                        "region (expected after Armbian/OPNsense + firmware update; it boots)")
            entries = entries[:max(0, FW_START - ent_start)]
        else:
            add("error", "GPT entry array CRC is wrong")
    parts = []
    for i in range(len(entries) // p["entry_size"]):
        e = entries[i * p["entry_size"]:(i + 1) * p["entry_size"]]
        ptype, _guid, first, last, _attr, name = struct.unpack_from(GPT_ENTRY_FMT, e)
        if ptype == GPT_UNUSED_TYPE:
            continue
        nm = name.decode(GPT_NAME_ENCODING, errors="replace").rstrip("\0")
        parts.append({"index": i + 1, "first": first, "last": last, "name": nm})
        if first > last or first < p["first"] or last > p["last"]:
            add("error", f"partition {i + 1} ({nm or 'unnamed'}) LBA {first}..{last} is outside "
                         f"the usable area {p['first']}..{p['last']}")
    parts.sort(key=lambda x: x["first"])
    for a, b in zip(parts, parts[1:]):
        if b["first"] <= a["last"]:
            add("error", f"partitions {a['index']} and {b['index']} overlap")
    out["partitions"] = parts
    # backup header at the end of the disk
    b = _gpt_header(tail)
    if p["alt_lba"] != last_lba:
        add("warn", f"backup GPT expected at LBA {p['alt_lba']}, the disk ends at LBA {last_lba} "
                    "(image written to a bigger disk; `sgdisk -e` fixes it)")
    elif b is None:
        add("warn", "no backup GPT header at the end of the disk")
    else:
        if not b["crc_ok"]:
            add("warn", "backup GPT header CRC is wrong")
        if b["alt_lba"] != GPT_HEADER_LBA or b["guid"] != p["guid"] \
                or b["entries_crc"] != p["entries_crc"] or b["n_entries"] != p["n_entries"]:
            add("warn", "backup GPT header does not match the primary")
    if out["status"] == "ok":
        out["findings"].append(("ok", f"GPT valid, {len(parts)} partition(s), backup header consistent"))
    return out


def build_fs_check_script(dev: Optional[str] = None, partitions: str = "/proc/partitions",
                          dev_dir: str = "/dev") -> str:
    """
    Read-only filesystem check of every eMMC partition: ext2/3/4 with
    `e2fsck -n -f`, FAT with `fsck.vfat -n` / `dosfsck -n` when the device
    has them. Never writes. FS_<n>=<type> <result> per partition.
    """
    dev = dev or EMMC_DEV
    b = dev.split("/")[-1]
    return "\n".join([
        f'for p in $(grep -o "{b}p[0-9][0-9]*$" {partitions}); do',
        f'  n=${{p##*p}}; d={dev_dir}/$p; t=other',
        f'  set -- $(dd if=$d bs=1 skip={EXT_MAGIC_OFFSET} count=2 2>/dev/null | od -b | head -n 1)',
        f'  [ "$2 $3" = "{" ".join(f"{b:03o}" for b in EXT_MAGIC)}" ] && t=ext',
        f'  for o in {" ".join(str(o) for o in FAT_LABEL_OFFSETS)}; do',
        '    [ "$(dd if=$d bs=1 skip=$o count=3 2>/dev/null)" = "FAT" ] && t=vfat',
        '  done',
        '  r=nocheck',
        '  if [ $t = ext ]; then',
        '    if command -v e2fsck >/dev/null 2>&1; then e2fsck -n -f $d >/dev/null 2>&1; r=$?; else r=notool; fi',
        '  elif [ $t = vfat ]; then',
        '    if command -v fsck.vfat >/dev/null 2>&1; then fsck.vfat -n $d >/dev/null 2>&1; r=$?',
        '    elif command -v dosfsck >/dev/null 2>&1; then dosfsck -n $d >/dev/null 2>&1; r=$?',
        '    else r=notool; fi',
        '  fi',
        '  echo "FS_$n=$t $r"',
        'done',
        'echo FSCHECK=done',
    ]) + "\n"


def parse_fs_check(kv: dict) -> dict:
    """{partition: {"type", "result", "level"}} from build_fs_check_script()."""
    out = {}
    for k, v in kv.items():
        m = re.fullmatch(r"FS_(\d+)", k)
        if not m:
            continue
        typ, _, res = v.partition(" ")
        if res in ("nocheck", "notool"):
            level = "none"
        elif res == str(E2FSCK_CLEAN):
            level = "ok"
        else:
            level = "error"
        out[int(m.group(1))] = {"type": typ, "result": res, "level": level}
    return out


# --- U-Boot: chainload NOR recovery Linux (RAM only) -----------------------------------

def nor_chainload_commands() -> list:
    """
    U-Boot commands that load NOR recovery Linux into RAM from whichever
    U-Boot is running (eMMC or NOR). Nothing is saved: the env change
    (`recovery`) lives in RAM and is gone after the next reset.
    """
    return [
        "sf probe 0",
        f"sf read {RAM_DTB} {NOR_DTB_OFF} {NOR_DTB_LEN}",
        f"sf read {RAM_KERNEL} {NOR_KERNEL_OFF} {NOR_KERNEL_LEN}",
        "setenv kernel_comp_addr_r 0xa0000000",
        "setenv kernel_comp_size 0x10000000",
        f"setenv recovery 'booti {RAM_KERNEL} - {RAM_DTB}'",
    ]
