#!/usr/bin/env python3
"""
mono-imager: Raw serial response inspector
Waits for autoboot, interrupts, and prints raw U-Boot response.
Dumps full console output to logs/serial_response_<timestamp>.log

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
from mono_imager.config import detect_serial_ports

# --- Logging setup -----------------------------------------------------------

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file  = LOG_DIR / f"serial_response_{timestamp}.log"

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
    logger.info(f"mono-imager serial_response.py v{__version__} by {__author__}")
    logger.info(f"Log: {log_file}")

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

    d = SerialDevice(mono_port.device, timeout=2)

    try:
        if not d.connect(115200):
            logger.error("Failed to connect")
            return False

        logger.info("✓ Connected — power cycle your device now!")
        print("Power cycle NOW...")

        buf = b""
        while True:
            chunk = d.safe_read(512)
            if chunk:
                buf += chunk
                if b"Hit any key" in buf:
                    logger.info("Spamming interrupt...")

                    interrupt_start = time.time()
                    interrupt_buf   = b""

                    while time.time() - interrupt_start < 5.0:
                        d.safe_write(b" ")
                        chunk = d.safe_read(64)
                        if chunk:
                            interrupt_buf += chunk
                            if b"=>" in interrupt_buf:
                                break

                    in_waiting = d.ser._ser.in_waiting
                    logger.info(f"in_waiting: {in_waiting}")
                    logger.info(f"response: {repr(interrupt_buf)}")
                    return True

    except Exception as e:
        logger.error(f"Failed: {e}")
        return False

    finally:
        d.disconnect()
        logger.info(f"📄 Log saved to: {log_file}")


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
