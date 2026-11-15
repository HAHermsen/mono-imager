"""
mono-imager: Running the device's own firmware tool (modern path)

Split out of recovery_orchestrator.py; import it through mono_imager.recovery_orchestrator,
which re-exports these names. Calls go through `R.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


from collections.abc import Callable

from mono_imager import recovery_orchestrator as R


def default_preserve_env(target: str) -> bool:
    """
    Default for the "preserve U-Boot env?" choice, per target medium.

    eMMC: False. Preserving on an eMMC flash restores the device's OLD eMMC
    env (e.g. a prior Armbian env with no "recovery" command) over the new
    firmware's env — that wiped the "recovery" command option 3 (NOR
    update) needs to boot recovery from eMMC ("run recovery -> not
    defined"). NOR (qspi): True — NOR carries the boot settings
    (e.g. OPNsense's bootcmd) the user normally wants to keep.
    """
    return target != "emmc"


def is_tls_error(output: str) -> bool:
    """True if `firmware update` output shows a TLS/certificate failure."""
    return bool(R._TLS_ERROR_RE.search(output or ""))


def firmware_tool_caps(d: R.SerialDevice) -> dict:
    """
    Which `firmware update` options this device's tool supports, from
    `firmware help`. Returns {"preserve_env": bool, "from": bool}.

    Only downgrades when the help text was actually read (contains
    "Usage: firmware") and the option is absent — an unreadable/odd
    response keeps the current assumption (modern tool, both supported)
    rather than guessing the device is old.
    """
    caps = {"preserve_env": True, "from": True}
    try:
        out = d.run_script("firmware help 2>&1", marker="fw_tool_caps", exec_timeout=R.FW_HELP_TIMEOUT)
    except RuntimeError as e:
        R.logger.warning(f"firmware_tool_caps: run_script failed: {e}")
        return caps
    if "Usage: firmware" in out:
        caps["preserve_env"] = "--preserve-env" in out
        caps["from"] = "--from" in out
    R.logger.info(f"firmware_tool_caps: {caps}")
    return caps


def _run_firmware_cmd(d: R.SerialDevice, fw_cmd: str, on_output, idle_timeout: float,
                      max_total: float):
    """
    Stream one `firmware update ...` invocation, auto-answering its
    "Type 'yes' to proceed" prompt, and judge the result.
    Returns (success, output).

    NOTE: confirmed on real hardware that exit code alone isn't a
    reliable success signal — a self-aborted run (prompt timed out with
    nothing answering it) still reported RC=0 despite printing
    "Aborted." and flashing nothing; "ERROR:" (die()) is likewise a hard
    failure. The tool's own "Firmware update complete" line is a
    definitive success signal and skips the slow `echo RC=$?` round-trip
    (~20 s of run_script hops).
    """
    output = R._stream_command(
        d, fw_cmd,
        idle_timeout=idle_timeout, max_total=max_total,
        auto_confirm_response="yes",
        on_output=on_output,
        done_markers=["Firmware update complete", "Aborted", "ERROR:"],
    )

    aborted      = "Aborted" in output
    error_output = "ERROR:" in output
    completed    = "Firmware update complete" in output

    if completed:
        rc_output = "RC=0 (inferred from 'Firmware update complete')"
    else:
        try:
            rc_output = d.run_script("echo RC=$?", marker="firmware_update_rc", exec_timeout=R.FW_RC_TIMEOUT)
        except RuntimeError as e:
            R.logger.warning(f"run_firmware_update: could not verify exit code: {e}")
            rc_output = ""

    success = ("RC=0" in rc_output) and not aborted and not error_output
    R.logger.info(f"{fw_cmd} — full streamed output:\n{output}")
    if aborted:
        R.logger.error(
            "firmware update printed 'Aborted.' — the confirmation prompt "
            "was not answered in time, nothing was flashed, regardless of "
            "the reported exit code."
        )
    elif error_output:
        R.logger.error(
            "firmware update printed 'ERROR:' — it stopped before or during "
            "the flash (see the streamed output above for the reason)."
        )
    elif not success:
        R.logger.error(
            f"firmware update did not report RC=0 (got: {rc_output!r}) — "
            "review the streamed output above to confirm what actually happened."
        )
    return success, output


def _manual_download(d: R.SerialDevice, target: str) -> bool:
    """
    Fetch firmware-<target>-gateway-dk.bin + .bin.sig into MANUAL_FW_DIR
    with `curl -k`, using the same mono:<MAC> auth and MAC detection as
    the official tool (a plain unauthenticated curl got a 401 before).
    -k only skips TLS verification of the transport; the tool still
    verifies the signature before flashing (see block comment above).
    """
    fw = f"firmware-{target}-{R.FIRMWARE_MACHINE}.bin"
    script = (
        f"rm -rf {R.MANUAL_FW_DIR} && mkdir -p {R.MANUAL_FW_DIR} && cd {R.MANUAL_FW_DIR} && "
        r"mac=$(ip -o link show | grep -m1 'ether' | sed 's|.*ether \([^ ]*\).*|\1|') && "
        f'curl -kfsS -u "mono:$mac" -o {fw} {R.FIRMWARE_BASE_URL}/{fw} && '
        f'curl -kfsS -u "mono:$mac" -o {fw}.sig {R.FIRMWARE_BASE_URL}/{fw}.sig; '
        "echo DL_RC=$?"
    )
    try:
        out = d.run_script(script, marker="manual_fw_download", exec_timeout=R.MANUAL_DOWNLOAD_TIMEOUT)
    except RuntimeError as e:
        R.logger.error(f"_manual_download: run_script failed: {e}")
        return False
    ok = "DL_RC=0" in out
    if not ok:
        R.logger.error(f"_manual_download: download failed — output:\n{out}")
    return ok


def _detect_update_target(d: R.SerialDevice) -> str:
    """The medium `firmware update` will flash: the one NOT booted (boot_medium= on
    the kernel cmdline); "emmc" when that cannot be read."""
    try:
        boot_output = d.run_script(
            "cat /proc/cmdline | grep -o 'boot_medium=[a-z]*' || echo 'boot_medium=unknown'",
            marker="detect_boot_source", exec_timeout=R.BOOT_SOURCE_TIMEOUT
        )
    except RuntimeError as e:
        R.logger.warning(f"run_firmware_update: could not detect boot source: {e}")
        target = "emmc"  # default: assume booted from NOR, so flash eMMC
    else:
        if "boot_medium=emmc" in boot_output:
            target = "qspi"
        elif "boot_medium=qspi" in boot_output:
            target = "emmc"
        else:
            R.logger.warning(
                f"run_firmware_update: boot_medium not found in cmdline "
                f"(got: {boot_output!r}) — defaulting to emmc target"
            )
            target = "emmc"
    return target


def _preserve_env_flags(caps: dict, preserve_env: bool, target: str) -> str:
    """The --preserve-env flag (with its leading space) when wanted and supported, else "".
    Tells the user what will happen to the env."""
    flags = ""
    if preserve_env:
        if caps["preserve_env"]:
            flags = " --preserve-env"
        else:
            R.console_logger.info("  (Older firmware tool: it always preserves the U-Boot env — no flag needed.)")
    elif not caps["preserve_env"]:
        R.console_logger.info(
            "  ⚠ Older firmware tool: it cannot wipe the U-Boot env — "
            "the existing env will be kept."
        )
    R.console_logger.info(f"  U-Boot env on {target}: {'preserved' if preserve_env or not caps['preserve_env'] else 'reset to factory defaults'}")

    # Tier 1: the tool's own download.
    return flags


def run_firmware_update(d: R.SerialDevice, on_output: Callable[[str], None] | None = None,
                         idle_timeout: float = R.STREAM_IDLE_TIMEOUT, max_total: float = R.STREAM_MAX_TOTAL,
                         preserve_env: bool | None = None) -> bool:
    """
    Flash the OTHER medium than the one booted (the tool auto-detects
    boot_medium= and never overwrites what you're running from) via the
    modern `firmware` tool, degrading gracefully and deterministically
    (#24):

      0. NTP clock sync, result shown on the console.
      1. `firmware update [--preserve-env]` — the tool downloads itself
         (TLS-verified).
      2. If that failed with a TLS/certificate error: WARN, re-sync NTP,
         retry once.
      3. If it still failed and the tool supports --from: download .bin
         and .bin.sig ourselves with `curl -k` (signature still verified
         by the tool) and run `firmware update --from DIR [--preserve-env]`
         — keeps the tool, and therefore the env choice, in charge.

    The legacy curl+dd / curl+flashcp path is NOT part of this ladder: it
    rewrites the env region regardless, so it only runs from the menu
    flows after an explicit user confirmation (see _run_medium_update()).

    Args:
        preserve_env: keep the target's U-Boot env (--preserve-env).
            None = default_preserve_env(target) — the historical policy
            (eMMC: no, NOR: yes), used by the OS journeys.
        on_output: optional callback(text_chunk) for live progress.
        idle_timeout, max_total: passed to _stream_command(); only
            overridden by tests.

    Requires real internet access on the device's network — a hard,
    documented prerequisite.
    """
    # Step 0: best-effort NTP sync before anything TLS-verified runs (#23).
    R.sync_device_clock(d)

    # Step 1: which medium are we booted from -> which one gets flashed.
    # Keys off boot_medium= (set by U-Boot's "emmc"/"recovery" boot
    # commands) — the same signal the tool itself uses. The old root=
    # grep made a detection FAILURE indistinguishable from an eMMC boot.
    target = R._detect_update_target(d)

    R.logger.info(f"run_firmware_update: will flash {target} (auto-detected by device)")

    if preserve_env is None:
        preserve_env = R.default_preserve_env(target)

    # Build flags from what this device's tool actually supports. A tool
    # older than 2026-04-18 has no --preserve-env and dies on it — but it
    # always preserves the env anyway, so plain `firmware update` gives
    # the "preserve" outcome there and "wipe" is simply unavailable.
    caps = R.firmware_tool_caps(d)
    flags = R._preserve_env_flags(caps, preserve_env, target)

    ok, output = R._run_firmware_cmd(d, f"firmware update{flags}", on_output, idle_timeout, max_total)
    if ok:
        return True

    # Tier 2: TLS error -> re-sync the clock and retry once.
    if R.is_tls_error(output):
        R.console_logger.info("  ⚠ WARN: TLS/certificate error from firmware.mono.si — "
                            "re-syncing the clock and retrying once...")
        R.logger.warning("run_firmware_update: TLS error detected, NTP re-sync + retry")
        R.sync_device_clock(d)
        ok, output = R._run_firmware_cmd(d, f"firmware update{flags}", on_output, idle_timeout, max_total)
        if ok:
            return True

    # Tier 3: our own curl -k download, flashed by the tool via --from.
    if not caps["from"]:
        R.console_logger.info("  ⚠ This firmware tool has no --from option — manual-download retry skipped.")
        return False
    R.console_logger.info("  ⚠ WARN: 'firmware update' failed — downloading firmware manually "
                        "(curl -k; the tool still verifies the signature)...")
    if not R._manual_download(d, target):
        R.console_logger.info("  ❌ Manual firmware download failed.")
        return False
    ok, _output = R._run_firmware_cmd(
        d, f"firmware update --from {R.MANUAL_FW_DIR}{flags}", on_output, idle_timeout, max_total,
    )
    if ok:
        R.console_logger.info("  ✓ Flashed via 'firmware update --from' (manual download).")
    return ok


__all__ = [
    "_detect_update_target",
    "_manual_download",
    "_preserve_env_flags",
    "_run_firmware_cmd",
    "default_preserve_env",
    "firmware_tool_caps",
    "is_tls_error",
    "run_firmware_update",
]
