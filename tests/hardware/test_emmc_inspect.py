#!/usr/bin/env python3
"""
mono-imager: Hardware test — eMMC contents inspection (read-only).

Requires: Mono Gateway connected, DIP switch on NOR.

What this tests (nothing is written):
  - Partition table presence (fdisk -l)
  - Filesystem signatures (blkid)
  - First 512 bytes of eMMC (blank vs written)
  - Firmware region (first 32MB): multiple offsets + non-zero byte count

Used before a flash session to understand the device's current state,
and after a flash to verify the partition table and firmware region
look correct.

Usage: python tests/hardware/test_emmc_inspect.py --port COM5 [--firmware-region]

Logs to: logs/test_emmc_inspect_<timestamp>.log
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime
import logging

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap
import time as _time


def _soft_reboot(port: str):
    """Send soft reboot so phase1_bootstrap can catch U-Boot without manual power cycle."""
    try:
        import serial as _serial
        s = _serial.Serial(port, 115200, timeout=1)
        s.write(b"\r\nreset\r\nreboot\r\n")
        _time.sleep(0.5)
        s.close()
    except Exception:
        pass  # port may already be in use; phase1_bootstrap will open it


LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file  = LOG_DIR / f"test_emmc_inspect_{timestamp}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
    force=True
)
logger = logging.getLogger(__name__)


OS_CHECKS = [
    ("Partition table",    "fdisk -l /dev/mmcblk0"),
    ("Filesystem types",   "blkid /dev/mmcblk0 /dev/mmcblk0p1 /dev/mmcblk0p2 2>/dev/null"),
    ("First 512 bytes",    "dd if=/dev/mmcblk0 bs=512 count=1 2>/dev/null | hexdump -C | head -5"),
    ("Device size",        "cat /sys/class/block/mmcblk0/size 2>/dev/null"),
]

FIRMWARE_REGION_CHECKS = [
    ("Sector 0 (start)",               "dd if=/dev/mmcblk0 bs=512 skip=0 count=4 2>/dev/null | hexdump -C"),
    ("~8MB in (sector 16384)",          "dd if=/dev/mmcblk0 bs=512 skip=16384 count=4 2>/dev/null | hexdump -C"),
    ("~16MB in (sector 32768)",         "dd if=/dev/mmcblk0 bs=512 skip=32768 count=4 2>/dev/null | hexdump -C"),
    ("Just before 32MB (sector 65530)", "dd if=/dev/mmcblk0 bs=512 skip=65530 count=4 2>/dev/null | hexdump -C"),
    ("Non-zero bytes in first 32MB",    "dd if=/dev/mmcblk0 bs=1M count=32 2>/dev/null | tr -d '\\000' | wc -c"),
]


def run_checks(d, checks: list):
    for label, cmd in checks:
        print()
        print("=" * 60)
        print(label)
        print(f"  $ {cmd}")
        print("=" * 60)
        try:
            output = d.run_script(f"{cmd}; echo RC=$?", marker="emmc_inspect", exec_timeout=60)
            print(output)
        except RuntimeError as e:
            print(f"  (command failed: {e})")


def main():
    parser = argparse.ArgumentParser(description="Read-only eMMC inspection — nothing is written")
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    parser.add_argument("--firmware-region", action="store_true",
                        help="Also inspect first 32MB firmware region (slower)")
    args = parser.parse_args()

    logger.info(f"Log: {log_file}")
    print("DIP switch must be on NOR. NOTHING WILL BE WRITTEN.")
    print()
    print("Triggering soft reboot, then running phase1_bootstrap()...")
    print("=" * 60)

    # Soft reboot — so no manual power cycle needed
    _soft_reboot(args.port)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        logger.error("Bootstrap failed — cannot inspect eMMC")
        sys.exit(1)

    logger.info("✓ At recovery shell")

    print()
    print("=" * 60)
    print("OS partition checks")
    print("=" * 60)
    run_checks(d, OS_CHECKS)

    if args.firmware_region:
        print()
        print("=" * 60)
        print("Firmware region checks (first 32MB)")
        print("=" * 60)
        run_checks(d, FIRMWARE_REGION_CHECKS)

    d.disconnect()

    print()
    print("=" * 60)
    print("Inspection complete. Nothing was written.")
    print("=" * 60)
    print()
    print("How to read the results:")
    print("  fdisk showing NO partitions, all-zero first 512 bytes,")
    print("  and non-zero byte count == 0 in firmware region")
    print("  → eMMC is blank / never written")
    print()
    print("  Real partitions, filesystem signatures, or non-zero")
    print("  bytes in the firmware region → something has been written")
    logger.info(f"Log saved to: {log_file}")


if __name__ == "__main__":
    main()
