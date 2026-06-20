#!/usr/bin/env python3
"""
mono-imager: FULLY MANUAL flash verifier (wrapper)
Usage:
    py tests/test_verify_flash_manual.py

Asks for EVERY config value interactively. Nothing is auto-detected,
nothing defaults silently. You are in full control A-Z — this exists
specifically because auto-detection got the device IP wrong on a
real network (192.168.1.10 default vs 192.168.168.x host subnet) and
the fix is either: full auto, or full manual. No middle ground.

All device-talking logic lives in flash_orchestrator.py — this file
only collects input and calls those functions. See
test_verify_flash_auto.py for zero-config automatic operation.

Author:  H.A. Hermsen
Version: 0.4.0
License: MIT
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.config import detect_serial_ports
from mono_imager import flash_orchestrator as core


def ask(prompt: str, default: str = None) -> str:
    """Prompt for input. If default given, shown in brackets and used on blank Enter."""
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else (default or "")


def ask_int(prompt: str, default: int) -> int:
    val = ask(prompt, str(default))
    try:
        return int(val)
    except ValueError:
        core.logger.warning(f"'{val}' is not a number, using default {default}")
        return default


def ask_yes_no(prompt: str) -> bool:
    val = input(f"{prompt} [y/n]: ").strip().lower()
    return val.startswith("y")


def main():
    print("=" * 60)
    print("  mono-imager flash verifier — FULLY MANUAL MODE")
    print("  Every value below is typed by you. Nothing auto-detected.")
    print("=" * 60)
    print()

    core.logger.info(f"mono-imager verify_flash_manual.py v{core.__version__} by {core.__author__}")
    core.logger.info("Mode: FULLY MANUAL (no auto-detection)")
    core.logger.info(f"Log: {core.log_file}")
    print()

    # --- Show available ports as reference only, do not auto-pick ---
    try:
        known, other = detect_serial_ports()
        all_ports = known + other
        if all_ports:
            print("Detected ports (for reference only — you still type the one to use):")
            for p in all_ports:
                print(f"    {p.device}  —  {p.description}")
        else:
            print("No ports detected by the OS — you can still type one manually.")
    except Exception as e:
        print(f"(Port listing failed: {e} — you can still type one manually.)")
    print()

    port = ask("Serial port (e.g. COM5)")
    if not port:
        core.logger.error("Port is required.")
        sys.exit(1)

    baud = ask_int("Baud rate", 115200)

    transport = ask("Transport ('serial' or 'tcp')", "tcp").lower()
    if transport not in ("serial", "tcp"):
        core.logger.error(f"Invalid transport '{transport}' — must be 'serial' or 'tcp'")
        sys.exit(1)

    # --- Phase 1: bootstrap ---
    d = core.phase1_bootstrap(port, baud)
    if d is None:
        core.print_report()
        sys.exit(1)

    if transport == "serial":
        core.logger.info("Serial-only run: bootstrap verified, no network/flash phases requested.")
        d.disconnect()
        success = core.print_report()
        sys.exit(0 if success else 1)

    # --- TCP path: every value typed, no defaults silently applied ---
    server = None
    try:
        print()
        print("Network configuration — type your actual values:")
        host_ip = ask("Host IP (this machine's IP on the device's network)")
        if not host_ip:
            core.logger.error("Host IP is required for TCP transport.")
            sys.exit(1)

        device_ip = ask("Device IP to assign (must be on host's subnet)")
        if not device_ip:
            core.logger.error("Device IP is required for TCP transport.")
            sys.exit(1)

        http_port = ask_int("HTTP server port", 8080)

        firmware = ask("Path to local firmware file")
        firmware_path = Path(firmware).expanduser() if firmware else None
        if not firmware_path or not firmware_path.exists():
            core.logger.error(f"Firmware file not found: {firmware}")
            sys.exit(1)

        flash_target = ask("Flash target device (e.g. /dev/mmcblk0)")
        if not flash_target:
            core.logger.error("Flash target is required.")
            sys.exit(1)

        print()
        print(f"  About to flash:")
        print(f"    Firmware:    {firmware_path}")
        print(f"    Target:      {flash_target}")
        print(f"    Device IP:   {device_ip}")
        print(f"    Host IP:     {host_ip}:{http_port}")
        print()
        if not ask_yes_no("This writes to the device. Proceed?"):
            core.logger.info("Aborted by user before any write occurred.")
            sys.exit(0)

        # --- Phase 2 ---
        server = core.phase2_network(d, host_ip, device_ip, http_port, firmware_path)
        if server is None:
            core.print_report()
            sys.exit(1)

        # --- Phase 3 ---
        ok = core.phase3_flash(d, host_ip, http_port, flash_target)
        if not ok:
            core.print_report()
            sys.exit(1)

        # --- Phase 4 ---
        if ask_yes_no("Flash reported success. Reboot device and verify it comes back up?"):
            core.phase4_postflash(d)
        else:
            core.logger.info("Skipping reboot/post-flash verification by user choice.")

    finally:
        if server:
            server.shutdown()
            core.logger.info("HTTP server stopped")
        d.disconnect()

    success = core.print_report()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
