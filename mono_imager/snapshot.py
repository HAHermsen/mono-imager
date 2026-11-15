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
from pathlib import Path
from typing import Optional

from mono_imager import constants as C

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


# --- Re-exports: the device-script builders and the health check live in their own
# modules. Resolved lazily: a star import at the bottom would silently come up empty
# when one of those modules is imported first (they import this one back).
def __getattr__(name: str):
    import importlib
    for mod in ("mono_imager.snapshot_checks", "mono_imager.snapshot_scripts"):
        m = importlib.import_module(mod)
        if name in m.__all__:
            return getattr(m, name)
    raise AttributeError(f"module 'mono_imager.snapshot' has no attribute {name!r}")
