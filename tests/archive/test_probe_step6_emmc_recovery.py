#!/usr/bin/env python3
"""
mono-imager: isolated, tightly-bounded probe of Step 6 specifically —
"boot into recovery Linux from eMMC" — after a REAL DIP-switch flip.

This does NOT write anything. `run recovery` only attempts to BOOT
something that may or may not be there; it has no write side effects
itself. This isolates the one open question from the dry-run: does
boot_recovery() succeed, fail cleanly, or genuinely hang, when
attempted on eMMC's current (unknown, but confirmed non-blank)
firmware region?

Includes the reconnect retry logic added after the earlier dry-run
session (reconnecting right after a DIP flip + power cycle is more
disruptive to the USB-serial bridge than a steady-state connect).

Usage:
    1. Make sure the device is ALREADY on eMMC (flip the switch and
       reboot/power-cycle BEFORE running this script — it does not
       do that part for you).
    2. py test_probe_step6_emmc_recovery.py --port COM5
"""

import sys
import argparse
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.serial_device import SerialDevice
from mono_imager import recovery_orchestrator as rec


def main():
    parser = argparse.ArgumentParser(
        description="Isolated probe of Step 6 (boot recovery from eMMC) — read-only"
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    args = parser.parse_args()

    print("=" * 60)
    print("This assumes the device is ALREADY booted/booting with the")
    print("DIP switch on eMMC (you should have flipped it and")
    print("rebooted/power-cycled BEFORE running this script).")
    print("=" * 60)
    print()
    confirm = input("Is the device currently on eMMC and rebooting/booted? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborting — flip the switch and reboot first, then re-run this.")
        sys.exit(0)

    print()
    print("Reconnecting (with retry, since this follows a physical")
    print("DIP flip + reboot)...")
    print("=" * 60)

    d = SerialDevice(args.port, timeout=5)
    reconnected = False
    for attempt in range(1, 4):
        print(f"Reconnect attempt {attempt}/3...")
        if d.connect(115200):
            reconnected = True
            break
        print(f"  attempt {attempt} failed, retrying...")

    if not reconnected:
        print("\nCould not reconnect after 3 attempts. Stopping.")
        sys.exit(1)

    print("\n✓ Reconnected.")
    print()
    print("=" * 60)
    print("Confirming boot source is actually eMMC before attempting")
    print("recovery (avoids a misleading result if the switch wasn't")
    print("really flipped, or the reboot hasn't happened yet)...")
    print("=" * 60)

    is_emmc = rec.verify_boot_source(d, "EMMC", timeout=90)
    print(f"Boot source is eMMC: {is_emmc}")

    if not is_emmc:
        print("\nDid not confirm eMMC boot — stopping before attempting")
        print("recovery, since the result would be meaningless if we're")
        print("not actually booted from eMMC right now.")
        d.disconnect()
        sys.exit(1)

    print()
    print("=" * 60)
    print("Interrupting U-Boot autoboot (bounded, max 60s)...")
    print("=" * 60)

    t0 = time.time()
    interrupted = d.wait_for_autoboot(timeout=60)
    print(f"wait_for_autoboot took {time.time() - t0:.1f}s, result: {interrupted}")

    if not interrupted:
        print("\nCould not interrupt autoboot — stopping.")
        d.disconnect()
        sys.exit(1)

    print()
    print("=" * 60)
    print("Attempting 'run recovery' (bounded, max 120s — this is the")
    print("actual question we're testing)...")
    print("=" * 60)

    t0 = time.time()
    booted = d.boot_recovery()
    elapsed = time.time() - t0
    print(f"boot_recovery() took {elapsed:.1f}s, result: {booted}")

    d.disconnect()

    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    if booted:
        print("✓ Recovery Linux DID boot from eMMC's firmware region.")
        print("  Something usable is already there (not just sparse")
        print("  leftover bytes) — Step 6 of the documented procedure")
        print("  can work without a prior firmware update on this device.")
    else:
        print("✗ Recovery Linux did NOT boot from eMMC within the bound.")
        print(f"  (boot_recovery() returned cleanly after {elapsed:.1f}s —")
        print("  no hang, just a real 'not there' result.)")
        print("  This confirms Step 6 needs a real firmware update first")
        print("  on this device, as suspected.")


if __name__ == "__main__":
    main()
