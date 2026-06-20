#!/usr/bin/env python3
"""
mono-imager: test whether the REAL boot sequence (Steps 1-8) leaves
the serial connection in a different state than connecting fresh.

Every isolated test so far (echo, single curl, chained commands, and
the exact Step 09 script via launch_script + wait_for_report) has
been 100% reliable across real hardware runs — but ALL of those tests
connect directly via SerialDevice.connect(), skipping the real boot
sequence (U-Boot interrupt, recovery Linux boot, login_recovery's
retry loop). The REAL test_verify_flash_auto.py goes through that
full sequence before reaching Step 09, and Step 09 has failed there
on every attempt.

This script uses the REAL phase1_bootstrap() and phase2_network()
functions from flash_orchestrator.py — not a simplified connect — so
it requires the same manual power-cycle interaction as the real test.
Immediately after, it fires the exact proven-reliable launch_script
pattern. If THIS fails (while the isolated launchtest mode passed
5/5), it directly confirms something about the real boot sequence
itself is the cause, not launch_script, curl, or chaining.

Usage:
    py test_real_boot_then_launch.py --port COM5 [--count 3]
"""

import sys
import argparse
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import (
    phase1_bootstrap, phase2_network, wait_for_report,
    detect_host_ip, pick_device_ip,
)


def main():
    parser = argparse.ArgumentParser(
        description="Test whether the real boot sequence leaves the connection "
                     "in a different state than connecting fresh"
    )
    parser.add_argument("--port", required=True, help="Serial port (e.g. COM5)")
    parser.add_argument("--count", type=int, default=3,
                         help="Number of launch_script repeats AFTER boot (default: 3)")
    args = parser.parse_args()

    print("This requires the SAME manual power-cycle as test_verify_flash_auto.py.")
    print("Running the REAL phase1_bootstrap() (Steps 1-5)...")
    print("=" * 60)

    d = phase1_bootstrap(args.port, 115200)
    if d is None:
        print("\nphase1_bootstrap FAILED — cannot proceed with this test.")
        sys.exit(1)

    print("\n✓ Real boot sequence complete (Steps 1-5 passed).")
    print("Bringing up networking via the REAL phase2_network() (Steps 6-8)...")

    host_ip = detect_host_ip()
    device_ip = pick_device_ip(host_ip)
    print(f"Host IP: {host_ip}  |  Device IP: {device_ip}")

    tmp_dir = Path(tempfile.mkdtemp())
    dummy_firmware = tmp_dir / "boottest_dummy.bin"
    dummy_firmware.write_bytes(b"BOOT_TEST_PAYLOAD" * 100)

    http_port = 8084  # distinct port, avoids collisions with other test scripts
    server = phase2_network(d, host_ip, device_ip, http_port, dummy_firmware)
    if server is None:
        print("phase2_network FAILED — cannot proceed with this test.")
        d.disconnect()
        sys.exit(1)

    print("\n✓ Real networking up (Steps 6-8 passed).")
    print(f"\nNow firing the EXACT proven-reliable launch_script pattern {args.count} "
          f"times, immediately after the real boot sequence...")
    print("=" * 60)

    url = f"http://{host_ip}:{http_port}/firmware.img"
    results = []

    for i in range(1, args.count + 1):
        print(f"\nRun {i}/{args.count}:")
        report_url = f"http://{host_ip}:{http_port}/report?step=09"
        script_body = (
            f"curl -s -o /dev/null -w '%{{http_code}}' {url} "
            f"> /tmp/mono_imager_boottest_{i}.txt; "
            f"curl -s -X POST --data-binary @/tmp/mono_imager_boottest_{i}.txt "
            f"\"{report_url}\" >/dev/null 2>&1"
        )

        _, launch_error = with_spinner(
            d.launch_script, script_body,
            marker=f"boottest_{i}",
            message=f"  Run {i} — launching script"
        )
        if launch_error is not None:
            print(f"  RAISED (launch_script): {launch_error}")
            print(f"  VERDICT: ✗ ERROR")
            results.append(False)
            continue

        result, report_error = with_spinner(
            wait_for_report, "09", timeout=20.0,
            message=f"  Run {i} — waiting for /report"
        )
        if report_error is not None:
            print(f"  RAISED (wait_for_report): {report_error}")
            print(f"  VERDICT: ✗ ERROR")
            results.append(False)
            continue

        if result is not None and "200" in result:
            print(f"  RESULT: {result!r}")
            print(f"  VERDICT: ✓ PASS")
            results.append(True)
        else:
            print(f"  RESULT: {result!r}")
            print(f"  VERDICT: ✗ FAIL (report never arrived or unexpected)")
            results.append(False)

    server.shutdown()
    print("\nHTTP server stopped.")
    try:
        for f in tmp_dir.iterdir():
            f.unlink()
        tmp_dir.rmdir()
    except Exception:
        pass

    d.disconnect()

    passed = sum(results)
    total = len(results)
    pct = (passed / total * 100) if total else 0

    print("\n" + "=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"Passed: {passed}/{total} ({pct:.0f}%)")
    print()
    if passed == total:
        print("100% pass rate AFTER the real boot sequence.")
        print("This means the boot sequence does NOT pollute the connection —")
        print("the original Step 09 failures must have another cause not yet")
        print("isolated by any test so far.")
    elif passed == 0:
        print("0% pass rate AFTER the real boot sequence, despite the EXACT")
        print("same script passing 100% when connecting fresh (launchtest mode).")
        print("This CONFIRMS the real boot sequence (Steps 1-8) leaves the")
        print("connection in a different/polluted state — the bug is in")
        print("phase1_bootstrap() or phase2_network(), not in Step 09's script,")
        print("launch_script(), or curl.")
    else:
        print("Mixed results AFTER the real boot sequence — this suggests the")
        print("boot sequence introduces genuine intermittent state pollution,")
        print("not a deterministic break.")


if __name__ == "__main__":
    main()
