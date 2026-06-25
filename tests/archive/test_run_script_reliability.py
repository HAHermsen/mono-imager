#!/usr/bin/env python3
"""
mono-imager: run_script() reliability baseline.

Runs a chosen script N times in a row against the SAME connection,
tallies pass/fail, and reports a real pass rate.

--mode echo (default): the SIMPLEST possible script ("echo
    hello_from_device"). Confirmed 100% reliable across 5 real runs —
    see session history. Use this mode to re-confirm that baseline,
    or as a sanity check before testing a more complex script.

--mode step09: the ACTUAL reachability-check portion of Step 09's
    real script (curl -s -o /dev/null -w '%{http_code}' <url>,
    redirected to a file then cat'd back — the same pattern proven
    reliable in isolation by a parallel diagnostic script's "Stage 3").
    Brings up device networking first (reusing phase2_network, same
    as flash_orchestrator.py itself). This exists because echo alone
    being 100% reliable does NOT tell us whether Step 09's specific,
    longer, curl-based script is also reliable — single one-off runs
    throughout today's session showed it failing repeatedly, but
    never with a real sample size. This gives one.

Usage:
    py test_run_script_reliability.py --port COM5 [--count 5] [--mode echo|step09]
"""

import sys
import argparse
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.config import detect_serial_ports
from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import detect_host_ip, pick_device_ip, phase2_network, wait_for_report


