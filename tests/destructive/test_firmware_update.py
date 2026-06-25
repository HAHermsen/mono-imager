#!/usr/bin/env python3
"""
mono-imager: Destructive test — real firmware update to eMMC.

⚠️  THIS WRITES TO THE DEVICE. ⚠️

Runs the documented Steps 1-4 from
https://docs.mono.si/gateway-development-kit/flashing-firmware:

  Step 2: Boot recovery from NOR
  Step 3: Set up networking
  Step 4: Run `firmware update` — downloads, verifies, and FLASHES eMMC

Only the first 32MB firmware region (bootloader/U-Boot/recovery Linux)
is written. The OS partition (mmcblk0p1, starting at 32MB) is not touched.

Output is streamed live — this is a multi-minute operation.

Network config (adjust if your setup differs):
  Interface: eth2
  Device IP: 192.168.168.122/24
  Gateway:   192.168.168.1

Usage: python tests/destructive/test_firmware_update.py --port COM5

Logs to: logs/test_firmware_update_<timestamp>.log
"""

import sys
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file  = LOG_DIR / f"test_firmware_update_{timestamp}.log"

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

IFACE     = "eth2"
DEVICE_IP = "192.168.168.122"
PREFIX    = "24"
GATEWAY   = "192.168.168.1"


def stream_command(d, command: str, idle_timeout: float = 30.0, max_total: float = 900.0,
                   auto_confirm_text: str = None, auto_confirm_response: str = "yes") -> str:
    """
    Send command and stream raw output live. Auto-confirms prompts if needed.
    Returns when idle_timeout seconds pass with no new output, or max_total exceeded.
    """
    d.ser.reset_input_buffer()
    d.ser.write((command + "\r\n").encode())

    buffer           = b""
    last_byte_time   = time.time()
    overall_start    = time.time()
    already_confirmed = False

    while True:
        now = time.time()
        if now - overall_start > max_total:
            print(f"\n[stream_command: hit ceiling of {max_total}s]")
            break
        if now - last_byte_time > idle_timeout:
            print(f"\n[stream_command: {idle_timeout}s idle — assuming done]")
            break

        chunk = d.ser.read(256)
        if chunk:
            text = chunk.decode("utf-8", errors="replace")
            print(text, end="", flush=True)
            buffer += chunk
            last_byte_time = now

            if (auto_confirm_text and not already_confirmed
                    and auto_confirm_text.encode() in buffer):
                print(f"\n[auto-confirming: '{auto_confirm_response}']")
                d.ser.write((auto_confirm_response + "\r\n").encode())
                already_confirmed = True

    return buffer.decode("utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(
        description="REAL firmware update — flashes eMMC firmware region"
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    args = parser.parse_args()

    logger.info(f"Log: {log_file}")

    print("=" * 60)
    print("⚠️  THIS WILL FLASH eMMC FIRMWARE REGION  ⚠️")
    print("=" * 60)
    print(f"Network: {IFACE} = {DEVICE_IP}/{PREFIX}, gateway {GATEWAY}")
    print()
    print("Requirements:")
    print("  - DIP switch on NOR")
    print("  - Ethernet on the eth2 port with internet access")
    print()
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    print()
    print("Running phase1_bootstrap() (boot to NOR recovery)...")
    print("=" * 60)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        logger.error("Bootstrap failed")
        sys.exit(1)
    logger.info("✓ At NOR recovery shell")

    # Network setup
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
        logger.error(f"Network setup failed: {e}")
        d.disconnect()
        sys.exit(1)

    if "RC=0" not in net_output:
        logger.error("Network setup did not report RC=0 — check cable/interface")
        d.disconnect()
        sys.exit(1)
    logger.info("✓ Network configured")

    # Firmware update
    print()
    print("=" * 60)
    print("Running 'firmware update' — streaming live output")
    print("This can take several minutes. Do not disconnect power.")
    print("=" * 60)
    print()

    t0     = time.time()
    output = stream_command(
        d, "firmware update",
        idle_timeout=30.0, max_total=900.0,
        auto_confirm_text="Type 'yes' to proceed", auto_confirm_response="yes"
    )
    elapsed = time.time() - t0

    print()
    print("=" * 60)
    print(f"Command finished after {elapsed:.1f}s")
    print("=" * 60)

    try:
        rc_check = d.run_script("echo RC=$?", marker="rc_check", exec_timeout=10)
        logger.info(f"Exit code: {rc_check.strip()}")
    except RuntimeError as e:
        logger.warning(f"Could not verify exit code: {e}")

    d.disconnect()

    print()
    print("=" * 60)
    print("Review the streamed output above to confirm success.")
    print("(signature verified + flash complete = OK)")
    print()
    print("Next: flip DIP to eMMC and reboot to verify eMMC boot.")
    print("=" * 60)
    logger.info(f"Log saved to: {log_file}")


if __name__ == "__main__":
    main()
