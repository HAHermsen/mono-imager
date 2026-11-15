"""
mono-imager: eMMC snapshot / restore journeys (#29).

    snapshot   whole eMMC -> PC (LAN) or USB stick in the Mono
    restore    snapshot -> whole eMMC, with an automatic snapshot of the
               current eMMC first, so a restore is itself undoable

Reaching recovery (shared): the user reboots the running OS cleanly and
mono-imager catches U-Boot on serial (eMMC or NOR U-Boot, whichever the
DIP switch selects). From that U-Boot, recovery Linux is started in RAM —
see _boot_recovery_in_ram(): this U-Boot's own recovery first (has
Ethernet), then NOR's own env imported + `run recovery`, then fixed NOR
offsets. Nothing is saved to any env. Recovery Linux then runs entirely from RAM, so the eMMC can be
overwritten while it runs, and boot_medium=qspi tells the `firmware`
tool it is NOR recovery (clone mode's `firmware update` then provisions
the eMMC). Only when that is impossible (U-Boot not caught: blank eMMC,
bootdelay=0; or a U-Boot without `sf`) is the user asked to flip the
DIP switch to NOR, and back afterwards.

Device-side work runs as POSIX/BusyBox scripts (snapshot.py). LAN: the
device streams raw dd output, the PC compresses and hashes, results come
back as HTTP reports (the reliable channel, see flash_orchestrator).
USB: no network involved; the job runs in the background on the device
and its log is polled over serial.

Before a snapshot: a read-only health check (GPT at both ends of the disk,
`e2fsck -n` / `fsck.vfat -n` per partition) — warnings, recorded in the
manifest; on errors the user decides. The NOR U-Boot env region is read
(read-only) and kept with the snapshot.

NOR is written in exactly one place: offer_nor_env_restore(), after a
same-board restore, on an explicit opt-in, and only the 8 KiB env region
(never the firmware). The optional full NOR dump is never restored.

Author:  H.A. Hermsen
License: GPLv3
"""

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from mono_imager import constants as C
from mono_imager import inventory as I
from mono_imager import snapshot as S
from mono_imager import uboot_env as E
from mono_imager.flash_orchestrator import (
    capture_uboot_env, console_logger, phase1_uboot, print_report, step, verbose,
    wait_for_report,
)
from mono_imager.spinner import with_spinner

DIP_NOR, DIP_EMMC = "NOR", "EMMC"
DIP_TEXT = {DIP_NOR: "RIGHT (NOR)", DIP_EMMC: "LEFT (eMMC)"}

REBOOT_MESSAGE = (
    "  ⚡ REBOOT THE RUNNING OS CLEANLY NOW ⚡\n"
    "  (`reboot` over SSH or its console). Power-cycle only if it is hung:\n"
    "  a dirty filesystem is captured as-is and replayed on restore."
)

# --- Tuning (seconds) ---------------------------------------------------------
CHUNK_TIMEOUT = 3600.0           # one 1 GiB chunk (read + send + read back), worst case
USB_POLL      = 15.0             # USB jobs: how often the device log is read
OS_SHUTDOWN_TIMEOUT = 300        # clean `reboot` of the running OS -> U-Boot banner
SF_READ_KERNEL_TIMEOUT = 90      # 32 MiB from QSPI NOR
LOGIN_TIMEOUT = 60
PROBE_TIMEOUT = 40
FW_HASH_TIMEOUT = 600            # first device answer of a LAN snapshot
BOOT_HASH_TIMEOUT = 120
UPLOAD_SETTLE_TIMEOUT = 30       # server-side record after the device's report
VERIFY_SETTLE_TIMEOUT = 60
NOR_DUMP_TIMEOUT = 600
LOG_POLL_TIMEOUT = 30
MOUNT_TIMEOUT = 60
USB_START_TIMEOUT = 30
DISCARD_TIMEOUT = 120
FINALIZE_TIMEOUT = 600           # sha256sum of every file on the stick
CAT_TIMEOUT = 60
FW_REGION_HASH_TIMEOUT = 120
FW_REGION_REPORT_TIMEOUT = 300
GPT_FIX_TIMEOUT = 60
PROBE_ATTEMPTS = 2
DUMP_TIMEOUT = 60                # od dump of the GPT area / NOR env over serial
FSCHECK_TIMEOUT = 1800           # e2fsck -n -f of every ext partition
NOR_ENV_FETCH_TIMEOUT = 60
NOR_ENV_WRITE_TIMEOUT = 120
INVENTORY_TIMEOUT = 300          # `strings` over the whole NOR + eMMC firmware region
UPLOAD_WRITE_TIMEOUT = 120       # rebuilt env region typed over serial (printf)


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def _say(msg: str = ""):
    console_logger.info(msg)


def _banner(*lines):
    _say("")
    _say("=" * C.DIVIDER_WIDTH)
    for line in lines:
        _say(line)
    _say("=" * C.DIVIDER_WIDTH)


def _gib(n: int) -> str:
    return f"{n / C.GIB:.1f} GiB"


# --- Reaching recovery ---------------------------------------------------------------

def ask_dip(ask: Callable = _ask) -> str:
    _say("")
    _say("  Which way is the DIP switch right now?")
    _say("    1) LEFT  (eMMC)")
    _say("    2) RIGHT (NOR)")
    while True:
        c = ask("  Select [1-2]: ")
        if c == "1":
            return DIP_EMMC
        if c == "2":
            return DIP_NOR


def _uboot_cmd(d, cmd: str, timeout: float) -> str:
    """
    One U-Boot command. Right after autoboot is interrupted, U-Boot can
    swallow the first character(s) it receives (confirmed on hardware:
    `sf probe 0` arrived as `f probe 0`). So: on "Unknown command",
    send an empty line to re-sync the prompt and try once more.
    """
    out = d.send_command(cmd, timeout=timeout)
    if "Unknown command" in out:
        d.send_command("", timeout=C.UBOOT_QUICK_TIMEOUT)
        out = d.send_command(cmd, timeout=timeout)
    return out


def _import_nor_env(d) -> bool:
    """
    Load NOR's own U-Boot env into THIS U-Boot's RAM env (`sf read` +
    `env import -c`, CRC-checked, never saved). Afterwards `run recovery`
    does exactly what the NOR U-Boot would: boot NOR recovery Linux.
    No addresses guessed — NOR's own `recovery` command knows them.
    """
    for size in S.NOR_ENV_SIZES:
        _uboot_cmd(d, f"sf read {S.RAM_KERNEL} {S.NOR_ENV_OFF} {size}", C.UBOOT_WRITE_TIMEOUT)
        out = _uboot_cmd(d, f"env import -c {S.RAM_KERNEL} {size}", C.UBOOT_CMD_TIMEOUT)
        if "error" in out.lower() or "bad crc" in out.lower():
            continue
        rec = _uboot_cmd(d, "printenv recovery", C.UBOOT_QUICK_TIMEOUT)
        if "recovery=" in rec:
            verbose(f"  NOR env imported ({size} bytes): {rec.strip()[:C.LOG_TAIL]}")
            return True
    verbose("  NOR env could not be imported (no valid env at the expected offset)", "warning")
    return False


def _load_nor_recovery_fixed(d) -> bool:
    """Second try: DTB + gzip kernel at the documented NOR offsets (magic-checked)."""
    probe, read_dtb, read_kernel, *set_env = S.nor_chainload_commands()
    _uboot_cmd(d, read_dtb, C.UBOOT_WRITE_TIMEOUT)
    if C.FIT_MAGIC_HEX not in _uboot_cmd(d, f"md.b {S.RAM_DTB} 4", C.UBOOT_QUICK_TIMEOUT):
        verbose(f"  no device tree at NOR {S.NOR_DTB_OFF}", "warning")
        return False
    _uboot_cmd(d, read_kernel, SF_READ_KERNEL_TIMEOUT)
    if C.GZIP_MAGIC_HEX not in _uboot_cmd(d, f"md.b {S.RAM_KERNEL} 2", C.UBOOT_QUICK_TIMEOUT):
        verbose(f"  no gzip kernel at NOR {S.NOR_KERNEL_OFF}", "warning")
        return False
    for c in set_env:
        _uboot_cmd(d, c, C.UBOOT_QUICK_TIMEOUT)
    return True


