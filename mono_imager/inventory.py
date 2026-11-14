"""
mono-imager: platform inventory — what a snapshot was taken from.

Collected read-only in recovery Linux and stored next to every snapshot
(inventory.txt, and the key facts in manifest.json["inventory"]):

  - date/time (UTC; marked whether the clock was NTP-synced first)
  - U-Boot version strings found in NOR (per MTD partition) and in the
    eMMC firmware region (4 KiB..32 MiB)
  - firmware tool, recovery kernel and recovery OS versions
  - MTD partition table, eMMC chip identity and wear level
  - MAC addresses, RAM
  - the U-Boot banner + power-on self-test of the boot that was caught
  - both U-Boot env sets (NOR uboot-env and the eMMC env region)

The U-Boot version strings are also what the restore's firmware check
compares (uboot_date()): newer firmware = later build date.

Author:  H.A. Hermsen
License: GPLv3
"""

import datetime as _dt
import re
from typing import Optional

from mono_imager import constants as C
from mono_imager.uboot_parse import parse_uboot_identity, parse_uboot_self_test

INVENTORY_TXT = "inventory.txt"
EMMC_ENV_TXT = "emmc_env.txt"

UBOOT_SIGNATURE = "U-Boot 20"         # start of every U-Boot version string
EMMC_FW_SCAN_MIB = C.EMMC_FW_END_MIB  # U-Boot lives inside the firmware region
EMMC_SYSFS_FIELDS = ("name", "manfid", "oemid", "serial", "date", "fwrev", "hwrev",
                     "life_time", "pre_eol_info")

# "U-Boot 2025.04-g1a2b3c (Jun 01 2025 - 12:00:00 +0000)"
_UBOOT_DATE_RE = re.compile(r"\((\w{3})\s+(\d{1,2})\s+(\d{4})\s+-\s+(\d{2}):(\d{2}):(\d{2})")
_UBOOT_VER_RE = re.compile(r"U-Boot\s+(\d{4})\.(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def build_inventory_script(*, dev: Optional[str] = None, emmc_sysfs: Optional[str] = None,
                           net_sysfs: str = "/sys/class/net", proc_mtd: str = "/proc/mtd",
                           mtd_dev_dir: str = "/dev") -> str:
    """Read-only facts about the board as KEY=VALUE lines (keys are unique)."""
    dev = dev or C.EMMC_DEV
    emmc_sysfs = emmc_sysfs or f"/sys/block/{dev.split('/')[-1]}/device"
    s = [
        "echo \"DATE=$(date -u '+%Y-%m-%dT%H:%M:%SZ')\"",
        'echo "KERNEL=$(uname -r)"',
        'echo "KERNEL_BUILD=$(uname -v)"',
        "[ -r /etc/os-release ] && echo \"RECOVERY_OS=$(. /etc/os-release; echo \"$PRETTY_NAME\")\"",
        # `firmware help` is the one sub-command known to be harmless (recovery_orchestrator)
        'command -v firmware >/dev/null 2>&1 && echo "FWTOOL=$(firmware help 2>&1 | head -n 1)"',
    ]
    s += [
        'echo "MEM_KB=$(grep MemTotal /proc/meminfo | tr -s " " | cut -d" " -f2)"',
        "i=0",
        f"[ -r {proc_mtd} ] && while read -r m size erase name; do case $m in mtd*)",
        '  echo "MTD_$i=${m%:} $size $erase $name"; i=$((i + 1));; esac',
        f"done < {proc_mtd}",
        f"for m in $(grep -o '^mtd[0-9]*' {proc_mtd} 2>/dev/null); do",
        f'  v=$(strings {mtd_dev_dir}/$m 2>/dev/null | grep -m 1 "^{UBOOT_SIGNATURE}")',
        '  [ -n "$v" ] && echo "UBOOT_NOR_$(echo $m | tr a-z A-Z)=$v"',
        "done",
        f'echo "UBOOT_EMMC=$(dd if={dev} bs=1M count={EMMC_FW_SCAN_MIB} 2>/dev/null | strings '
        f'| grep -m 1 "^{UBOOT_SIGNATURE}")"',
        f"for f in {' '.join(EMMC_SYSFS_FIELDS)}; do",
        f'  [ -r {emmc_sysfs}/$f ] && echo "EMMC_$(echo $f | tr a-z A-Z)=$(cat {emmc_sysfs}/$f)"',
        "done",
        f"for n in {net_sysfs}/eth*; do",
        '  [ -r "$n/address" ] && echo "MAC_$(basename "$n" | tr a-z A-Z)=$(cat "$n/address")"',
        "done",
        "echo INVENTORY=done",
    ]
    return "\n".join(s) + "\n"


