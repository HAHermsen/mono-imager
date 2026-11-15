"""
mono-imager: pre-snapshot health check (GPT + filesystems, warnings only)

Split out of snapshot.py; import everything through mono_imager.snapshot,
which re-exports these names.

Author:  H.A. Hermsen
License: GPLv3
"""

import re
import struct
import zlib
from typing import Optional

from mono_imager import snapshot as S

# --- Health check before a snapshot: GPT + filesystems (warnings only) -----------------

def _gpt_header(raw: bytes) -> Optional[dict]:
    if len(raw) < S.GPT_HEADER_MIN_SIZE or raw[:len(S.GPT_SIGNATURE)] != S.GPT_SIGNATURE:
        return None
    f = struct.unpack_from(S.GPT_HEADER_FMT, raw, 0)
    h = dict(zip(("sig", "rev", "size", "crc", "_r", "my_lba", "alt_lba", "first", "last",
                  "guid", "entries_lba", "n_entries", "entry_size", "entries_crc"), f))
    if not S.GPT_HEADER_MIN_SIZE <= h["size"] <= S.SECTOR:
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

    last_lba = disk_bytes // S.SECTOR - 1
    p = _gpt_header(head[S.GPT_HEADER_LBA * S.SECTOR:(S.GPT_HEADER_LBA + 1) * S.SECTOR])
    if p is None:
        out["status"] = "none"
        out["findings"].append(("none", "no GPT (MBR or no partition table) — not checked"))
        return out
    if not p["crc_ok"]:
        add("error", "primary GPT header CRC is wrong")
    if p["my_lba"] != S.GPT_HEADER_LBA:
        add("error", f"primary GPT header says it lives at LBA {p['my_lba']}")
    if p["last"] > last_lba or p["first"] > p["last"]:
        add("error", f"usable area LBA {p['first']}..{p['last']} does not fit the disk")
    # entries
    ent_start = p["entries_lba"] * S.SECTOR
    ent_len = p["n_entries"] * p["entry_size"]
    entries = head[ent_start:ent_start + ent_len]
    if p["entry_size"] < struct.calcsize(S.GPT_ENTRY_FMT) or len(entries) != ent_len:
        add("warn", "GPT entry array is outside the checked area — partitions not checked")
        entries = b""
    elif (zlib.crc32(entries) & 0xFFFFFFFF) != p["entries_crc"]:
        # Mono's firmware region starts at 4 KiB. When the entry array reaches
        # past it and that part is no longer blank, the firmware overwrote it
        # (Armbian / OPNsense + `firmware update`): expected, still boots.
        past_fw = entries[max(0, S.FW_START - ent_start):]
        if ent_start + ent_len > S.FW_START and any(past_fw):
            add("warn", "GPT entry CRC mismatch: the entry array reaches into Mono's firmware "
                        "region (expected after Armbian/OPNsense + firmware update; it boots)")
            entries = entries[:max(0, S.FW_START - ent_start)]
        else:
            add("error", "GPT entry array CRC is wrong")
    parts = []
    for i in range(len(entries) // p["entry_size"]):
        e = entries[i * p["entry_size"]:(i + 1) * p["entry_size"]]
        ptype, _guid, first, last, _attr, name = struct.unpack_from(S.GPT_ENTRY_FMT, e)
        if ptype == S.GPT_UNUSED_TYPE:
            continue
        nm = name.decode(S.GPT_NAME_ENCODING, errors="replace").rstrip("\0")
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
        if b["alt_lba"] != S.GPT_HEADER_LBA or b["guid"] != p["guid"] \
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
    dev = dev or S.EMMC_DEV
    b = dev.split("/")[-1]
    return "\n".join([
        f'for p in $(grep -o "{b}p[0-9][0-9]*$" {partitions}); do',
        f'  n=${{p##*p}}; d={dev_dir}/$p; t=other',
        f'  set -- $(dd if=$d bs=1 skip={S.EXT_MAGIC_OFFSET} count=2 2>/dev/null | od -b | head -n 1)',
        f'  [ "$2 $3" = "{" ".join(f"{b:03o}" for b in S.EXT_MAGIC)}" ] && t=ext',
        f'  for o in {" ".join(str(o) for o in S.FAT_LABEL_OFFSETS)}; do',
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
        elif res == str(S.E2FSCK_CLEAN):
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
        f"sf read {S.RAM_DTB} {S.NOR_DTB_OFF} {S.NOR_DTB_LEN}",
        f"sf read {S.RAM_KERNEL} {S.NOR_KERNEL_OFF} {S.NOR_KERNEL_LEN}",
        "setenv kernel_comp_addr_r 0xa0000000",
        "setenv kernel_comp_size 0x10000000",
        f"setenv recovery 'booti {S.RAM_KERNEL} - {S.RAM_DTB}'",
    ]


__all__ = [
    "_gpt_header",
    "build_fs_check_script",
    "check_gpt",
    "nor_chainload_commands",
    "parse_fs_check",
]