def _boot(d, medium: str, message: str) -> bool:
    booted, err = with_spinner(d.boot_recovery, boot_medium=medium, message=message)
    return bool(booted) and not err


def _boot_recovery_in_ram(d) -> Optional[str]:
    """
    Start recovery Linux (it runs from RAM) from whatever U-Boot is at the
    prompt, without touching the DIP switch. Returns the boot medium of
    the recovery that runs (BOOT_MEDIUM_NOR / BOOT_MEDIUM_EMMC) or None.

      1. this U-Boot's own `run recovery` — the recovery mono-imager already
         boots at launch, with working Ethernet. With the DIP LEFT that is
         eMMC recovery: fine for snapshots and same-board restores.
      2. NOR's env imported into RAM + NOR's own `run recovery`. CONFIRMED ON
         HARDWARE: it boots, but from an eMMC U-Boot that kernel came up
         with NO Ethernet ports — so it is only a fallback (USB snapshots).
      3. DTB + kernel at the documented NOR offsets (0x500000 was empty on
         real hardware; magic-checked, so harmless to try).
    Clone mode needs NOR recovery with network: it asks for the DIP switch.
    """
    try:
        d.send_command("", timeout=C.UBOOT_QUICK_TIMEOUT)      # re-sync the prompt
    except Exception:
        pass
    if _boot(d, C.BOOT_MEDIUM_EMMC, "Booting recovery Linux into RAM..."):
        return C.BOOT_MEDIUM_EMMC
    try:
        d.send_command("", timeout=C.UBOOT_QUICK_TIMEOUT)
        out = _uboot_cmd(d, "sf probe 0", C.UBOOT_CMD_TIMEOUT)
        if "SF:" not in out or "Unknown command" in out:
            verbose(f"  sf probe not usable here: {out.strip()[:C.LOG_TAIL]!r}", "warning")
            return None
        if _import_nor_env(d) or _load_nor_recovery_fixed(d):
            if _boot(d, C.BOOT_MEDIUM_NOR, "Booting NOR recovery Linux into RAM..."):
                return C.BOOT_MEDIUM_NOR
    except Exception as e:
        verbose(f"  NOR recovery preparation failed: {e}", "warning")
    return None


def _capture_env(d) -> dict:
    """U-Boot env of the interrupted U-Boot; recaptured once on a fresh
    prompt if phase1_uboot()'s capture came back empty (dropped chars)."""
    env = dict(getattr(d, "captured_uboot_env", None) or {})
    if not env:
        try:
            d.send_command("", timeout=C.UBOOT_QUICK_TIMEOUT)
            env = dict(capture_uboot_env(d) or {})
        except Exception as e:
            verbose(f"  U-Boot env capture retry failed: {e}", "warning")
    return env


def reach_recovery(port: str, dip_now: str, soft_reboot: Optional[Callable] = None,
                   ask: Callable = _ask):
    """
    -> (device at root shell, info) or (None, info).
    info: {"flipped": bool, "env": dict, "uboot_dip": DIP the interrupted U-Boot came from}
    """
    info = {"flipped": False, "env": {}, "uboot_dip": dip_now}
    if soft_reboot:
        soft_reboot(port)
    d = phase1_uboot(port, C.SERIAL_BAUD, wait_message=REBOOT_MESSAGE, autoboot_timeout=OS_SHUTDOWN_TIMEOUT)
    if d is None:
        if dip_now == DIP_NOR:
            return None, info
        _banner("  U-Boot was not caught (blank eMMC, bootdelay=0, ...).",
                f"  ⚡ FLIP THE DIP SWITCH TO {DIP_TEXT[DIP_NOR]}, THEN POWER-CYCLE ⚡")
        ask("  Press Enter once you've done that... ")
        info["flipped"], info["uboot_dip"] = True, DIP_NOR
        d = phase1_uboot(port, C.SERIAL_BAUD)
        if d is None:
            return None, info
    info["env"] = _capture_env(d)

    # On a NOR U-Boot (DIP RIGHT) its own `run recovery` IS NOR recovery.
    if info["uboot_dip"] == DIP_NOR:
        medium = C.BOOT_MEDIUM_NOR if _boot(d, C.BOOT_MEDIUM_NOR, "Booting recovery Linux...") else None
    else:
        medium = _boot_recovery_in_ram(d)
    if medium is None and not info["flipped"] and dip_now == DIP_EMMC:
        _banner("  Recovery Linux could not be started from this U-Boot.",
                f"  ⚡ FLIP THE DIP SWITCH TO {DIP_TEXT[DIP_NOR]}, THEN POWER-CYCLE ⚡")
        ask("  Press Enter once you've done that... ")
        info["flipped"], info["uboot_dip"] = True, DIP_NOR
        d.disconnect()
        d = phase1_uboot(port, C.SERIAL_BAUD)
        if d is None:
            return None, info
        info["env"] = _capture_env(d)
        medium = C.BOOT_MEDIUM_NOR if _boot(d, C.BOOT_MEDIUM_NOR, "Booting recovery Linux...") else None
    info["recovery_medium"] = medium
    ok = medium is not None
    where = "NOR" if medium == C.BOOT_MEDIUM_NOR else "eMMC"
    if not step(0, f"Recovery Linux running from RAM ({where} recovery)", ok):
        d.disconnect()
        return None, info
    logged, err = with_spinner(d.login_recovery, timeout=LOGIN_TIMEOUT, message="Logging into recovery shell...")
    if not step(0, "Logged into recovery shell", bool(logged) and not err):
        d.disconnect()
        return None, info
    return d, info


def probe_device(d) -> Optional[dict]:
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        try:
            out = d.run_script(S.build_probe_script(), marker=f"snap_probe{attempt}", exec_timeout=PROBE_TIMEOUT)
        except Exception as e:
            verbose(f"  probe attempt {attempt}: {e}", "warning")
            continue
        p = S.parse_probe(out)
        if p["ok"] and p["emmc_bytes"] > 0:
            return p
    return None


def _identity(probe: dict, env: dict) -> dict:
    return {"mac": probe["mac"], "serial": probe["serial"], "model": probe["model"],
            "uboot_version": env.get("ver", "")}


def _finish_device(d, info: dict, final_dip: str, any_dip: bool = False):
    """Reboot out of recovery and tell the user where the DIP must end up."""
    try:
        d.ser.write(b"\r\nsync; reboot\r\n")
    except Exception:
        pass
    if any_dip:
        _say("  Rebooting — it boots with the DIP switch in either position.")
    elif info.get("flipped") or info.get("dip_now") != final_dip:
        _banner(f"  ⚡ SET THE DIP SWITCH TO {DIP_TEXT[final_dip]}, THEN POWER-CYCLE ⚡")
    else:
        _say(f"  Rebooting — DIP stays {DIP_TEXT[final_dip]}.")


def _stop_device_job(d):
    try:
        d.ser.write(b"\x03\r\n")
    except Exception:
        pass


# --- LAN snapshot -----------------------------------------------------------------------

def _base_url(host_ip: str, port: int) -> str:
    return f"http://{host_ip}:{port}"


@dataclass
class Capture:
    """Everything one snapshot records besides the eMMC bytes themselves."""
    label: str
    probe: dict                       # probe_device() result (size, tools, identity)
    identity: dict                    # mac / serial / model / U-Boot version
    env: dict                         # env of the U-Boot that was interrupted (info)
    os_label: str                     # display-only OS guess
    boot_dip: str                     # DIP position the captured OS boots with
    nor_dump: bool = False            # also keep a read-only full NOR dump
    nor_env: Optional[bytes] = None   # raw NOR U-Boot env region (8 KiB)
    health: Optional[dict] = None     # pre-snapshot GPT / filesystem check
    inventory: Optional[dict] = None  # platform inventory (inventory.py)
    emmc_env: Optional[bytes] = None  # raw eMMC U-Boot env region (info; it is in the image)
    auto: bool = False                # taken automatically before a restore
    thorough: bool = False            # LAN: re-read every chunk from the eMMC (2nd media read, ~2x slower)


