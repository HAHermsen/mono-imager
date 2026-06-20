#!/usr/bin/env python3
"""
mono-imager: PASSIVE, READ-ONLY check of the device's CURRENT state
after the Y-flood incident — connects and reads whatever's already
arriving on the wire, WITHOUT sending any commands (not even Enter),
to avoid interfering with anything that might still be in progress.

Usage:
    py test_check_current_device_state.py --port COM5
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.serial_device import SerialDevice


def main():
    parser = argparse.ArgumentParser(description="Passive read-only check of current device state")
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    parser.add_argument("--seconds", type=float, default=15.0, help="How long to passively listen")
    args = parser.parse_args()

    print("=" * 60)
    print("PASSIVE READ-ONLY — connecting and listening only.")
    print("NOTHING will be sent to the device, not even Enter.")
    print("=" * 60)

    d = SerialDevice(args.port, timeout=2)
    if not d.connect(115200):
        print("Failed to connect.")
        sys.exit(1)

    print(f"\n✓ Connected. Listening passively for {args.seconds}s...")
    print("=" * 60)

    start = time.time()
    buffer = b""
    while time.time() - start < args.seconds:
        chunk = d.ser.read(256)
        if chunk:
            text = chunk.decode("utf-8", errors="replace")
            print(text, end="", flush=True)
            buffer += chunk

    d.disconnect()

    print()
    print("=" * 60)
    print(f"Listened for {args.seconds}s. Captured {len(buffer)} bytes total.")
    print("=" * 60)
    if not buffer:
        print("No output at all — device may be idle at a prompt with")
        print("nothing new to say, or may be stuck silently.")
    else:
        print("See the raw output above for whatever the device is")
        print("currently doing/showing.")


if __name__ == "__main__":
    main()
