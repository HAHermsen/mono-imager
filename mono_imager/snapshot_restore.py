"""
mono-imager: Restoring a snapshot to the device (pick, checks, write, verify)

Split out of snapshot_orchestrator.py; import it through mono_imager.snapshot_orchestrator,
which re-exports these names. Calls go through `SO.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from mono_imager import snapshot_orchestrator as SO


def _ask_label(os_label: str, ask: Callable, existing) -> str:
    existing = set(existing)
    default = SO.S.default_label(os_label)
    while True:
        v = ask(f"  Snapshot label [{default}]: ") or default
        if not SO.S.valid_label(v):
            SO._say("  Use letters, digits, '.', '_' or '-' (max 64).")
        elif v in existing or f"{v}.partial" in existing:
            SO._say(f"  '{v}' already exists — pick another label.")
        else:
            return v


def pick_snapshot(snaps: list, ask: Callable = SO._ask) -> Optional[int]:
    """snaps: [(label, manifest or None)] -> index or None."""
    if not snaps:
        return None
    SO._say("")
    for i, (label, m) in enumerate(snaps, 1):
        m = m or {}
        size = SO._gib(m["emmc"]["bytes"]) if m.get("emmc") else "?"
        extra = " (auto)" if m.get("auto") else ""
        SO._say(f"  {i}) {label}{extra}  — {m.get('os_label', '?')}, {size}, "
             f"{m.get('created', '?')}, MAC {m.get('device', {}).get('mac', '?')}")
    SO._say(f"  {len(snaps) + 1}) Back")
    while True:
        c = ask(f"  Select [1-{len(snaps) + 1}]: ")
        if c.isdigit() and 1 <= int(c) <= len(snaps):
            return int(c) - 1
        if c == str(len(snaps) + 1):
            return None


def _verify_lan_snapshot(folder: Path) -> tuple | None:
    """LAN: check the picked folder against SHA256SUMS before anything else.
    Returns (manifest, snapshot's U-Boot env), or None when it fails."""
    res, err = SO.with_spinner(SO.S.verify_store_snapshot, folder,
                            message="Checking the snapshot against SHA256SUMS...")
    ok, why, manifest = res if not err else (False, str(err), None)
    if not SO.step(0, "Snapshot verified on the PC (SHA256SUMS)", ok, why):
        return None
    snap_env = {}
    if manifest.get("uboot_env_file") and (folder / manifest["uboot_env_file"]).is_file():
        snap_env = SO.S.parse_env_file((folder / manifest["uboot_env_file"]).read_text())
    return manifest, snap_env


def _pick_usb_snapshot(d, have: set, ask: Callable) -> tuple | None:
    """USB: mount the stick, pick a snapshot, check it can be decompressed here.
    Returns (mount info, label, manifest, snapshot's U-Boot env), or None."""
    if "sha256sum" not in have or "head" not in have or "tail" not in have:
        SO.step(0, "sha256sum/head/tail available on the device", False, "needed for USB restore")
        return None
    mnt = SO.usb_mount(d, "restore")
    if not SO.step(0, "USB stick with snapshots mounted", mnt is not None and bool(mnt["snapshots"]),
                "" if mnt else f"no {SO.S.USB_ROOT}/ folder found on {SO.S.USB_DEV}"):
        return None
    listed = []
    for lab in mnt["snapshots"]:
        m, env = SO.usb_read_manifest(d, lab)
        listed.append((lab, m, env))
    usable = [x for x in listed if x[1] and not SO.S.validate_manifest(x[1])]
    idx = SO.pick_snapshot([(lab, m) for lab, m, _ in usable], ask)
    if idx is None:
        SO.step(0, "Snapshot picked", False, "cancelled")
        return None
    label, manifest, snap_env = usable[idx]
    comps = {c["compression"] for c in manifest["chunks"]}
    missing = [c for c in comps if not SO.S.can_decompress(c, have)]
    if not SO.step(0, "Device can decompress the snapshot", not missing,
                f"no decompressor for {missing} — restore it over LAN instead"):
        return None
    return mnt, label, manifest, snap_env


def _restore_board_checks(manifest: dict, identity: dict, target_inv: dict, probe: dict,
                          ask: Callable) -> bool | None:
    """
    MAC match (clone mode needs a yes and NOR recovery), the firmware gate and
    the eMMC size. Returns whether this is a clone, or None to stop.
    """
    match, snap_macs, here_macs = SO.S.mac_match(manifest, identity, target_inv.get("macs"))
    clone = match == "other"
    SO.step(0, f"Snapshot MACs vs this board: {match}", True)
    if match == "partial":
        SO._say(f"  ⚠ Some MACs differ (snapshot {', '.join(sorted(snap_macs))};"
             f" here {', '.join(sorted(here_macs))}) — treated as this board.")
    if clone:
        SO._say("")
        for line in SO.clone_warning(manifest, snap_macs, here_macs):
            SO._say(line)
        SO._say("  Clone mode: `firmware update` provisions this board first, then only")
        SO._say("  0..4 KiB and 32 MiB..end are written; this board keeps its firmware region.")
        if ask("  Clone onto this board? [y/N]: ").lower() != "y":
            SO.step(0, "Snapshot belongs to this board", False, "clone mode declined")
            return None
        if probe["boot_medium"] != SO.C.BOOT_MEDIUM_NOR:
            SO.step(0, "Running NOR recovery (needed for clone mode's firmware update)", False,
                 f"this is {probe['boot_medium'] or '?'} recovery — set the DIP switch to "
                 f"{SO.DIP_TEXT[SO.DIP_NOR]}, power-cycle and run the restore again")
            return None
    if not SO.firmware_gate(target_inv, manifest.get("inventory"), clone=clone, ask=ask):
        SO.step(0, "Restore", False, "stopped to update the firmware first")
        return None
    size = manifest["emmc"]["bytes"]
    if not SO.step(0, f"Target eMMC large enough ({SO._gib(probe['emmc_bytes'])} >= {SO._gib(size)})",
                probe["emmc_bytes"] >= size, "target eMMC is smaller — refused"):
        return None
    return clone


def _auto_snapshot_label(transfer: str, mnt: dict | None, store: Path, env: dict) -> str:
    """auto-<os>-<date>, numbered past any snapshot (or .partial) already there."""
    taken = set(mnt["snapshots"]) if transfer == "usb" else \
        ({x.name for x in store.iterdir()} if store.is_dir() else set())
    auto_label = base_auto = SO.S.default_label(f"auto-{SO.S.detect_os_label(env)}")
    n = 2
    while auto_label in taken or f"{auto_label}.partial" in taken:
        auto_label, n = f"{base_auto}-{n}", n + 1
    return auto_label


def _take_auto_snapshot(d, *, transfer: str, mnt: dict | None, store: Path, sess, base_url,
                        cap_args: dict, ask: Callable):
    """
    Undo point: auto-snapshot the current eMMC first. Returns (label, manifest),
    None when there is no undo point and the user goes on anyway, or False to stop.
    """
    probe, info = cap_args["probe"], cap_args["info"]
    auto = None
    auto_label = SO._auto_snapshot_label(transfer, mnt, store, info["env"])
    room = (mnt["free_kb"] * SO.C.KIB) if transfer == "usb" else shutil.disk_usage(store).free
    go = True
    if room < probe["emmc_bytes"]:
        SO._say(f"  ⚠ Possibly not enough space for the auto-snapshot ({SO._gib(room)} free, "
             f"up to {SO._gib(probe['emmc_bytes'])} needed).")
        go = ask("  Try the auto-snapshot anyway? [Y/n]: ").lower() != "n"
    if go:
        SO._say(f"  Auto-snapshot of the current eMMC: {auto_label}")
        cap = SO.Capture(label=auto_label, probe=probe, identity=cap_args["identity"], env=info["env"],
                      os_label=SO.S.detect_os_label(info["env"]), boot_dip=cap_args["dip_now"],
                      nor_env=cap_args["nor_env"], inventory=cap_args["inventory"],
                      emmc_env=cap_args["emmc_env"], auto=True)
        if transfer == "usb":
            m = SO.snapshot_usb(d, cap)
        else:
            store.mkdir(parents=True, exist_ok=True)
            m = SO.snapshot_lan(d, sess, base_url, store, cap)
        if m is not None:
            auto = (auto_label, m)
    if auto is None:
        SO._say("  ⚠ No auto-snapshot: this restore cannot be undone.")
        if ask("  Continue WITHOUT an undo point? [y/N]: ").lower() != "y":
            SO.step(0, "Restore", False, "cancelled")
            return False
    return auto


def _restore_summary(*, transfer: str, label, folder, manifest: dict, identity: dict, probe: dict,
                     total: int, skip_fw: bool, clone: bool, auto) -> None:
    src = f"{SO.S.USB_ROOT}/{label} on the USB stick" if transfer == "usb" else str(folder)
    SO._banner("  RESTORE SUMMARY",
            f"  Source:  {src}",
            f"           {manifest.get('os_label', '?')}, taken {manifest.get('created', '?')}",
            f"  Target:  this board, MAC {identity['mac'] or '?'}, eMMC {SO._gib(probe['emmc_bytes'])}",
            f"  Writes:  {SO._gib(total)} — " + (
                "everything except the firmware region (4 KiB..32 MiB)" if skip_fw
                else "the ENTIRE eMMC, firmware region included"),
            f"  Mode:    {'CLONE (firmware update first)' if clone else 'same board'}",
            f"  Undo:    {'auto-snapshot ' + auto[0] if auto else 'NONE'}",
            f"  Health:  {(manifest.get('health') or {}).get('status', 'not checked')} "
            "(when the snapshot was taken)",
            f"  NOR env: {'saved with the snapshot (offered after the write)' if manifest.get('nor_env') and not clone else 'not restored'}",
            "  Everything currently on the eMMC is overwritten.")


def _write_until_done(d, *, transfer: str, sess, base_url, folder, label, manifest: dict,
                      ranges: list, have: set, auto, store: Path, info: dict, dip_now: str,
                      ask: Callable) -> bool:
    """
    Write the planned ranges; on failure offer retry / roll back to the
    auto-snapshot / abort. True when the restore itself is on the eMMC; False
    when the journey ends here (rolled back or aborted).
    """
    ok, why = SO._write(d, transfer, sess, base_url, folder, label, manifest, ranges, have)
    while not ok:
        SO.step(0, "eMMC restored and re-hashed", False, why)
        SO._say("  Recovery Linux is still running from RAM.")
        opts = "[r]etry" + (", roll [b]ack to the auto-snapshot" if auto else "") + ", [a]bort"
        c = ask(f"  {opts}: ").lower()
        if c == "r":
            ok, why = SO._write(d, transfer, sess, base_url, folder, label, manifest, ranges, have)
        elif c == "b" and auto:
            a_label, a_m = auto
            a_folder = store / a_label if transfer == "lan" else None
            ok, why = SO._write(d, transfer, sess, base_url, a_folder, a_label, a_m,
                             SO.S.plan_restore(a_m, False), have)
            if ok:
                SO.step(0, f"Rolled back to {a_label}", True)
                SO._finish_device(d, info, a_m.get("boot_dip", dip_now))
                return False
        elif c == "a":
            SO._banner("  ⚠ The eMMC may be inconsistent now. Run the restore again,",
                    "  or restore the auto-snapshot, before booting it.")
            return False
    SO.step(0, "eMMC restored and re-hashed on the device", True)
    return True


def _after_restore(d, *, transfer: str, sess, base_url, folder, label, manifest: dict, clone: bool,
                   snap_nor_vars, snap_env: dict, current_nor_env, have: set, info: dict,
                   dip_now: str, ask: Callable) -> None:
    """NOR env write-back (not in clone mode), dual boot, advice, final DIP/reboot."""
    nor_written = False
    if clone and manifest.get("nor_env"):
        SO._say("  NOR env not offered in clone mode: it holds this other board's settings.")
    elif not clone:
        nor_written = SO.offer_nor_env_restore(
            d, transfer=transfer, sess=sess, base_url=base_url, folder=folder, label=label,
            manifest=manifest, snap_vars=snap_nor_vars, current_blob=current_nor_env,
            have=have, ask=ask)
    any_dip = SO.offer_dual_boot(d, manifest, have=have, ask=ask)
    if not nor_written and not any_dip:
        cur_vars = SO.S.parse_uboot_env_blob(current_nor_env)
        advice = SO.nor_env_advice(manifest, snap_nor_vars or snap_env, cur_vars or info["env"],
                                bool(cur_vars) or info["uboot_dip"] == SO.DIP_NOR)
        for line in advice:
            SO._say(line)
    SO._finish_device(d, info, manifest.get("boot_dip") or dip_now, any_dip=any_dip)


def _restore_on_device(d, *, info: dict, dip_now: str, transfer: str, host_ip: str, http_port: int,
                       setup_network: Callable, store: Path, folder, manifest, snap_env: dict,
                       auto_snapshot: bool, ask: Callable, servers: list) -> bool:
    """
    Everything that needs recovery Linux. False when it ended early (after the
    failing step), True when it ran to the end. A started server is put in
    `servers` so the caller can shut it down.
    """
    from mono_imager.snapshot_server import SnapshotSession, start_snapshot_server
    probe = SO.probe_device(d)
    if not SO.step(0, "Device identity and eMMC size read", probe is not None):
        return False
    if probe["emmc_mounted"]:
        SO.step(0, "eMMC not mounted by recovery Linux", False, "unmount it first")
        return False
    have = probe["have"]
    mnt, label = None, None
    if transfer == "usb":
        picked = SO._pick_usb_snapshot(d, have, ask)
        if picked is None:
            return False
        mnt, label, manifest, snap_env = picked

    identity = SO._identity(probe, info["env"])
    target_inv = SO.collect_inventory(d, probe, ntp_synced=False)
    current_nor_env = SO.read_nor_env(d)
    current_emmc_env = SO.read_emmc_env(d)
    snap_nor_vars = SO._snapshot_nor_env_vars(d, transfer, folder, label, manifest)
    clone = SO._restore_board_checks(manifest, identity, target_inv, probe, ask)
    if clone is None:
        return False

    sess = base_url = None
    if transfer == "lan" or clone:
        if not setup_network(d):
            SO.step(0, "Device network ready", False)
            return False
    if transfer == "lan":
        sess = SnapshotSession()
        server = start_snapshot_server(host_ip, http_port, sess)
        if server:
            servers.append(server)
        if not SO.step(0, f"Snapshot server up ({host_ip}:{http_port})", server is not None):
            return False
        base_url = SO._base_url(host_ip, http_port)

    auto = None
    if auto_snapshot:
        auto = SO._take_auto_snapshot(
            d, transfer=transfer, mnt=mnt, store=store, sess=sess, base_url=base_url, ask=ask,
            cap_args={"probe": probe, "info": info, "identity": identity, "dip_now": dip_now,
                      "nor_env": current_nor_env, "inventory": target_inv,
                      "emmc_env": current_emmc_env})
        if auto is False:
            return False

    # Firmware region identical? Then skip it (fewer erase cycles).
    skip_fw = clone
    if not clone:
        cur = auto[1]["emmc"]["fw_region"]["sha256"] if auto else (
            SO._device_fw_sha_usb(d) if transfer == "usb" else SO._device_fw_sha_lan(d, sess, base_url))
        skip_fw = bool(cur) and cur == manifest["emmc"]["fw_region"]["sha256"]
    ranges = SO.S.plan_restore(manifest, skip_fw)
    SO._restore_summary(transfer=transfer, label=label, folder=folder, manifest=manifest,
                     identity=identity, probe=probe, total=sum(r["len"] for r in ranges),
                     skip_fw=skip_fw, clone=clone, auto=auto)
    if ask("  Type 'yes' to write: ").lower() != "yes":
        SO.step(0, "Restore confirmed", False, "cancelled")
        return False

    if clone:   # firmware first, then the restore (it never touches 4 KiB..32 MiB)
        from mono_imager.recovery_orchestrator import run_firmware_update
        fw_ok, err = SO.with_spinner(run_firmware_update, d,
                                  message="Provisioning this board (firmware update)...")
        if not SO.step(0, "Firmware region provisioned (firmware update)", bool(fw_ok) and not err,
                    str(err) if err else ""):
            return False

    if not SO._write_until_done(d, transfer=transfer, sess=sess, base_url=base_url, folder=folder,
                             label=label, manifest=manifest, ranges=ranges, have=have, auto=auto,
                             store=store, info=info, dip_now=dip_now, ask=ask):
        return False

    if clone and probe["emmc_bytes"] > manifest["emmc"]["bytes"]:
        SO._fix_backup_gpt(d)
    SO._after_restore(d, transfer=transfer, sess=sess, base_url=base_url, folder=folder, label=label,
                   manifest=manifest, clone=clone, snap_nor_vars=snap_nor_vars, snap_env=snap_env,
                   current_nor_env=current_nor_env, have=have, info=info, dip_now=dip_now, ask=ask)
    return True


def run_restore(*, port: str, transfer: str, host_ip: str, http_port: int,
                setup_network: Callable, store: Path, folder: Optional[Path] = None,
                manifest: Optional[dict] = None, auto_snapshot: bool = True,
                soft_reboot: Optional[Callable] = None, ask: Callable = SO._ask) -> bool:
    """
    Restore journey. For LAN the snapshot is picked (folder/manifest) by the
    caller and checked against SHA256SUMS here before anything else; for USB
    it is picked from the stick once recovery Linux is up.
    """
    from mono_imager.flash_orchestrator import reset_results
    reset_results()

    snap_env = {}
    if transfer == "lan":
        verified = SO._verify_lan_snapshot(folder)
        if verified is None:
            return SO.print_report()
        manifest, snap_env = verified

    dip_now = SO.ask_dip(ask)
    d, info = SO.reach_recovery(port, dip_now, soft_reboot, ask)
    info["dip_now"] = dip_now
    if d is None:
        return SO.print_report()
    servers: list = []
    try:
        # An early end prints the report before the shutdown/disconnect below,
        # a completed restore after it (same order as before the split).
        if not SO._restore_on_device(d, info=info, dip_now=dip_now, transfer=transfer,
                                  host_ip=host_ip, http_port=http_port,
                                  setup_network=setup_network, store=store, folder=folder,
                                  manifest=manifest, snap_env=snap_env,
                                  auto_snapshot=auto_snapshot, ask=ask, servers=servers):
            return SO.print_report()
    finally:
        for server in servers:
            server.shutdown()
        d.disconnect()
    return SO.print_report()


def _fix_backup_gpt(d):
    """After a clone to a bigger eMMC: move the backup GPT to the real end (sgdisk -e)."""
    try:
        kv = SO.S.parse_kv(d.run_script(SO.S.build_gpt_fix_script(), marker="snap_gpt",
                                     exec_timeout=SO.GPT_FIX_TIMEOUT))
    except Exception as e:
        kv = {"GPT_FIX": str(e)}
    g = kv.get("GPT_FIX", "?")
    if g == "nosgdisk":
        SO._say("  ⚠ Target eMMC is larger and has GPT, but sgdisk is not on the device:")
        SO._say(f"    run `sgdisk -e {SO.S.EMMC_DEV}` (or let parted fix it) once the OS is up.")
    elif g != "nogpt":
        SO.step(0, "Backup GPT moved to the end of the larger eMMC (sgdisk -e)", g == "0", g)


def _snapshot_nor_env_vars(d, transfer, folder, label, manifest) -> Optional[dict]:
    """Variables of the NOR env saved with a snapshot (PC file or stick)."""
    rec = manifest.get("nor_env") or {}
    if not rec.get("file"):
        return None
    if transfer == "lan":
        f = folder / rec["file"]
        return SO.S.parse_uboot_env_blob(f.read_bytes()) if f.is_file() else None
    try:
        src = f"{SO.S.USB_MOUNT}/{SO.S.USB_ROOT}/{label}/{rec['file']}"
        return SO.S.parse_uboot_env_blob(SO.S.parse_od(d.run_script(
            SO.S.build_dump_script(src, 0, 1, bs=SO.S.NOR_ENV_SIZE),
            marker="snap_snap_norenv", exec_timeout=SO.DUMP_TIMEOUT)))
    except Exception:
        return None


def _write(d, transfer, sess, base_url, folder, label, manifest, ranges, have) -> tuple:
    if transfer == "usb":
        return SO.restore_usb(d, label, manifest, ranges)
    return SO.restore_lan(d, sess, base_url, folder, manifest, ranges, "sha256sum" in have)


__all__ = [
    "_after_restore",
    "_ask_label",
    "_auto_snapshot_label",
    "_fix_backup_gpt",
    "_pick_usb_snapshot",
    "_restore_board_checks",
    "_restore_on_device",
    "_restore_summary",
    "_snapshot_nor_env_vars",
    "_take_auto_snapshot",
    "_verify_lan_snapshot",
    "_write",
    "_write_until_done",
    "pick_snapshot",
    "run_restore",
]