def parse_inventory(kv: dict, *, ntp_synced: bool, boot_output: Optional[str] = None,
                    probe: Optional[dict] = None) -> dict:
    """Structured inventory from build_inventory_script() output (+ boot banner)."""
    nor = {k[len("UBOOT_NOR_"):].lower(): v for k, v in kv.items() if k.startswith("UBOOT_NOR_") and v}
    inv = {
        "date_utc": kv.get("DATE", ""),
        "clock_ntp_synced": bool(ntp_synced),
        "recovery": {"kernel": kv.get("KERNEL", ""), "kernel_build": kv.get("KERNEL_BUILD", ""),
                     "os": kv.get("RECOVERY_OS", ""), "firmware_tool": kv.get("FWTOOL", "")},
        "uboot": {"nor": nor, "emmc": kv.get("UBOOT_EMMC", "")},
        "mtd": [v for k, v in sorted(kv.items(), key=lambda x: _idx(x[0]))
                if re.fullmatch(r"MTD_\d+", k)],
        "emmc": {f: kv[f"EMMC_{f.upper()}"] for f in EMMC_SYSFS_FIELDS if f"EMMC_{f.upper()}" in kv},
        "macs": {k[len("MAC_"):].lower(): v for k, v in kv.items() if k.startswith("MAC_")},
        "mem_kb": int(kv["MEM_KB"]) if kv.get("MEM_KB", "").isdigit() else None,
        "complete": kv.get("INVENTORY") == "done",
    }
    if probe:
        inv["board"] = {"model": probe.get("model", ""), "serial": probe.get("serial", ""),
                        "emmc_bytes": probe.get("emmc_bytes")}
    if boot_output:
        inv["boot"] = {"identity": parse_uboot_identity(boot_output),
                       "self_test": [{"item": a, "value": b, "ok": ok}
                                     for a, b, ok in parse_uboot_self_test(boot_output)]}
    return inv


def _idx(key: str) -> int:
    m = re.search(r"(\d+)$", key)
    return int(m.group(1)) if m else 0


def nor_uboot(inv: Optional[dict]) -> str:
    """The NOR U-Boot version string of an inventory ('' if unknown)."""
    versions = list(((inv or {}).get("uboot") or {}).get("nor", {}).values())
    return versions[0] if versions else ""


def emmc_uboot(inv: Optional[dict]) -> str:
    return ((inv or {}).get("uboot") or {}).get("emmc", "") or ""


def uboot_date(version: str) -> Optional[_dt.datetime]:
    """Build time of a U-Boot version string; release (YYYY.MM) when no date is in it."""
    if not version:
        return None
    m = _UBOOT_DATE_RE.search(version)
    if m and m.group(1) in _MONTHS:
        return _dt.datetime(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)),
                            int(m.group(4)), int(m.group(5)), int(m.group(6)))
    m = _UBOOT_VER_RE.search(version)
    if m:
        return _dt.datetime(int(m.group(1)), int(m.group(2)), 1)
    return None


def compare_uboot(target: str, snapshot: str) -> str:
    """'older' | 'same' | 'newer' | 'unknown' — target relative to snapshot."""
    a, b = uboot_date(target), uboot_date(snapshot)
    if a is None or b is None:
        return "unknown"
    if target.strip() == snapshot.strip() or a == b:
        return "same"
    return "older" if a < b else "newer"


def format_inventory(inv: dict, *, label: str, nor_env: Optional[dict],
                     emmc_env: Optional[dict], tool_version: str) -> str:
    """inventory.txt — readable, one fact per line."""
    rec, ub, em = inv.get("recovery", {}), inv.get("uboot", {}), inv.get("emmc", {})
    board = inv.get("board", {})
    lines = [
        f"mono-imager {tool_version} — platform inventory for snapshot '{label}'",
        "",
        f"Date (UTC):          {inv.get('date_utc', '?')}"
        + ("" if inv.get("clock_ntp_synced") else "   (clock NOT NTP-synced — may be wrong)"),
        f"Board:               {board.get('model', '?')}   serial {board.get('serial', '?')}",
        f"RAM:                 {inv.get('mem_kb') or '?'} kB",
        "",
        "Firmware",
    ]
    for part, v in (ub.get("nor") or {}).items():
        lines.append(f"  NOR {part:<16} {v}")
    if not ub.get("nor"):
        lines.append("  NOR                  (no U-Boot version string found)")
    lines.append(f"  eMMC fw region       {ub.get('emmc') or '(no U-Boot version string found)'}")
    lines.append(f"  firmware tool        {rec.get('firmware_tool') or '?'}")
    lines.append(f"  recovery kernel      {rec.get('kernel', '?')} {rec.get('kernel_build', '')}".rstrip())
    lines.append(f"  recovery OS          {rec.get('os') or '?'}")
    lines += ["", "eMMC"]
    for k, v in em.items():
        lines.append(f"  {k:<20} {v}")
    lines += ["", "MTD partitions (dev size erasesize name)"]
    lines += [f"  {m}" for m in inv.get("mtd", [])] or ["  (none)"]
    lines += ["", "MAC addresses"]
    lines += [f"  {k:<8} {v}" for k, v in sorted((inv.get("macs") or {}).items())] or ["  (none)"]
    boot = inv.get("boot")
    if boot:
        lines += ["", "U-Boot banner (the boot that was caught)"]
        lines += [f"  {k:<12} {v}" for k, v in boot.get("identity", {}).items()]
        if boot.get("self_test"):
            lines += ["", "Power-on self-test"]
            lines += [f"  [{'OK' if t['ok'] else 'FAIL'}] {t['item']:<22} {t['value']}"
                      for t in boot["self_test"]]
    for title, env in (("U-Boot env on NOR (uboot-env)", nor_env),
                       ("U-Boot env on eMMC (firmware region)", emmc_env)):
        lines += ["", title]
        lines += [f"  {k}={v}" for k, v in sorted((env or {}).items())] or ["  (not readable)"]
    return "\n".join(lines) + "\n"
