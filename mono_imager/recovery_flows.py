"""
mono-imager: The update flows: prompts, legacy fallback, eMMC/NOR update, report

Split out of recovery_orchestrator.py; import it through mono_imager.recovery_orchestrator,
which re-exports these names. Calls go through `R.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


from collections.abc import Callable

from mono_imager import recovery_orchestrator as R


def _ask_yes_no(question: str, default: bool) -> bool:
    """input()-based Y/n prompt; empty answer (or EOF) = default."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {question} {hint}: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "j", "ja")


def ask_preserve_env(target: str, label: str) -> bool:
    """Ask whether to keep the target's U-Boot env; default per target."""
    default = R.default_preserve_env(target)
    R.console_logger.info("")
    if target == "emmc":
        R.console_logger.info("  U-Boot env: default is NOT to preserve on eMMC — an old eMMC env")
        R.console_logger.info("  (e.g. from Armbian) can lack the 'recovery' command the NOR update")
        R.console_logger.info("  (option 3) needs. Preserve it if an OS on eMMC depends on it.")
    else:
        R.console_logger.info("  U-Boot env: default is to preserve on NOR — it holds your boot")
        R.console_logger.info("  settings (e.g. OPNsense's bootcmd). Not preserving resets it to")
        R.console_logger.info("  factory defaults.")
    choice = R._ask_yes_no(f"Preserve the {label} U-Boot environment?", default)
    R.logger.info(f"ask_preserve_env: target={target} preserve={choice}")
    return choice


def _confirm_legacy_fallback(cfg: dict, preserve_env: bool) -> bool:
    """Explicit consent before the legacy last resort (default: No)."""
    R.console_logger.info("")
    R.console_logger.info(f"  ⚠ The firmware tool could not update {cfg['label']} (all retries failed).")
    R.console_logger.info(f"  Last resort: legacy {cfg['tool_name']}, which bypasses the tool and")
    R.console_logger.info("  rewrites the U-Boot env region.")
    if preserve_env:
        R.console_logger.info("  The env will be stashed first and written back afterwards.")
    else:
        R.console_logger.info("  The U-Boot env WILL be reset to factory defaults.")
    return R._ask_yes_no(f"Run the legacy {cfg['tool_name']} fallback?", False)


def _run_legacy(d: R.SerialDevice, cfg: dict, preserve_env: bool) -> bool:
    """
    Legacy flash with the env choice applied: stash the env region
    first when preserving; if that stash fails, WARN and ask before
    flashing anyway (env would be lost).
    """
    env_dev = None
    if preserve_env:
        env_dev = R.stash_uboot_env(d, cfg["target"])
        if env_dev:
            R.console_logger.info(f"  ✓ U-Boot env stashed from {env_dev}")
        else:
            R.console_logger.info("  ⚠ WARN: could not stash the U-Boot env — flashing now will reset")
            R.console_logger.info("    it to factory defaults.")
            if not R._ask_yes_no("Flash anyway and lose the U-Boot env?", False):
                return False
    ok, _leg_err = R.with_spinner(cfg["legacy_flash"], d, env_dev, message=cfg["legacy_message"])
    if _leg_err:
        R.logger.error(f"legacy flash raised: {_leg_err}")
        return False
    return bool(ok)


def _run_medium_update(
    medium: str,
    port: str,
    setup_network: Callable[[R.SerialDevice], bool],
    on_output: Callable[[str], None] | None = None,
) -> bool:
    """
    Shared flow behind run_emmc_update()/run_nor_update(): bootstrap
    into the recovery shell for the OTHER medium, detect modern vs.
    legacy firmware tool, resolve the device network, ask whether to
    preserve the target's U-Boot env, then flash the target medium via
    the modern `firmware update` ladder (run_firmware_update(): NTP,
    TLS retry, curl -k + --from). The legacy curl-based path runs only
    when the device has no usable tool, or — after an explicit y/N —
    as the last resort; either way the env choice is applied via
    stash_uboot_env()/restore_uboot_env_region() (#24).
    Prints its own step-by-step report before returning.

    This ~90-line bootstrap/detect/flash/fallback sequence used to be
    duplicated almost verbatim between run_emmc_update() and
    run_nor_update() themselves (previously copy-pasted from
    tui.py's menu_update_emmc()/menu_update_nor() — see git history);
    _MEDIUM_CONFIG above now carries the only real differences.

    Returns True on overall success.
    """
    from mono_imager import flash_orchestrator as core

    cfg = R._MEDIUM_CONFIG[medium]
    d = None
    try:
        # NOTE: no auto soft-reboot here. phase1_uboot()'s own
        # "POWER CYCLE NOW" prompt drives the reboot, so we don't send a
        # silent reset that would contradict that on-screen instruction.
        d = core.phase1_bootstrap(port, R.C.SERIAL_BAUD, boot_medium=cfg["boot_medium"])
        if d is None:
            R.console_logger.info("")
            R.console_logger.info("  ❌ Could not bootstrap into the recovery shell.")
            return core.print_report()

        R.reset_results()
        is_modern, _fw_err = R.with_spinner(
            R.detect_modern_firmware_tool, d,
            message="Detecting firmware tool type..."
        )
        if _fw_err:
            is_modern = None

        if is_modern is None:
            R.console_logger.info("")
            R.console_logger.info("  ❌ Could not determine the device's firmware tool type.")
            return R.print_report()

        if not setup_network(d):
            return R.print_report()

        # One env choice per run, honoured by every tier below (#24):
        # the modern tool (--preserve-env) and the legacy path (manual
        # stash + restore of the env region).
        preserve_env = R.ask_preserve_env(cfg["target"], cfg["label"])

        if is_modern:
            R.console_logger.info("")
            R.console_logger.info("  Modern firmware tool detected.")
            R.console_logger.info("")
            ok = cfg["modern_flash"](d, on_output, preserve_env)
            if not ok:
                # Last resort only, and only with consent: the legacy path
                # bypasses the tool and rewrites the env region itself.
                if not R._confirm_legacy_fallback(cfg, preserve_env):
                    R.console_logger.info(f"  ❌ Update of {cfg['label']} aborted — nothing further flashed.")
                    return R.print_report()
                ok = R._run_legacy(d, cfg, preserve_env)
                if not ok:
                    R.console_logger.info(f"  ❌ Legacy fallback also failed for {cfg['label']}.")
                    return R.print_report()
                R.console_logger.info("  ✓ Legacy fallback succeeded.")
        else:
            R.console_logger.info("")
            R.console_logger.info(f"  Legacy firmware tool detected — using {cfg['tool_name']} directly.")
            if cfg["legacy_only_extra_note"]:
                R.console_logger.info(cfg["legacy_only_extra_note"])
            R.console_logger.info("")
            ok = R._run_legacy(d, cfg, preserve_env)
            if not ok:
                return R.print_report()

    finally:
        if d:
            d.disconnect()

    return R.print_report()


