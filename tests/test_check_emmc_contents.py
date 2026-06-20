#!/usr/bin/env python3
"""
mono-imager: SAFE, READ-ONLY check of what's currently on eMMC
(/dev/mmcblk0) — partition table, filesystem signatures, first bytes.

NOTHING IS WRITTEN. Every command here is inspection-only:
    fdisk -l /dev/mmcblk0    — list partition table (read-only)
    blkid /dev/mmcblk0*      — filesystem type/UUID if any (read-only)
    dd ... | hexdump          — peek at first 512 bytes (read-only,
                                 no 'of=' target — nothing written)

This exists to answer: "does eMMC already have something bootable on
it, or is it blank?" — before deciding whether dry-run testing of the
recovery flow needs a real `firmware update` to be meaningful past
the boot-source-detection step.

Usage:
    py test_check_emmc_contents.py --port COM5
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap


def main():
    parser = argparse.ArgumentParser(
        description="Read-only check of eMMC contents — nothing is written"
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

    checks = [
        ("Partition table (fdisk -l)", "fdisk -l /dev/mmcblk0"),
        ("Filesystem signatures (blkid)", "blkid /dev/mmcblk0 /dev/mmcblk0p1 /dev/mmcblk0p2 2>/dev/null"),
        ("First 512 bytes (hex, read-only peek)",
         "dd if=/dev/mmcblk0 bs=512 count=1 2>/dev/null | hexdump -C | head -5"),
        ("Device size", "cat /sys/class/block/mmcblk0/size 2>/dev/null"),
    ]

    for label, cmd in checks:
        print()
        print("=" * 60)
        print(label)
        print(f"  $ {cmd}")
        print("=" * 60)
        try:
            output = d.run_script(f"{cmd}; echo RC=$?", marker="emmc_check")
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
    print("  - 'fdisk -l' showing NO partitions, or an error about")
    print("    no recognized partition table -> eMMC is likely blank")
    print("  - 'blkid' returning nothing -> no recognized filesystem")
    print("  - first 512 bytes all-zero (00 00 00 00 ...) -> blank/unwritten")
    print("  - any of these showing real partitions/filesystem info ->")
    print("    eMMC already has something on it")


if __name__ == "__main__":
    main()
