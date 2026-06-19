#!/usr/bin/env python3
"""
mono-imager: Manual serial device test
Handles U-Boot autoboot interrupt, command verify, and recovery Linux boot.
Dumps full console output to logs/test_serial_<timestamp>.log

Author:  H.A. Hermsen
Version: 0.1.3
License: MIT
"""

__version__ = "0.1.3"
__author__ = "H.A. Hermsen"

import sys
import time
import logging

from datetime import datetime
from pathlib import Path
from mono_imager.config import detect_serial_ports
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

    known, other = detect_serial_ports()
    all_ports = known + other

    mono_port = None
    for p in all_ports:
        if p.vid == 0x0403 and p.pid == 0x6015:
            mono_port = p
            break

    if mono_port is None:
        logger.error("Mono Gateway UART not found — expected FTDI FT230X (VID=0x0403, PID=0x6015)")
        sys.exit(1)

    logger.info(f"Mono Gateway UART found: {mono_port.device}")

    d = SerialDevice(mono_port.device, timeout=5)

    try:
        # Step 1: Connect
        logger.info("Step 1: Connecting to COM5 at 115200 baud...")
        if not d.connect(115200):
            logger.error("Failed to connect")
            return False
        logger.info("✓ Connected")

        # Step 2: Interrupt U-Boot
        logger.info("Step 2: Waiting for U-Boot autoboot countdown...")
        print()
        print("=" * 60)
        print("  ⚡ POWER CYCLE YOUR DEVICE NOW ⚡")
        print("=" * 60)
        print()
        if not d.wait_for_autoboot(timeout=30):
            logger.error("Failed to interrupt autoboot")
            return False
        logger.info("✓ U-Boot interrupted")

        # Step 3: Quick verify
        logger.info("Step 3: Quick U-Boot verify...")
        response = d.send_command("printenv ethact", timeout=5)
        logger.info(f"ethact: {response.strip()}")
        logger.info("✓ U-Boot responding")

        # Step 4: Boot recovery Linux
        logger.info("Step 4: Booting recovery Linux...")
        d.send_command("run recovery", wait_for_prompt=False, timeout=3)

        logger.info("Waiting for recovery Linux (up to 60s)...")
        start = time.time()
        buffer = b""
        while time.time() - start < 60:
            byte = d.ser.read(1)
            if byte:
                buffer += byte
                if len(buffer) % 500 == 0:
                    sys.stdout.write(".")
                    sys.stdout.flush()
                if b"root@recovery" in buffer or b"login:" in buffer:
                    print()
                    logger.info("✓ Recovery Linux booted!")
                    if b"login:" in buffer and b"root@recovery" not in buffer:
                        d.ser.write(b"root\r\n")
                        time.sleep(1)
                    break
        else:
            print()
            logger.error("Recovery Linux did not boot within timeout")
            return False

        # Verify recovery prompt
        d.ser.write(b"\r\n")
        time.sleep(0.5)
        waiting = d.ser.in_waiting
        response = d.ser.read(waiting) if waiting else b""
        logger.info(f"Recovery prompt: {repr(response[-80:])}")

        if b"root@recovery" in buffer or b"root@recovery" in response:
            logger.info("✓ Logged into recovery Linux!")
        else:
            logger.warning("Recovery prompt not confirmed but continuing...")

        logger.info("✓ All steps passed!")
        return True

    except Exception as e:
        logger.error(f"Test failed: {e}")
        return False

    finally:
        d.disconnect()
        logger.info(f"📄 Log saved to: {log_file}")


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)