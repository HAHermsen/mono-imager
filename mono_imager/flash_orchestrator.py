#!/usr/bin/env python3
"""
mono-imager: Core flash verification logic.

This module contains ONLY the device-talking logic — bootstrap,
network prep, flash, post-flash — with zero argument parsing and
zero assumptions about where config values come from.

It is not run directly. Two wrappers consume it:

    test_verify_flash_auto.py    — zero config, everything auto-detected
    test_verify_flash_manual.py  — full manual control, every value prompted

This split exists so the actual hardware-talking logic lives in
exactly one place. Bug fixes here apply to both wrappers automatically.
The phase functions below are unchanged from the originally verified
test_flash_verify.py (5/5 bootstrap, network phase confirmed working
once device-ip is on the correct subnet).

Author:  H.A. Hermsen
License: GPLv3
"""

__author__  = "H.A. Hermsen"

import logging
import socket  # noqa: F401  (patched by tests; used as F.socket)

from http.server import HTTPServer  # noqa: F401  (patched by tests; used as F.HTTPServer)
from pathlib import Path
from typing import Optional, Callable

from mono_imager.config import detect_serial_ports  # noqa: F401  (used as F.detect_serial_ports)
from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner  # noqa: F401  (used as F.with_spinner)
from mono_imager.step_tracker import StepTracker
from mono_imager import constants as C

# --- Tuning (seconds unless noted) ----------------------------------------------
HTTP_COPY_BLOCK = 64 * C.KIB          # firmware file -> socket copy size
REPORT_POLL_SLEEP = 0.05              # wait_for_report() poll interval
PRINTENV_TIMEOUT = 10
SETENV_TIMEOUT = 5
CONNECT_READ_TIMEOUT = 5              # pyserial timeout for the bootstrap connection
PROMPT_PROBE_TIMEOUT = 2.0            # "already at the U-Boot prompt?" check
RECOVERY_LOGIN_TIMEOUT = 60

# Set MONO_DEBUG=1 (or `mono-imager --debug`/`--verbose`) to restore full
# verbose output to console. debug_enabled() reads MONO_DEBUG live rather
# than freezing it at import time — see logging_setup.debug_enabled().


# --- Logging setup -----------------------------------------------------------
# Logging is initialised exactly once by tui.py/cli.py via
# mono_imager.logging_setup.configure_logging(). This module does NOT call
# basicConfig — doing so at import time caused mutual destruction when tui.py
# also called it, silently dropping handlers depending on import order.

from mono_imager.logging_setup import get_log_file, make_verbose, debug_enabled as _debug_enabled  # noqa: F401

logger = logging.getLogger(__name__)

verbose = make_verbose(logger)


file_logger    = logger
console_logger = logging.getLogger("mono_imager.console")



# --- Result tracker ----------------------------------------------------------
# Bookkeeping (format/log/accumulate a step result) lives in StepTracker,
# shared with recovery_orchestrator.py — see step_tracker.py's module
# docstring for why print_report() itself stays separate per module.

_tracker = StepTracker(file_logger, console_logger, auto_number=True)
results  = _tracker.results  # (step, description, passed, reason) — same list object across reset_results() calls; journeys/tests read this directly

def reset_results():
    """
    Clear accumulated step results before starting a new flash attempt.

    BUG THIS FIXES: results is module-level state, not per-attempt. The
    one-shot test scripts (test_verify_flash_auto.py etc.) never needed
    this — each is a fresh process, so results always started empty.
    But tui.py is a long-running, looping application: without this
    reset, a single failed/cancelled step anywhere earlier in the same
    session (a previous attempt, a validation failure, anything that
    called step()) stays in the list forever. A LATER, fully successful
    flash would then still report failure, because print_report()
    checks ALL accumulated results, including stale ones from earlier
    in the process — not just the current attempt's. Confirmed
    reproducible: a failed step() call followed by 12 successful ones
    still yields print_report() -> False.

    Call this at the start of every flash attempt — phase1_bootstrap()
    calls it automatically since that's the true entry point shared by
    every caller (auto, manual, and any future one).
    """
    _tracker.reset()

step = _tracker.step

# --- HTTP server -------------------------------------------------------------


# --- U-Boot env capture/restore -----------------------------------------------
# This hardware's U-Boot env backend is MMC-primary (see opnsense_lan.py's
# uboot step comment: "MMC env backend is primary, so we must write while
# MMC is intact, then erase after") — a whole-disk `dd` to eMMC (Armbian,
# OPNsense) can reset the env storage area to factory defaults along with
# the OS. capture_uboot_env() snapshots printenv before that happens
# (called from phase1_uboot(), before any journey-specific U-Boot commands
# run); restore_uboot_env() re-applies it afterward, for journeys that
# re-enter U-Boot post-flash.

