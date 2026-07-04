#!/usr/bin/env python3
"""
mono-imager: Core flash verification logic.

This module contains ONLY the device-talking logic — bootstrap,
network prep, flash, post-flash — with zero argument parsing and
zero assumptions about where config values come from.

It is not run directly. Two wrappers consume it:

    test_verify_flash_auto.py    — zero config, everything auto-detected
    test_verify_flash_manual.py  — full manual control, every value prompted

This split exists so the actual hardware-talking logic lives in
exactly one place. Bug fixes here apply to both wrappers automatically.
The phase functions below are unchanged from the originally verified
test_flash_verify.py (5/5 bootstrap, network phase confirmed working
once device-ip is on the correct subnet).

Author:  H.A. Hermsen
Version: v1.1.0
License: GPLv3
"""

__author__  = "H.A. Hermsen"

import itertools
import logging
import shutil
import socket
import threading
import time

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from typing import Optional, Callable

from mono_imager.config import detect_serial_ports
from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner

# Set MONO_DEBUG=1 (or `mono-imager --debug`/`--verbose`) to restore full
# verbose output to console. debug_enabled() reads MONO_DEBUG live rather
# than freezing it at import time — see logging_setup.debug_enabled().


# --- Logging setup -----------------------------------------------------------
# Logging is initialised exactly once by tui.py/cli.py via
# mono_imager.logging_setup.configure_logging(). This module does NOT call
# basicConfig — doing so at import time caused mutual destruction when tui.py
# also called it, silently dropping handlers depending on import order.

from mono_imager.logging_setup import get_log_file, make_verbose, debug_enabled as _debug_enabled  # noqa: F401

logger = logging.getLogger(__name__)

verbose = make_verbose(logger)


file_logger    = logger
console_logger = logging.getLogger("mono_imager.console")



# --- Result tracker ----------------------------------------------------------

results: list[tuple[int, str, bool, str]] = []  # (step, description, passed, reason)
_step_seq = itertools.count(1)

def reset_results():
    """
    Clear accumulated step results before starting a new flash attempt.

    BUG THIS FIXES: results is module-level state, not per-attempt. The
    one-shot test scripts (test_verify_flash_auto.py etc.) never needed
    this — each is a fresh process, so results always started empty.
    But tui.py is a long-running, looping application: without this
    reset, a single failed/cancelled step anywhere earlier in the same
    session (a previous attempt, a validation failure, anything that
    called step()) stays in the list forever. A LATER, fully successful
    flash would then still report failure, because print_report()
    checks ALL accumulated results, including stale ones from earlier
    in the process — not just the current attempt's. Confirmed
    reproducible: a failed step() call followed by 12 successful ones
    still yields print_report() -> False.

    Call this at the start of every flash attempt — phase1_bootstrap()
    calls it automatically since that's the true entry point shared by
    every caller (auto, manual, and any future one).
    """
    global _step_seq
    results.clear()
    _step_seq = itertools.count(1)

def step(num: int, description: str, passed: bool, reason: str = ""):
    if num == 0:
        num = next(_step_seq)
    mark = "✓" if passed else "✗"

    # File gets the full technical detail: step number, PASS/FAIL, reason.
    file_msg = f"Step {num:02d}: {'✓ PASS' if passed else '✗ FAIL'} — {description}"
    if reason:
        file_msg += f" ({reason})"
    log = file_logger.info if passed else file_logger.error
    log(file_msg)

    # Console gets a short, plain line — no step numbers, no technical
    # reason strings (those are jargon like "wc -c returned unparseable
    # output" that mean nothing to someone just running the tool).
    console_logger.info(f"  {mark} {description}")

    results.append((num, description, passed, reason))
    return passed

# --- HTTP server -------------------------------------------------------------

