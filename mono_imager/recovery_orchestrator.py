#!/usr/bin/env python3
"""
mono-imager: Recovery orchestration logic.

Implements the documented Mono Gateway recovery/firmware-update
procedure (https://docs.mono.si/gateway-development-kit/flashing-firmware):

  Modern path (firmware has the `firmware` command):
    1. Boot recovery from NOR, run `firmware update` (flashes eMMC)
    2. User flips DIP to eMMC, reboots, tool verifies eMMC boot
    3. Boot recovery from eMMC, run `firmware update` (flashes NOR)
    4. User flips DIP back to NOR, reboots

  Legacy path (no `firmware` command — older devices in the wild):
    curl + dd (eMMC, with the documented skip=1 seek=1 4KB offset)
    curl + flashcp (NOR)
    This tool's policy: legacy devices are always brought up to the
    CURRENT firmware via the legacy download, never re-flashed with
    old firmware.

  Which path applies is DETECTED LIVE per device (`which firmware`)
  — there is no published version cutoff to gate on; devices in the
  wild may have either.

DIP-switch flips and the reboots that follow them are physical user
actions this tool cannot perform — those steps explicitly pause and
prompt, matching the "POWER CYCLE NOW" pattern used elsewhere.

This is a SEPARATE module from flash_orchestrator.py (and gets its
own isolated `results` list) rather than reusing its reporting state,
since mixing two different orchestrators' results in one shared list
is exactly the stale-state bug class fixed earlier this session.

Author:  H.A. Hermsen
License: GPLv3
"""

__author__  = "H.A. Hermsen"

import re
import time  # noqa: F401  (used as R.time by the split-out modules)
import logging
from typing import Optional, Callable  # noqa: F401  (used in the split-out modules' annotations)

from mono_imager.serial_device import SerialDevice  # noqa: F401  (used as R.SerialDevice)
from mono_imager.spinner import with_spinner  # noqa: F401  (used as R.with_spinner; tests patch it here)
from mono_imager.step_tracker import StepTracker
from mono_imager import constants as C

# --- Tuning (seconds unless noted) ----------------------------------------------
CMDLINE_TIMEOUT = 5                  # cat /proc/cmdline
INTERNET_CHECK_TIMEOUT = 15
DHCP_TIMEOUT = 12
STREAM_IDLE_TIMEOUT = 30.0           # firmware tool: silence that means "done"
STREAM_MAX_TOTAL = 900.0             # firmware tool: hard ceiling
STREAM_POLL_INTERVAL = 0.01          # 10 ms polling loop
STREAM_MARKER_GRACE = 0.3            # after a done marker the result is known
STREAM_READ_BYTES = 256
CLOCK_SYNC_TIMEOUT = 15.0
FW_HELP_TIMEOUT = 10
FW_RC_TIMEOUT = 10
MANUAL_DOWNLOAD_TIMEOUT = 300
BOOT_SOURCE_TIMEOUT = 5
ENV_STASH_TIMEOUT = 60
VERIFY_BOOT_TIMEOUT = 60
VERIFY_BOOT_MODERN_TIMEOUT = 90
LEGACY_FLASH_TIMEOUT = 300

logger = logging.getLogger(__name__)
# Must match logging_setup.py's "mono_imager.console" exactly — that's the
# only logger name with a stdout handler attached (see configure_logging()).
# This used to be __name__ + ".console" ("mono_imager.recovery_orchestrator
# .console"), a different, unconfigured logger with no stdout handler — every
# console_logger.info() call in this module was silently going to the
# file-only root logger and never reaching the terminal. flash_orchestrator.py
# already uses the correct fixed name; this brings recovery_orchestrator.py
# in line with it.
console_logger = logging.getLogger("mono_imager.console")

# --- Result tracker (ISOLATED from flash_orchestrator.results — see
#     module docstring for why) -----------------------------------------
# Bookkeeping itself (format/log/accumulate) is shared via StepTracker —
# see step_tracker.py's module docstring — but this module keeps its own
# instance/list, same isolation as before.

_tracker = StepTracker(logger, console_logger, auto_number=False)
results  = _tracker.results

def reset_results():
    """Clear accumulated step results before a new recovery attempt."""
    _tracker.reset()

step = _tracker.step


# --- Firmware URLs (per documented "Manual flashing (legacy)" section) ------

LEGACY_EMMC_URL = "https://firmware.mono.si/firmware-emmc-gateway-dk.bin"
LEGACY_NOR_URL  = "https://firmware.mono.si/firmware-qspi-gateway-dk.bin"


# --- Detection ---------------------------------------------------------



# --- firmware tool capabilities / helpers (#24) ---------------------------
#
# Facts below are taken from the official tool's source,
# we-are-mono/meta-mono recipes-support/firmware-tools/files/firmware:
#   - `update` options: --usb, --from PATH, --url URL, --preserve-env
#     (--from/--usb/--preserve-env all added 2026-04-18). Default mode is a
#     FULL REWRITE: the target's U-Boot env resets to firmware defaults.
#     Before 2026-04-18 the tool always backed up + restored the env and
#     rejected unknown options ("ERROR: Unknown option").
#   - Files: firmware-<emmc|qspi>-gateway-dk.bin + .bin.sig, fetched from
#     https://firmware.mono.si with basic auth mono:<MAC of the first
#     interface `ip -o link` lists with an ether address>.
#   - The .bin is ALWAYS verified against /etc/firmware/firmware-signing.pub
#     (openssl dgst -sha256 -verify) before flashing — which is why
#     downloading it ourselves with `curl -k` is safe: TLS only protects
#     the transport, the signature check protects what gets flashed.
#   - U-Boot env: QSPI = MTD partition labelled "uboot-env", 0x2000 bytes at
#     offset 0; eMMC = /dev/mmcblk0, 0x2000 bytes at offset 0x300000.