def _text_files(cap: Capture) -> dict:
    """The readable side files of a snapshot: {name: text}."""
    from mono_imager import __version__
    nor_vars = S.parse_uboot_env_blob(cap.nor_env)
    emmc_vars = S.parse_uboot_env_blob(cap.emmc_env)
    files = {S.UBOOT_ENV_TXT: S.format_env(cap.env) if cap.env else "",
             S.NOR_ENV_TXT: S.format_env(nor_vars) if nor_vars else "",
             I.EMMC_ENV_TXT: S.format_env(emmc_vars) if emmc_vars else ""}
    if cap.inventory:
        files[I.INVENTORY_TXT] = I.format_inventory(cap.inventory, label=cap.label,
                                                    nor_env=nor_vars, emmc_env=emmc_vars,
                                                    tool_version=__version__)
    return {k: v for k, v in files.items() if v}


def _nor_env_record(cap: Capture) -> Optional[dict]:
    if not cap.nor_env:
        return None
    return {"file": S.NOR_ENV_FILE, "sha256": hashlib.sha256(cap.nor_env).hexdigest(),
            "bytes": len(cap.nor_env), "vars_file": S.NOR_ENV_TXT}


def _manifest(cap: Capture, *, chunks: list, fw_sha: str, boot_sha: str, transfer: str,
              nor_rec: Optional[dict]) -> dict:
    from mono_imager import __version__
    return S.build_manifest(label=cap.label, identity=cap.identity,
                            emmc_bytes=cap.probe["emmc_bytes"], chunks=chunks,
                            fw_sha256=fw_sha, boot_sha256=boot_sha, os_label=cap.os_label,
                            boot_dip=cap.boot_dip, transfer=transfer, tool_version=__version__,
                            auto=cap.auto, nor=nor_rec, has_env=bool(cap.env),
                            nor_env=_nor_env_record(cap), health=cap.health,
                            inventory=cap.inventory)


def snapshot_lan(d, sess, base_url: str, folder: Path, cap: Capture) -> Optional[dict]:
    """Whole eMMC -> <folder>/<label>. Returns the manifest, or None (nothing kept)."""
    label, probe, nor = cap.label, cap.probe, cap.nor_dump
    partial = folder / f"{label}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    sess.begin_snapshot(partial)
    chunks = S.plan_chunks(probe["emmc_bytes"])
    have_sha = "sha256sum" in probe["have"]
    single = not cap.thorough and {"tee", "mkfifo"} <= probe["have"] \
        and bool({"openssl", "sha256sum"} & probe["have"])
    if not cap.thorough and not single:
        _say("  ⚠ tee/mkfifo/openssl/sha256sum missing on the device — using the slower read-back verify.")
    spin = "eMMC -> PC, hashing" if single else "eMMC -> PC, read back"
    done = "captured and verified" if single else "captured and read-back verified"

    def fail(what, why):
        _stop_device_job(d)
        shutil.rmtree(partial, ignore_errors=True)
        step(0, what, False, why + " — snapshot discarded")
        return None

    script = S.build_lan_snapshot_script(base_url=base_url, token=sess.token, chunks=chunks,
                                         have_sha=have_sha, nor=nor, single_pass=single)
    try:
        d.launch_script(script, marker="snap_lan")
    except Exception as e:
        return fail("Snapshot started on the device", str(e))

    fw = sess.wait("verify", "fw", FW_HASH_TIMEOUT)
    boot = sess.wait("verify", "boot", BOOT_HASH_TIMEOUT)
    if not fw or not boot or fw.get("error") or boot.get("error") \
            or fw["bytes"] != S.FW_END - S.FW_START or boot["bytes"] != S.FW_START:
        return fail("Firmware region hashed", "no or short data from the device")

    out = []
    for i, off, n in chunks:
        raw, err = with_spinner(wait_for_report, f"snap_{i}", timeout=CHUNK_TIMEOUT,
                                message=f"Chunk {i + 1}/{len(chunks)}: {spin}")
        kv = S.parse_kv(raw or "")
        up = sess.wait("uploads", i, UPLOAD_SETTLE_TIMEOUT)
        if err or raw is None:
            return fail(f"Chunk {i}", "no report from the device")
        if kv.get("UP_RC") != "0" or not up or up.get("error") or up["bytes"] != n:
            return fail(f"Chunk {i}", f"transfer failed (curl rc={kv.get('UP_RC')}, "
                                      f"{(up or {}).get('bytes', 0)} of {n} bytes)")
        if kv.get("VSHA") == "host":
            v = sess.wait("verify", f"c{i}", VERIFY_SETTLE_TIMEOUT)
            vsha = (v or {}).get("sha256") if v and v.get("bytes") == n else None
        else:
            vsha = kv.get("VSHA")
        if vsha != up["sha256"]:
            return fail(f"Chunk {i}", "device hash does not match what landed on the PC")
        out.append({"index": i, "offset": off, "bytes": n, "sha256": up["sha256"],
                    "file": up["file"], "compression": up["compression"]})
        step(0, f"Chunk {i + 1}/{len(chunks)} {done}", True)

    nor_rec = None
    if nor:
        raw = wait_for_report("snap_nor", timeout=NOR_DUMP_TIMEOUT)
        rec = sess.wait("nor", None, UPLOAD_SETTLE_TIMEOUT)
        if S.parse_kv(raw or "").get("NOR_RC") == "0" and rec and not rec.get("error"):
            nor_rec = rec
            step(0, f"NOR dumped read-only ({rec['bytes'] // S.MIB} MiB, never restored)", True)
        else:
            step(0, "NOR dump (optional)", False, "skipped — the eMMC snapshot is unaffected")
            (partial / "nor.bin").unlink(missing_ok=True)

    m = _manifest(cap, chunks=out, fw_sha=fw["sha256"], boot_sha=boot["sha256"],
                  transfer="lan", nor_rec=nor_rec)
    return _publish_host(partial, folder / label, m, cap)


def _publish_host(partial: Path, final: Path, m: dict, cap: Capture) -> Optional[dict]:
    (partial / "manifest.json").write_text(S.manifest_json(m))
    for name, text in _text_files(cap).items():
        (partial / name).write_text(text)
    if cap.nor_env:
        (partial / S.NOR_ENV_FILE).write_bytes(cap.nor_env)
    sums = {p.name: S.sha256_file(p) for p in sorted(partial.iterdir())
            if p.is_file() and p.name != "SHA256SUMS"}
    (partial / "SHA256SUMS").write_text(S.sha256sums_text(sums))
    if final.exists():
        step(0, "Snapshot saved", False, f"{final} already exists")
        return None
    partial.rename(final)
    step(0, f"Snapshot saved: {final}", True)
    return m


# --- USB snapshot -------------------------------------------------------------------------

def _poll_log(d, done_key: str, progress_prefix: str, total: int, label: str,
              timeout: float) -> dict:
    end = time.time() + timeout
    seen = -1
    kv = {}
    while time.time() < end:
        try:
            kv = S.parse_kv(d.run_script(S.build_log_script(), marker="snap_log", exec_timeout=LOG_POLL_TIMEOUT))
        except Exception as e:
            verbose(f"  log poll: {e}", "debug")
            kv = {}
        n = sum(1 for k in kv if k.startswith(progress_prefix + "_"))
        if n != seen:
            seen = n
            _say(f"  {label}: {n}/{total}")
        if kv.get(done_key) == "1":
            return kv
        time.sleep(USB_POLL)
    kv["TIMEOUT"] = "1"
    return kv


def usb_mount(d, mode: str) -> Optional[dict]:
    try:
        out = d.run_script(S.build_usb_mount_script(mode=mode), marker="snap_mount", exec_timeout=MOUNT_TIMEOUT)
    except Exception as e:
        verbose(f"  USB mount: {e}", "warning")
        return None
    m = S.parse_usb_mount(out)
    return m if m["ok"] and m["part"] else None


