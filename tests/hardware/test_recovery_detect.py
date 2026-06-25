#!/usr/bin/env python3
"""
mono-imager: Hardware test — recovery detection (read-only).

Requires: Mono Gateway connected, DIP switch on NOR.

What this tests (nothing is written):
  - detect_modern_firmware_tool() reports correct result for this device
  - Device MAC address is readable via ip addr

Run this before a real recovery/firmware-update session to confirm:
  - Which recovery path the device will use (modern vs legacy)
  - The correct MAC address for firmware server authentication

Usage: python tests/hardware/test_recovery_detect.py --port COM5

Logs to: logs/test_recovery_detect_<timestamp>.log
"""

import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap
from mono_imager import recovery_orchestrator as rec
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
        pass


LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file  = LOG_DIR / f"test_recovery_detect_{timestamp}.log"

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


def main():
    parser = argparse.ArgumentParser(description="Read-only recovery detection probe")
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    args = parser.parse_args()

    logger.info(f"Log: {log_file}")
    print("DIP switch must be on NOR. NOTHING WILL BE WRITTEN.")
    print()
    print("Triggering soft reboot, then running phase1_bootstrap()...")
    print("=" * 60)

    _soft_reboot(args.port)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        logger.error("Bootstrap failed")
        sys.exit(1)

    logger.info("✓ At recovery shell")

    # Check 1: firmware tool detection
    print()
    print("=" * 60)
    print("detect_modern_firmware_tool()  (`which firmware`)")
    print("=" * 60)

    has_modern = rec.detect_modern_firmware_tool(d)
    if has_modern is True:
        logger.info("RESULT: Modern 'firmware' command present → MODERN recovery path")
    elif has_modern is False:
        logger.info("RESULT: Modern 'firmware' command absent → LEGACY recovery path")
    else:
        logger.warning("RESULT: Detection inconclusive — check serial output above")

    # Check 2: MAC address
    print()
    print("=" * 60)
    print("get_device_mac()  (`ip addr`)")
    print("=" * 60)

    mac = None
    for iface in ["eth0", "eth1", "eth2"]:
        mac = rec.get_device_mac(d, iface)
        if mac:
            logger.info(f"RESULT: {iface} MAC = {mac}")
            logger.info("        Use this MAC for firmware server authentication")
            break
    if not mac:
        logger.warning("RESULT: No MAC found on eth0/eth1/eth2")
        logger.warning("        Ensure an Ethernet cable is connected")

    d.disconnect()

    print()
    print("=" * 60)
    print("Detection complete. Nothing was written.")
    print(f"Modern firmware tool: {has_modern}")
    print(f"Device MAC:           {mac or 'not found'}")
    print("=" * 60)
    logger.info(f"Log saved to: {log_file}")


if __name__ == "__main__":
    main()
