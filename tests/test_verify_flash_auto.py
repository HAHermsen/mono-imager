#!/usr/bin/env python3
"""
mono-imager: FULLY AUTOMATIC flash verifier (wrapper)
Usage:
    py tests/test_verify_flash_auto.py --firmware C:\\path\\to\\image.img

Zero manual config. Auto-detects:
  - Serial port  (first device found)
  - Host IP      (outbound socket trick)
  - Device IP    (derived from host's own subnet — not a hardcoded guess)

Only --firmware is required, since the firmware file is the one thing
that genuinely cannot be auto-detected.

All device-talking logic lives in flash_orchestrator.py — this file
only resolves config and calls those functions. See
test_verify_flash_manual.py for full manual control with zero
auto-detection. No middle ground between the two.

Author:  H.A. Hermsen
Version: 0.3.0
License: MIT
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.config import detect_serial_ports
from mono_imager import flash_orchestrator as core

# pick_device_ip() lives in flash_orchestrator.py (as core.pick_device_ip)
# rather than being duplicated here — shared by every script that needs
# to derive a device IP on the host's subnet.


def main():
    parser = argparse.ArgumentParser(
        description="mono-imager flash verifier — FULLY AUTOMATIC"
    )
    parser.add_argument("--firmware", required=True,
                        help="Path to local firmware file")
    parser.add_argument("--port", default=None,
                        help="Serial port override (default: auto-detect)")
    args = parser.parse_args()

    core.logger.info(f"mono-imager verify_flash_auto.py v{core.__version__} by {core.__author__}")
    core.logger.info("Mode: FULLY AUTOMATIC (no manual config)")
    core.logger.info(f"Log: {core.log_file}")
    print()

    # --- Resolve port ---
    if args.port:
        port = args.port
        core.logger.info(f"Using specified port: {port}")
    else:
        try:
            known, other = detect_serial_ports()
            all_ports = known + other
            if not all_ports:
                core.logger.error("No serial ports detected — connect device or use --port to override")
                sys.exit(1)
            port = all_ports[0].device
            core.logger.info(f"Auto-detected port: {port} ({all_ports[0].description})")
        except Exception as e:
            core.logger.error(f"Port auto-detection failed: {e}")
            sys.exit(1)

    # --- Resolve firmware ---
    firmware_path = Path(args.firmware).expanduser()
    if not firmware_path.exists():
        core.logger.error(f"Firmware file not found: {firmware_path}")
        sys.exit(1)

    # --- Phase 1: bootstrap (always serial, 115200 — proven working) ---
    d = core.phase1_bootstrap(port, 115200)
    if d is None:
        core.print_report()
        sys.exit(1)

    server = None

    try:
        # --- Resolve network config ---
        host_ip = core.detect_host_ip()
        device_ip = core.pick_device_ip(host_ip)
        core.logger.info(f"Host IP (auto-detected): {host_ip}")
        core.logger.info(f"Device IP (auto-derived, same subnet): {device_ip}")

        http_port = 8080
        flash_target = "/dev/mmcblk0"

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
        core.phase4_postflash(d)

    finally:
        if server:
            server.shutdown()
            core.logger.info("HTTP server stopped")
        d.disconnect()

    success = core.print_report()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