def snapshot_usb(d, cap: Capture) -> Optional[dict]:
    """Whole eMMC -> mono-snapshots/<label> on the stick. Returns the manifest, or None."""
    label, probe, nor = cap.label, cap.probe, cap.nor_dump
    have = probe["have"]
    comp = S.device_compression(have)
    chunks = S.plan_chunks(probe["emmc_bytes"])
    try:
        out = d.run_script(S.build_usb_snapshot_script(label=label, chunks=chunks, compression=comp,
                                                       have=have, nor=nor),
                           marker="snap_usb", exec_timeout=USB_START_TIMEOUT)
    except Exception as e:
        step(0, "Snapshot started on the device", False, str(e))
        return None
    if "STARTED=1" not in out:
        step(0, "Snapshot started on the device", False, out.strip()[-C.LOG_TAIL:])
        return None
    _say(f"  Writing {_gib(probe['emmc_bytes'])} to the stick ({comp}); this takes a while.")
    kv = _poll_log(d, "SNAP_DONE", "C", len(chunks), "chunks captured + verified",
                   timeout=CHUNK_TIMEOUT * len(chunks))
    res = S.parse_usb_chunk_results(kv, "C")
    ok = kv.get("SNAP_DONE") == "1" and "FAILED" not in kv and len(res) == len(chunks) \
        and all(r[0] == "0" and r[1] == r[2] for r in res.values()) \
        and len(kv.get("FW_SHA", "")) == S.SHA256_HEX_LEN
    if not ok:
        _stop_device_job(d)
        try:
            d.run_script(S.build_usb_discard_script(label=label), marker="snap_discard", exec_timeout=DISCARD_TIMEOUT)
        except Exception:
            pass
        why = "timed out" if kv.get("TIMEOUT") else f"failed at chunk {kv.get('FAILED', '?')}"
        step(0, "Snapshot on the USB stick", False, why + " — read-back mismatch or write error; discarded")
        return None
    out_chunks = [{"index": i, "offset": off, "bytes": n, "sha256": res[i][2],
                   "file": S.chunk_file(i, comp), "compression": comp} for i, off, n in chunks]
    nor_rec = None
    if nor and kv.get("NOR", "").startswith("0 "):
        nor_rec = {"file": "nor.bin", "sha256": kv["NOR"].split()[1]}
    if cap.nor_env:
        want = hashlib.sha256(cap.nor_env).hexdigest()
        try:
            got = S.parse_kv(d.run_script(S.build_usb_nor_env_save_script(label=label),
                                          marker="snap_norenv", exec_timeout=DUMP_TIMEOUT))
        except Exception as e:
            got = {"ERR": str(e)}
        if got.get("NOR_ENV_SHA") != want:
            step(0, "NOR U-Boot env saved with the snapshot", False,
                 "copy on the stick differs from what was read — not kept")
            cap.nor_env = None
    m = _manifest(cap, chunks=out_chunks, fw_sha=kv["FW_SHA"], boot_sha=kv.get("BOOT_SHA", ""),
                  transfer="usb", nor_rec=nor_rec)
    try:
        fin = d.run_script(S.build_usb_finalize_script(label=label, manifest_text=S.manifest_json(m),
                                                       text_files=_text_files(cap)),
                           marker="snap_final", exec_timeout=FINALIZE_TIMEOUT)
    except Exception as e:
        fin = str(e)
    if "FINAL=ok" not in fin:
        step(0, "Snapshot saved on the USB stick", False, fin.strip()[-C.LOG_TAIL:])
        return None
    step(0, f"Snapshot saved on the USB stick: {S.USB_ROOT}/{label}", True)
    return m


def usb_read_manifest(d, label: str) -> tuple:
    """(manifest or None, env dict) of a snapshot on the stick."""
    try:
        m = json.loads(S.parse_cat(d.run_script(S.build_usb_cat_script(label=label, name="manifest.json"),
                                                marker="snap_cat", exec_timeout=CAT_TIMEOUT)))
    except Exception as e:
        verbose(f"  manifest of {label}: {e}", "warning")
        return None, {}
    env = {}
    if m.get("uboot_env_file"):
        try:
            env = S.parse_env_file(S.parse_cat(d.run_script(
                S.build_usb_cat_script(label=label, name=m["uboot_env_file"]),
                marker="snap_cat_env", exec_timeout=CAT_TIMEOUT)))
        except Exception:
            env = {}
    return m, env


# --- Restore (write) -----------------------------------------------------------------------

def restore_lan(d, sess, base_url: str, folder: Path, manifest: dict, ranges: list,
                have_sha: bool) -> tuple:
    ok, why = _restore_lan(d, sess, base_url, folder, manifest, ranges, have_sha)
    if not ok:
        _stop_device_job(d)   # the write script may still be running: stop it
        time.sleep(1)         # before anything else is typed into the shell
    return ok, why


def _restore_lan(d, sess, base_url, folder, manifest, ranges, have_sha) -> tuple:
    sess.begin_restore(folder, manifest)
    try:
        d.launch_script(S.build_lan_restore_script(base_url=base_url, token=sess.token,
                                                   ranges=ranges, have_sha=have_sha),
                        marker="snap_restore")
    except Exception as e:
        return False, f"could not start the write on the device: {e}"
    for k, r in enumerate(ranges):
        raw, err = with_spinner(wait_for_report, f"rst_{k}", timeout=CHUNK_TIMEOUT,
                                message=f"Range {k + 1}/{len(ranges)}: PC -> eMMC, re-hash")
        if err or raw is None:
            return False, f"no report from the device for range {k}"
        kv = S.parse_kv(raw)
        sent = sess.wait("sent", k, UPLOAD_SETTLE_TIMEOUT)
        if kv.get("CURL_RC") != "0" or kv.get("DD_RC") != "0":
            return False, f"range {k}: transfer/write failed (curl {kv.get('CURL_RC')}, dd {kv.get('DD_RC')})"
        if not sent or sent.get("error") or sent["bytes"] != r["len"]:
            return False, f"range {k}: PC could not serve it ({(sent or {}).get('error', 'no data')})"
        if sent.get("manifest_ok") is False:
            return False, f"range {k}: chunk {r['chunk']} does not match its manifest hash"
        if kv.get("VSHA") == "host":
            v = sess.wait("verify", f"r{k}", VERIFY_SETTLE_TIMEOUT)
            vsha = (v or {}).get("sha256") if v and v.get("bytes") == r["len"] else None
        else:
            vsha = kv.get("VSHA")
        if vsha != sent["sha256"]:
            return False, f"range {k}: on-device re-hash does not match what was sent"
        step(0, f"Range {k + 1}/{len(ranges)} written and re-hashed on the device", True)
    return True, ""


def restore_usb(d, label: str, manifest: dict, ranges: list) -> tuple:
    try:
        out = d.run_script(S.build_usb_restore_script(label=label, manifest=manifest, ranges=ranges),
                           marker="snap_urestore", exec_timeout=USB_START_TIMEOUT)
    except Exception as e:
        return False, f"could not start the write on the device: {e}"
    if "STARTED=1" not in out:
        return False, "could not start the write on the device"
    kv = _poll_log(d, "RESTORE_DONE", "R", len(ranges), "ranges written + re-hashed",
                   timeout=CHUNK_TIMEOUT * len(ranges))
    if kv.get("SUMS") != "ok":
        return False, f"SHA256SUMS check on the stick: {kv.get('SUMS', 'not run')} — nothing written"
    res = S.parse_usb_chunk_results(kv, "R")
    if kv.get("RESTORE_DONE") != "1" or "FAILED" in kv or len(res) != len(ranges):
        return False, f"failed at range {kv.get('FAILED', '?')}" + (" (timed out)" if kv.get("TIMEOUT") else "")
    bad = [k for k, (rc, a, b) in res.items() if rc != "0" or a != b]
    if bad:
        return False, f"on-device re-hash mismatch in range(s) {bad}"
    return True, ""


def _device_fw_sha_usb(d) -> Optional[str]:
    script = f"DEV={S.EMMC_DEV}\n{S._RD_FN}\necho \"CURFW=$(rd {S.FW_START} {S.FW_END - S.FW_START} | sha)\"\n"
    try:
        v = S.parse_kv(d.run_script(script, marker="snap_curfw", exec_timeout=FW_REGION_HASH_TIMEOUT)).get("CURFW", "")
    except Exception:
        return None
    return v if len(v) == S.SHA256_HEX_LEN else None


