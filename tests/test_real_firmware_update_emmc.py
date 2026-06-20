#!/usr/bin/env python3
"""
mono-imager: REAL, DESTRUCTIVE firmware update — eMMC.

Runs the actual documented Steps 1-4 from
https://docs.mono.si/gateway-development-kit/flashing-firmware:

    Step 2: Boot recovery from NOR (DIP switch must be on NOR)
    Step 3: Set up networking (eth2, static IP)
    Step 4: Run `firmware update` for real — downloads, verifies,
            and FLASHES eMMC's firmware region.

This DOES write to the device. Per the docs, this only touches the
reserved first-32MB firmware region (bootloader/U-Boot/recovery
Linux) — the confirmed OS partition (mmcblk0p1, starting at the 32MB
boundary) should not be touched.

Output is streamed LIVE (raw serial reads, not buffered via
run_script()) since this is a real multi-minute operation and you
should see progress, not stare at a blank screen.

Network config (adjust below if your setup changes):
    Interface: eth2
    Device IP: 192.168.168.122/24
    Gateway:   192.168.168.1

Usage:
    py run_real_firmware_update_emmc.py --port COM5
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap

IFACE = "eth2"
DEVICE_IP = "192.168.168.122"
PREFIX = "24"
GATEWAY = "192.168.168.1"


def stream_command(d, command: str, idle_timeout: float = 30.0, max_total: float = 900.0,
                    auto_confirm_text: str = None, auto_confirm_response: str = "yes") -> str:
    """
    Send a command and stream raw output live to the console as it
    arrives, rather than buffering silently (run_script()'s normal
    behavior) — appropriate here since `firmware update` is
    documented as showing on-screen prompts and can run for several
    real minutes.

    If auto_confirm_text is given and that exact text appears in the
    live output, automatically sends auto_confirm_response once (not
    repeatedly — guarded so a string that happens to reappear later
    in normal output, e.g. echoed back, doesn't trigger a second
    send). This handles the REAL firmware tool's own internal
    confirmation prompt — confirmed via live testing that without
    this, the tool sits waiting for input that never arrives and
    eventually aborts on its own.

    Returns when either:
      - idle_timeout seconds pass with no new bytes (command likely
        finished and we're back at a prompt), or
      - max_total seconds pass overall (hard ceiling)
    """
    d.ser.reset_input_buffer()
    d.ser.write((command + "\r\n").encode())

    buffer = b""
    last_byte_time = time.time()
    overall_start = time.time()
    already_confirmed = False

    while True:
        now = time.time()
        if now - overall_start > max_total:
            print(f"\n[stream_command: hit hard ceiling of {max_total}s]")
            break
        if now - last_byte_time > idle_timeout:
            print(f"\n[stream_command: {idle_timeout}s with no new output — assuming done]")
            break

        chunk = d.ser.read(256)
        if chunk:
            text = chunk.decode("utf-8", errors="replace")
            print(text, end="", flush=True)
            buffer += chunk
            last_byte_time = now

            if (auto_confirm_text and not already_confirmed
                    and auto_confirm_text.encode() in buffer):
                print(f"\n[stream_command: detected '{auto_confirm_text}' — "
                      f"sending '{auto_confirm_response}']")
                d.ser.write((auto_confirm_response + "\r\n").encode())
                already_confirmed = True

    return buffer.decode("utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(
        description="REAL firmware update — flashes eMMC. Boot from NOR first."
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    args = parser.parse_args()

    print("=" * 60)
    print("⚠️  THIS WILL ACTUALLY FLASH eMMC'S FIRMWARE REGION  ⚠️")
    print("=" * 60)
    print(f"Network: {IFACE} = {DEVICE_IP}/{PREFIX}, gateway {GATEWAY}")
    print()
    print("Make sure:")
    print("  - DIP switch is set to NOR")
    print("  - Ethernet cable is connected to the port for eth2")
    print("  - That network has real internet access")
    print()
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    print()
    print("Running real phase1_bootstrap() (boot to NOR recovery)...")
    print("=" * 60)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        print("\nphase1_bootstrap FAILED — cannot proceed.")
        sys.exit(1)

    print("\n✓ Boot sequence complete, at recovery shell (NOR).")

    print()
    print("=" * 60)
    print(f"Setting up networking on {IFACE}...")
    print("=" * 60)

    net_cmd = (
        f"ip link set {IFACE} up && "
        f"ip addr add {DEVICE_IP}/{PREFIX} dev {IFACE} && "
        f"ip route add default via {GATEWAY} dev {IFACE}; "
        f"echo RC=$?"
    )
    try:
        net_output = d.run_script(net_cmd, marker="net_setup", exec_timeout=20)
        print(net_output)
    except RuntimeError as e:
        print(f"Network setup failed: {e}")
        d.disconnect()
        sys.exit(1)

    if "RC=0" not in net_output:
        print("\nNetwork setup did not report success — check cable/interface.")
        d.disconnect()
        sys.exit(1)

    print("✓ Network configured.")

    print()
    print("=" * 60)
    print("Running 'firmware update' FOR REAL — streaming live output")
    print("below. This can take several minutes (download + verify +")
    print("flash). Do not disconnect power.")
    print("=" * 60)
    print()

    t0 = time.time()
    output = stream_command(
        d, "firmware update", idle_timeout=30.0, max_total=900.0,
        auto_confirm_text="Type 'yes' to proceed", auto_confirm_response="yes"
    )
    elapsed = time.time() - t0

    print()
    print("=" * 60)
    print(f"Command finished/stopped streaming after {elapsed:.1f}s")
    print("=" * 60)

    # Confirm completion with an explicit exit-code check now that the
    # interactive part is done — short command, safe via run_script.
    try:
        rc_check = d.run_script("echo RC=$?", marker="rc_check", exec_timeout=10)
        print(f"Exit code check: {rc_check.strip()}")
    except RuntimeError as e:
        print(f"(could not verify exit code: {e})")

    d.disconnect()

    print()
    print("=" * 60)
    print("DONE. Review the streamed output above to confirm the")
    print("update actually reported success (signature verified,")
    print("flash complete) — don't rely solely on the exit code.")
    print("=" * 60)
    print()
    print("Next: per the docs, flip the DIP switch to eMMC and reboot,")
    print("then confirm with the boot-source verification tooling.")


if __name__ == "__main__":
    main()