class _FirmwareHandler(BaseHTTPRequestHandler):
    """
    Serves a single firmware file, suppresses access logs.

    Also handles GET /report?step=<n>&result=<value> — this is the
    device reporting a result BACK to the host over the network it
    already has working, rather than the host trying to read that
    result back over serial.

    Rationale: serial-echo of command results was found to be
    intermittently unreliable on real hardware (curl genuinely
    succeeding per the host's own access log, but its output never
    arriving back over the serial read — roughly a 50% failure rate
    across many real test runs, with no single root cause confirmed
    despite testing several theories: terminal line-wrapping,
    line-editor interference, stdout buffering). The TCP/IP path,
    in every test run all session, has been 100% reliable. So:
    once TCP/IP is confirmed up (Step 06-07), every result the
    device needs to report back is sent as a second outbound HTTP
    request to this server, not read back over serial. Serial is
    only ever used to LAUNCH a script on the device from this point
    on, never to receive its result.
    """
    firmware_path: Optional[Path] = None

    # Class-level, thread-safe store of reports received from the
    # device. The HTTP server runs its own background thread per
    # request; this lock protects concurrent access from those
    # threads vs. the main thread polling for a report via
    # wait_for_report().
    _reports_lock = threading.Lock()
    _reports = {}  # e.g. {"09": "200"}

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/report":
            self._handle_report(parsed)
            return

        if self.firmware_path is None or not self.firmware_path.exists():
            self.send_error(404)
            return
        size = self.firmware_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(self.firmware_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=65536)

    def do_HEAD(self):
        """
        Same response as do_GET, but headers only — no body. Used by
        Step 09's reachability check (curl -I), which only needs to
        confirm the firmware URL responds 200, not actually download
        it. Without this handler, BaseHTTPRequestHandler returns 501
        Not Implemented for HEAD requests by default, which curl -I
        would correctly report as a non-200 status — not a hang, but
        still the wrong check. This exists because Step 09 previously
        used a full GET (curl -s -o /dev/null ... <url>), which for a
        real ~400MB firmware file took long enough that the SECOND
        chained command in the same script (the report-back curl)
        never ran within the 20s wait_for_report() timeout — explaining
        every real-world Step 09 failure this session, none of which
        were reproduced by isolated tests because those all used tiny
        (~1.7KB) dummy payloads.
        """
        parsed = urlparse(self.path)
        if self.firmware_path is None or not self.firmware_path.exists():
            self.send_error(404)
            return
        size = self.firmware_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()

    def do_POST(self):
        """
        Handle POST /report?step=<n> with arbitrary content as the
        request body — used for delivering LOG content (which can be
        long, contain special characters, etc. — unsuitable for a URL
        query string) over TCP/IP instead of a second serial
        round-trip. The GET-based /report (see _handle_report) stays
        for short status codes; this is for anything longer/freeform.
        """
        parsed = urlparse(self.path)
        if parsed.path != "/report":
            self.send_error(404)
            return

        qs = parse_qs(parsed.query)
        step_id = qs.get("step", [None])[0]

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else ""

        if step_id is not None:
            with self._reports_lock:
                self._reports[step_id] = body
            verbose(f"✓ POST /report received: step={step_id} ({len(body)} bytes)", "debug")
            self.send_response(200)
            self.end_headers()
        else:
            verbose(f"POST /report received but missing step param: {parsed.query!r}", "debug")
            self.send_response(400)
            self.end_headers()

    def _handle_report(self, parsed):
        """
        Handle GET /report?step=<n>&result=<value> from the device.
        Stores the result and responds 200 OK — the device doesn't
        need to do anything with this response, it's fire-and-forget
        from the device's side.
        """
        qs = parse_qs(parsed.query)
        step_id = qs.get("step", [None])[0]
        result = qs.get("result", [None])[0]

        if step_id is not None and result is not None:
            with self._reports_lock:
                self._reports[step_id] = result
            verbose(f"✓ /report received: step={step_id} result={result!r}", "debug")
            self.send_response(200)
            self.end_headers()
        else:
            verbose(f"/report received but missing step/result params: {parsed.query!r}", "debug")
            self.send_response(400)
            self.end_headers()

    @classmethod
    def wait_for_report(cls, step_id: str, timeout: float = 15.0) -> Optional[str]:
        """
        Poll the thread-safe reports store for a result reported by
        the device for the given step_id, blocking up to timeout
        seconds. Returns the reported value, or None if it never
        arrived within timeout.

        This is the TCP/IP-based replacement for reading a result
        back over serial — call this AFTER instructing the device
        (over serial) to run a script that calls back to /report.

        CONSUMES the value (pops it) — only use this for a one-shot
        final result. For progress updates that get overwritten
        repeatedly while a long operation runs, use peek_report()
        instead, which does not consume.
        """
        start = time.time()
        while time.time() - start < timeout:
            with cls._reports_lock:
                if step_id in cls._reports:
                    return cls._reports.pop(step_id)
            time.sleep(0.05)
        return None

    @classmethod
    def peek_report(cls, step_id: str) -> Optional[str]:
        """
        Non-destructive read of the reports store — returns the
        current value for step_id without removing it, or None if
        nothing has been reported yet.

        Used for polling in-progress updates (e.g. dd's periodic
        status=progress output) where the device repeatedly overwrites
        the same step_id with newer values as an operation continues,
        and the host wants to read the latest value at any moment
        without consuming/erasing it (consuming would race against the
        device's next update and could miss values).
        """
        with cls._reports_lock:
            return cls._reports.get(step_id)

    def log_message(self, fmt, *args):
        verbose(f"HTTP: {fmt % args}", "debug")


def wait_for_report(step_id: str, timeout: float = 15.0) -> Optional[str]:
    """
    Module-level convenience wrapper around
    _FirmwareHandler.wait_for_report() — see that method's docstring
    for the full rationale (TCP/IP-based result reporting, replacing
    unreliable serial-echo readback). Call this AFTER launching a
    script (via SerialDevice.launch_script()) that reports its result
    back to this host's /report endpoint.
    """
    return _FirmwareHandler.wait_for_report(step_id, timeout=timeout)


