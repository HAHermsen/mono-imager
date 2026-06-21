import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0, '.')
from mono_imager.tui import MonoImager

def make_app():
    return MonoImager()

def fake_port():
    p = MagicMock(); p.device = 'COM5'; p.description = 'fake'; return p

def run_flow(extra_patches, inputs):
    app = make_app()
    d = MagicMock()
    d.wait_for_autoboot.return_value = True
    d.boot_recovery.return_value = True
    d.login_recovery.return_value = True

    base_patches = [
        patch('mono_imager.config.detect_serial_ports', return_value=([fake_port()], [])),
        patch('mono_imager.config.get_last_port', return_value=None),
        patch('mono_imager.config.save_last_port'),
        patch('mono_imager.flash_orchestrator.phase1_bootstrap', return_value=d),
        patch('mono_imager.flash_orchestrator.print_report', return_value=False),
        patch.object(MonoImager, '_setup_recovery_network', return_value=True),
        patch('mono_imager.recovery_orchestrator.reset_results'),
        patch('mono_imager.recovery_orchestrator.detect_modern_firmware_tool', return_value=True),
        patch('mono_imager.recovery_orchestrator.print_report', return_value=True),
    ]
    all_patches = base_patches + extra_patches
    started = [p.start() for p in all_patches]
    try:
        with patch('builtins.input', side_effect=inputs), patch('builtins.print'):
            app.menu_recovery_flow()
    finally:
        for p in reversed(all_patches):
            p.stop()
    return started

def check(label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)

print("=" * 60)
print("Scenario 1: modern eMMC fails, legacy eMMC fallback SUCCEEDS")
print("=" * 60)
mock_modern_emmc = MagicMock(return_value=False)
mock_legacy_emmc = MagicMock(return_value=True)
mock_legacy_nor = MagicMock(return_value=True)
mock_modern_verify_emmc = MagicMock(return_value=True)  # should never be called
mock_modern_nor = MagicMock(return_value=True)  # should never be called
extra = [
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_emmc', mock_modern_emmc),
    patch('mono_imager.recovery_orchestrator.phase_legacy_flash_emmc', mock_legacy_emmc),
    patch('mono_imager.recovery_orchestrator.phase_legacy_flash_nor', mock_legacy_nor),
    patch('mono_imager.recovery_orchestrator.phase_modern_verify_emmc_boot', mock_modern_verify_emmc),
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_nor', mock_modern_nor),
]
run_flow(extra, ['1', 'y', ''])
check("legacy eMMC fallback called", mock_legacy_emmc.called)
check("legacy NOR called (no DIP flip needed)", mock_legacy_nor.called)
check("modern verify_emmc_boot NEVER called (no DIP-flip dance)", not mock_modern_verify_emmc.called)
check("modern flash_nor NEVER called (skipped after fallback)", not mock_modern_nor.called)

print()
print("=" * 60)
print("Scenario 2: modern eMMC fails, legacy eMMC fallback ALSO fails")
print("=" * 60)
mock_modern_emmc = MagicMock(return_value=False)
mock_legacy_emmc = MagicMock(return_value=False)
mock_legacy_nor = MagicMock(return_value=True)  # should never be called
extra = [
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_emmc', mock_modern_emmc),
    patch('mono_imager.recovery_orchestrator.phase_legacy_flash_emmc', mock_legacy_emmc),
    patch('mono_imager.recovery_orchestrator.phase_legacy_flash_nor', mock_legacy_nor),
]
run_flow(extra, ['1', 'y', ''])
check("legacy eMMC fallback attempted", mock_legacy_emmc.called)
check("legacy NOR never attempted (total eMMC failure)", not mock_legacy_nor.called)

print()
print("=" * 60)
print("Scenario 3: modern eMMC OK, modern NOR fails, legacy NOR fallback SUCCEEDS")
print("=" * 60)
mock_modern_emmc = MagicMock(return_value=True)
mock_modern_verify_emmc = MagicMock(return_value=True)
mock_modern_nor = MagicMock(return_value=False)
mock_legacy_nor = MagicMock(return_value=True)
mock_modern_verify_nor = MagicMock(return_value=True)
extra = [
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_emmc', mock_modern_emmc),
    patch('mono_imager.recovery_orchestrator.phase_modern_verify_emmc_boot', mock_modern_verify_emmc),
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_nor', mock_modern_nor),
    patch('mono_imager.recovery_orchestrator.phase_legacy_flash_nor', mock_legacy_nor),
    patch('mono_imager.recovery_orchestrator.phase_modern_verify_nor_boot', mock_modern_verify_nor),
]
run_flow(extra, ['1', 'y', '', '', ''])
check("modern eMMC succeeded path taken (DIP flip prompts consumed)", mock_modern_verify_emmc.called)
check("modern NOR attempted first", mock_modern_nor.called)
check("legacy NOR fallback called after modern NOR failure", mock_legacy_nor.called)
check("final verify_nor_boot still runs after fallback success", mock_modern_verify_nor.called)

print()
print("=" * 60)
print("Scenario 4: modern eMMC OK, modern NOR fails, legacy NOR fallback ALSO fails")
print("=" * 60)
mock_modern_emmc = MagicMock(return_value=True)
mock_modern_verify_emmc = MagicMock(return_value=True)
mock_modern_nor = MagicMock(return_value=False)
mock_legacy_nor = MagicMock(return_value=False)
mock_modern_verify_nor = MagicMock(return_value=True)
extra = [
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_emmc', mock_modern_emmc),
    patch('mono_imager.recovery_orchestrator.phase_modern_verify_emmc_boot', mock_modern_verify_emmc),
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_nor', mock_modern_nor),
    patch('mono_imager.recovery_orchestrator.phase_legacy_flash_nor', mock_legacy_nor),
    patch('mono_imager.recovery_orchestrator.phase_modern_verify_nor_boot', mock_modern_verify_nor),
]
run_flow(extra, ['1', 'y', '', ''])
check("legacy NOR fallback attempted", mock_legacy_nor.called)
check("final verify_nor_boot NEVER runs (total NOR failure, no DIP flip back prompt)", not mock_modern_verify_nor.called)

print()
print("=" * 60)
print("Scenario 5: full modern success (no fallback triggered at all)")
print("=" * 60)
mock_modern_emmc = MagicMock(return_value=True)
mock_legacy_emmc = MagicMock(return_value=True)
mock_modern_verify_emmc = MagicMock(return_value=True)
mock_modern_nor = MagicMock(return_value=True)
mock_legacy_nor = MagicMock(return_value=True)
mock_modern_verify_nor = MagicMock(return_value=True)
extra = [
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_emmc', mock_modern_emmc),
    patch('mono_imager.recovery_orchestrator.phase_legacy_flash_emmc', mock_legacy_emmc),
    patch('mono_imager.recovery_orchestrator.phase_modern_verify_emmc_boot', mock_modern_verify_emmc),
    patch('mono_imager.recovery_orchestrator.phase_modern_flash_nor', mock_modern_nor),
    patch('mono_imager.recovery_orchestrator.phase_legacy_flash_nor', mock_legacy_nor),
    patch('mono_imager.recovery_orchestrator.phase_modern_verify_nor_boot', mock_modern_verify_nor),
]
run_flow(extra, ['1', 'y', '', '', ''])
check("modern eMMC succeeded, no legacy eMMC fallback called", not mock_legacy_emmc.called)
check("modern NOR succeeded, no legacy NOR fallback called", not mock_legacy_nor.called)
check("final verify_nor_boot runs normally", mock_modern_verify_nor.called)