def _device_fw_sha_lan(d, sess, base_url) -> Optional[str]:
    try:
        d.launch_script(S.build_lan_hash_script(base_url=base_url, token=sess.token, start=S.FW_START,
                                                length=S.FW_END - S.FW_START, tag="curfw"),
                        marker="snap_curfw")
    except Exception:
        return None
    wait_for_report("hash_curfw", timeout=FW_REGION_REPORT_TIMEOUT)
    v = sess.wait("verify", "curfw", UPLOAD_SETTLE_TIMEOUT)
    return v["sha256"] if v and v.get("bytes") == S.FW_END - S.FW_START else None


# --- NOR U-Boot env (read always, write only on opt-in) ---------------------------------------

def read_nor_env(d) -> Optional[bytes]:
    """Raw NOR env region via `od` over serial (read-only), or None."""
    try:
        out = d.run_script(S.build_nor_env_dump_script(), marker="snap_norenv_dump",
                           exec_timeout=DUMP_TIMEOUT)
    except Exception as e:
        verbose(f"  NOR env read: {e}", "warning")
        return None
    blob = S.parse_od(out)
    return blob if blob and len(blob) == S.NOR_ENV_SIZE else None


def _env_bootcmd_line(env: Optional[dict]) -> str:
    return (env or {}).get("bootcmd", "(none)")