FIRMWARE_BASE_URL = "https://firmware.mono.si"
FIRMWARE_MACHINE  = "gateway-dk"
MANUAL_FW_DIR     = "/tmp/mono_imager_fw"

UBOOT_ENV_SIZE        = C.UBOOT_ENV_SIZE
UBOOT_ENV_EMMC_OFFSET = C.UBOOT_ENV_EMMC_OFFSET
UBOOT_ENV_STASH       = "/tmp/mono_imager_uboot_env.bin"

# curl/openssl wording for a TLS failure caused by a wrong device clock
# (or TLS in general) — the case NTP re-sync + retry can actually fix.
_TLS_ERROR_RE = re.compile(
    r"certificate is not yet valid|certificate has expired|"
    r"SSL certificate problem|curl: \((?:35|60)\)",
    re.IGNORECASE,
)




# --- U-Boot env stash/restore for the legacy path (#24) -------------------



# --- Top-level update flows ----------------------------------------------
#
# run_emmc_update() / run_nor_update() are the single entry points
# tui.py calls for menu options 2/3 — same "one call, own your report"
# shape as a flash journey's get_journey()+.run(), and the same
# "domain module owns its full flow, including any physical-action
# pause + prompt" convention flash_orchestrator.phase1_uboot() and
# device_net.RecoveryNetwork.resolve() already use elsewhere in this
# codebase. Previously this ~120-line bootstrap/detect/flash/fallback
# sequence was duplicated almost verbatim between tui.py's
# menu_update_emmc() and menu_update_nor(); it now lives here once.
#
# soft_reboot / setup_network are passed in rather than imported —
# both are session-scoped on MonoImager (soft-reboot is a serial-only
# best-effort nudge; setup_network shares the single cached
# device-network resolution used by every other caller: journeys,
# Test LAN, startup). Passing them in keeps this module with no
# dependency on tui.py at all, same pattern as diagnostics.py.

# _MEDIUM_CONFIG parametrizes the one real difference between the eMMC
# and NOR update flows: which medium to boot from/target, which modern-
# path and legacy-path functions to call, and the messages that name
# them. See _run_medium_update() below for the shared flow itself.
#
# modern_flash/legacy_flash are lambdas, not bare function references —
# a bare `phase_legacy_flash_emmc` here would bind that name's value at
# module-import time, so a test's patch.object(rec, "phase_legacy_flash_emmc", ...)
# (the pattern this file's own tests use elsewhere) would silently miss
# this dict's already-captured reference. The lambda defers the name
# lookup to call time, same as every other call site in this module.
_MEDIUM_CONFIG = {
    "emmc": {
        "boot_medium":   "qspi",   # boot from NOR recovery so 'firmware update' targets eMMC
        "label":         "eMMC",
        "target":        "emmc",   # firmware tool's name for the flashed medium
        "tool_name":     "curl+dd",
        "modern_flash":  lambda d, on_output, preserve_env: phase_modern_flash_emmc(d, on_output=on_output, preserve_env=preserve_env),
        "legacy_flash":  lambda d, env_dev: phase_legacy_flash_emmc(d, env_dev=env_dev),
        "legacy_message": "Flashing eMMC (legacy curl+dd)...",
        "legacy_only_extra_note": None,
    },
    "nor": {
        "boot_medium":   "emmc",   # boot from eMMC recovery so 'firmware update' targets NOR
        "label":         "NOR",
        "target":        "qspi",
        "tool_name":     "curl+flashcp",
        "modern_flash":  lambda d, on_output, preserve_env: phase_modern_flash_nor(d, on_output=on_output, preserve_env=preserve_env),
        "legacy_flash":  lambda d, env_dev: phase_legacy_flash_nor(d, env_dev=env_dev),
        "legacy_message": "Flashing NOR (legacy curl+flashcp)...",
        "legacy_only_extra_note": "  (No DIP-switch flip needed for this path.)",
    },
}



# --- Interactive choices for the menu update flows (#24) ------------------
# Only _run_medium_update() (menu options 2/3) prompts; the OS journeys call
# run_firmware_update() non-interactively with the default env policy.


# --- Re-exports ---------------------------------------------------------------
# These flows live in their own modules (split for size); they are re-exported
# here so callers and tests keep using mono_imager.recovery_orchestrator.<name>.
from mono_imager.recovery_firmware import (  # noqa: F401  (re-export; that module imports this one)
    _detect_update_target,
    _manual_download,
    _preserve_env_flags,
    _run_firmware_cmd,
    default_preserve_env,
    firmware_tool_caps,
    is_tls_error,
    run_firmware_update,
)
from mono_imager.recovery_flows import (  # noqa: F401  (re-export; that module imports this one)
    _ask_yes_no,
    _confirm_legacy_fallback,
    _run_legacy,
    _run_medium_update,
    ask_preserve_env,
    print_report,
    run_emmc_update,
    run_nor_update,
)
from mono_imager.recovery_net import (  # noqa: F401  (re-export; that module imports this one)
    _stream_command,
    check_internet_reachable,
    detect_modern_firmware_tool,
    get_device_mac,
    sync_device_clock,
    try_dhcp,
)
from mono_imager.recovery_phases import (  # noqa: F401  (re-export; that module imports this one)
    _legacy_flash_with_env,
    legacy_flash_emmc,
    legacy_flash_nor,
    phase_legacy_flash_emmc,
    phase_legacy_flash_nor,
    phase_modern_flash_emmc,
    phase_modern_flash_nor,
    phase_modern_verify_emmc_boot,
    phase_modern_verify_nor_boot,
    restore_uboot_env_region,
    stash_uboot_env,
    verify_boot_source,
)
