#!/usr/bin/env python3
"""
mono-imager: Hardware test — U-Boot environment and capability dump.

Requires: Mono Gateway connected via USB-to-UART, device on (at any
prompt — U-Boot, recovery shell, or booted OS).

Fully autonomous — no manual power cycle required. Triggers a software
reboot over serial, captures the full boot diagnostics during startup,
then interrupts autoboot and queries the U-Boot environment.

Queries:
  version     — U-Boot build date, git hash
  printenv    — full environment (boot sources, MACs, IP config,
                recovery/flash command definitions)
  mmc info    — eMMC geometry (size, bus width, speed mode)
  bdinfo      — board info, memory map, CPU and bus clock speeds
  i2c probe   — which I2C addresses respond (hardware variant detection)
  boot_source — derived from RCW output captured during boot

Output is printed in clearly labelled sections and saved to a timestamped
log. Run before/after a flash session as a baseline reference.

Usage:
  python tests/hardware/test_uboot_dump.py --port COM5
  python tests/hardware/test_uboot_dump.py --port COM5 --section version
  python tests/hardware/test_uboot_dump.py --port COM5 --section printenv
  python tests/hardware/test_uboot_dump.py --port COM5 --section mmc
  python tests/hardware/test_uboot_dump.py --port COM5 --section bdinfo
  python tests/hardware/test_uboot_dump.py --port COM5 --section i2c
  python tests/hardware/test_uboot_dump.py --port COM5 --section all

Logs to: logs/test_uboot_dump_<timestamp>.log
"""

import sys
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.serial_device import SerialDevice
from mono_imager.config import detect_serial_ports

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file  = LOG_DIR / f"test_uboot_dump_{timestamp}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file, encoding="utf-8")],
    force=True
)
logger = logging.getLogger(__name__)

SECTIONS = ["version", "printenv", "mmc", "bdinfo", "i2c", "all"]


def soft_reboot(d: SerialDevice):
    """
    Trigger a software reboot from whatever state the device is in.
    Sends both 'reset' (U-Boot) and 'reboot' (Linux) — one will be
    at the right prompt, the other is a no-op.
    The FTDI bridge stays enumerated, so we watch the same connection
    for the U-Boot countdown immediately after.
    """
    try:
        d.ser.reset_input_buffer()
        d.ser.write(b"\r\n")
        time.sleep(0.3)
        d.ser.write(b"reset\r\n")
        time.sleep(0.2)
        d.ser.write(b"reboot\r\n")
    except Exception as e:
        logger.warning(f"Soft reboot send warning: {e}")


def section_header(title: str):
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def query(d: SerialDevice, label: str, command: str, timeout: float = 10.0) -> str:
    section_header(label)
    print(f"  $ {command}")
    print()
    try:
        response = d.send_command(command, timeout=timeout)
        print(response)
        logger.info(f"[{label}]\n{response}")
        return response
    except Exception as e:
        msg = f"  ✗ Command failed: {e}"
        print(msg)
        logger.error(msg)
        return ""


def derive_boot_source(boot_diag: str) -> str:
    if "SD/EMMC" in boot_diag:
        return "eMMC  (DIP switch LEFT)"
    if "QSPI" in boot_diag:
        return "NOR / QSPI  (DIP switch RIGHT)"
    return "unknown  (boot output not captured)"


