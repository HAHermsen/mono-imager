#!/usr/bin/env python3
"""
mono-imager: Hardware test — serial connection and U-Boot interrupt.

Requires: Mono Gateway connected via USB-to-UART, device on (at any
prompt — U-Boot, recovery shell, or booted OS).

Fully autonomous — no manual power cycle required. The script sends
'reset' or 'reboot' over serial to trigger the reboot itself, then
catches the U-Boot countdown on the same connection.

What this tests:
  - Serial port detected and connected at 115200 baud
  - Software-triggered reboot reaches U-Boot countdown
  - U-Boot autoboot interrupt succeeds
  - U-Boot responds to commands
  - Recovery Linux boots and login succeeds

Usage: python tests/hardware/test_serial_connect.py [--port COM5]

Logs to: logs/test_serial_connect_<timestamp>.log
"""

import sys
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.serial_device import SerialDevice
from mono_imager.config import detect_serial_ports

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file  = LOG_DIR / f"test_serial_connect_{timestamp}.log"

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


def soft_reboot(d: SerialDevice) -> bool:
    """
    Trigger a software reboot from whatever state the device is currently in.

    Tries, in order:
      1. 'reset'   — U-Boot command (works if at U-Boot prompt)
      2. 'reboot'  — Linux command (works if at recovery or OS shell)

    The FTDI bridge stays enumerated across reboot, so we don't wait for
    the port to disappear — we just send and immediately start watching
    for the U-Boot countdown on the same open connection.

    Returns True (always — the reboot is fire-and-forget; success is
    confirmed by wait_for_autoboot() seeing the countdown).
    """
    logger.info("Sending 'reset' (U-Boot) / 'reboot' (Linux) to trigger soft reboot...")
    try:
        # Send both — one will be at the right prompt, the other is a no-op
        d.ser.reset_input_buffer()
        d.ser.write(b"\r\n")          # wake up any sleeping prompt
        time.sleep(0.3)
        d.ser.write(b"reset\r\n")     # U-Boot
        time.sleep(0.2)
        d.ser.write(b"reboot\r\n")    # Linux
    except Exception as e:
        logger.warning(f"Soft reboot send failed: {e} — may still reboot")
    return True


def main():
    parser = argparse.ArgumentParser(description="Serial connection and U-Boot interrupt test")
    parser.add_argument("--port", default=None, help="Serial port (default: auto-detect FTDI)")
    args = parser.parse_args()

    logger.info(f"Log: {log_file}")
    print("=" * 60)
    print("Serial Connection Test  (autonomous)")
    print("=" * 60)

    # Port resolution
    if args.port:
        port = args.port
        logger.info(f"Using specified port: {port}")
    else:
        known, other = detect_serial_ports()
        mono_port = next(
            (p for p in known + other if p.vid == 0x0403 and p.pid == 0x6015),
            None
        )
        if mono_port is None:
            logger.error("Mono Gateway UART not found. Use --port to specify.")
            sys.exit(1)
        port = mono_port.device
        logger.info(f"Auto-detected: {port}")

    d = SerialDevice(port, timeout=5)
    results = []

    def check(label, passed, detail=""):
        mark = "✓" if passed else "✗"
        logger.info(f"{'PASS' if passed else 'FAIL'}: {label}{' — ' + detail if detail else ''}")
        print(f"  {mark}  {label}" + (f"\n     {detail}" if detail else ""))
        results.append(passed)
        return passed

    # Step 1: connect
    if not check("Connect at 115200 baud", d.connect(115200)):
        sys.exit(1)

    try:
        # Step 2: soft reboot
        soft_reboot(d)

        # Step 3: catch U-Boot countdown
        logger.info("Waiting for U-Boot autoboot countdown...")
        print()
        print("  (Waiting for U-Boot — device rebooting over serial...)")
        print()
        if not check("U-Boot autoboot interrupted", d.wait_for_autoboot(timeout=60)):
            sys.exit(1)

        # Step 4: U-Boot responds to a command
        response = d.send_command("printenv ethact", timeout=5)
        check("U-Boot responds to commands",
              bool(response.strip()),
              response.strip() or "no response")

        # Step 5: boot recovery
        logger.info("Booting recovery Linux...")
        d.ser.write(b"run recovery\r\n")

        start  = time.time()
        buffer = b""
        booted = False
        while time.time() - start < 90:
            byte = d.ser.read(1)
            if byte:
                buffer += byte
                if b"recovery login:" in buffer:
                    d.ser.write(b"root\r\n")
                    time.sleep(1)
                    booted = True
                    break
                elif b"root@recovery" in buffer:
                    booted = True
                    break
        check("Recovery Linux booted", booted)

        # Step 6: confirm shell
        if booted:
            d.ser.write(b"\r\n")
            time.sleep(0.5)
            waiting  = d.ser.in_waiting
            response = d.ser.read(waiting) if waiting else b""
            at_shell = b"root@recovery" in buffer or b"root@recovery" in response
            check("Logged into recovery shell", at_shell)

    finally:
        d.disconnect()

    print()
    print("─" * 60)
    total  = len(results)
    passed = sum(results)
    if passed == total:
        print(f"  ✓  All {total} checks passed")
    else:
        print(f"  ✗  {total - passed}/{total} checks failed")
    logger.info(f"Log: {log_file}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
