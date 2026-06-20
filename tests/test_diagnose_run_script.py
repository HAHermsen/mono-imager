#!/usr/bin/env python3
"""
mono-imager: run_script() root-cause diagnostic — staged isolation.

Runs a SEQUENCE of increasingly specific tests via run_script(), each
building on the previous one, to pin down exactly where curl's output
gets lost when called from inside a device-side script:

  Stage 1: echo (no curl, no network)       — proven PASS previously
  Stage 2: curl --version (no network call) — isolates: does curl's
           OWN startup output return at all, with zero network I/O?
  Stage 3: curl against a real HTTP URL,
           output redirected to a file, then
           cat'ing that file back              — isolates: does curl's
           STDOUT get lost specifically when curl performs a real
           network request, even if we sidestep "did curl's own
           stdout reach our serial read" by writing to a file instead
           and reading the file separately (same trick run_script
           itself uses for verifying the heredoc write).
  Stage 4: the ORIGINAL failing command exactly as flash_orchestrator
           runs it (curl -s -o /dev/null -w '%{http_code}' <url>) —
           the actual regression, run last so stages 1-3 narrow the
           search before we look at the real failure again.
  Stage 5: disable shell line-editing (set +o emacs; set +o vi), THEN
           retry the original failing command in the same script —
           tests whether the interactive line editor observed during
           every heredoc write (visible via \x1b[A / \x1b[K ANSI
           escapes in the raw write logs) is also responsible for
           swallowing curl's output during the exec step.

Stages 1-4 only run if the previous one passed, since a failure
narrows the cause and further stages would not add new information.
Stage 5 always runs after Stage 4 regardless of Stage 4's outcome,
since Stage 4 reproducing the bug is exactly the state Stage 5 needs
to test its fix against.

This exists because Step 09 in test_verify_flash_auto.py showed curl
genuinely succeeding (confirmed via the host's own HTTP server log)
but ZERO bytes ever returning afterward from "sh <script>" — not even
a prompt — while a parallel test proved plain `echo` returns its
output correctly via the exact same run_script() mechanism. That
narrows the cause specifically to curl's behavior, not sh/run_script
itself; this script narrows further.

Usage:
    py test_diagnose_run_script.py --port COM5
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mono_imager.config import detect_serial_ports
from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import (
    detect_host_ip, pick_device_ip, phase2_network, start_http_server,
)


def _run_stage(d: SerialDevice, stage_num: int, title: str, script_body: str,
                marker: str, exec_timeout: float, expect_substring: str = None):
    """
    Run one diagnostic stage via run_script() with spinner feedback,
    print a clear PASS/FAIL/ERROR verdict, and return whether the
    stage passed (so main() can decide whether to continue to the
    next stage — a failure narrows the cause and further stages
    wouldn't add new information beyond it).

    Args:
        expect_substring: if given, stage passes only if this string
            appears in the result. If None, stage passes as long as
            the result is non-empty (used for stages where we don't
            know the exact expected text, only that SOMETHING should
            come back).
    """
    print(f"\n{'=' * 60}")
    print(f"STAGE {stage_num}: {title}")
    print("=" * 60)

    result, error = with_spinner(
        d.run_script, script_body,
        marker=marker, exec_timeout=exec_timeout,
        message=f"Stage {stage_num} — waiting for device response"
    )

    if error is not None:
        print(f"\nRAISED: {error}")
        print(f"VERDICT: ✗ ERROR — could not even verify the script write/exec.")
        return False

    print(f"\nRESULT: {result!r}")

    if expect_substring is not None:
        passed = expect_substring in result
    else:
        passed = result.strip() != ""

    if passed:
        print(f"VERDICT: ✓ PASS")
    else:
        print(f"VERDICT: ✗ FAIL — expected {'non-empty output' if expect_substring is None else repr(expect_substring) + ' in output'}, got {result!r}")

    return passed


def main():
    parser = argparse.ArgumentParser(
        description="Staged root-cause diagnostic for run_script() + curl"
    )
    parser.add_argument("--port", default=None, help="Serial port (default: auto-detect)")
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
        print("(This script assumes the device is ALREADY logged into recovery —")
        print(" it does not re-run the U-Boot/recovery boot sequence.)")
        sys.exit(1)
    print("✓ Connected.")

    # --- Stage 1: echo, no curl, no network ---
    # Confirms sh exec + run_script() mechanism itself works.
    if not _run_stage(
        d, 1, "echo (no curl, no network)",
        "echo hello_from_device",
        marker="diag_s1_echo", exec_timeout=15,
        expect_substring="hello_from_device",
    ):
        print("\nStopping — sh exec itself is broken, no point testing curl on top of it.")
        d.disconnect()
        sys.exit(1)

    # --- Stage 2: curl --version, no network call ---
    # Isolates: does curl's OWN startup output return, with zero
    # actual network I/O involved? If this fails but Stage 1 passed,
    # the problem is specific to invoking curl at all, not networking.
    if not _run_stage(
        d, 2, "curl --version (curl invoked, but no network call)",
        "curl --version",
        marker="diag_s2_curlversion", exec_timeout=15,
        expect_substring="curl",
    ):
        print("\nStopping — curl's own output doesn't return even with zero networking.")
        print("Root cause is in how curl's stdout interacts with this shell/serial")
        print("session, unrelated to actual network requests.")
        d.disconnect()
        sys.exit(1)

    # --- Stage 3: real curl request, output to FILE, then cat the file ---
    # Sidesteps "did curl's stdout reach our serial read" by writing
    # curl's output to a file (same trick run_script() uses to verify
    # the heredoc write) and reading the file back separately. If this
    # passes but Stage 4 (curl's stdout read DIRECTLY) fails, the
    # cause is specifically curl's stdout buffering when not writing
    # to a real interactive terminal.
    print("\nBringing up device networking for stages 3-4 (reusing proven phase2_network)...")
    host_ip = detect_host_ip()
    device_ip = pick_device_ip(host_ip)
    print(f"Host IP: {host_ip}  |  Device IP: {device_ip}")

    tmp_dir = Path(__file__).resolve().parent / "_diag_tmp"
    tmp_dir.mkdir(exist_ok=True)
    dummy_firmware = tmp_dir / "diag_dummy.bin"
    dummy_firmware.write_bytes(b"DIAG_TEST_PAYLOAD" * 100)

    http_port = 8081  # different from flash_orchestrator's default 8080,
                       # avoids colliding if another test left a server up
    server = phase2_network(d, host_ip, device_ip, http_port, dummy_firmware)
    if server is None:
        print("\nCould not bring up device networking — cannot test real curl requests.")
        print("(Stages 1-2 results above are still valid and conclusive on their own.)")
        d.disconnect()
        sys.exit(1)

    try:
        url = f"http://{host_ip}:{http_port}/firmware.img"

        stage3_passed = _run_stage(
            d, 3, "curl real request -> FILE -> cat (sidesteps direct stdout capture)",
            f"curl -s -o /tmp/diag_curl_out.txt -w '%{{http_code}}' {url} > /tmp/diag_curl_code.txt; "
            f"echo '---'; cat /tmp/diag_curl_code.txt; echo '---'; "
            f"wc -c < /tmp/diag_curl_out.txt",
            marker="diag_s3_curlfile", exec_timeout=20,
            expect_substring="200",
        )

        if not stage3_passed:
            print("\nStopping — even file-redirected curl output doesn't come back.")
            print("Root cause is likely in curl's behavior during the actual network")
            print("request/response cycle itself, not just stdout capture method.")
        else:
            print("\nStage 3 passed — curl's result IS retrievable via file redirection.")
            print("Proceeding to Stage 4: the ORIGINAL failing command, for direct comparison.")

            # --- Stage 4: the exact original failing command ---
            # Run last, now that stages 1-3 have narrowed the search.
            stage4_passed = _run_stage(
                d, 4, "ORIGINAL command exactly as flash_orchestrator runs it",
                f"curl -s -o /dev/null -w '%{{http_code}}' {url}",
                marker="diag_s4_original", exec_timeout=20,
                expect_substring="200",
            )

            print("\n" + "=" * 60)
            print("CONCLUSION (Stage 3 vs Stage 4)")
            print("=" * 60)
            print("If Stage 3 PASSED and Stage 4 FAILED:")
            print("  -> Root cause is curl's stdout buffering/handling when its")
            print("     output goes directly to the terminal (-o /dev/null -w ...)")
            print("     vs. when redirected to a file. Fix: always redirect curl's")
            print("     real output to a file inside the script, then cat/wc the")
            print("     file separately, mirroring Stage 3's pattern.")
            print("If Stage 3 and Stage 4 BOTH FAILED:")
            print("  -> Root cause is deeper in curl's network-request behavior")
            print("     itself, not just stdout capture method.")

            # --- Stage 5: disable shell line-editing, then retry the
            # ORIGINAL failing command in the SAME script ---
            #
            # Every run_script() write so far has shown the recovery
            # shell actively redrawing input with ANSI escape sequences
            # (cursor-up \x1b[A, clear-line \x1b[K) — evidence of an
            # interactive line editor (likely BusyBox ash/hush in
            # emacs- or vi-style editing mode). This is a real,
            # reproducible observation, not a guess — but whether it's
            # ALSO what swallows curl's output during the exec step is
            # still unconfirmed.
            #
            # This stage tests that directly: disable line editing
            # FIRST (set +o emacs; set +o vi — harmless no-ops if the
            # shell doesn't support one of them), THEN run the exact
            # original failing command in the same script. If curl's
            # output comes back now, the line-editor theory is
            # confirmed by direct evidence. If it still fails, the
            # theory is cleanly eliminated and the cause lies elsewhere
            # (e.g. curl's own stdout buffering, per Stage 3 vs 4).
            print("\nProceeding to Stage 5: disable line editing, then retry original command.")
            _run_stage(
                d, 5, "set +o emacs; set +o vi — THEN retry original command",
                f"set +o emacs 2>/dev/null; set +o vi 2>/dev/null; "
                f"curl -s -o /dev/null -w '%{{http_code}}' {url}",
                marker="diag_s5_noediting", exec_timeout=20,
                expect_substring="200",
            )

            print("\n" + "=" * 60)
            print("CONCLUSION (Stage 5)")
            print("=" * 60)
            if stage4_passed:
                print("Stage 4 already passed on its own this run — Stage 5 result")
                print("is informative but not a clean before/after comparison.")
                print("(See earlier runs in this session: Stage 4 has been flaky —")
                print(" passed once, failed on the next two clean reproductions —")
                print(" so a single PASS here is not yet conclusive on its own.)")
            else:
                print("If Stage 5 PASSED (Stage 4 above FAILED):")
                print("  -> Line-editor theory CONFIRMED. The shell's interactive")
                print("     editing mode was interfering with curl's output reaching")
                print("     our serial read. Fix: send 'set +o emacs; set +o vi' once")
                print("     after entering recovery shell, before any run_script() use.")
                print("If Stage 5 ALSO FAILED:")
                print("  -> Line-editor theory ELIMINATED. The redraw behavior seen")
                print("     during heredoc writes is unrelated to the exec-step bug.")
                print("     Look elsewhere — likely curl's own stdout buffering when")
                print("     not writing to a real interactive terminal.")

    finally:
        server.shutdown()
        print("\nHTTP server stopped.")
        try:
            dummy_firmware.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            pass

    d.disconnect()
    print("\nDone. Device left connected to recovery shell; safe to re-run other tests.")


if __name__ == "__main__":
    main()