def main():
    parser = argparse.ArgumentParser(
        description="Baseline reliability test for run_script()"
    )
    parser.add_argument("--port", default=None, help="Serial port (default: auto-detect)")
    parser.add_argument("--count", type=int, default=5, help="Number of repeats (default: 5)")
    parser.add_argument("--mode", choices=["echo", "step09", "multicmd", "launchtest"], default="echo",
                         help="echo = simplest script (default); "
                              "step09 = real reachability-check script via run_script(), network brought up first; "
                              "multicmd = isolates command chaining via run_script(); "
                              "launchtest = the SAME proven-reliable two-command curl script as step09, "
                              "but fired via launch_script() (fire-and-forget) instead of run_script() "
                              "(wait-and-read) — isolates launch_script() itself as the one remaining "
                              "untested variable, since every passing test so far used run_script() and "
                              "every failing real-world run used launch_script()")
    args = parser.parse_args()

    if args.port:
        port = args.port
    else:
        known, other = detect_serial_ports()
        all_ports = known + other
        if not all_ports:
            print("No serial ports detected. Use --port to specify.")
            sys.exit(1)
        port = all_ports[0].device
        print(f"Auto-detected port: {port}")

    print(f"Connecting to {port}...")
    d = SerialDevice(port, timeout=5)
    if not d.connect(115200):
        print("FAILED to connect. Is the device already at the recovery shell prompt?")
        sys.exit(1)
    print("✓ Connected.")

    # Explicitly verify the device is AT the recovery shell prompt right
    # now, rather than assuming connect() succeeding means the right
    # state. A previous baseline run produced a misleading 0% pass rate
    # entirely because the device had booted onward to its normal OS
    # (Armbian login prompt) by the time the test ran — every "failure"
    # was really run_script() correctly refusing to execute on garbage
    # input, not genuine serial flakiness. See verify_recovery_shell()
    # docstring in serial_device.py for the full rationale.
    print("Verifying device is at recovery shell prompt...")
    if not d.verify_recovery_shell(timeout=5.0):
        print()
        print("✗ Device is NOT at the recovery shell prompt (root@recovery:~#).")
        print("  This baseline test requires a confirmed, stable starting state —")
        print("  results would be meaningless otherwise (a previous run hit this")
        print("  exact problem and reported a misleading 0% pass rate).")
        print()
        print("  Power-cycle the device and run test_verify_flash_auto.py up to")
        print("  Step 05 (\"Logged into recovery shell\"), THEN run this script")
        print("  immediately afterward without disconnecting/power-cycling again.")
        d.disconnect()
        sys.exit(1)
    print("✓ Confirmed at recovery shell.\n")

    # Set up the script body and pass criteria for the chosen mode.
    server = None
    tmp_dir = None
    if args.mode == "echo":
        script_body = "echo hello_from_device"
        expected = "hello_from_device"
        def judge(result):
            return result.strip() == expected
        print(f"Running echo (simplest possible script) via run_script() {args.count} times...")

    elif args.mode == "multicmd":
        # No curl, no network — purely isolates whether a semicolon-
        # separated TWO-command script loses everything after the
        # first command. Every previous attempt (complex multi-curl,
        # simplified two-curl, with/without launch_script's post-exec
        # drain) showed the device's FIRST command succeeding and
        # everything after it never running/completing. This test
        # removes curl and the network entirely from the equation —
        # if "second" never comes back here either, the bug is in how
        # the device's shell handles multiple ;-separated commands in
        # a single sh invocation, not in curl or networking at all.
        #
        # "set -x" turns on the shell's own trace mode: BEFORE running
        # each command, the shell prints it (prefixed with "+") to
        # stderr. Since run_script() captures whatever the device
        # sends back, this lets us see what the device's shell ACTUALLY
        # attempted, not just the final output — distinguishing
        # "second command never got reached at all" from "it ran but
        # its own output specifically got lost".
        script_body = "set -x; echo first; echo second"
        def judge(result):
            # Must see BOTH the actual output lines, not just trace
            # lines mentioning the command name. A bare "+ echo second"
            # trace line (command was attempted) is not the same as
            # "second" actually being printed as output — check for
            # the output appearing on its own line, not just as a
            # substring of the trace.
            lines = result.splitlines()
            has_first_output = "first" in lines
            has_second_output = "second" in lines
            return has_first_output and has_second_output
        print(f"Running 'set -x; echo first; echo second' via run_script() {args.count} times...")
        print("(set -x traces every command the device's shell actually attempts)")

    elif args.mode == "step09" or args.mode == "launchtest":
        print("Bringing up device networking (reusing proven phase2_network)...")
        host_ip = detect_host_ip()
        device_ip = pick_device_ip(host_ip)
        print(f"Host IP: {host_ip}  |  Device IP: {device_ip}")

        tmp_dir = Path(tempfile.mkdtemp())
        dummy_firmware = tmp_dir / "reliability_dummy.bin"
        dummy_firmware.write_bytes(b"RELIABILITY_TEST_PAYLOAD" * 100)

        http_port = 8083 if args.mode == "launchtest" else 8082  # distinct ports, avoid collisions
        server = phase2_network(d, host_ip, device_ip, http_port, dummy_firmware)
        if server is None:
            print(f"Could not bring up device networking — cannot run {args.mode} mode.")
            d.disconnect()
            sys.exit(1)

        url = f"http://{host_ip}:{http_port}/firmware.img"

        if args.mode == "step09":
            # Same pattern as the real Step 09 reachability check in
            # flash_orchestrator.py: redirect to a file, cat it back —
            # via run_script() (waits for and reads the result).
            script_body = (
                f"curl -s -o /dev/null -w '%{{http_code}}' {url} "
                f"> /tmp/mono_imager_reliability_code.txt; "
                f"cat /tmp/mono_imager_reliability_code.txt"
            )
            def judge(result):
                return "200" in result
            print(f"\nRunning REAL Step 09-style curl script via run_script() {args.count} times...")
        else:
            # launchtest: the EXACT current flash_orchestrator.py Step
            # 09 script — one curl writes its code to a file, a second
            # curl POSTs that file's content to /report?step=<n> — but
            # fired via launch_script() (fire-and-forget), with the
            # result picked up via wait_for_report(), exactly as the
            # real Step 09 does it. Every test so far that used
            # run_script() passed; every real-world run that used
            # launch_script() failed. This is the direct, final test
            # of launch_script() itself as the variable.
            print(f"\nRunning the SAME script as step09 mode, but via launch_script() {args.count} times...")
            print("(this isolates launch_script() itself vs run_script())")

    print("=" * 60)

    results = []
    for i in range(1, args.count + 1):
        print(f"\nRun {i}/{args.count}:")

        if args.mode == "launchtest":
            report_url = f"http://{host_ip}:{http_port}/report?step=09"
            launch_script_body = (
                f"curl -s -o /dev/null -w '%{{http_code}}' {url} "
                f"> /tmp/mono_imager_launchtest_{i}.txt; "
                f"curl -s -X POST --data-binary @/tmp/mono_imager_launchtest_{i}.txt "
                f"\"{report_url}\" >/dev/null 2>&1"
            )
            _, launch_error = with_spinner(
                d.launch_script, launch_script_body,
                marker=f"launchtest_{i}",
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
            continue

        result, error = with_spinner(
            d.run_script, script_body,
            marker=f"reliability_{i}", exec_timeout=20,
            message=f"  Run {i} — waiting for device response"
        )

        if error is not None:
            print(f"  RAISED: {error}")
            print(f"  VERDICT: ✗ ERROR")
            results.append(False)
        elif judge(result):
            print(f"  RESULT: {result!r}")
            print(f"  VERDICT: ✓ PASS")
            results.append(True)
        else:
            print(f"  RESULT: {result!r}")
            print(f"  VERDICT: ✗ FAIL (unexpected output)")
            results.append(False)

    if server is not None:
        server.shutdown()
        print("\nHTTP server stopped.")
    if tmp_dir is not None:
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
    print("BASELINE RESULT")
    print("=" * 60)
    print(f"Mode: {args.mode}")
    print(f"Passed: {passed}/{total} ({pct:.0f}%)")
    print()
    if passed == total:
        print(f"100% pass rate in '{args.mode}' mode.")
        if args.mode == "echo":
            print("This means failures seen elsewhere are likely correlated")
            print("with script complexity/length, NOT pure random flakiness")
            print("on every serial exchange.")
        else:
            print("The real Step 09-style curl script is ALSO reliable when")
            print("isolated this way — earlier failures may be specific to")
            print("the multi-command report-back version, not curl itself.")
    elif passed == 0:
        print(f"0% pass rate in '{args.mode}' mode — fails consistently right now.")
        if args.mode == "echo":
            print("This points to a connection/environment issue in THIS")
            print("session, not a complexity-dependent bug.")
        else:
            print("This confirms Step 09's script genuinely has a reliability")
            print("problem, not an artifact of one-off bad luck.")
    else:
        print(f"Mixed results in '{args.mode}' mode — {pct:.0f}% pass rate.")
        print("This is genuine intermittent flakiness specific to this")
        print("script, not pure 0%/100% determinism.")

    print("\nDone. Device left connected to recovery shell; safe to re-run other tests.")


if __name__ == "__main__":
    main()
