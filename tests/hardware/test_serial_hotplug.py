#!/usr/bin/env python3
"""
mono-imager: Hardware test — serial hotplug reconnect.

Requires: Mono Gateway connected via USB-to-UART, device at any state.

What this tests:
  - Simulated disconnect (software close of serial port) triggers reconnect
  - Real hardware unplug/replug triggers reconnect (interactive mode)

Both modes run 3 cycles and report pass/fail per cycle.

Usage:
  python tests/hardware/test_serial_hotplug.py --mode simulated [--port COM5]
  python tests/hardware/test_serial_hotplug.py --mode interactive [--port COM5]

Logs to: logs/test_serial_hotplug_<mode>_<timestamp>.log
"""

import sys
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.serial_device import SerialDevice

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def run_simulated(d: SerialDevice, cycles: int, logger) -> int:
    """
    Simulate unplug by software-closing the underlying serial port.
    Tests whether safe_read() triggers auto-reconnect transparently.
    Returns number of successful reconnects.
    """
    successes = 0
    for cycle in range(1, cycles + 1):
        logger.info(f"=== Cycle {cycle}/{cycles} ===")
        logger.info("Simulating UNPLUG (closing real serial port)...")
        try:
            d.ser._ser.close()
        except Exception:
            pass

        start = time.time()
        reconnected = False
        while time.time() - start < 30:
            if d.safe_read(1) is not None:
                reconnected = True
                break
            time.sleep(0.05)

        if reconnected:
            logger.info(f"✓ Reconnected (cycle {cycle})")
            successes += 1
        else:
            logger.error(f"✗ Reconnect FAILED (cycle {cycle})")
            break

    return successes


def run_interactive(d: SerialDevice, cycles: int, logger) -> int:
    """
    Prompt user to physically unplug and replug the device each cycle.
    Tests whether the reconnect logic handles real USB re-enumeration.
    Returns number of successful reconnects.
    """
    successes = 0
    for cycle in range(1, cycles + 1):
        logger.info(f"=== Cycle {cycle}/{cycles} ===")
        print(f"\n→ UNPLUG the device now (cycle {cycle}/{cycles})...")

        # Wait for real disconnect
        while True:
            try:
                _ = d.ser._ser.in_waiting
                time.sleep(0.05)
            except Exception:
                logger.info("✓ Disconnect detected")
                break

        print("→ REPLUG the device now...")
        logger.info("Waiting for reconnect...")

        start = time.time()
        reconnected = False
        while time.time() - start < 30:
            if d.safe_read(1) is not None:
                reconnected = True
                break
            time.sleep(0.05)

        if reconnected:
            logger.info(f"✓ Reconnected (cycle {cycle})")
            successes += 1
        else:
            logger.error(f"✗ Reconnect FAILED (cycle {cycle})")
            break

    return successes


def main():
    parser = argparse.ArgumentParser(description="Serial hotplug reconnect test")
    parser.add_argument("--port", default="COM5", help="Serial port (default: COM5)")
    parser.add_argument("--mode", choices=["simulated", "interactive"], default="simulated",
                        help="simulated = software close; interactive = physical unplug")
    parser.add_argument("--cycles", type=int, default=3, help="Number of reconnect cycles (default: 3)")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = LOG_DIR / f"test_serial_hotplug_{args.mode}_{timestamp}.log"

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
    logger.info(f"Log: {log_file}")

    print(f"=== Serial Hotplug Test ({args.mode}, {args.cycles} cycles) ===")
    print()

    d = SerialDevice(args.port, timeout=1)
    logger.info(f"Connecting to {args.port}...")

    if not d.connect(115200):
        logger.error("Initial connect failed — is the device plugged in?")
        sys.exit(1)
    logger.info("✓ Initial connection OK")

    if args.mode == "simulated":
        successes = run_simulated(d, args.cycles, logger)
    else:
        successes = run_interactive(d, args.cycles, logger)

    d.disconnect()

    print()
    print("=" * 60)
    logger.info(f"Successful reconnects: {successes}/{args.cycles}")
    if successes == args.cycles:
        logger.info("✓ PASS — reconnect logic is stable")
    else:
        logger.error("✗ FAIL — reconnect logic is unreliable")
    logger.info(f"Log saved to: {log_file}")

    sys.exit(0 if successes == args.cycles else 1)


if __name__ == "__main__":
    main()