def offer_nor_env_restore(d, *, transfer: str, sess, base_url: Optional[str],
                          folder: Optional[Path], label: Optional[str], manifest: dict,
                          snap_vars: Optional[dict], current_blob: Optional[bytes],
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
        step(0, "NOR U-Boot env already matches the snapshot", True)
        return True
    if not {"flashcp", "sha256sum"} <= have:
        _say("  ⚠ flashcp/sha256sum missing on the device — NOR env cannot be written here.")
        return False
    current_vars = S.parse_uboot_env_blob(current_blob)
    _say("")
    _say("  The NOR U-Boot env differs from the one saved with this snapshot.")
    _say(f"    NOR bootcmd now:      {_env_bootcmd_line(current_vars)}")
    _say(f"    Snapshot's bootcmd:   {_env_bootcmd_line(snap_vars)}")
    _say("  Writing it touches NOR (only the 8 KiB env, never the firmware).")
    if ask("  Write the snapshot's NOR U-Boot env? [y/N]: ").lower() != "y":
        return False
    if transfer == "usb":
        src = f"{S.USB_MOUNT}/{S.USB_ROOT}/{label}/{rec['file']}"
        fetch = ""
    else:
        src = S.NOR_ENV_TMP
        url = f"{base_url}/file?token={sess.token}&name={rec['file']}"
        fetch = f"curl -s -o {src} '{url}' || echo NOR_ENV_FETCH=fail\n"
    try:
        out = d.run_script(fetch + S.build_nor_env_write_script(src, want),
                           marker="snap_norenv_write", exec_timeout=NOR_ENV_WRITE_TIMEOUT)
    except Exception as e:
        out = str(e)
    result = S.parse_kv(out).get("NOR_ENV_WRITE", "no answer")
    return step(0, "NOR U-Boot env written and read back", result == "ok", result)


def nor_env_advice(manifest: dict, snap_env: dict, current_env: dict, current_from_nor: bool) -> list:
    """Lines telling the user which NOR env the restored OS needs (when it was not written)."""
    if manifest.get("boot_dip") != DIP_NOR or not snap_env.get("bootcmd"):
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

def read_emmc_env(d) -> Optional[bytes]:
    """Raw eMMC U-Boot env region (inside the firmware region) via `od`, or None."""
    try:
        blob = S.parse_od(d.run_script(S.build_emmc_env_dump_script(), marker="snap_emmc_env",
                                       exec_timeout=DUMP_TIMEOUT))
    except Exception as e:
        verbose(f"  eMMC env read: {e}", "warning")
        return None
    return blob if blob and len(blob) == S.NOR_ENV_SIZE else None


def collect_inventory(d, probe: dict, ntp_synced: bool) -> dict:
    """Platform inventory (read-only) + the U-Boot banner caught on serial."""
    try:
        out = d.run_script(I.build_inventory_script(), marker="snap_inventory",
                           exec_timeout=INVENTORY_TIMEOUT)
    except Exception as e:
        verbose(f"  inventory: {e}", "warning")
        out = ""
    inv = I.parse_inventory(S.parse_kv(out), ntp_synced=ntp_synced,
                            boot_output=getattr(d, "last_boot_output", None), probe=probe)
    step(0, "Platform inventory collected", True,
         "" if inv["complete"] else "partial — the device did not answer every question")
    return inv


def _sync_clock(d) -> bool:
    from mono_imager.recovery_orchestrator import sync_device_clock
    try:
        return bool(sync_device_clock(d))
    except Exception:
        return False


# --- Firmware check before a restore -------------------------------------------------------

def firmware_gate(target_inv: dict, snap_inv: Optional[dict], *, clone: bool, ask: Callable) -> bool:
    """
    Compare this board's U-Boot versions with the snapshot's. NOR is never
    written by a restore, so an older NOR is worth updating FIRST (menu 3).
    eMMC: same board -> the snapshot's firmware region comes back with the
    image; clone -> `firmware update` provisions it before the write.
    Returns True to go ahead.
    """
    if not snap_inv:
        _say("  Firmware check: the snapshot has no inventory (older format) — skipped.")
        return True
    t_nor, s_nor = I.nor_uboot(target_inv), I.nor_uboot(snap_inv)
    t_emmc, s_emmc = I.emmc_uboot(target_inv), I.emmc_uboot(snap_inv)
    nor_cmp = I.compare_uboot(t_nor, s_nor)
    _say("")
    _say("  Firmware (this board vs the snapshot):")
    _say(f"    NOR   now:  {t_nor or '?'}")
    _say(f"          snap: {s_nor or '?'}   -> this board is {nor_cmp}")
    _say(f"    eMMC  now:  {t_emmc or '?'}")
    _say(f"          snap: {s_emmc or '?'}")
    if clone:
        _say("    eMMC firmware: provisioned by `firmware update` BEFORE the write.")
    else:
        _say("    eMMC firmware: comes back from the snapshot when it differs.")
    step(0, f"Firmware check: NOR {nor_cmp}", True)
    if nor_cmp != "older":
        return True
    _say("  ⚠ This board's NOR firmware is OLDER than the one the snapshot was taken with.")
    _say("    A restore never writes NOR firmware. Recommended: stop now, run")
    _say("    'Update NOR firmware' (main menu 3), then restore.")
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
    hint = _CLONE_HINTS.get(os_label)
    lines.append(f"    Check: {hint}" if hint else
                 "    Check the OS's network config for settings pinned to MAC addresses.")
    return lines


# --- Boot from either DIP position (opt-in) ---------------------------------------------------

def _upload(d, blob: bytes, dst: str) -> bool:
    """Send a small blob over serial in pieces; True when the device's copy hashes right."""
    out = ""
    try:
        for n, script in enumerate(S.build_upload_blob_scripts(blob, dst)):
            out = d.run_script(script, marker=f"snap_upload{n}",
                               write_timeout=UPLOAD_WRITE_TIMEOUT, exec_timeout=UPLOAD_WRITE_TIMEOUT)
    except Exception as e:
        verbose(f"  upload: {e}", "warning")
        return False
    return S.parse_kv(out).get("UPLOAD_SHA") == hashlib.sha256(blob).hexdigest()


def offer_dual_boot(d, manifest: dict, *, have: set, ask: Callable) -> bool:
    """
    Copy the restored OS's boot variables (bootcmd + everything it runs,
    never `recovery`) into the OTHER U-Boot env, so it boots with the DIP
    in either position. Opt-in; one env region is rewritten, read back and
    compared. Returns True when both envs now boot the OS.
    """
    boot_dip = manifest.get("boot_dip") or DIP_EMMC
    emmc, nor = E.parse(read_emmc_env(d)), E.parse(read_nor_env(d))
    if boot_dip == DIP_NOR:
        src, dst, dst_name = nor, emmc, "eMMC"
    else:
        src, dst, dst_name = emmc, nor, "NOR"
    if not src or not dst or "bootcmd" not in src.vars:
        _say("  Boot from either DIP position: an env region could not be read — not offered.")
        return False
    changes = E.merge_for_boot(dst.vars, src.vars)
    if not changes:
        step(0, "Boots with the DIP in either position (envs already agree)", True)
        return True
    if dst.redundant:
        _say(f"  Boot from either DIP position: the {dst_name} env is a redundant env — not offered.")
        return False
    need = {"sha256sum"} | ({"flashcp"} if dst_name == "NOR" else set())
    if not need <= have:
        _say(f"  Boot from either DIP position: {', '.join(sorted(need - have))} missing — not offered.")
        return False
    _say("")
    _say(f"  To also boot with the DIP on {DIP_TEXT[DIP_EMMC if dst_name == 'eMMC' else DIP_NOR]},")
    _say(f"  these variables would be written into the {dst_name} U-Boot env:")
    for k, v in changes.items():
        _say(f"    {k}={v[:C.LOG_TAIL_SHORT]}{'…' if len(v) > C.LOG_TAIL_SHORT else ''}")
    what = "touches NOR: only its env region, never the firmware" if dst_name == "NOR" \
        else "eMMC env region only"
    if ask(f"  Make it boot whatever the DIP position? ({what}) [y/N]: ").lower() != "y":
        return False
    try:
        blob = E.build(E.EnvRegion(dict(dst.vars, **changes), dst.redundant, dst.flag, dst.size))
    except ValueError as e:
        return step(0, f"{dst_name} env rebuilt", False, str(e))
    want = hashlib.sha256(blob).hexdigest()
    if not step(0, f"New {dst_name} env sent to the device", _upload(d, blob, S.ENV_UPLOAD_TMP)):
        return False
    script = (S.build_nor_env_write_script(S.ENV_UPLOAD_TMP, want) if dst_name == "NOR"
              else S.build_emmc_env_write_script(S.ENV_UPLOAD_TMP, want))
    key = "NOR_ENV_WRITE" if dst_name == "NOR" else "EMMC_ENV_WRITE"
    try:
        result = S.parse_kv(d.run_script(script, marker="snap_dual_boot",
                                         exec_timeout=NOR_ENV_WRITE_TIMEOUT)).get(key, "no answer")
    except Exception as e:
        result = str(e)
    return step(0, f"{dst_name} env updated — boots with the DIP in either position",
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
            head = S.parse_od(d.run_script(S.build_dump_script(S.EMMC_DEV, 0, S.GPT_STD_SECTORS),
                                           marker="snap_gpt_head", exec_timeout=DUMP_TIMEOUT))
            tail = S.parse_od(d.run_script(S.build_dump_script(S.EMMC_DEV, emmc // S.SECTOR - 1, 1),
                                           marker="snap_gpt_tail", exec_timeout=DUMP_TIMEOUT))
        except Exception as e:
            head = tail = None
            verbose(f"  GPT read: {e}", "warning")
        if head and len(head) == S.GPT_STD_SECTORS * S.SECTOR and tail and len(tail) == S.SECTOR:
            gpt = S.check_gpt(head, tail, emmc)
            health["gpt"] = {"status": gpt["status"],
                             "findings": [list(f) for f in gpt["findings"]],
                             "partitions": gpt["partitions"]}
            worst(gpt["status"])
        else:
            health["notes"].append("GPT area could not be read over serial — not checked")
            worst("warn")          # unchecked is never "ok"
    try:
        kv = S.parse_kv(d.run_script(S.build_fs_check_script(), marker="snap_fsck",
                                     exec_timeout=FSCHECK_TIMEOUT))
    except Exception as e:
        kv = {}
        verbose(f"  filesystem check: {e}", "warning")
    if kv.get("FSCHECK") != "done":
        health["notes"].append("filesystem check did not finish — not checked")
        worst("warn")          # unchecked is never "ok"
    for n, r in S.parse_fs_check(kv).items():
        health["filesystems"][str(n)] = r
        worst(r["level"])
    return health


def review_health(health: dict, ask: Callable) -> bool:
    """Show the check; on errors the user decides. True = go ahead."""
    icon = {"ok": "✓", "warn": "⚠", "error": "✗", "none": "·"}
    _say("")
    _say("  Health check (read-only):")
    for level, text in (health.get("gpt") or {}).get("findings", []):
        _say(f"    {icon.get(level, '·')} {text}")
    for n, r in sorted(health["filesystems"].items(), key=lambda x: int(x[0])):
        what = {"ok": "clean", "error": f"errors found (rc {r['result']})",
                "none": "not checked" + (" (no fsck tool)" if r["result"] == "notool" else "")}
        _say(f"    {icon.get(r['level'], '·')} partition {n} ({r['type']}): {what[r['level']]}")
    for note in health["notes"]:
        _say(f"    · {note}")
    step(0, f"Health check: {health['status']}", True)
    if health["status"] != "error":
        return True
    _say("  ⚠ The snapshot would capture these errors as they are.")
    return ask("  Snapshot anyway? [y/N]: ").lower() == "y"


# --- Journeys ---------------------------------------------------------------------------------

def run_snapshot(*, port: str, transfer: str, host_ip: str, http_port: int,
                 setup_network: Callable, store: Path,
                 soft_reboot: Optional[Callable] = None, ask: Callable = _ask) -> bool:
    """Snapshot journey. Returns print_report()'s verdict."""
    from mono_imager.snapshot_server import SnapshotSession, start_snapshot_server
    from mono_imager.flash_orchestrator import reset_results
    reset_results()
    dip_now = ask_dip(ask)
    d, info = reach_recovery(port, dip_now, soft_reboot, ask)
    info["dip_now"] = dip_now
    if d is None:
        return print_report()
    server = None
    try:
        probe = probe_device(d)
        if not step(0, "Device identity and eMMC size read", probe is not None):
            return print_report()
        env = info["env"]
        identity = _identity(probe, env)
        _say(f"  Board: {identity['model'] or 'Mono'}  MAC {identity['mac'] or '?'}  "
             f"serial {identity['serial'] or '?'}")
        _say(f"  eMMC:  {_gib(probe['emmc_bytes'])}")
        if probe["emmc_mounted"]:
            step(0, "eMMC not mounted by recovery Linux", False, "unmount it first")
            return print_report()

        os_label = S.detect_os_label(env)
        ntp = False
        if transfer == "lan":
            if not setup_network(d):
                step(0, "Device network ready", False)
                return print_report()
            ntp = _sync_clock(d)
        inventory = collect_inventory(d, probe, ntp)
        emmc_env = read_emmc_env(d)
        mnt = None
        if transfer == "usb":
            if not probe["usb_present"]:
                step(0, "USB stick in the Mono", False, f"no {S.USB_DEV}")
                return print_report()
            if "sha256sum" not in probe["have"]:
                step(0, "sha256sum available on the device", False, "needed for USB snapshots")
                return print_report()
            mnt = usb_mount(d, "snapshot")
            if not step(0, "USB stick mounted", mnt is not None):
                return print_report()
            if mnt["free_kb"] * C.KIB < probe["emmc_bytes"]:
                _say(f"  ⚠ Only {mnt['free_kb'] * C.KIB / C.GIB:.1f} GiB free on the stick; the snapshot can need "
                     f"up to {_gib(probe['emmc_bytes'])} (less if it compresses well).")
                if ask("  Try anyway? [y/N]: ").lower() != "y":
                    return step(0, "Snapshot", False, "cancelled") or print_report()
        else:
            store.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(store).free
            if free < probe["emmc_bytes"]:
                _say(f"  ⚠ Only {_gib(free)} free in {store}; the snapshot can need up to "
                     f"{_gib(probe['emmc_bytes'])}.")
                if ask("  Try anyway? [y/N]: ").lower() != "y":
                    return step(0, "Snapshot", False, "cancelled") or print_report()

        nor_env = read_nor_env(d)
        if nor_env:
            step(0, "NOR U-Boot env read (kept with the snapshot)", True)
        else:
            _say("  ⚠ NOR U-Boot env not readable — the snapshot continues without it.")
        if dip_now == DIP_NOR and nor_env:
            os_label = S.detect_os_label(S.parse_uboot_env_blob(nor_env)) or os_label

        health = health_check(d, probe)
        if not review_health(health, ask):
            return step(0, "Snapshot", False, "cancelled after the health check") or print_report()

        label = _ask_label(os_label, ask, existing=(mnt["snapshots"] if mnt else
                                                    [n for n, _ in S.list_snapshots(store)]
                                                    + [p.name for p in store.iterdir()]))
        nor = False
        if probe["nor_present"]:
            nor = ask("  Also keep a read-only NOR dump for forensics (never restored)? [y/N]: ").lower() == "y"
        thorough = False
        if transfer == "lan":
            thorough = ask("  Thorough verify: re-read every chunk from the eMMC (~2x slower)? [y/N]: ").lower() == "y"
        cap = Capture(label=label, probe=probe, identity=identity, env=env, os_label=os_label,
                      boot_dip=dip_now, nor_dump=nor, nor_env=nor_env, health=health,
                      inventory=inventory, emmc_env=emmc_env, thorough=thorough)

        if transfer == "usb":
            m = snapshot_usb(d, cap)
        else:
            sess = SnapshotSession()
            server = start_snapshot_server(host_ip, http_port, sess)
            if not step(0, f"Snapshot server up ({host_ip}:{http_port})", server is not None):
                return print_report()
            m = snapshot_lan(d, sess, _base_url(host_ip, http_port), store, cap)
        if m is not None:
            _finish_device(d, info, dip_now)
    finally:
        if server:
            server.shutdown()
        d.disconnect()
    return print_report()


def _ask_label(os_label: str, ask: Callable, existing) -> str:
    existing = set(existing)
    default = S.default_label(os_label)
    while True:
        v = ask(f"  Snapshot label [{default}]: ") or default
        if not S.valid_label(v):
            _say("  Use letters, digits, '.', '_' or '-' (max 64).")
        elif v in existing or f"{v}.partial" in existing:
            _say(f"  '{v}' already exists — pick another label.")
        else:
            return v


def pick_snapshot(snaps: list, ask: Callable = _ask) -> Optional[int]:
    """snaps: [(label, manifest or None)] -> index or None."""
    if not snaps:
        return None
    _say("")
    for i, (label, m) in enumerate(snaps, 1):
        m = m or {}
        size = _gib(m["emmc"]["bytes"]) if m.get("emmc") else "?"
        extra = " (auto)" if m.get("auto") else ""
        _say(f"  {i}) {label}{extra}  — {m.get('os_label', '?')}, {size}, "
             f"{m.get('created', '?')}, MAC {m.get('device', {}).get('mac', '?')}")
    _say(f"  {len(snaps) + 1}) Back")
    while True:
        c = ask(f"  Select [1-{len(snaps) + 1}]: ")
        if c.isdigit() and 1 <= int(c) <= len(snaps):
            return int(c) - 1
        if c == str(len(snaps) + 1):
            return None


def run_restore(*, port: str, transfer: str, host_ip: str, http_port: int,
                setup_network: Callable, store: Path, folder: Optional[Path] = None,
                manifest: Optional[dict] = None, auto_snapshot: bool = True,
                soft_reboot: Optional[Callable] = None, ask: Callable = _ask) -> bool:
    """
    Restore journey. For LAN the snapshot is picked (folder/manifest) by the
    caller and checked against SHA256SUMS here before anything else; for USB
    it is picked from the stick once recovery Linux is up.
    """
    from mono_imager.snapshot_server import SnapshotSession, start_snapshot_server
    from mono_imager.flash_orchestrator import reset_results
    reset_results()

    snap_env = {}
    if transfer == "lan":
        res, err = with_spinner(S.verify_store_snapshot, folder,
                                message="Checking the snapshot against SHA256SUMS...")
        ok, why, manifest = res if not err else (False, str(err), None)
        if not step(0, "Snapshot verified on the PC (SHA256SUMS)", ok, why):
            return print_report()
        if manifest.get("uboot_env_file") and (folder / manifest["uboot_env_file"]).is_file():
            snap_env = S.parse_env_file((folder / manifest["uboot_env_file"]).read_text())

    dip_now = ask_dip(ask)
    d, info = reach_recovery(port, dip_now, soft_reboot, ask)
    info["dip_now"] = dip_now
    if d is None:
        return print_report()
    server = None
    try:
        probe = probe_device(d)
        if not step(0, "Device identity and eMMC size read", probe is not None):
            return print_report()
        if probe["emmc_mounted"]:
            step(0, "eMMC not mounted by recovery Linux", False, "unmount it first")
            return print_report()
        have = probe["have"]
        mnt, label = None, None
        if transfer == "usb":
            if "sha256sum" not in have or "head" not in have or "tail" not in have:
                step(0, "sha256sum/head/tail available on the device", False, "needed for USB restore")
                return print_report()
            mnt = usb_mount(d, "restore")
            if not step(0, "USB stick with snapshots mounted", mnt is not None and bool(mnt["snapshots"]),
                        "" if mnt else f"no {S.USB_ROOT}/ folder found on {S.USB_DEV}"):
                return print_report()
            listed = []
            for lab in mnt["snapshots"]:
                m, env = usb_read_manifest(d, lab)
                listed.append((lab, m, env))
            usable = [x for x in listed if x[1] and not S.validate_manifest(x[1])]
            idx = pick_snapshot([(lab, m) for lab, m, _ in usable], ask)
            if idx is None:
                return step(0, "Snapshot picked", False, "cancelled") or print_report()
            label, manifest, snap_env = usable[idx]
            comps = {c["compression"] for c in manifest["chunks"]}
            missing = [c for c in comps if not S.can_decompress(c, have)]
            if not step(0, "Device can decompress the snapshot", not missing,
                        f"no decompressor for {missing} — restore it over LAN instead"):
                return print_report()

        identity = _identity(probe, info["env"])
        target_inv = collect_inventory(d, probe, ntp_synced=False)
        current_nor_env = read_nor_env(d)
        current_emmc_env = read_emmc_env(d)
        snap_nor_vars = _snapshot_nor_env_vars(d, transfer, folder, label, manifest)
        match, snap_macs, here_macs = S.mac_match(manifest, identity, target_inv.get("macs"))
        clone = match == "other"
        step(0, f"Snapshot MACs vs this board: {match}", True)
        if match == "partial":
            _say(f"  ⚠ Some MACs differ (snapshot {', '.join(sorted(snap_macs))};"
                 f" here {', '.join(sorted(here_macs))}) — treated as this board.")
        if clone:
            _say("")
            for line in clone_warning(manifest, snap_macs, here_macs):
                _say(line)
            _say("  Clone mode: `firmware update` provisions this board first, then only")
            _say("  0..4 KiB and 32 MiB..end are written; this board keeps its firmware region.")
            if ask("  Clone onto this board? [y/N]: ").lower() != "y":
                return step(0, "Snapshot belongs to this board", False, "clone mode declined") or print_report()
            if probe["boot_medium"] != C.BOOT_MEDIUM_NOR:
                step(0, "Running NOR recovery (needed for clone mode's firmware update)", False,
                     f"this is {probe['boot_medium'] or '?'} recovery — set the DIP switch to "
                     f"{DIP_TEXT[DIP_NOR]}, power-cycle and run the restore again")
                return print_report()
        if not firmware_gate(target_inv, manifest.get("inventory"), clone=clone, ask=ask):
            return step(0, "Restore", False, "stopped to update the firmware first") or print_report()
        size = manifest["emmc"]["bytes"]
        if not step(0, f"Target eMMC large enough ({_gib(probe['emmc_bytes'])} >= {_gib(size)})",
                    probe["emmc_bytes"] >= size, "target eMMC is smaller — refused"):
            return print_report()

        sess = base_url = None
        if transfer == "lan" or clone:
            if not setup_network(d):
                step(0, "Device network ready", False)
                return print_report()
        if transfer == "lan":
            sess = SnapshotSession()
            server = start_snapshot_server(host_ip, http_port, sess)
            if not step(0, f"Snapshot server up ({host_ip}:{http_port})", server is not None):
                return print_report()
            base_url = _base_url(host_ip, http_port)

        # Undo point: auto-snapshot the current eMMC first.
        auto = None
        if auto_snapshot:
            taken = set(mnt["snapshots"]) if transfer == "usb" else \
                ({x.name for x in store.iterdir()} if store.is_dir() else set())
            auto_label = base_auto = S.default_label(f"auto-{S.detect_os_label(info['env'])}")
            n = 2
            while auto_label in taken or f"{auto_label}.partial" in taken:
                auto_label, n = f"{base_auto}-{n}", n + 1
            room = (mnt["free_kb"] * C.KIB) if transfer == "usb" else shutil.disk_usage(store).free
            go = True
            if room < probe["emmc_bytes"]:
                _say(f"  ⚠ Possibly not enough space for the auto-snapshot ({_gib(room)} free, "
                     f"up to {_gib(probe['emmc_bytes'])} needed).")
                go = ask("  Try the auto-snapshot anyway? [Y/n]: ").lower() != "n"
            if go:
                _say(f"  Auto-snapshot of the current eMMC: {auto_label}")
                cap = Capture(label=auto_label, probe=probe, identity=identity, env=info["env"],
                              os_label=S.detect_os_label(info["env"]), boot_dip=dip_now,
                              nor_env=current_nor_env, inventory=target_inv,
                              emmc_env=current_emmc_env, auto=True)
                if transfer == "usb":
                    m = snapshot_usb(d, cap)
                else:
                    store.mkdir(parents=True, exist_ok=True)
                    m = snapshot_lan(d, sess, base_url, store, cap)
                if m is not None:
                    auto = (auto_label, m)
            if auto is None:
                _say("  ⚠ No auto-snapshot: this restore cannot be undone.")
                if ask("  Continue WITHOUT an undo point? [y/N]: ").lower() != "y":
                    return step(0, "Restore", False, "cancelled") or print_report()

        # Firmware region identical? Then skip it (fewer erase cycles).
        skip_fw = clone
        if not clone:
            cur = auto[1]["emmc"]["fw_region"]["sha256"] if auto else (
                _device_fw_sha_usb(d) if transfer == "usb" else _device_fw_sha_lan(d, sess, base_url))
            skip_fw = bool(cur) and cur == manifest["emmc"]["fw_region"]["sha256"]
        ranges = S.plan_restore(manifest, skip_fw)
        total = sum(r["len"] for r in ranges)

        src = f"{S.USB_ROOT}/{label} on the USB stick" if transfer == "usb" else str(folder)
        _banner("  RESTORE SUMMARY",
                f"  Source:  {src}",
                f"           {manifest.get('os_label', '?')}, taken {manifest.get('created', '?')}",
                f"  Target:  this board, MAC {identity['mac'] or '?'}, eMMC {_gib(probe['emmc_bytes'])}",
                f"  Writes:  {_gib(total)} — " + (
                    "everything except the firmware region (4 KiB..32 MiB)" if skip_fw
                    else "the ENTIRE eMMC, firmware region included"),
                f"  Mode:    {'CLONE (firmware update first)' if clone else 'same board'}",
                f"  Undo:    {'auto-snapshot ' + auto[0] if auto else 'NONE'}",
                f"  Health:  {(manifest.get('health') or {}).get('status', 'not checked')} "
                "(when the snapshot was taken)",
                f"  NOR env: {'saved with the snapshot (offered after the write)' if manifest.get('nor_env') and not clone else 'not restored'}",
                "  Everything currently on the eMMC is overwritten.")
        if ask("  Type 'yes' to write: ").lower() != "yes":
            return step(0, "Restore confirmed", False, "cancelled") or print_report()

        if clone:   # firmware first, then the restore (it never touches 4 KiB..32 MiB)
            from mono_imager.recovery_orchestrator import run_firmware_update
            fw_ok, err = with_spinner(run_firmware_update, d,
                                      message="Provisioning this board (firmware update)...")
            if not step(0, "Firmware region provisioned (firmware update)", bool(fw_ok) and not err,
                        str(err) if err else ""):
                return print_report()

        ok, why = _write(d, transfer, sess, base_url, folder, label, manifest, ranges, have)
        while not ok:
            step(0, "eMMC restored and re-hashed", False, why)
            _say("  Recovery Linux is still running from RAM.")
            opts = "[r]etry" + (", roll [b]ack to the auto-snapshot" if auto else "") + ", [a]bort"
            c = ask(f"  {opts}: ").lower()
            if c == "r":
                ok, why = _write(d, transfer, sess, base_url, folder, label, manifest, ranges, have)
            elif c == "b" and auto:
                a_label, a_m = auto
                a_folder = store / a_label if transfer == "lan" else None
                ok, why = _write(d, transfer, sess, base_url, a_folder, a_label, a_m,
                                 S.plan_restore(a_m, False), have)
                if ok:
                    step(0, f"Rolled back to {a_label}", True)
                    _finish_device(d, info, a_m.get("boot_dip", dip_now))
                    return print_report()
            elif c == "a":
                _banner("  ⚠ The eMMC may be inconsistent now. Run the restore again,",
                        "  or restore the auto-snapshot, before booting it.")
                return print_report()
        step(0, "eMMC restored and re-hashed on the device", True)

        if clone and probe["emmc_bytes"] > size:
            _fix_backup_gpt(d)

        nor_written = False
        if clone and manifest.get("nor_env"):
            _say("  NOR env not offered in clone mode: it holds this other board's settings.")
        elif not clone:
            nor_written = offer_nor_env_restore(
                d, transfer=transfer, sess=sess, base_url=base_url, folder=folder, label=label,
                manifest=manifest, snap_vars=snap_nor_vars, current_blob=current_nor_env,
                have=have, ask=ask)
        any_dip = offer_dual_boot(d, manifest, have=have, ask=ask)
        if not nor_written and not any_dip:
            cur_vars = S.parse_uboot_env_blob(current_nor_env)
            advice = nor_env_advice(manifest, snap_nor_vars or snap_env, cur_vars or info["env"],
                                    bool(cur_vars) or info["uboot_dip"] == DIP_NOR)
            for line in advice:
                _say(line)
        _finish_device(d, info, manifest.get("boot_dip") or dip_now, any_dip=any_dip)
    finally:
        if server:
            server.shutdown()
        d.disconnect()
    return print_report()


def _fix_backup_gpt(d):
    """After a clone to a bigger eMMC: move the backup GPT to the real end (sgdisk -e)."""
    try:
        kv = S.parse_kv(d.run_script(S.build_gpt_fix_script(), marker="snap_gpt",
                                     exec_timeout=GPT_FIX_TIMEOUT))
    except Exception as e:
        kv = {"GPT_FIX": str(e)}
    g = kv.get("GPT_FIX", "?")
    if g == "nosgdisk":
        _say("  ⚠ Target eMMC is larger and has GPT, but sgdisk is not on the device:")
        _say(f"    run `sgdisk -e {S.EMMC_DEV}` (or let parted fix it) once the OS is up.")
    elif g != "nogpt":
        step(0, "Backup GPT moved to the end of the larger eMMC (sgdisk -e)", g == "0", g)


def _snapshot_nor_env_vars(d, transfer, folder, label, manifest) -> Optional[dict]:
    """Variables of the NOR env saved with a snapshot (PC file or stick)."""
    rec = manifest.get("nor_env") or {}
    if not rec.get("file"):
        return None
    if transfer == "lan":
        f = folder / rec["file"]
        return S.parse_uboot_env_blob(f.read_bytes()) if f.is_file() else None
    try:
        src = f"{S.USB_MOUNT}/{S.USB_ROOT}/{label}/{rec['file']}"
        return S.parse_uboot_env_blob(S.parse_od(d.run_script(
            S.build_dump_script(src, 0, 1, bs=S.NOR_ENV_SIZE),
            marker="snap_snap_norenv", exec_timeout=DUMP_TIMEOUT)))
    except Exception:
        return None


def _write(d, transfer, sess, base_url, folder, label, manifest, ranges, have) -> tuple:
    if transfer == "usb":
        return restore_usb(d, label, manifest, ranges)
    return restore_lan(d, sess, base_url, folder, manifest, ranges, "sha256sum" in have)