def parse_uboot_env(printenv_output: str) -> dict:
    """
    Parse `printenv` output into a dict of {var: value}. U-Boot prints
    one "key=value" pair per line; the trailing "Environment size: X/Y
    bytes" summary line is excluded. Lines that don't look like a
    plain key=value pair (no "=", or a key containing whitespace —
    which would indicate a wrapped/garbled line rather than a real
    var) are skipped rather than guessed at.
    """
    env = {}
    for line in printenv_output.splitlines():
        if "=" not in line or line.strip().startswith("Environment size"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and " " not in key:
            env[key] = value
    return env


def capture_uboot_env(d: SerialDevice) -> Optional[dict]:
    """
    Snapshot the device's current U-Boot environment via `printenv`.
    Returns None on failure (no response, or nothing parseable) —
    callers must treat that as "no backup available" and skip the
    restore later, not fail the whole bootstrap over it; capturing a
    backup is a nice-to-have, not a hard prerequisite for flashing.
    """
    try:
        output = d.send_command("printenv", timeout=PRINTENV_TIMEOUT)
    except Exception as e:
        verbose(f"capture_uboot_env: printenv failed: {e}", "warning")
        return None

    env = parse_uboot_env(output)
    if not env:
        verbose("capture_uboot_env: printenv returned no parseable vars", "warning")
        return None
    verbose(f"✓ Captured {len(env)} U-Boot env var(s) for post-flash restore", "debug")
    return env


def restore_uboot_env(d: SerialDevice, backup: Optional[dict]) -> int:
    """
    Re-apply a previously captured U-Boot environment snapshot (see
    capture_uboot_env()) — used after a whole-disk flash to eMMC that
    may have reset the env storage area to factory defaults.

    Restores every captured var unconditionally, with no diffing
    against the device's current env. This is safe as long as callers
    restore BEFORE making their own intentional env changes (e.g.
    setting bootcmd to boot the freshly-flashed OS) — U-Boot env is
    last-write-wins, so those explicit setenv calls simply run after
    this and take precedence; nothing intentional gets clobbered.

    Caller is responsible for the final `saveenv` — batched together
    with any of its own changes rather than saving twice here.

    Returns the number of variables restored (0 if backup is falsy).
    """
    if not backup:
        return 0
    restored = 0
    for key, value in backup.items():
        try:
            d.send_command(f'setenv {key} "{value}"', timeout=SETENV_TIMEOUT)
            restored += 1
        except Exception as e:
            verbose(f"restore_uboot_env: failed to restore {key}: {e}", "warning")
    if restored:
        verbose(f"✓ Restored {restored} U-Boot env var(s) from pre-flash snapshot")
    return restored

# --- Phase implementations ---------------------------------------------------
# Unchanged from the proven test_flash_verify.py run (5/5 bootstrap on
# real hardware, network phase confirmed once device-ip matches subnet).


# --- Full journey orchestration ------------------------------------------
#
# run_flash_journey() is the single entry point tui.py calls for the
# "Flash OS" menu — restores the get_journey()+.run() contract
# JOURNEYS.md documents ("tui.py calls get_journey() and runner.run(),
# nothing else"). menu_network_flashing() used to violate that by
# driving phase1_uboot/phase1_recovery/network-setup inline itself;
# that ~90-line sequence now lives here once.
#
# get_device_net/setup_network are passed in rather than imported —
# both are session-scoped on MonoImager (the cached device network
# used by every other caller: eMMC/NOR updates, Test LAN, startup).
#
# Returns None if the journey never got far enough to run
# (bootstrap/U-Boot-steps/network-setup failure) — callers must NOT
# overwrite their own flash_success in that case, matching the
# original menu_network_flashing()'s behavior of leaving flash_success
# untouched on those early failures. Returns a bool (this module's own
# print_report() verdict) once the journey actually ran.

def _cleanup_flash_journey(journey, d) -> None:
    """Stop the journey's HTTP server, delete any extracted rootfs, close serial."""
    server = None
    if journey is not None:
        try:
            server = journey.ctx.get("http_server")
        except Exception:
            pass
        try:
            extracted = journey.ctx.get("extracted_rootfs")
            if extracted:
                Path(extracted).unlink(missing_ok=True)
        except Exception:
            pass
    if server:
        server.shutdown()
        verbose("HTTP server stopped")
    if d:
        d.disconnect()


def _print_flash_banner(os_name: str, firmware_path, host_ip: str, http_port: int, device_ip) -> None:
    print("=" * C.DIVIDER_WIDTH)
    print("PHASE 2+: Flashing Firmware")
    print("=" * C.DIVIDER_WIDTH)
    fw_display = "auto-detected from USB" if Path(firmware_path) == Path(".") else str(firmware_path)
    print(f"OS:          {os_name}")
    print(f"Firmware:    {fw_display}")
    print(f"Host IP:     {host_ip}:{http_port}")
    print(f"Device IP:   {device_ip or '(not needed for this journey)'}")
    print()


def run_flash_journey(
    port: str,
    os_name: str,
    transfer: str,
    host_ip: str,
    http_port: int,
    firmware_path,
    get_device_net: Callable[[], Optional[dict]],
    setup_network: Callable[[SerialDevice], bool],
) -> Optional[bool]:
    from mono_imager.journeys import get_journey
    from mono_imager.step_registry import get_staging_boot_methods, list_journey

    d = None
    journey = None
    try:
        print()
        print("=" * C.DIVIDER_WIDTH)
        print("PHASE 1: Bootstrap (Serial Connection)")
        print("=" * C.DIVIDER_WIDTH)
        print(f"Port: {port}")
        print()

        # Step 1: Connect and interrupt U-Boot — no OS awareness
        d = phase1_uboot(port, C.SERIAL_BAUD)
        if d is None:
            print("❌ Bootstrap FAILED")
            return None

        # Step 2: Journey-specific U-Boot commands (eMMC erase, bootcmd, etc.)
        # Delegated entirely to the journey file via run_uboot_steps()
        print()
        print("  Configuring U-Boot...")
        journey = get_journey(
            os_name       = os_name,
            transfer      = transfer,
            device        = d,
            host_ip       = host_ip,
            device_ip     = (get_device_net() or {}).get("ip", ""),
            firmware_path = Path(firmware_path),
            http_port     = http_port,
            device_net    = get_device_net(),
        )
        if not journey.run_uboot_steps():
            print("❌ U-Boot setup FAILED")
            return None

        # Step 3: Boot staging Linux (recovery or alternative, per journey)
        staging = get_staging_boot_methods(os_name, transfer)
        d = phase1_recovery(d, **staging)
        if d is None:
            print("❌ Bootstrap FAILED")
            return None

        print("✓ Bootstrap successful")
        print()

        # Step 3b: resolve the device's own network — same DHCP-first,
        # verified, manual-fallback mechanism used everywhere else.
        # Only needed by journeys whose step list actually depends on
        # it (LAN transfer, or a post-flash internet-requiring step
        # like OpenWRT/OPNsense's firmware update) — skip it otherwise
        # so e.g. Armbian-via-USB never prompts for network settings
        # it will never use.
        needs_network = "Device network ready" in list_journey(os_name, transfer)
        if needs_network:
            if not setup_network(d):
                print("❌ Device network setup FAILED — cannot continue without it.")
                return None
            # get_journey() was called earlier (before recovery boot,
            # for run_uboot_steps()) with a placeholder device_net —
            # now that it's actually resolved, forward it into the
            # already-built ctx rather than rebuilding the journey.
            device_net = get_device_net()
            journey.ctx.device_net = device_net
            journey.ctx.device_ip  = device_net["ip"]

        print()
        _print_flash_banner(os_name, firmware_path, host_ip, http_port, journey.ctx.device_ip)

        ok = journey.run()

        if not ok:
            print("❌ Flashing did not complete successfully")
        else:
            print("✓ Flashing completed successfully")
        print()

    finally:
        _cleanup_flash_journey(journey, d)

    return print_report()


# --- Report ------------------------------------------------------------------

def print_report():
    # Full detail to the file, unchanged — every step, numbered, with
    # technical reasons. This is the troubleshooting record.
    verbose("=" * C.DIVIDER_WIDTH)
    verbose("Verification Report")
    verbose("=" * C.DIVIDER_WIDTH)
    passed = sum(1 for _, _, p, _ in results if p)
    total  = len(results)
    for num, desc, p, reason in results:
        mark = "✓ PASS" if p else "✗ FAIL"
        line = f"  Step {num:02d}: {mark} — {desc}"
        if reason:
            line += f"\n           {reason}"
        verbose(line)
    verbose("-" * C.DIVIDER_WIDTH)
    verdict = "OK" if passed == total else "NOK"
    verbose(f"Result: {verdict} ({passed}/{total} steps passed)")
    verbose(f"📄 Report saved to: {get_log_file()}")

    # Short, plain summary to the console — what a person actually
    # wants to know: did it work, and if not, what failed (in plain
    # terms, no step numbers, no technical reason strings).
    console_logger.info("")
    if verdict == "OK":
        console_logger.info("✓ Flash completed successfully.")
    else:
        console_logger.info("✗ Flash did not complete successfully.")
        failed = [desc for _, desc, p, _ in results if not p]
        for desc in failed:
            console_logger.info(f"  - {desc}")
        console_logger.info(f"  Full details: {get_log_file()}")

    return verdict == "OK"


# --- Re-exports ---------------------------------------------------------------
# These flows live in their own modules (split for size); they are re-exported
# here so callers and tests keep using mono_imager.flash_orchestrator.<name>.
from mono_imager.flash_http import (  # noqa: F401  (re-export; that module imports this one)
    _FirmwareHandler,
    detect_host_ip,
    parse_active_eth_iface,
    parse_active_eth_ifaces,
    peek_report,
    start_http_server,
    wait_for_report,
)
from mono_imager.flash_phases import (  # noqa: F401  (re-export; that module imports this one)
    _flash_script,
    _step09_source_reachable,
    phase1_bootstrap,
    phase1_recovery,
    phase1_uboot,
    phase3_flash,
    phase4_postflash,
)
