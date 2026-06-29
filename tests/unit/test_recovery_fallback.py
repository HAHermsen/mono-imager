#!/usr/bin/env python3
"""
mono-imager: Unit tests for recovery fallback logic in menu_update_emmc / menu_update_nor.

No hardware required. All external calls are mocked.

eMMC scenarios (menu_update_emmc):
  1. modern eMMC fails  → legacy eMMC fallback succeeds
  2. modern eMMC fails  → legacy eMMC fallback also fails
  3. modern eMMC succeeds — no fallback triggered

NOR scenarios (menu_update_nor):
  4. modern NOR fails  → legacy NOR fallback succeeds  (verify_nor_boot still runs)
  5. modern NOR fails  → legacy NOR fallback also fails (verify_nor_boot NOT called)
  6. modern NOR succeeds — no fallback triggered         (verify_nor_boot runs)

Key invariant: fallback paths must not call steps that belong to a
different branch (e.g. eMMC fallback must never touch NOR functions).

Run: python tests/unit/test_recovery_fallback.py
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.tui import MonoImager


def make_app():
    app = MonoImager()
    app.clear_screen  = lambda: None
    app.print_header  = lambda: None
    return app


def fake_port():
    p = MagicMock()
    p.device      = "COM5"
    p.description = "fake"
    return p


def _base_patches(d):
    """Patches common to both menu_update_emmc and menu_update_nor."""
    return [
        patch("mono_imager.config.detect_serial_ports",
              return_value=([fake_port()], [])),
        patch("mono_imager.config.get_last_port", return_value=None),
        patch("mono_imager.config.save_last_port"),
        patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d),
        patch.object(MonoImager, "_setup_recovery_network", return_value=True),
        patch("mono_imager.recovery_orchestrator.reset_results"),
        patch("mono_imager.recovery_orchestrator.detect_modern_firmware_tool",
              return_value=True),
        patch("mono_imager.recovery_orchestrator.print_report", return_value=True),
    ]


def run_emmc_flow(extra_patches, inputs):
    """Drive menu_update_emmc() with the given patches and input sequence.

    Input sequence for the happy path:
      "1"  — select port 1 in _select_port()
      "y"  — confirm 'Proceed? [y/N]:'
      ""   — _recovery_finish() 'Press Enter to continue...'
    """
    app = make_app()
    d   = MagicMock()

    all_patches = _base_patches(d) + extra_patches
    [p.start() for p in all_patches]
    try:
        with patch("builtins.input", side_effect=inputs), patch("builtins.print"):
            app.menu_update_emmc()
    finally:
        for p in reversed(all_patches):
            p.stop()


def run_nor_flow(extra_patches, inputs):
    """Drive menu_update_nor() with the given patches and input sequence.

    Input sequence for success paths:
      "1"  — select port 1 in _select_port()
      "y"  — confirm 'Proceed? [y/N]:'
      ""   — 'Press Enter once you've done that...' (DIP flip prompt, modern branch)
      ""   — _recovery_finish() 'Press Enter to continue...'

    For total failure (both modern+legacy fail), the DIP flip prompt is
    never reached, so only 3 inputs are needed: "1", "y", "".
    """
    app = make_app()
    d   = MagicMock()

    all_patches = _base_patches(d) + extra_patches
    [p.start() for p in all_patches]
    try:
        with patch("builtins.input", side_effect=inputs), patch("builtins.print"):
            app.menu_update_nor()
    finally:
        for p in reversed(all_patches):
            p.stop()


def check(label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)


# ============================================================================
# eMMC scenario 1: modern fails → legacy fallback succeeds
# ============================================================================

print("=" * 60)
print("eMMC scenario 1: modern fails, legacy eMMC fallback succeeds")
print("=" * 60)

mock_modern_emmc = MagicMock(return_value=False)
mock_legacy_emmc = MagicMock(return_value=True)
mock_modern_nor  = MagicMock()
mock_legacy_nor  = MagicMock()

run_emmc_flow([
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_emmc", mock_modern_emmc),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_emmc", mock_legacy_emmc),
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_nor",  mock_modern_nor),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_nor",  mock_legacy_nor),
], ["1", "y", ""])

check("modern eMMC was attempted",              mock_modern_emmc.called)
check("legacy eMMC fallback was called",        mock_legacy_emmc.called)
check("NOR flash never touched",                not mock_modern_nor.called and not mock_legacy_nor.called)


# ============================================================================
# eMMC scenario 2: modern fails → legacy fallback also fails
# ============================================================================

print()
print("=" * 60)
print("eMMC scenario 2: modern fails, legacy eMMC fallback also fails")
print("=" * 60)

mock_modern_emmc = MagicMock(return_value=False)
mock_legacy_emmc = MagicMock(return_value=False)
mock_modern_nor  = MagicMock()
mock_legacy_nor  = MagicMock()

run_emmc_flow([
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_emmc", mock_modern_emmc),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_emmc", mock_legacy_emmc),
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_nor",  mock_modern_nor),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_nor",  mock_legacy_nor),
], ["1", "y", ""])

check("both eMMC paths attempted",              mock_modern_emmc.called and mock_legacy_emmc.called)
check("NOR flash never touched",                not mock_modern_nor.called and not mock_legacy_nor.called)


# ============================================================================
# eMMC scenario 3: modern succeeds — no fallback triggered
# ============================================================================

print()
print("=" * 60)
print("eMMC scenario 3: modern eMMC succeeds (no fallback)")
print("=" * 60)

mock_modern_emmc = MagicMock(return_value=True)
mock_legacy_emmc = MagicMock()

run_emmc_flow([
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_emmc", mock_modern_emmc),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_emmc", mock_legacy_emmc),
], ["1", "y", ""])

check("modern eMMC called",                     mock_modern_emmc.called)
check("legacy eMMC fallback NOT called",        not mock_legacy_emmc.called)


# ============================================================================
# NOR scenario 4: modern fails → legacy NOR fallback succeeds
# ============================================================================

print()
print("=" * 60)
print("NOR scenario 4: modern NOR fails, legacy NOR fallback succeeds")
print("=" * 60)

mock_modern_nor        = MagicMock(return_value=False)
mock_legacy_nor        = MagicMock(return_value=True)
mock_verify_nor        = MagicMock(return_value=True)
mock_modern_emmc       = MagicMock()
mock_legacy_emmc       = MagicMock()

run_nor_flow([
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_nor",      mock_modern_nor),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_nor",      mock_legacy_nor),
    patch("mono_imager.recovery_orchestrator.phase_modern_verify_nor_boot", mock_verify_nor),
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_emmc",     mock_modern_emmc),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_emmc",     mock_legacy_emmc),
], ["1", "y", "", ""])

check("modern NOR was attempted",               mock_modern_nor.called)
check("legacy NOR fallback was called",         mock_legacy_nor.called)
check("verify_nor_boot runs after fallback",    mock_verify_nor.called)
check("eMMC flash never touched",               not mock_modern_emmc.called and not mock_legacy_emmc.called)


# ============================================================================
# NOR scenario 5: modern fails → legacy NOR fallback also fails
# ============================================================================

print()
print("=" * 60)
print("NOR scenario 5: modern NOR fails, legacy NOR fallback also fails")
print("=" * 60)

mock_modern_nor  = MagicMock(return_value=False)
mock_legacy_nor  = MagicMock(return_value=False)
mock_verify_nor  = MagicMock(return_value=True)

run_nor_flow([
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_nor",       mock_modern_nor),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_nor",       mock_legacy_nor),
    patch("mono_imager.recovery_orchestrator.phase_modern_verify_nor_boot", mock_verify_nor),
], ["1", "y", ""])

check("both NOR paths attempted",               mock_modern_nor.called and mock_legacy_nor.called)
check("verify_nor_boot NOT called on failure",  not mock_verify_nor.called)


# ============================================================================
# NOR scenario 6: modern succeeds — no fallback triggered
# ============================================================================

print()
print("=" * 60)
print("NOR scenario 6: modern NOR succeeds (no fallback)")
print("=" * 60)

mock_modern_nor  = MagicMock(return_value=True)
mock_legacy_nor  = MagicMock()
mock_verify_nor  = MagicMock(return_value=True)

run_nor_flow([
    patch("mono_imager.recovery_orchestrator.phase_modern_flash_nor",       mock_modern_nor),
    patch("mono_imager.recovery_orchestrator.phase_legacy_flash_nor",       mock_legacy_nor),
    patch("mono_imager.recovery_orchestrator.phase_modern_verify_nor_boot", mock_verify_nor),
], ["1", "y", "", ""])

check("modern NOR called",                      mock_modern_nor.called)
check("legacy NOR fallback NOT called",         not mock_legacy_nor.called)
check("verify_nor_boot runs normally",          mock_verify_nor.called)
