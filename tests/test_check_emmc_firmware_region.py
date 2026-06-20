#!/usr/bin/env python3
"""
mono-imager: SAFE, READ-ONLY check of the RESERVED FIRMWARE REGION on
eMMC — the first 32 MB (sectors 0-65535), per the docs:

    "The first 32 MB of the eMMC is reserved for the firmware
    (bootloader, U-Boot, and recovery Linux)."

This is DIFFERENT from the OS partition check done previously
(test_check_emmc_contents.py), which only confirmed the OS partition
(mmcblk0p1, starting AT the 32MB boundary) has a real Armbian
filesystem. This script checks whether the region BEFORE that
boundary — the actual firmware/recovery area Step 6 of the docs
depends on — has ever been written, without writing anything itself.

NOTHING IS WRITTEN. Peeks at several offsets within the first 32MB
using read-only dd (no 'of=' target).

Usage:
    py test_check_emmc_firmware_region.py --port COM5
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap


def main():
    parser = argparse.ArgumentParser(
        description="Read-only check of eMMC's reserved firmware region (first 32MB)"
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    args = parser.parse_args()

    print("Boots to recovery from NOR (make sure DIP switch is on NOR).")
    print("This is READ-ONLY — nothing will be written to eMMC.")
    print()
    print("Running real phase1_bootstrap() (Steps 1-5)...")
    print("=" * 60)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        print("\nphase1_bootstrap FAILED — cannot proceed.")
        sys.exit(1)

    print("\n✓ Boot sequence complete, at recovery shell (NOR).")

    # Check several offsets within the first 32MB (sectors 0-65535):
    # start (sector 0), a middle point, and just before the 32MB
    # boundary — a single all-zero check at sector 0 alone wouldn't
    # rule out a real but sparse layout (e.g. U-Boot env at one
    # offset, recovery kernel at another).
    checks = [
        ("Sector 0 (very start)", "dd if=/dev/mmcblk0 bs=512 skip=0 count=4 2>/dev/null | hexdump -C"),
        ("~8MB in (sector 16384)", "dd if=/dev/mmcblk0 bs=512 skip=16384 count=4 2>/dev/null | hexdump -C"),
        ("~16MB in (sector 32768)", "dd if=/dev/mmcblk0 bs=512 skip=32768 count=4 2>/dev/null | hexdump -C"),
        ("Just before 32MB boundary (sector 65530)",
         "dd if=/dev/mmcblk0 bs=512 skip=65530 count=4 2>/dev/null | hexdump -C"),
        ("Any non-zero bytes anywhere in first 32MB? (slow-ish, full scan)",
         "dd if=/dev/mmcblk0 bs=1M count=32 2>/dev/null | tr -d '\\000' | wc -c"),
    ]

    for label, cmd in checks:
        print()
        print("=" * 60)
        print(label)
        print(f"  $ {cmd}")
        print("=" * 60)
        try:
            output = d.run_script(f"{cmd}; echo RC=$?", marker="fwregion_check", exec_timeout=60)
            print(output)
        except RuntimeError as e:
            print(f"  (command failed: {e})")

    d.disconnect()

    print()
    print("=" * 60)
    print("Check complete. Nothing was written to eMMC.")
    print("=" * 60)
    print()
    print("How to read this:")
    print("  - All offsets show '00 00 00 00 ...' (all zero) AND the")
    print("    full-scan non-zero byte count is 0 -> firmware region")
    print("    is genuinely blank, never written")
    print("  - Any offset shows real (non-zero, non-0xFF-erased-flash)")
    print("    data, or the full-scan count is > 0 -> something has")
    print("    been written here before")


if __name__ == "__main__":
    main()