def peek_report(step_id: str) -> Optional[str]:
    """
    Module-level convenience wrapper around
    _FirmwareHandler.peek_report() — non-destructive read for polling
    in-progress updates (e.g. flash progress percentage) without
    consuming them. See that method's docstring for the full rationale.
    """
    return _FirmwareHandler.peek_report(step_id)


def start_http_server(host_ip: str, port: int, firmware_path: Path) -> Optional[HTTPServer]:
    """Start HTTP server in a daemon thread. Returns server or None on failure."""
    try:
        with _FirmwareHandler._reports_lock:
            _FirmwareHandler._reports.clear()
        handler = type("Handler", (_FirmwareHandler,), {"firmware_path": firmware_path})
        server  = HTTPServer((host_ip, port), handler)
        thread  = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        verbose(f"✓ HTTP server listening on http://{host_ip}:{port}/", "debug")
        return server
    except OSError as e:
        verbose(f"Failed to start HTTP server: {e}", "error")
        return None

# --- Network helpers ---------------------------------------------------------

def detect_host_ip() -> str:
    """Best-effort detection of the host's primary non-loopback IPv4 address.
    Returns "" on failure — callers must handle this and fail loudly rather than
    proceeding with a guessed IP (which wastes minutes before the real error surfaces)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return ""


def parse_active_eth_iface(ip_link_output: str) -> Optional[str]:
    """
    Parse `ip link show` output and return the first eth* interface
    that reports LOWER_UP (has a live carrier — a cable is actually
    plugged in), or None if none do.

    Auto-detects whichever physical port the cable is actually in
    rather than assuming a specific jack — recovery Linux boots with
    every eth port administratively DOWN, so callers must bring all
    candidate ports up first before this can find anything.
    """
    for line in ip_link_output.split('\n'):
        if 'LOWER_UP' in line and ': ' in line:
            parts = line.split(': ')
            if len(parts) >= 2:
                iface_name = parts[1].split()[0]
                if iface_name.startswith('eth'):
                    return iface_name
    return None

# --- U-Boot env capture/restore -----------------------------------------------
# This hardware's U-Boot env backend is MMC-primary (see opnsense_lan.py's
# uboot step comment: "MMC env backend is primary, so we must write while
# MMC is intact, then erase after") — a whole-disk `dd` to eMMC (Armbian,
# OPNsense) can reset the env storage area to factory defaults along with
# the OS. capture_uboot_env() snapshots printenv before that happens
# (called from phase1_uboot(), before any journey-specific U-Boot commands
# run); restore_uboot_env() re-applies it afterward, for journeys that
# re-enter U-Boot post-flash.

def parse_uboot_env(printenv_output: str) -> dict:
    """
    Parse `printenv` output into a dict of {var: value}. U-Boot prints
    one "key=value" pair per line; the trailing "Environment size: X/Y
    bytes" summary line is excluded. Lines that don't look like a
    plain key=value pair (no "=", or a key containing whitespace —
    which would indicate a wrapped/garbled line rather than a real
    var) are skipped rather than guessed at.
    """
    env = {}
    for line in printenv_output.splitlines():
        if "=" not in line or line.strip().startswith("Environment size"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and " " not in key:
            env[key] = value
    return env


def capture_uboot_env(d: SerialDevice) -> Optional[dict]:
    """
    Snapshot the device's current U-Boot environment via `printenv`.
    Returns None on failure (no response, or nothing parseable) —
    callers must treat that as "no backup available" and skip the
    restore later, not fail the whole bootstrap over it; capturing a
    backup is a nice-to-have, not a hard prerequisite for flashing.
    """
    try:
        output = d.send_command("printenv", timeout=10)
    except Exception as e:
        verbose(f"capture_uboot_env: printenv failed: {e}", "warning")
        return None

    env = parse_uboot_env(output)
    if not env:
        verbose("capture_uboot_env: printenv returned no parseable vars", "warning")
        return None
    verbose(f"✓ Captured {len(env)} U-Boot env var(s) for post-flash restore", "debug")
    return env


def restore_uboot_env(d: SerialDevice, backup: Optional[dict]) -> int:
    """
    Re-apply a previously captured U-Boot environment snapshot (see
    capture_uboot_env()) — used after a whole-disk flash to eMMC that
    may have reset the env storage area to factory defaults.

    Restores every captured var unconditionally, with no diffing
    against the device's current env. This is safe as long as callers
    restore BEFORE making their own intentional env changes (e.g.
    setting bootcmd to boot the freshly-flashed OS) — U-Boot env is
    last-write-wins, so those explicit setenv calls simply run after
    this and take precedence; nothing intentional gets clobbered.

    Caller is responsible for the final `saveenv` — batched together
    with any of its own changes rather than saving twice here.

    Returns the number of variables restored (0 if backup is falsy).
    """
    if not backup:
        return 0
    restored = 0
    for key, value in backup.items():
        try:
            d.send_command(f'setenv {key} "{value}"', timeout=5)
            restored += 1
        except Exception as e:
            verbose(f"restore_uboot_env: failed to restore {key}: {e}", "warning")
    if restored:
        verbose(f"✓ Restored {restored} U-Boot env var(s) from pre-flash snapshot")
    return restored

# --- Phase implementations ---------------------------------------------------
# Unchanged from the proven test_flash_verify.py run (5/5 bootstrap on
# real hardware, network phase confirmed once device-ip matches subnet).

def phase1_uboot(port: str, baud: int = 115200) -> Optional[SerialDevice]:
    """
    Phase 1a: Serial bootstrap up to the U-Boot prompt.

    Detects the port, connects, waits for the user to power-cycle,
    and interrupts U-Boot autoboot. Returns the device at the U-Boot
    prompt — ready for the caller to run any U-Boot commands before
    handing off to phase1_recovery().

    If the device is already sitting at the U-Boot prompt (e.g. a
    retry within the same session), the power-cycle wait is skipped —
    see SerialDevice.probe_uboot_prompt(). This never skips anything
    else; journey-specific U-Boot commands and phase1_recovery() still
    run identically either way.

    Also snapshots the current U-Boot environment (printenv) before
    returning, so a later whole-disk flash to eMMC — which can reset
    this hardware's env storage area to factory defaults, since its
    U-Boot env backend is MMC-primary — has something to restore from.
    See capture_uboot_env()/restore_uboot_env() below.

    No OS awareness. No eMMC erase. No bootcmd changes.
    Those belong in journey files.

    Returns SerialDevice at U-Boot prompt on success, None on failure.
    """
    reset_results()
    verbose("=" * 60)
    verbose("Phase 1 — Bootstrap (serial)")
    verbose("=" * 60)
    console_logger.info("Connecting to device...")

    # Step 1: Device detected
    try:
        known, other = detect_serial_ports()
        all_ports    = known + other
        found        = any(p.device == port for p in all_ports)
        if not step(1, f"Device detected on {port}", found, "" if found else "port not in list"):
            return None
    except RuntimeError as e:
        step(1, f"Device detected on {port}", False, str(e))
        return None

    # Step 2: Connect
    d = SerialDevice(port, timeout=5)
    connected = d.connect(baud)
    if not step(2, f"Connect at {baud} baud", connected):
        return None

    # Step 3: Interrupt U-Boot — skip the wait if we're already there.
    already_at_prompt = d.probe_uboot_prompt(timeout=2.0)
    if already_at_prompt:
        verbose("✓ Device already at U-Boot prompt — skipping power-cycle wait")
        interrupted = True
    else:
        print()
        print("=" * 60)
        print("  ⚡ POWER CYCLE YOUR DEVICE NOW ⚡")
        print("=" * 60)
        print()
        interrupted, _autoboot_err = with_spinner(
            d.wait_for_autoboot, timeout=60,
            message="Waiting for U-Boot autoboot interrupt..."
        )
        if _autoboot_err:
            interrupted = False
    if not step(3, "U-Boot autoboot interrupted", interrupted,
                "already at prompt — power cycle skipped" if already_at_prompt else ""):
        d.disconnect()
        return None

    # Snapshot the env now, before any journey-specific U-Boot commands
    # run — see capture_uboot_env() below. Failure is non-fatal here;
    # it just leaves nothing to restore later.
    d.captured_uboot_env = capture_uboot_env(d)

    return d


def phase1_recovery(d: SerialDevice,
                     boot_method: str = "boot_recovery",
                     login_method: str = "login_recovery",
                     boot_medium: Optional[str] = None) -> Optional[SerialDevice]:
    """
    Phase 1b: Boot staging Linux and log in.

    Called after phase1_uboot() — and after any journey-specific
    U-Boot commands (eMMC erase, bootcmd changes, etc.) have been
    run by the journey file.

    boot_method / login_method select which SerialDevice methods to call.
    Default "boot_recovery" + "login_recovery" is the original behaviour.
    Journeys that need a different staging OS (e.g., Armbian when the
    'recovery' U-Boot variable has been lost) register alternative methods
    via step_registry.register_staging_boot() and tui.py passes them here.

    boot_medium: forwarded to boot_recovery() (only when that's the boot
    method) so the modern firmware tool can detect the medium. Default
    None leaves every existing caller unchanged.

    Returns the same SerialDevice now at the staging shell, or None on failure.
    """
    boot_fn  = getattr(d, boot_method)
    login_fn = getattr(d, login_method)

    boot_label  = "Recovery Linux booted" if boot_method == "boot_recovery" else "Staging Linux booted"
    login_label = ("Logged into recovery shell (root@recovery)"
                   if login_method == "login_recovery"
                   else "Logged into staging shell")

    # Step 4: Boot
    boot_msg = "Booting recovery Linux..." if boot_method == "boot_recovery" else "Booting staging Linux..."
    boot_args = {"boot_medium": boot_medium} if (boot_method == "boot_recovery" and boot_medium) else {}
    booted, _boot_err = with_spinner(boot_fn, message=boot_msg, **boot_args)
    if _boot_err:
        booted = False
    if not step(4, boot_label, booted):
        d.disconnect()
        return None

    # Step 5: Login
    login_msg = "Logging into recovery shell..." if login_method == "login_recovery" else "Logging into staging shell..."
    logged_in, _login_err = with_spinner(login_fn, timeout=60, message=login_msg)
    if _login_err:
        logged_in = False
    if not step(5, login_label, logged_in):
        d.disconnect()
        return None

    return d


def phase1_bootstrap(port: str, baud: int = 115200,
                     boot_medium: Optional[str] = None) -> Optional[SerialDevice]:
    """
    Phase 1: Full bootstrap — U-Boot interrupt + recovery boot + login.

    Convenience wrapper around phase1_uboot() + phase1_recovery() for
    callers that have no U-Boot commands to run in between (option 2
    firmware update, raw serial console, etc.).

    For OS flash journeys, call phase1_uboot() and phase1_recovery()
    separately so the journey can run its U-Boot steps in between.

    boot_medium: forwarded to phase1_recovery() so the modern firmware
    tool can detect the medium. Default None leaves existing callers
    unchanged.

    Returns SerialDevice at recovery shell on success, None on failure.
    """
    d = phase1_uboot(port, baud)
    if d is None:
        return None
    return phase1_recovery(d, boot_medium=boot_medium)


def phase3_flash(d: SerialDevice, host_ip: str, port: int, flash_target: str,
                  firmware_size: int = 0) -> bool:
    """
    Phase 3: Flash — curl | dd on device, verify dd output.

    Uses run_script() with output redirected to files on-device, then
    read back via cat/wc — NOT direct stdout capture. This matters:
    direct-stdout capture (curl's real output landing straight in the
    exec response, e.g. "curl -s -o /dev/null -w '%{http_code}' <url>"
    read directly) was found to be FLAKY on real hardware — passing
    roughly 1 in 3 runs with identical code, identical command, same
    device. Across many real runs, the file-redirect pattern (write
    output to a file, then separately cat/wc that file — exactly what
    a parallel diagnostic script's "Stage 3" does) passed 100% of the
    time, while the direct-stdout form failed intermittently. Rather
    than chase the exact cause of that intermittent loss (suspected:
    curl's stdout buffering mode varies depending on pty/line-
    discipline state at process start, though this was not confirmed
    — a competing theory, shell line-editing interference, was tested
    and found inconclusive on a single run), this switches to the
    pattern with a 100% observed pass rate.
    """
    verbose("=" * 60)
    verbose("Phase 3 — Flash")
    verbose("=" * 60)
    console_logger.info("Preparing to flash...")

    url = f"http://{host_ip}:{port}/firmware.img"

    # Step 9: Firmware source reachable.
    #
    # PROTOTYPE: TCP/IP-based result reporting, replacing serial-echo
    # readback. Reading a script's result back over serial was found
    # to be intermittently unreliable on real hardware in EARLIER
    # testing (~50% failure rate) — but that was measured against
    # multi-command scripts using $() substitution and curl chaining,
    # NOT against simple single-curl commands. A later baseline test
    # (test_run_script_reliability.py --mode step09) proved the exact
    # curl-against-real-URL pattern below is 100% reliable across 5
    # real hardware runs via run_script() (which DOES wait for and
    # read the serial response). The earlier theory — that ALL serial
    # readback is unreliable — was wrong; only the longer, chained,
    # $()-substitution script was failing. This version is
    # deliberately kept as close to that proven-reliable pattern as
    # possible: ONE curl call writing its status code to a file, then
    # ONE follow-up curl call uploading that file's content via POST.
    # No command substitution, no semicolon-chained multi-command
    # script. Still launched via launch_script() (fire-and-forget) and
    # reported back via TCP/IP (not parsed from serial), per the
    # earlier architectural decision that the TCP/IP link has been
    # 100% reliable all session — this keeps that part.
    #
    # ACTUAL ROOT CAUSE (found after the above still failed on real
    # hardware with the real ~400MB firmware file, despite passing
    # 100% in every isolated test that used a tiny dummy payload):
    # the first curl call used a full GET, which downloads the ENTIRE
    # firmware file before "completing" (-o /dev/null discards the
    # body, but curl still waits for the full transfer). For a real
    # multi-hundred-MB file, that download alone could exceed the 20s
    # wait_for_report() timeout, so the second chained command (the
    # report-back curl) genuinely never got a chance to run in time.
    # Switched to "curl -I" (HEAD request) below — confirms reachability
    # via headers only, no body transferred — which is also a more
    # correct check for Step 09's actual goal ("is the source
    # reachable", not "can we download the whole thing"; the real
    # download happens later in Steps 10-12 via curl | dd anyway).
    # Requires a do_HEAD handler on the host's HTTP server (added
    # alongside do_GET above), since BaseHTTPRequestHandler returns 501
    # for HEAD by default.
    check_script = (
        f"curl -sk -I -o /dev/null -w '%{{http_code}}' {url} "
        f"> /tmp/mono_imager_step09_code.txt; "
        f"curl -sk -X POST --data-binary @/tmp/mono_imager_step09_code.txt "
        f"\"http://{host_ip}:{port}/report?step=09\" >/dev/null 2>&1"
    )
    # Every isolated test of this exact script (test_run_script_reliability.py
    # --mode launchtest, and test_real_boot_then_launch.py) explicitly
    # checked launch_script()'s return value / raised exceptions and
    # passed 100% of the time. This real call site previously discarded
    # the return value entirely — if launch_script() raised here (e.g.
    # the byte-count verification mismatch seen elsewhere in this
    # session), that exception would propagate silently into whatever
    # wraps phase3_flash(), with no specific log line pointing at this
    # being the cause. Capturing and logging it explicitly closes that
    # gap, matching what every passing isolated test already did.
    try:
        remote_path = d.launch_script(check_script, marker="step09_reachable")
        verbose(f"✓ Step 09: launch_script() returned without raising: {remote_path}", "debug")
    except Exception as e:
        verbose(f"✗ Step 09: launch_script() RAISED: {e}", "error")
        remote_path = None

    check = wait_for_report("09", timeout=20.0)
    reachable = check is not None and "200" in check

    debug_detail = f"HTTP status: {check}" if not reachable else ""
    if not reachable:
        logger.error(
            f"Step 09: report never arrived or was unexpected (got: {check!r}). "
            "This script is the simplified, baseline-proven-reliable version — "
            "if this still fails, the issue is likely NOT script complexity."
        )

    if not step(9, f"Firmware source reachable ({url})", reachable, debug_detail):
        return False

    # Step 10+11+12: download firmware, then dd from local file —
    # UNLESS the image is too large to fit in recovery Linux's root
    # filesystem (confirmed on real hardware to be a 3.8GB tmpfs-
    # backed root, NOT the full 8GB physical RAM — `df -h /tmp` shows
    # `rootfs 3.8G`, and /tmp is not a separate, larger mount). A
    # ~5GB OPNsense image was confirmed via manual on-device curl
    # testing to fail at 74% (3.74GB written) with curl error 23
    # ("Failure writing output to destination, passed 16384 returned
    # 257") — a disk-full write failure, not a network or server
    # issue (ruled out: Python's stock http.server delivered the same
    # file at full transfer speed with no server-side error; the
    # device's own curl 8.19.0 is a full build with Largefile support,
    # not a limited busybox curl).
    #
    # FLASH_SIZE_CAP below is set at 80% of that confirmed 3.8GB cap
    # (≈3.0GB) to leave headroom for the flash script itself plus
    # curl's in-flight buffers — not a guess, a deliberate margin
    # under the hard, confirmed ceiling.
    #
    # Below the cap: download-then-dd, matching Mono's own documented
    # procedures (both the OpenWRT recovery guide and the OPNsense
    # install guide at docs.mono.si / opnsense.mono.si never pipe
    # curl directly into dd — both download the full image to disk
    # first, then dd from that local file, bs=1M). This was changed
    # from an earlier `curl | dd bs=4M` streaming-pipe version after
    # real-hardware testing showed dd reporting 100% partial records
    # (e.g. "0+95365 records in / 0+95365 records out", zero full
    # records) — a known consequence of piping curl directly into dd,
    # since a pipe never delivers clean fixed-size blocks regardless
    # of bs. The flash still worked when piped (later verified by
    # mounting /dev/mmcblk0p1 and confirming a complete, valid
    # filesystem), but the all-partial-records log output is
    # confusing and is an unverified deviation from the
    # vendor-documented method on hardware where a bad flash means
    # physical re-recovery.
    #
    # Above the cap: there is no other option — streaming curl | dd
    # directly to the eMMC target is the only way an oversized image
    # can be flashed at all, since it never touches the
    # capacity-limited root filesystem. This re-enables that
    # previously-tested, previously-reverted path, but ONLY for
    # images that need it; small images keep using the safer,
    # quieter, vendor-documented buffered method.
    #
    # dd's "records in/out" summary is written to stderr, which 2>&1
    # merges into stdout — redirect that combined stream to a file,
    # then cat it back, same reasoning as Step 9 above.
    #
    # NOTE: a live-progress version (dd status=progress + periodic
    # TCP/IP reporting) was attempted and reverted. Root cause: this
    # device runs BUSYBOX dd, which does NOT support status=progress
    # at all (confirmed directly: BusyBox dd only accepts
    # status=noxfer or status=none). A SIGUSR1-based alternative
    # (BusyBox dd does print intermediate progress on SIGUSR1) was
    # explored but the PID-tracking needed to signal the right
    # process reliably wasn't solid by testing time. Live progress
    # remains a real opportunity for a future, more careful pass —
    # not worth the risk of breaking a working flash step in the
    # meantime.
    FLASH_SIZE_CAP = int(3.8 * 1024**3 * 0.8)  # ≈3.0GB, 80% of confirmed 3.8GB root cap

    use_streaming = firmware_size > 0 and firmware_size > FLASH_SIZE_CAP

    verbose(f"Flashing {flash_target} — this may take several minutes...")
    console_logger.info("Flashing firmware — this may take several minutes...")

    if use_streaming:
        verbose(
            f"Firmware size ({firmware_size / 1024**3:.2f} GB) exceeds the "
            f"{FLASH_SIZE_CAP / 1024**3:.2f} GB buffered-flash cap — using "
            "streaming mode (curl | dd direct to target).",
            "warning"
        )
        flash_script = (
            f"curl -sk {url} 2>/tmp/mono_imager_step10_flash.log | "
            f"dd of={flash_target} bs=1M "
            f">> /tmp/mono_imager_step10_flash.log 2>&1; "
            f"cat /tmp/mono_imager_step10_flash.log"
        )
    else:
        local_fw_path = "/tmp/mono_imager_firmware.img"
        flash_script = (
            f"curl -sk -o {local_fw_path} {url} "
            f"> /tmp/mono_imager_step10_flash.log 2>&1; "
            f"dd if={local_fw_path} of={flash_target} bs=4096 "
            f">> /tmp/mono_imager_step10_flash.log 2>&1; "
            f"rm -f {local_fw_path}; "
            f"cat /tmp/mono_imager_step10_flash.log"
        )
    response, flash_error = with_spinner(
        d.run_script, flash_script,
        marker="step10_flash", exec_timeout=600,
        message="Flashing — this may take several minutes"
    )
    if flash_error is not None:
        verbose(f"✗ Step 10: run_script() RAISED: {flash_error}", "error")
        step(10, "curl | dd executed on device", False, str(flash_error))
        step(11, "dd confirmed records in/out", False)
        step(12, "No curl errors", False)
        return False

    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower() or "failed" in response.lower()

    step(10, "curl | dd executed on device", True)
    step(11, "dd confirmed records in/out", has_records,
         f"output: {response[-200:]}" if not has_records else "")
    step(12, "No curl errors", not has_error,
         f"output: {response[-200:]}" if has_error else "")

    return has_records and not has_error


def phase4_postflash(d: SerialDevice) -> bool:
    """
    Phase 4: Post-flash reboot.

    Sends reboot to the device. Journey files handle any post-flash
    steps before calling this (firmware re-image, DIP flip prompts, etc.).
    """
    verbose("=" * 60)
    verbose("Phase 4 — Post-flash")
    verbose("=" * 60)
    verbose("Sending reboot command...")
    d.send_command("reboot", wait_for_prompt=False, timeout=5)
    verbose("Flash complete. Reboot sent.")
    console_logger.info("Rebooting device...")
    return True

# --- Full journey orchestration ------------------------------------------
#
# run_flash_journey() is the single entry point tui.py calls for the
# "Flash OS" menu — restores the get_journey()+.run() contract
# JOURNEYS.md documents ("tui.py calls get_journey() and runner.run(),
# nothing else"). menu_network_flashing() used to violate that by
# driving phase1_uboot/phase1_recovery/network-setup inline itself;
# that ~90-line sequence now lives here once.
#
# get_device_net/setup_network are passed in rather than imported —
# both are session-scoped on MonoImager (the cached device network
# used by every other caller: eMMC/NOR updates, Test LAN, startup).
#
# Returns None if the journey never got far enough to run
# (bootstrap/U-Boot-steps/network-setup failure) — callers must NOT
# overwrite their own flash_success in that case, matching the
# original menu_network_flashing()'s behavior of leaving flash_success
# untouched on those early failures. Returns a bool (this module's own
# print_report() verdict) once the journey actually ran.

def run_flash_journey(
    port: str,
    os_name: str,
    transfer: str,
    host_ip: str,
    http_port: int,
    firmware_path,
    get_device_net: Callable[[], Optional[dict]],
    setup_network: Callable[[SerialDevice], bool],
) -> Optional[bool]:
    from mono_imager.journeys import get_journey
    from mono_imager.step_registry import get_staging_boot_methods, list_journey

    d = None
    journey = None
    try:
        print()
        print("=" * 60)
        print("PHASE 1: Bootstrap (Serial Connection)")
        print("=" * 60)
        print(f"Port: {port}")
        print()

        # Step 1: Connect and interrupt U-Boot — no OS awareness
        d = phase1_uboot(port, 115200)
        if d is None:
            print("❌ Bootstrap FAILED")
            return None

        # Step 2: Journey-specific U-Boot commands (eMMC erase, bootcmd, etc.)
        # Delegated entirely to the journey file via run_uboot_steps()
        print()
        print("  Configuring U-Boot...")
        journey = get_journey(
            os_name       = os_name,
            transfer      = transfer,
            device        = d,
            host_ip       = host_ip,
            device_ip     = (get_device_net() or {}).get("ip", ""),
            firmware_path = Path(firmware_path),
            http_port     = http_port,
            device_net    = get_device_net(),
        )
        if not journey.run_uboot_steps():
            print("❌ U-Boot setup FAILED")
            return None

        # Step 3: Boot staging Linux (recovery or alternative, per journey)
        staging = get_staging_boot_methods(os_name, transfer)
        d = phase1_recovery(d, **staging)
        if d is None:
            print("❌ Bootstrap FAILED")
            return None

        print("✓ Bootstrap successful")
        print()

        # Step 3b: resolve the device's own network — same DHCP-first,
        # verified, manual-fallback mechanism used everywhere else.
        # Only needed by journeys whose step list actually depends on
        # it (LAN transfer, or a post-flash internet-requiring step
        # like OpenWRT/OPNsense's firmware update) — skip it otherwise
        # so e.g. Armbian-via-USB never prompts for network settings
        # it will never use.
        needs_network = "Device network ready" in list_journey(os_name, transfer)
        if needs_network:
            if not setup_network(d):
                print("❌ Device network setup FAILED — cannot continue without it.")
                return None
            # get_journey() was called earlier (before recovery boot,
            # for run_uboot_steps()) with a placeholder device_net —
            # now that it's actually resolved, forward it into the
            # already-built ctx rather than rebuilding the journey.
            device_net = get_device_net()
            journey.ctx.device_net = device_net
            journey.ctx.device_ip  = device_net["ip"]

        print()
        print("=" * 60)
        print("PHASE 2+: Flashing Firmware")
        print("=" * 60)
        fw_display = "auto-detected from USB" if Path(firmware_path) == Path(".") else str(firmware_path)
        print(f"OS:          {os_name}")
        print(f"Firmware:    {fw_display}")
        print(f"Host IP:     {host_ip}:{http_port}")
        print(f"Device IP:   {journey.ctx.device_ip or '(not needed for this journey)'}")
        print()

        ok = journey.run()

        if not ok:
            print("❌ Flashing did not complete successfully")
        else:
            print("✓ Flashing completed successfully")
        print()

    finally:
        server = None
        if journey is not None:
            try:
                server = journey.ctx.get("http_server")
            except Exception:
                pass
            try:
                extracted = journey.ctx.get("extracted_rootfs")
                if extracted:
                    Path(extracted).unlink(missing_ok=True)
            except Exception:
                pass
        if server:
            server.shutdown()
            verbose("HTTP server stopped")
        if d:
            d.disconnect()

    return print_report()


# --- Report ------------------------------------------------------------------

def print_report():
    # Full detail to the file, unchanged — every step, numbered, with
    # technical reasons. This is the troubleshooting record.
    verbose("=" * 60)
    verbose("Verification Report")
    verbose("=" * 60)
    passed = sum(1 for _, _, p, _ in results if p)
    total  = len(results)
    for num, desc, p, reason in results:
        mark = "✓ PASS" if p else "✗ FAIL"
        line = f"  Step {num:02d}: {mark} — {desc}"
        if reason:
            line += f"\n           {reason}"
        verbose(line)
    verbose("-" * 60)
    verdict = "OK" if passed == total else "NOK"
    verbose(f"Result: {verdict} ({passed}/{total} steps passed)")
    verbose(f"📄 Report saved to: {get_log_file()}")

    # Short, plain summary to the console — what a person actually
    # wants to know: did it work, and if not, what failed (in plain
    # terms, no step numbers, no technical reason strings).
    console_logger.info("")
    if verdict == "OK":
        console_logger.info("✓ Flash completed successfully.")
    else:
        console_logger.info("✗ Flash did not complete successfully.")
        failed = [desc for _, desc, p, _ in results if not p]
        for desc in failed:
            console_logger.info(f"  - {desc}")
        console_logger.info(f"  Full details: {get_log_file()}")

    return verdict == "OK"
