#!/usr/bin/env python3
"""
mono-imager: SAFE real-hardware probe for recovery_orchestrator.py's
read-only detection functions.

Boots to recovery Linux (NOR, the documented default — DIP switch
should be set to NOR) via the proven phase1_bootstrap(), then runs
ONLY the read-only checks:

    detect_modern_firmware_tool()  — `which firmware`
    get_device_mac()               — `ip addr`

NOTHING DESTRUCTIVE happens here — no `firmware update`, no `curl`,
no `dd`, no `flashcp`. This just confirms detection works correctly
against your actual device before the real flashing driver is built
and tested.

Usage:
    py test_probe_recovery_detect.py --port COM5
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap
from mono_imager import recovery_orchestrator as rec


def main():
    parser = argparse.ArgumentParser(
        description="Safe, read-only probe of recovery detection against real hardware"
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    args = parser.parse_args()

    print("This boots into recovery Linux from NOR (the documented default —")
    print("make sure the DIP switch is set to NOR).")
    print()
    print("Running real phase1_bootstrap() (Steps 1-5)...")
    print("=" * 60)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        print("\nphase1_bootstrap FAILED — cannot proceed with this probe.")
        sys.exit(1)

    print("\n✓ Boot sequence complete, at recovery shell.")
    print()
    print("=" * 60)
    print("CHECK 1: detect_modern_firmware_tool()  ('which firmware')")
    print("=" * 60)

    has_modern = rec.detect_modern_firmware_tool(d)
    if has_modern is True:
        print("RESULT: Modern 'firmware' command IS present on this device.")
        print("        This device would use the MODERN recovery path.")
    elif has_modern is False:
        print("RESULT: Modern 'firmware' command is NOT present.")
        print("        This device would use the LEGACY recovery path.")
    else:
        print("RESULT: Could not determine (check failed/inconclusive).")
        print("        See the warning above for details.")

    print()
    print("=" * 60)
    print("CHECK 2: get_device_mac()  ('ip addr')")
    print("=" * 60)

    # Try a couple of common interface names since which one is "up"
    # depends on which physical port is cabled — same as the docs'
    # own port-to-interface table.
    for iface in ["eth0", "eth1", "eth2"]:
        mac = rec.get_device_mac(d, iface)
        if mac:
            print(f"RESULT: {iface} MAC = {mac}")
            break
    else:
        print("RESULT: No MAC found on eth0/eth1/eth2.")
        print("        Make sure an Ethernet cable is connected and the")
        print("        interface is up (ip link set <iface> up).")

    d.disconnect()

    print()
    print("=" * 60)
    print("Probe complete. Nothing was flashed or modified — this only")
    print("read information from the device.")
    print("=" * 60)


if __name__ == "__main__":
    main()
