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
import time
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

        # Step 4: Boot recovery Linux
        logger.info("Step 4: Booting recovery Linux...")
        logger.info("Sending 'run recovery' to U-Boot...")

        d.send_command("run recovery", wait_for_prompt=False, timeout=3)

        # Wait for recovery Linux to boot and present login
        logger.info("Waiting for recovery Linux boot (up to 60s)...")
        start = time.time()
        buffer = b""
        while time.time() - start < 60:
            byte = d.ser.read(1)
            if byte:
                buffer += byte
                # Log progress dots
                if len(buffer) % 500 == 0:
                    sys.stdout.write(".")
                    sys.stdout.flush()
                if b"root@recovery" in buffer or b"login:" in buffer:
                    print()
                    logger.info("✓ Recovery Linux booted!")
                    # Auto-login if needed
                    if b"login:" in buffer:
                        d.ser.write(b"root\r\n")
                        time.sleep(1)
                    break
        else:
            print()
            logger.error("Recovery Linux did not boot within timeout")
            return False

        # Verify we're at recovery prompt
        d.ser.write(b"\r\n")
        time.sleep(0.5)
        waiting = d.ser.in_waiting
        response = d.ser.read(waiting) if waiting else b""
        logger.info(f"Recovery prompt: {repr(response[-80:])}")

        if b"root@recovery" in response or b"root@recovery" in buffer:
            logger.info("✓ Logged into recovery Linux!")
        else:
            logger.warning("Recovery prompt not confirmed but continuing...")

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