def run_emmc_update(
    port: str,
    soft_reboot: Callable[[str], None],
    setup_network: Callable[[R.SerialDevice], bool],
    on_output: Callable[[str], None] | None = None,
) -> bool:
    """
    Flash eMMC firmware only. Device must be in NOR recovery (DIP RIGHT)
    — bootstraps into it, detects modern vs. legacy firmware tool,
    resolves the device network, then flashes eMMC via the modern
    `firmware update` (falling back to legacy curl+dd if that fails or
    isn't available). Prints its own step-by-step report before
    returning. See _run_medium_update() for the shared implementation.

    Returns True on overall success.
    """
    return R._run_medium_update("emmc", port, setup_network, on_output)


def run_nor_update(
    port: str,
    soft_reboot: Callable[[str], None],
    setup_network: Callable[[R.SerialDevice], bool],
    on_output: Callable[[str], None] | None = None,
) -> bool:
    """
    Flash NOR firmware only. Device must be in eMMC recovery (DIP LEFT)
    — bootstraps into it, detects modern vs. legacy firmware tool,
    resolves the device network, then flashes NOR via the modern
    `firmware update` (falling back to legacy curl+flashcp if that
    fails or isn't available). Does not prompt for a DIP-switch flip
    back to NOR or verify the resulting boot — the caller is
    responsible for that if/when they want it (see
    phase_modern_verify_nor_boot() below, still available but no
    longer called from here). Prints its own step-by-step report
    before returning. See _run_medium_update() for the shared
    implementation.

    Returns True on overall success.
    """
    return R._run_medium_update("nor", port, setup_network, on_output)


def print_report() -> bool:
    """
    Summarize the recovery attempt's results — same OK/NOK verdict
    pattern as flash_orchestrator.py's print_report(), but recovery
    doesn't have its own dedicated log file, so this only logs via
    the standard logger/console_logger rather than referencing a
    log_file path.
    """
    R.logger.info("=" * R.C.DIVIDER_WIDTH)
    R.logger.info("Recovery Report")
    R.logger.info("=" * R.C.DIVIDER_WIDTH)
    passed = sum(1 for _, _, p, _ in R.results if p)
    total = len(R.results)
    for num, desc, p, reason in R.results:
        mark = "✓ PASS" if p else "✗ FAIL"
        line = f"  Step {num:02d}: {mark} — {desc}"
        if reason:
            line += f"\n           {reason}"
        R.logger.info(line)
    R.logger.info("-" * R.C.DIVIDER_WIDTH)
    verdict = "OK" if total > 0 and passed == total else "NOK"
    R.logger.info(f"Result: {verdict} ({passed}/{total} steps passed)")

    R.console_logger.info("")
    if verdict == "OK":
        R.console_logger.info("✓ Recovery completed successfully.")
    else:
        R.console_logger.info("✗ Recovery did not complete successfully.")
        failed = [desc for _, desc, p, _ in R.results if not p]
        for desc in failed:
            R.console_logger.info(f"  - {desc}")

    return verdict == "OK"


__all__ = [
    "_ask_yes_no",
    "_confirm_legacy_fallback",
    "_run_legacy",
    "_run_medium_update",
    "ask_preserve_env",
    "print_report",
    "run_emmc_update",
    "run_nor_update",
]
