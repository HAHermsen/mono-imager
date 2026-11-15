"""
mono-imager: Pre-snapshot gates and advice: firmware check, clone warning, dual boot, health

Split out of snapshot_orchestrator.py; import it through mono_imager.snapshot_orchestrator,
which re-exports these names. Calls go through `SO.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


import hashlib
from collections.abc import Callable

from mono_imager import snapshot_orchestrator as SO


def firmware_gate(target_inv: dict, snap_inv: dict | None, *, clone: bool, ask: Callable) -> bool:
    """
    Compare this board's U-Boot versions with the snapshot's. NOR is never
    written by a restore, so an older NOR is worth updating FIRST (menu 3).
    eMMC: same board -> the snapshot's firmware region comes back with the
    image; clone -> `firmware update` provisions it before the write.
    Returns True to go ahead.
    """
    if not snap_inv:
        SO._say("  Firmware check: the snapshot has no inventory (older format) — skipped.")
        return True
    t_nor, s_nor = SO.I.nor_uboot(target_inv), SO.I.nor_uboot(snap_inv)
    t_emmc, s_emmc = SO.I.emmc_uboot(target_inv), SO.I.emmc_uboot(snap_inv)
    nor_cmp = SO.I.compare_uboot(t_nor, s_nor)
    SO._say("")
    SO._say("  Firmware (this board vs the snapshot):")
    SO._say(f"    NOR   now:  {t_nor or '?'}")
    SO._say(f"          snap: {s_nor or '?'}   -> this board is {nor_cmp}")
    SO._say(f"    eMMC  now:  {t_emmc or '?'}")
    SO._say(f"          snap: {s_emmc or '?'}")
    if clone:
        SO._say("    eMMC firmware: provisioned by `firmware update` BEFORE the write.")
    else:
        SO._say("    eMMC firmware: comes back from the snapshot when it differs.")
    SO.step(0, f"Firmware check: NOR {nor_cmp}", True)
    if nor_cmp != "older":
        return True
    SO._say("  ⚠ This board's NOR firmware is OLDER than the one the snapshot was taken with.")
    SO._say("    A restore never writes NOR firmware. Recommended: stop now, run")
    SO._say("    'Update NOR firmware' (main menu 3), then restore.")
    return ask("  [s]top here (recommended) or [c]ontinue anyway: ").lower() == "c"


# --- Clone warning (read-only: the OS config is never edited) ------------------------------

# Where each OS keeps settings that are tied to the original board.
_CLONE_HINTS = {
    "openwrt":  "OpenWRT: `option macaddr` in /etc/config/network, DHCP static leases",
    "armbian":  "Armbian/Debian: /etc/udev/rules.d/*persistent-net*, /etc/machine-id, netplan/NetworkManager MAC matches",
    "opnsense": "OPNsense: interface assignments and spoofed MACs (Interfaces > Assignments)",
    "vyos":     "VyOS: `hw-id` under `interfaces ethernet`",
}


def clone_warning(manifest: dict, snap_macs: set, here_macs: set) -> list:
    """Lines shown before a clone: what comes along from the other board."""
    os_label = manifest.get("os_label", "")
    lines = [
        "  ⚠ This is ANOTHER board's snapshot.",
        f"    Snapshot MACs:   {', '.join(sorted(snap_macs)) or manifest.get('device', {}).get('mac') or '?'}",
        f"    This board:      {', '.join(sorted(here_macs)) or '?'}",
        "    Everything in its OS comes along: hostname, SSH host keys, IP addresses,",
        "    users and any MAC-pinned interface settings. mono-imager does NOT edit them.",
        "    Two boxes with the same identity on one LAN will clash — change it before",
        "    connecting both.",
    ]
    hint = SO._CLONE_HINTS.get(os_label)
    lines.append(f"    Check: {hint}" if hint else
                 "    Check the OS's network config for settings pinned to MAC addresses.")
    return lines


# --- Boot from either DIP position (opt-in) ---------------------------------------------------

def _upload(d, blob: bytes, dst: str) -> bool:
    """Send a small blob over serial in pieces; True when the device's copy hashes right."""
    out = ""
    try:
        for n, script in enumerate(SO.S.build_upload_blob_scripts(blob, dst)):
            out = d.run_script(script, marker=f"snap_upload{n}",
                               write_timeout=SO.UPLOAD_WRITE_TIMEOUT, exec_timeout=SO.UPLOAD_WRITE_TIMEOUT)
    except Exception as e:
        SO.verbose(f"  upload: {e}", "warning")
        return False
    return SO.S.parse_kv(out).get("UPLOAD_SHA") == hashlib.sha256(blob).hexdigest()