def capture_boot_output(d: SerialDevice, timeout: float = 60.0) -> str:
    """
    Read all serial output until 'Hit any key' appears (the autoboot
    countdown). Returns the captured text so the caller can extract
    boot diagnostics (RCW BOOT SRC, POST results, etc.) before
    wait_for_autoboot() interrupts the countdown.
    """
    buffer = b""
    start  = time.time()
    while time.time() - start < timeout:
        chunk = d.ser.read(256)
        if chunk:
            buffer += chunk
            if b"Hit any key to stop autoboot" in buffer:
                break
    return buffer.decode("utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(
        description="U-Boot environment and capability dump — read-only, autonomous"
    )
    parser.add_argument("--port", default=None,
                        help="Serial port (default: auto-detect FTDI)")
    parser.add_argument("--section", choices=SECTIONS, default="all",
                        help="Which section to dump (default: all)")
    args = parser.parse_args()

    # Port resolution
    if args.port:
        port = args.port
    else:
        known, other = detect_serial_ports()
        mono_port = next(
            (p for p in known + other if p.vid == 0x0403 and p.pid == 0x6015),
            None
        )
        if mono_port:
            port = mono_port.device
            print(f"Auto-detected: {port} ({mono_port.description})")
        elif known:
            port = known[0].device
            print(f"Using first known UART: {port} ({known[0].description})")
        else:
            print("No serial port detected. Use --port COM5 to specify one.")
            sys.exit(1)

    logger.info(f"U-Boot dump — port={port} section={args.section}")

    print()
    print("=" * 60)
    print("  mono-imager — U-Boot Dump  (autonomous)")
    print(f"  Port:    {port}")
    print(f"  Section: {args.section}")
    print(f"  Log:     {log_file}")
    print("=" * 60)

    d = SerialDevice(port, timeout=5)
    if not d.connect(115200):
        print("✗ Failed to connect.")
        sys.exit(1)
    print("✓ Connected")

    # Software reboot
    print()
    print("  Triggering software reboot over serial...")
    soft_reboot(d)
    print("  (Waiting for U-Boot boot output...)")
    print()

    # Capture boot diagnostics while waiting for autoboot countdown
    boot_diag = capture_boot_output(d, timeout=60)
    logger.info(f"[boot diagnostics]\n{boot_diag}")

    # Interrupt the countdown (it's already at "Hit any key" from capture above)
    if not d.wait_for_autoboot(timeout=10):
        # Countdown may have already passed — try once more with a fresh reboot
        print("  Missed countdown window — retrying...")
        soft_reboot(d)
        boot_diag = capture_boot_output(d, timeout=60)
        if not d.wait_for_autoboot(timeout=10):
            print("✗ Failed to reach U-Boot prompt.")
            d.disconnect()
            sys.exit(1)

    print("✓ At U-Boot prompt")

    # Boot source
    boot_source = derive_boot_source(boot_diag)
    section_header("Boot Source")
    print(f"  Detected: {boot_source}")
    for line in boot_diag.splitlines():
        if "BOOT SRC" in line or ("RCW" in line and "BOOT" in line):
            print(f"  Raw:      {line.strip()}")
    logger.info(f"[boot source] {boot_source}")

    run_all = args.section == "all"

    if run_all or args.section == "version":
        query(d, "U-Boot Version", "version")

    if run_all or args.section == "printenv":
        env = query(d, "Full Environment (printenv)", "printenv", timeout=15)
        if env:
            section_header("Key Variables (parsed)")
            keys_of_interest = [
                "ethaddr", "eth1addr", "eth2addr", "eth3addr", "eth4addr",
                "ipaddr", "serverip", "gatewayip", "netmask",
                "bootcmd", "recovery", "bootdelay",
                "SoC", "board", "board_name",
            ]
            found_any = False
            for line in env.splitlines():
                if "=" in line:
                    key = line.split("=")[0].strip()
                    if any(k.lower() in key.lower() for k in keys_of_interest):
                        print(f"  {line.strip()}")
                        found_any = True
            if not found_any:
                print("  (none of the expected variables found)")

    if run_all or args.section == "mmc":
        query(d, "eMMC Info (mmc info)", "mmc info")

    if run_all or args.section == "bdinfo":
        query(d, "Board Info (bdinfo)", "bdinfo")

    if run_all or args.section == "i2c":
        query(d, "I2C Bus 0 Probe", "i2c dev 0; i2c probe")

    print()
    print("=" * 60)
    print("  Dump complete — device left at U-Boot prompt.")
    print("  Run 'run recovery' manually or power cycle to continue.")
    print(f"  Full output: {log_file}")
    print("=" * 60)

    d.disconnect()


if __name__ == "__main__":
    main()
