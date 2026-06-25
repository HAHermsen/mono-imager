#!/usr/bin/env python3
"""
mono-imager: Hardware test — recovery sequence dry run (non-destructive).

Requires: Mono Gateway connected, Ethernet connected.

What this tests (nothing is flashed):
  - Full modern recovery sequence flow and timing
  - verify_boot_source() detects eMMC and NOR markers
  - Re-entry into recovery shell from eMMC works

Steps 3 and 7 (the actual `firmware update` commands) are skipped.

AUTONOMOUS vs MANUAL:
  Software reboots are handled automatically — no manual power cycling.
  The two DIP switch flips ARE still manual (they require physical
  access to the PCB) but the script waits for serial confirmation
  rather than asking you to "press Enter when done":

    1. Script boots to NOR recovery (autonomous soft reboot)
    2. Script detects modern firmware tool
    3. [SKIPPED] Would run: firmware update (eMMC)
    4. Prompt: "Flip DIP to eMMC and press Enter"  ← only manual step
    5. Script sends 'reboot', watches for eMMC boot marker
    6. Script re-enters recovery from eMMC (autonomous)
    7. [SKIPPED] Would run: firmware update (NOR)
    8. Prompt: "Flip DIP back to NOR and press Enter"  ← only manual step
    9. Script sends 'reboot', watches for NOR boot marker

Usage: python tests/hardware/test_recovery_dryrun.py --port COM5

Logs to: logs/test_recovery_dryrun_<timestamp>.log
"""

import sys
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.flash_orchestrator import phase1_bootstrap
from mono_imager.serial_device import SerialDevice
from mono_imager import recovery_orchestrator as rec

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file  = LOG_DIR / f"test_recovery_dryrun_{timestamp}.log"

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


def soft_reboot_and_watch(d: SerialDevice, expected_source: str, timeout: int = 90) -> bool:
    """
    Send software reboot and watch for the expected boot source marker.
    Sends 'reset' (U-Boot) and 'reboot' (Linux shell) — one applies,
    the other is a no-op. The FTDI bridge stays enumerated, so we
    watch the same connection for the RCW BOOT SRC line.
    """
    logger.info(f"Soft reboot → watching for {expected_source} boot marker...")
    try:
        d.ser.reset_input_buffer()
        d.ser.write(b"\r\n")
        time.sleep(0.3)
        d.ser.write(b"reset\r\n")
        time.sleep(0.2)
        d.ser.write(b"reboot\r\n")
    except Exception as e:
        logger.warning(f"Soft reboot send warning: {e}")

    return rec.verify_boot_source(d, expected_source, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(
        description="Dry-run recovery sequence — no flashing, two manual DIP flips"
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    args = parser.parse_args()

    rec.reset_results()

    logger.info(f"Log: {log_file}")
    print("=" * 60)
    print("DRY RUN — nothing will be flashed.")
    print("Two DIP switch flips required (all reboots are autonomous).")
    print("=" * 60)
    print()

    # Steps 1-5: autonomous — soft reboot → catch U-Boot → recovery boot
    print("Triggering soft reboot to reach NOR recovery...")
    d_init = SerialDevice(args.port, timeout=5)
    if not d_init.connect(115200):
        logger.error("Failed to connect")
        sys.exit(1)
    # Send reboot commands to get back to a clean state
    d_init.ser.write(b"\r\nreset\r\nreboot\r\n")
    time.sleep(0.5)
    d_init.disconnect()

    print("Running phase1_bootstrap() (Steps 1-5)...")
    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        logger.error("Bootstrap failed")
        sys.exit(1)
    logger.info("✓ At NOR recovery shell")

    # Step 2: detect firmware tool
    print()
    print("=" * 60)
    print("detect_modern_firmware_tool()  [REAL]")
    print("=" * 60)
    has_modern = rec.detect_modern_firmware_tool(d)
    logger.info(f"Result: {has_modern}")
    if has_modern is not True:
        logger.warning("Device does not have modern firmware tool — dry run is for modern path only")
        d.disconnect()
        sys.exit(0)

    # Step 3: SKIPPED
    print()
    print("=" * 60)
    print("[SKIPPED] Would run: firmware update  (flashes eMMC — not executed)")
    print("=" * 60)

    # ── Only manual action: flip DIP to eMMC ─────────────────────────
    print()
    print("=" * 60)
    print("  ⚡  MANUAL ACTION REQUIRED  ⚡")
    print("  Flip DIP switch to eMMC (LEFT), then press Enter.")
    print("  The script will trigger the reboot automatically.")
    print("=" * 60)
    input("  Press Enter when DIP is LEFT... ")

    # Step 5: autonomous reboot + verify eMMC boot
    print()
    print("=" * 60)
    print("Rebooting and verifying eMMC boot source  [AUTONOMOUS]")
    print("=" * 60)
    emmc_confirmed = soft_reboot_and_watch(d, "EMMC", timeout=90)
    logger.info(f"eMMC boot confirmed: {emmc_confirmed}")
    if not emmc_confirmed:
        logger.error("eMMC boot marker not seen — check DIP switch position")
        d.disconnect()
        sys.exit(1)

    # Step 6: autonomous — interrupt U-Boot, boot recovery from eMMC
    print()
    print("=" * 60)
    print("Interrupting U-Boot and entering recovery from eMMC  [AUTONOMOUS]")
    print("=" * 60)
    interrupted = d.wait_for_autoboot(timeout=60)
    if not interrupted:
        logger.error("Could not interrupt autoboot")
        d.disconnect()
        sys.exit(1)

    booted    = d.boot_recovery()
    logged_in = d.login_recovery(timeout=60) if booted else False
    logger.info(f"Recovery boot: {booted}, login: {logged_in}")
    if not logged_in:
        logger.error("Could not enter recovery shell on eMMC")
        d.disconnect()
        sys.exit(1)

    # Step 7: SKIPPED
    print()
    print("=" * 60)
    print("[SKIPPED] Would run: firmware update  (flashes NOR — not executed)")
    print("=" * 60)

    # ── Only manual action: flip DIP back to NOR ─────────────────────
    print()
    print("=" * 60)
    print("  ⚡  MANUAL ACTION REQUIRED  ⚡")
    print("  Flip DIP switch back to NOR (RIGHT), then press Enter.")
    print("  The script will trigger the reboot automatically.")
    print("=" * 60)
    input("  Press Enter when DIP is RIGHT... ")

    # Step 9: autonomous reboot + verify NOR boot
    print()
    print("=" * 60)
    print("Rebooting and verifying NOR boot source  [AUTONOMOUS]")
    print("=" * 60)
    nor_confirmed = soft_reboot_and_watch(d, "NOR", timeout=90)
    logger.info(f"NOR boot confirmed: {nor_confirmed}")
    d.disconnect()

    # Summary
    print()
    print("=" * 60)
    print("DRY RUN COMPLETE")
    print("=" * 60)
    print(f"detect_modern_firmware_tool: {has_modern}")
    print(f"eMMC boot verified:          {emmc_confirmed}")
    print(f"Re-entered recovery on eMMC: {logged_in}")
    print(f"NOR boot verified:           {nor_confirmed}")
    print()

    all_ok = all([has_modern, emmc_confirmed, logged_in, nor_confirmed])
    if all_ok:
        logger.info("✓ Full sequence flow confirmed. Nothing was flashed.")
    else:
        logger.error("✗ Something did not behave as expected — review output above")

    logger.info(f"Log: {log_file}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
