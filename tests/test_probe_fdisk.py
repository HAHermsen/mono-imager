#!/usr/bin/env python3
"""
mono-imager: probe BusyBox fdisk's real interactive prompts on actual
hardware, so the "Recover bricked device" feature's fdisk driver can
be built against VERIFIED prompt text instead of generic/util-linux
documentation (this device's recovery Linux is confirmed BusyBox-based
elsewhere this session — BusyBox fdisk's prompt wording can differ
from util-linux fdisk).

SAFE BY DEFAULT: this script sends 'o' (new in-memory table) and 'n'
(start creating a partition) so we can see the real prompt sequence,
but does NOT commit anything to disk — it aborts with 'q' before any
write happens, unless --commit is explicitly passed. The partition
table on /dev/mmcblk0 is NOT touched in default (probe-only) mode.

Logs the raw bytes received after every single keystroke sent, so the
exact prompt text (and any BusyBox quirks vs. the recipe/generic docs)
can be read directly from the log rather than assumed.

Usage:
    py test_probe_fdisk.py --port COM5                # probe only, safe
    py test_probe_fdisk.py --port COM5 --commit        # ACTUALLY WIPES THE DISK
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap


def send_and_capture(device, label: str, data: str, wait: float = 3.0, read_chunk: int = 2048) -> str:
    """
    Send raw bytes to the device and capture whatever comes back over
    `wait` seconds. No prompt-matching, no assumptions about what the
    response should look like — this is purely for OBSERVING the real
    behavior so we know what to match against later.
    """
    print(f"\n{'='*60}")
    print(f"SENDING: {label!r}  (raw bytes: {data!r})")
    print('='*60)

    device.ser.reset_input_buffer()
    device.ser.write(data.encode())

    start = time.time()
    response = b""
    while time.time() - start < wait:
        chunk = device.ser.read(read_chunk)
        if chunk:
            response += chunk

    decoded = response.decode("utf-8", errors="replace")
    print(f"RECEIVED ({len(response)} bytes):")
    print(decoded)
    print('-'*60)
    return decoded


def main():
    parser = argparse.ArgumentParser(
        description="Probe real BusyBox fdisk prompts on actual hardware"
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    parser.add_argument(
        "--commit", action="store_true",
        help="DANGEROUS: actually send 'w' and commit the partition table "
             "write. Without this flag, the script aborts with 'q' before "
             "any write happens."
    )
    parser.add_argument(
        "--device", default="/dev/mmcblk0",
        help="Target block device (default: /dev/mmcblk0)"
    )
    args = parser.parse_args()

    if args.commit:
        print("!" * 60)
        print("  --commit IS SET. This WILL wipe the partition table on")
        print(f"  {args.device} for real. This is NOT reversible.")
        print("!" * 60)
        confirm = input("  Type 'yes wipe it' to continue: ").strip()
        if confirm != "yes wipe it":
            print("  Confirmation not received. Aborting.")
            sys.exit(1)

    print("Running real phase1_bootstrap() (Steps 1-5)...")
    print("=" * 60)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        print("\nphase1_bootstrap FAILED — cannot proceed with this probe.")
        sys.exit(1)

    print("\n✓ Boot sequence complete, at recovery shell.")
    print(f"\nStarting fdisk probe on {args.device}...")

    try:
        # Step 1: launch fdisk itself
        send_and_capture(d, "launch fdisk", f"fdisk {args.device}\r\n")

        # Step 2: 'o' — create new empty DOS partition table (in memory only)
        send_and_capture(d, "'o' (new DOS table)", "o\r\n")

        # Step 3: 'n' — start creating a new partition
        out_n = send_and_capture(d, "'n' (new partition)", "n\r\n")

        # BusyBox fdisk may or may not ask for partition TYPE (p/e) —
        # log what we got and decide the next send based on what's
        # actually there, rather than assuming the recipe's sequence.
        if "primary" in out_n.lower() or "extended" in out_n.lower():
            send_and_capture(d, "'p' (primary partition)", "p\r\n")

        # Step 4: partition number, if asked
        send_and_capture(d, "partition number (default/Enter)", "\r\n")

        # Step 5: first sector — 65536 per the recipe (32MB offset)
        send_and_capture(d, "first sector = 65536", "65536\r\n")

        # Step 6: last sector — accept default (rest of disk)
        send_and_capture(d, "last sector (default/Enter)", "\r\n")

        # Step 7: print table before deciding to commit or abort —
        # always safe, 'p' never writes anything
        send_and_capture(d, "'p' (print table, verify before commit)", "p\r\n")

        if args.commit:
            print("\nCOMMITTING with 'w'...")
            send_and_capture(d, "'w' (WRITE — COMMITS TO DISK)", "w\r\n", wait=5.0)
        else:
            print("\nProbe-only mode — aborting with 'q' (NOTHING written to disk)...")
            send_and_capture(d, "'q' (abort, no write)", "q\r\n")

    finally:
        d.disconnect()

    print("\n" + "=" * 60)
    print("Probe complete. Review the RECEIVED blocks above to confirm")
    print("the exact real prompt text at each step before building the")
    print("automated fdisk driver against it.")
    print("=" * 60)


if __name__ == "__main__":
    main()
