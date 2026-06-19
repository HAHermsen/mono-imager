#!/usr/bin/env python3
"""
mono-imager: Manual serial device test
Handles U-Boot autoboot interrupt and basic command verification.
Dumps full console output to logs/test_serial_<timestamp>.log

Author:  H.A. Hermsen
Version: 0.1.0
License: MIT
"""

__version__ = "0.1.0"
__author__ = "H.A. Hermsen"

import sys
import logging
from datetime import datetime
from pathlib import Path

from mono_imager.serial_device import SerialDevice

# --- Logging setup -----------------------------------------------------------

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = LOG_DIR / f"test_serial_{timestamp}.log"

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

# --- Test --------------------------------------------------------------------

def main():
    logger.info(f"mono-imager test_serial.py v{__version__} by {__author__}")
    logger.info(f"Log: {log_file}")

    print("=" * 60)
    print("mono-imager Serial Device Test")
    print(f"Log file: {log_file}")
    print("=" * 60)
    print()

    # Step 1: Connect
    logger.info("Step 1: Connecting to COM5 at 115200 baud...")
    d = SerialDevice('COM5', timeout=15)

    if not d.connect(115200):
        logger.error("Failed to connect")
        return False

    logger.info("✓ Connected")

    # Step 2: Wait for autoboot
    logger.info("Step 2: Waiting for U-Boot autoboot countdown...")
    logger.info("(Power cycle your device NOW if it's off)")

    if not d.wait_for_autoboot(timeout=30):
        logger.error("Failed to detect and interrupt autoboot")
        return False

    logger.info("✓ U-Boot autoboot interrupted")

    # Step 3: U-Boot commands
    logger.info("Step 3: Testing U-Boot commands...")

    try:
        d.send_command("", wait_for_prompt=True, timeout=3)

        for cmd in ["printenv ethact", "printenv load_addr", "version"]:
            response = d.send_command(cmd)
            logger.info(f"[{cmd}]:\n{response}\n")

        logger.info("✓ All tests passed!")
        return True

    except Exception as e:
        logger.error(f"Command failed: {e}")
        return False

    finally:
        d.disconnect()
        logger.info(f"📄 Full log saved to: {log_file}")


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)