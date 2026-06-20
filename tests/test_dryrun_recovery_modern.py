#!/usr/bin/env python3
"""
mono-imager: DRY-RUN of the modern recovery sequence against real
hardware — verifies the FLOW and TIMING work correctly without ever
calling the actual destructive `firmware update` command.

This walks through the real sequence:
    1. Boot recovery from NOR (real)
    2. Detect modern firmware tool (real, read-only)
    3. [SKIPPED] Would run: firmware update (flashes eMMC)
    4. Prompt: flip DIP to eMMC, reboot yourself
    5. Verify eMMC boot marker (real, read-only)
    6. Re-enter recovery from eMMC (real)
    7. [SKIPPED] Would run: firmware update (flashes NOR)
    8. Prompt: flip DIP back to NOR, reboot yourself
    9. Verify NOR boot marker (real, read-only)

NOTHING IS FLASHED. Steps 3 and 7 print what command WOULD run, but
never send it. This confirms the prompting, timing, and verification
logic all work correctly against real hardware before the actual
destructive driver is trusted.

Usage:
    py test_dryrun_recovery_modern.py --port COM5
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap
from mono_imager import recovery_orchestrator as rec


def main():
    parser = argparse.ArgumentParser(
        description="Dry-run the modern recovery sequence — NO flashing happens"
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    args = parser.parse_args()

    rec.reset_results()

    print("=" * 60)
    print("DRY RUN — nothing will be flashed. This only verifies the")
    print("sequence's flow, prompting, and detection logic.")
    print("=" * 60)
    print()
    print("Make sure the DIP switch is currently set to NOR.")
    print()
    print("Running real phase1_bootstrap() (Steps 1-5)...")
    print("=" * 60)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        print("\nphase1_bootstrap FAILED — cannot proceed.")
        sys.exit(1)

    print("\n✓ Boot sequence complete, at recovery shell (NOR).")

    print()
    print("=" * 60)
    print("STEP 2: detect_modern_firmware_tool()  [REAL]")
    print("=" * 60)
    has_modern = rec.detect_modern_firmware_tool(d)
    print(f"Result: {has_modern}")
    if has_modern is not True:
        print("\nThis device does not report the modern 'firmware' command.")
        print("Dry-run is for the MODERN path only — stopping here.")
        d.disconnect()
        sys.exit(0)

    print()
    print("=" * 60)
    print("STEP 3: [SKIPPED] Would run on device: firmware update")
    print("         (this would flash eMMC — NOT executed in dry-run)")
    print("=" * 60)

    d.disconnect()
    print("\nSerial connection closed (need a clean read buffer for the next boot).")

    print()
    print("=" * 60)
    print("  ⚡ NOW: flip the DIP switch to eMMC, then run 'reboot' ⚡")
    print("  ⚡ on the device (or power-cycle it), and press Enter   ⚡")
    print("  ⚡ here once you've done that.                          ⚡")
    print("=" * 60)
    input("Press Enter once the device is rebooting...")

    print()
    print("=" * 60)
    print("STEP 5: verify_boot_source(d, 'EMMC')  [REAL]")
    print("=" * 60)

    # Reconnect fresh to watch the boot output from scratch
    from mono_imager.serial_device import SerialDevice
    d2 = SerialDevice(args.port, timeout=5)
    if not d2.connect(115200):
        print("Failed to reconnect.")
        sys.exit(1)

    emmc_confirmed = rec.verify_boot_source(d2, "EMMC", timeout=90)
    print(f"Result: {emmc_confirmed}")

    if not emmc_confirmed:
        print("\nDid not see the eMMC boot marker — check the DIP switch")
        print("position and that you actually rebooted. Stopping dry-run.")
        d2.disconnect()
        sys.exit(1)

    print()
    print("=" * 60)
    print("STEP 6: re-enter recovery from eMMC  [REAL]")
    print("=" * 60)
    print("Interrupt the U-Boot countdown if it's still running...")

    # The device already rebooted into U-Boot at this point (we just
    # confirmed the RCW BOOT SRC line printed) — wait_for_autoboot()
    # picks up from here the same way phase1_bootstrap's Step 3 does.
    interrupted = d2.wait_for_autoboot(timeout=60)
    if not interrupted:
        print("Could not interrupt autoboot.")
        d2.disconnect()
        sys.exit(1)

    booted = d2.boot_recovery()
    logged_in = d2.login_recovery(timeout=60) if booted else False
    print(f"Recovery boot: {booted}, login: {logged_in}")

    if not logged_in:
        print("Could not get back into recovery shell on eMMC.")
        d2.disconnect()
        sys.exit(1)

    print()
    print("=" * 60)
    print("STEP 7: [SKIPPED] Would run on device: firmware update")
    print("         (this would flash NOR — NOT executed in dry-run)")
    print("=" * 60)

    d2.disconnect()
    print("\nSerial connection closed.")

    print()
    print("=" * 60)
    print("  ⚡ NOW: flip the DIP switch BACK to NOR, then run         ⚡")
    print("  ⚡ 'reboot' on the device (or power-cycle it), and press  ⚡")
    print("  ⚡ Enter here once you've done that.                      ⚡")
    print("=" * 60)
    input("Press Enter once the device is rebooting...")

    print()
    print("=" * 60)
    print("STEP 9: verify_boot_source(d, 'NOR')  [REAL]")
    print("=" * 60)

    d3 = SerialDevice(args.port, timeout=5)
    if not d3.connect(115200):
        print("Failed to reconnect.")
        sys.exit(1)

    nor_confirmed = rec.verify_boot_source(d3, "NOR", timeout=90)
    print(f"Result: {nor_confirmed}")
    d3.disconnect()

    print()
    print("=" * 60)
    print("DRY RUN COMPLETE")
    print("=" * 60)
    print(f"detect_modern_firmware_tool: {has_modern}")
    print(f"eMMC boot verified:          {emmc_confirmed}")
    print(f"Re-entered recovery on eMMC: {logged_in}")
    print(f"NOR boot verified:           {nor_confirmed}")
    print()
    if all([has_modern, emmc_confirmed, logged_in, nor_confirmed]):
        print("✓ Full sequence flow confirmed working. Nothing was flashed.")
    else:
        print("✗ Something in the sequence did not behave as expected —")
        print("  review the results above before trusting the real driver.")


if __name__ == "__main__":
    main()