def offer_dual_boot(d, manifest: dict, *, have: set, ask: Callable) -> bool:
    """
    Copy the restored OS's boot variables (bootcmd + everything it runs,
    never `recovery`) into the OTHER U-Boot env, so it boots with the DIP
    in either position. Opt-in; one env region is rewritten, read back and
    compared. Returns True when both envs now boot the OS.
    """
    boot_dip = manifest.get("boot_dip") or SO.DIP_EMMC
    emmc, nor = SO.E.parse(SO.read_emmc_env(d)), SO.E.parse(SO.read_nor_env(d))
    if boot_dip == SO.DIP_NOR:
        src, dst, dst_name = nor, emmc, "eMMC"
    else:
        src, dst, dst_name = emmc, nor, "NOR"
    if not src or not dst or "bootcmd" not in src.vars:
        SO._say("  Boot from either DIP position: an env region could not be read — not offered.")
        return False
    changes = SO.E.merge_for_boot(dst.vars, src.vars)
    if not changes:
        SO.step(0, "Boots with the DIP in either position (envs already agree)", True)
        return True
    if dst.redundant:
        SO._say(f"  Boot from either DIP position: the {dst_name} env is a redundant env — not offered.")
        return False
    need = {"sha256sum"} | ({"flashcp"} if dst_name == "NOR" else set())
    if not need <= have:
        SO._say(f"  Boot from either DIP position: {', '.join(sorted(need - have))} missing — not offered.")
        return False
    SO._say("")
    SO._say(f"  To also boot with the DIP on {SO.DIP_TEXT[SO.DIP_EMMC if dst_name == 'eMMC' else SO.DIP_NOR]},")
    SO._say(f"  these variables would be written into the {dst_name} U-Boot env:")
    for k, v in changes.items():
        SO._say(f"    {k}={v[:SO.C.LOG_TAIL_SHORT]}{'…' if len(v) > SO.C.LOG_TAIL_SHORT else ''}")
    what = "touches NOR: only its env region, never the firmware" if dst_name == "NOR" \
        else "eMMC env region only"
    if ask(f"  Make it boot whatever the DIP position? ({what}) [y/N]: ").lower() != "y":
        return False
    try:
        blob = SO.E.build(SO.E.EnvRegion(dict(dst.vars, **changes), dst.redundant, dst.flag, dst.size))
    except ValueError as e:
        return SO.step(0, f"{dst_name} env rebuilt", False, str(e))
    want = hashlib.sha256(blob).hexdigest()
    if not SO.step(0, f"New {dst_name} env sent to the device", SO._upload(d, blob, SO.S.ENV_UPLOAD_TMP)):
        return False
    script = (SO.S.build_nor_env_write_script(SO.S.ENV_UPLOAD_TMP, want) if dst_name == "NOR"
              else SO.S.build_emmc_env_write_script(SO.S.ENV_UPLOAD_TMP, want))
    key = "NOR_ENV_WRITE" if dst_name == "NOR" else "EMMC_ENV_WRITE"
    try:
        result = SO.S.parse_kv(d.run_script(script, marker="snap_dual_boot",
                                         exec_timeout=SO.NOR_ENV_WRITE_TIMEOUT)).get(key, "no answer")
    except Exception as e:
        result = str(e)
    return SO.step(0, f"{dst_name} env updated — boots with the DIP in either position",
                result == "ok", result)


