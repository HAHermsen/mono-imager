#!/usr/bin/env python3
"""
mono-imager: Simulated hotplug stress test (3 cycles, software close)
Dumps full console output to logs/test_serial_hotplug_simulated_<timestamp>.log

Author:  H.A. Hermsen
Version: 0.3.0
License: MIT
"""

__version__ = "0.3.0"
__author__  = "H.A. Hermsen"

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
log_file  = LOG_DIR / f"test_serial_hotplug_simulated_{timestamp}.log"

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

# --- Main --------------------------------------------------------------------

def main():
    logger.info(f"mono-imager test_serial_hotplug_simulated.py v{__version__} by {__author__}")
    logger.info(f"Log: {log_file}")

    print("=== Automated Hotplug Test (3 simulated cycles) ===")
    print()

    d = SerialDevice("COM5", timeout=1)

    logger.info("Connecting initially...")
    if not d.connect(115200):
        logger.error("Initial connect failed — is the device plugged in?")
        return False

    logger.info("✓ Initial connection OK")

    cycles    = 3
    successes = 0

    for cycle in range(1, cycles + 1):
        logger.info(f"=== Cycle {cycle} of {cycles} ===")

        # Simulate unplug by closing the real port
        logger.info("→ Simulating UNPLUG (closing real serial port)")
        try:
            d.ser._ser.close()
        except Exception:
            pass

        logger.info("Waiting for reconnect...")

        reconnected = False
        start = time.time()

        while time.time() - start < 30:
            b = d.safe_read(1)  # triggers auto-reconnect
            if b is not None:
                reconnected = True
                break
            time.sleep(0.05)

        if reconnected:
            logger.info(f"✓ Reconnected successfully (cycle {cycle})")
            successes += 1
        else:
            logger.error(f"✗ Reconnect FAILED (cycle {cycle})")
            break

    logger.info("=== Test Summary ===")
    logger.info(f"Successful reconnects: {successes}/{cycles}")

    if successes == cycles:
        logger.info("✓ PASS — reconnect logic is stable")
    else:
        logger.error("✗ FAIL — reconnect logic is unreliable")

    d.disconnect()
    logger.info(f"📄 Log saved to: {log_file}")
    return successes == cycles


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
