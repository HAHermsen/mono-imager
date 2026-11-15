"""
mono-imager: U-Boot environment on NOR/eMMC, inventory and clock sync (snapshot flow)

Split out of snapshot_orchestrator.py; import it through mono_imager.snapshot_orchestrator,
which re-exports these names. Calls go through `SO.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


import hashlib
from collections.abc import Callable
from pathlib import Path

from mono_imager import snapshot_orchestrator as SO


def read_nor_env(d) -> bytes | None:
    """Raw NOR env region via `od` over serial (read-only), or None."""
    try:
        out = d.run_script(SO.S.build_nor_env_dump_script(), marker="snap_norenv_dump",
                           exec_timeout=SO.DUMP_TIMEOUT)
    except Exception as e:
        SO.verbose(f"  NOR env read: {e}", "warning")
        return None
    blob = SO.S.parse_od(out)
    return blob if blob and len(blob) == SO.S.NOR_ENV_SIZE else None


def _env_bootcmd_line(env: dict | None) -> str:
    return (env or {}).get("bootcmd", "(none)")


def offer_nor_env_restore(d, *, transfer: str, sess, base_url: str | None,
                          folder: Path | None, label: str | None, manifest: dict,
                          snap_vars: dict | None, current_blob: bytes | None,
                          have: set, ask: Callable) -> bool:
    """
    Opt-in: write the NOR env region saved with the snapshot back to NOR.
    The only place mono-imager writes NOR in snapshot/restore — 8 KiB env,
    never the firmware. Returns True if NOR now holds the snapshot's env.
    """
    rec = manifest.get("nor_env") or {}
    want = rec.get("sha256")
    if not want:
        return False
    if current_blob and hashlib.sha256(current_blob).hexdigest() == want:
        SO.step(0, "NOR U-Boot env already matches the snapshot", True)
        return True
    if not {"flashcp", "sha256sum"} <= have:
        SO._say("  ⚠ flashcp/sha256sum missing on the device — NOR env cannot be written here.")
        return False
    current_vars = SO.S.parse_uboot_env_blob(current_blob)
    SO._say("")
    SO._say("  The NOR U-Boot env differs from the one saved with this snapshot.")
    SO._say(f"    NOR bootcmd now:      {SO._env_bootcmd_line(current_vars)}")
    SO._say(f"    Snapshot's bootcmd:   {SO._env_bootcmd_line(snap_vars)}")
    SO._say("  Writing it touches NOR (only the 8 KiB env, never the firmware).")
    if ask("  Write the snapshot's NOR U-Boot env? [y/N]: ").lower() != "y":
        return False
    if transfer == "usb":
        src = f"{SO.S.USB_MOUNT}/{SO.S.USB_ROOT}/{label}/{rec['file']}"
        fetch = ""
    else:
        src = SO.S.NOR_ENV_TMP
        url = f"{base_url}/file?token={sess.token}&name={rec['file']}"
        fetch = f"curl -s -o {src} '{url}' || echo NOR_ENV_FETCH=fail\n"
    try:
        out = d.run_script(fetch + SO.S.build_nor_env_write_script(src, want),
                           marker="snap_norenv_write", exec_timeout=SO.NOR_ENV_WRITE_TIMEOUT)
    except Exception as e:
        out = str(e)
    result = SO.S.parse_kv(out).get("NOR_ENV_WRITE", "no answer")
    return SO.step(0, "NOR U-Boot env written and read back", result == "ok", result)


def nor_env_advice(manifest: dict, snap_env: dict, current_env: dict, current_from_nor: bool) -> list:
    """Lines telling the user which NOR env the restored OS needs (when it was not written)."""
    if manifest.get("boot_dip") != SO.DIP_NOR or not snap_env.get("bootcmd"):
        return []
    want = snap_env["bootcmd"]
    if current_from_nor and current_env.get("bootcmd") == want:
        return []
    import re as _re
    names = [n for n in _re.findall(r"run\s+([A-Za-z0-9_]+)", want) if n in snap_env]
    lines = ["  This OS boots via the U-Boot env on NOR, which was not written."]
    if current_from_nor:
        lines.append(f"  NOR bootcmd now:      {current_env.get('bootcmd', '(none)')}")
    lines.append(f"  Snapshot needs:       {want}")
    lines.append("  If it does not boot, at the NOR U-Boot prompt run:")
    for n in names:
        lines.append(f"    setenv {n} '{snap_env[n]}'")
    lines.append(f"    setenv bootcmd '{want}'")
    lines.append("    saveenv")
    return lines


# --- eMMC env, inventory -------------------------------------------------------------------

def read_emmc_env(d) -> bytes | None:
    """Raw eMMC U-Boot env region (inside the firmware region) via `od`, or None."""
    try:
        blob = SO.S.parse_od(d.run_script(SO.S.build_emmc_env_dump_script(), marker="snap_emmc_env",
                                       exec_timeout=SO.DUMP_TIMEOUT))
    except Exception as e:
        SO.verbose(f"  eMMC env read: {e}", "warning")
        return None
    return blob if blob and len(blob) == SO.S.NOR_ENV_SIZE else None


def collect_inventory(d, probe: dict, ntp_synced: bool) -> dict:
    """Platform inventory (read-only) + the U-Boot banner caught on serial."""
    try:
        out = d.run_script(SO.I.build_inventory_script(), marker="snap_inventory",
                           exec_timeout=SO.INVENTORY_TIMEOUT)
    except Exception as e:
        SO.verbose(f"  inventory: {e}", "warning")
        out = ""
    inv = SO.I.parse_inventory(SO.S.parse_kv(out), ntp_synced=ntp_synced,
                            boot_output=getattr(d, "last_boot_output", None), probe=probe)
    SO.step(0, "Platform inventory collected", True,
         "" if inv["complete"] else "partial — the device did not answer every question")
    return inv


def _sync_clock(d) -> bool:
    from mono_imager.recovery_orchestrator import sync_device_clock
    try:
        return bool(sync_device_clock(d))
    except Exception:
        return False


__all__ = [
    "_env_bootcmd_line",
    "_sync_clock",
    "collect_inventory",
    "nor_env_advice",
    "offer_nor_env_restore",
    "read_emmc_env",
    "read_nor_env",
]