# --- Health check before a snapshot (warnings; the user decides) --------------------------

def health_check(d, probe: dict) -> dict:
    """GPT (start + end of disk) and read-only filesystem checks."""
    emmc = probe["emmc_bytes"]
    health = {"status": "ok", "gpt": None, "filesystems": {}, "notes": []}
    rank = {"none": 0, "ok": 0, "warn": 1, "error": 2}

    def worst(level):
        if rank.get(level, 0) > rank[health["status"]]:
            health["status"] = level

    if "od" not in probe["have"]:
        health["notes"].append("od missing on the device — GPT not checked")
        worst("warn")          # unchecked is never "ok"
    else:
        try:
            head = SO.S.parse_od(d.run_script(SO.S.build_dump_script(SO.S.EMMC_DEV, 0, SO.S.GPT_STD_SECTORS),
                                           marker="snap_gpt_head", exec_timeout=SO.DUMP_TIMEOUT))
            tail = SO.S.parse_od(d.run_script(SO.S.build_dump_script(SO.S.EMMC_DEV, emmc // SO.S.SECTOR - 1, 1),
                                           marker="snap_gpt_tail", exec_timeout=SO.DUMP_TIMEOUT))
        except Exception as e:
            head = tail = None
            SO.verbose(f"  GPT read: {e}", "warning")
        if head and len(head) == SO.S.GPT_STD_SECTORS * SO.S.SECTOR and tail and len(tail) == SO.S.SECTOR:
            gpt = SO.S.check_gpt(head, tail, emmc)
            health["gpt"] = {"status": gpt["status"],
                             "findings": [list(f) for f in gpt["findings"]],
                             "partitions": gpt["partitions"]}
            worst(gpt["status"])
        else:
            health["notes"].append("GPT area could not be read over serial — not checked")
            worst("warn")          # unchecked is never "ok"
    try:
        kv = SO.S.parse_kv(d.run_script(SO.S.build_fs_check_script(), marker="snap_fsck",
                                     exec_timeout=SO.FSCHECK_TIMEOUT))
    except Exception as e:
        kv = {}
        SO.verbose(f"  filesystem check: {e}", "warning")
    if kv.get("FSCHECK") != "done":
        health["notes"].append("filesystem check did not finish — not checked")
        worst("warn")          # unchecked is never "ok"
    for n, r in SO.S.parse_fs_check(kv).items():
        health["filesystems"][str(n)] = r
        worst(r["level"])
    return health


def review_health(health: dict, ask: Callable) -> bool:
    """Show the check; on errors the user decides. True = go ahead."""
    icon = {"ok": "✓", "warn": "⚠", "error": "✗", "none": "·"}
    SO._say("")
    SO._say("  Health check (read-only):")
    for level, text in (health.get("gpt") or {}).get("findings", []):
        SO._say(f"    {icon.get(level, '·')} {text}")
    for n, r in sorted(health["filesystems"].items(), key=lambda x: int(x[0])):
        what = {"ok": "clean", "error": f"errors found (rc {r['result']})",
                "none": "not checked" + (" (no fsck tool)" if r["result"] == "notool" else "")}
        SO._say(f"    {icon.get(r['level'], '·')} partition {n} ({r['type']}): {what[r['level']]}")
    for note in health["notes"]:
        SO._say(f"    · {note}")
    SO.step(0, f"Health check: {health['status']}", True)
    if health["status"] != "error":
        return True
    SO._say("  ⚠ The snapshot would capture these errors as they are.")
    return ask("  Snapshot anyway? [y/N]: ").lower() == "y"


__all__ = [
    "_CLONE_HINTS",
    "_upload",
    "clone_warning",
    "firmware_gate",
    "health_check",
    "offer_dual_boot",
    "review_health",
]
