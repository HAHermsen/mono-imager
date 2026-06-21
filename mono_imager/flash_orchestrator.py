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
Version: 0.6.0
License: MIT
"""

__version__ = "0.6.0"
__author__  = "H.A. Hermsen"

import sys
import time
import logging
import platform
import socket
import subprocess
import threading

from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from typing import Optional

from mono_imager.config import detect_serial_ports
from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner

# --- Logging setup -----------------------------------------------------------

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file  = LOG_DIR / f"verify_flash_{timestamp}.log"

# Two genuinely separate destinations, not just two handlers on one logger:
#
# 1. ROOT logger (file only, DEBUG level) — captures EVERYTHING, including
#    serial_device.py's raw >> sent / << received byte-level tracing. This
#    preserves full troubleshooting detail in the log file, same as before.
#    Nothing is lost here — it just no longer also prints to the screen.
#
# 2. console_logger (console only, INFO level, plain format) — a SEPARATE
#    logger used explicitly for user-facing messages: step results, phase
#    headers, the final summary. No timestamps, no [DEBUG]/[INFO] tags, no
#    raw serial dumps. This is what a person running the tool actually sees.
#
# Previously these were the same logger/handler, set to DEBUG on console
# too — leftover from diagnosing the Step 09 root cause (fixed several
# iterations ago, never reverted), which is why console output looked like
# a raw debug trace dump instead of a normal app's progress messages.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file, encoding="utf-8")],
    force=True
)
logger = logging.getLogger(__name__)  # full detail, file only — used by serial_device.py etc.

def verbose(msg: str, level: str = "info"):
    """Print to console immediately AND log it"""
    print(msg, flush=True)
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "debug":
        logger.debug(msg)
    else:
        logger.info(msg)

file_logger = logger  # alias for clarity at call sites that explicitly want file-only detail

console_logger = logging.getLogger("mono_imager.console")
console_logger.setLevel(logging.INFO)
console_logger.propagate = False  # don't also send these through the root/file handler chain
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(message)s"))
console_logger.addHandler(_console_handler)

# --- Result tracker ----------------------------------------------------------

results: list[tuple[int, str, bool, str]] = []  # (step, description, passed, reason)

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
    results.clear()

def step(num: int, description: str, passed: bool, reason: str = ""):
    mark = "✓" if passed else "✗"

    # File gets the full technical detail: step number, PASS/FAIL, reason.
    file_msg = f"Step {num:02d}: {'✓ PASS' if passed else '✗ FAIL'} — {description}"
    if reason:
        file_msg += f" ({reason})"
    file_logger.info(file_msg) if passed else file_logger.error(file_msg)

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
    firmware_path: Path = None

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
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

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
        handler = type("Handler", (_FirmwareHandler,), {"firmware_path": firmware_path})
        server  = HTTPServer((host_ip, port), handler)
        thread  = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        verbose(f"✓ HTTP server listening on http://{host_ip}:{port}/")
        return server
    except OSError as e:
        verbose(f"Failed to start HTTP server: {e}", "error")
        return None

# --- Network helpers ---------------------------------------------------------

def detect_host_ip() -> str:
    """Best-effort detection of the host's primary non-loopback IPv4 address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.1.1"


def pick_device_ip(host_ip: str) -> Optional[str]:
    """
    Derive a device IP on the SAME /24 subnet as the host, instead of
    a hardcoded default that may sit on a different subnet (the bug
    that caused Step 07 to fail with 192.168.1.10 on a 192.168.168.x
    host — see test_verify_flash_auto.py history).

    Uses .222 on the host's subnet — high, rarely DHCP-assigned.

    Returns None if host_ip can't be parsed as a normal dotted-quad
    IPv4 address — callers MUST handle this explicitly and should fail
    loudly rather than guess. A previous version of this function
    fell back to a hardcoded "192.168.1.10" here, which is exactly the
    same class of bug this function exists to fix: a fixed IP that may
    not be anywhere near the actual host's subnet. Silently returning
    a guess in the error path just relocates the original bug rather
    than fixing it. If host_ip can't be parsed, the right move is to
    tell the user to switch to Manual mode and supply IPs themselves
    — not to guess on their behalf.

    Shared here (rather than duplicated per test script) since any
    caller bringing up device networking needs this same derivation —
    test_verify_flash_auto.py, test_diagnose_run_script.py, and
    tui.py all rely on this.
    """
    try:
        octets = host_ip.split(".")
        if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
            raise ValueError(f"unexpected host IP format: {host_ip}")
        subnet = ".".join(octets[:3])
        candidate = f"{subnet}.222"
        if candidate.split(".")[3] == octets[3]:
            candidate = f"{subnet}.223"
        return candidate
    except Exception as e:
        verbose(f"Could not derive device IP from host IP ({e}) — no safe fallback exists, caller must handle", "warning")
        return None


def ping(ip: str, count: int = 3) -> bool:
    """Ping an IP address. Returns True if reachable."""
    flag = "-n" if platform.system().lower() == "windows" else "-c"
    try:
        result = subprocess.run(
            ["ping", flag, str(count), "-w", "2000" if platform.system().lower() == "windows" else "2", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10
        )
        return result.returncode == 0
    except Exception:
        return False

# --- Phase implementations ---------------------------------------------------
# Unchanged from the proven test_flash_verify.py run (5/5 bootstrap on
# real hardware, network phase confirmed once device-ip matches subnet).

def phase1_bootstrap(port: str, baud: int = 115200) -> Optional[SerialDevice]:
    """
    Phase 1: Serial bootstrap — detect, connect, interrupt U-Boot, boot recovery, login.
    Returns connected SerialDevice on full success, None on any failure.
    """
    reset_results()  # clear any stale results from a prior attempt in
                      # this same process — see reset_results() docstring
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

    # Step 3: Interrupt U-Boot
    print()
    print("=" * 60)
    print("  ⚡ POWER CYCLE YOUR DEVICE NOW ⚡")
    print("=" * 60)
    print()
    interrupted = d.wait_for_autoboot(timeout=60)
    if not step(3, "U-Boot autoboot interrupted", interrupted):
        d.disconnect()
        return None

    # Step 4: Boot recovery Linux
    booted = d.boot_recovery()
    if not step(4, "Recovery Linux booted", booted):
        d.disconnect()
        return None

    # Step 5: Login
    logged_in = d.login_recovery(timeout=60)
    if not step(5, "Logged into recovery shell (root@recovery)", logged_in):
        d.disconnect()
        return None

    return d


def phase2_network(d: SerialDevice, host_ip: str, device_ip: str, port: int, firmware_path: Path):
    """
    Phase 2: Network prep — bring up active Ethernet port, assign IP, ping check, start HTTP server.
    Auto-detects which port has carrier (LOWER_UP) instead of hardcoding eth0.
    Returns HTTPServer on success, None on failure.
    """
    verbose("=" * 60)
    verbose("Phase 2 — Network prep (TCP)")
    verbose("=" * 60)
    console_logger.info("Setting up network...")

    # Step 6a: Auto-detect and bring up active Ethernet port
    # Bring up all eth* ports to allow them to acquire carrier,
    # then detect which one has LOWER_UP. Recovery Linux doesn't persist
    # network config across boots, so ports start DOWN.
    try:
        # Try to bring up all eth ports (eth0-eth4 typically)
        for port_num in range(5):
            try:
                d.send_command(f"ip link set eth{port_num} up", timeout=5)
            except:
                pass  # Port might not exist, that's ok
        
        # Now check for LOWER_UP on any eth port
        ip_output = d.send_command("ip link show", timeout=5)
        active_iface = None
        for line in ip_output.split('\n'):
            if 'LOWER_UP' in line and ': ' in line:
                parts = line.split(': ')
                if len(parts) >= 2:
                    iface_name = parts[1].split()[0]
                    if iface_name.startswith('eth'):
                        active_iface = iface_name
                        break
        if not active_iface:
            verbose("No active Ethernet port detected (no LOWER_UP on any eth port)", "error")
            return None
        iface = active_iface
        verbose(f"Auto-detected active Ethernet port: {iface}")
    except Exception as e:
        verbose(f"Failed to detect Ethernet port: {e}", "error")
        return None

    # Step 6: Assign IP to the active interface (it's already up from Step 6a)
    r = d.send_command(f"ip addr add {device_ip}/24 dev {iface}".format(device_ip), timeout=5)
    up = "error" not in r.lower() or "exists" in r.lower()
    if not step(6, f"{iface} up, device IP {device_ip} assigned", up, r if not up else ""):
        return None

    # Step 7: Ping check
    reachable = ping(device_ip)
    if not step(7, f"Device {device_ip} reachable from host", reachable,
                "check host NIC is on same subnet" if not reachable else ""):
        return None

    # Step 8: HTTP server
    server = start_http_server(host_ip, port, firmware_path)
    if not step(8, f"HTTP server up on {host_ip}:{port}", server is not None):
        return None

    return server


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
        f"curl -s -I -o /dev/null -w '%{{http_code}}' {url} "
        f"> /tmp/mono_imager_step09_code.txt; "
        f"curl -s -X POST --data-binary @/tmp/mono_imager_step09_code.txt "
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
            f"curl -s {url} 2>/tmp/mono_imager_step10_flash.log | "
            f"dd of={flash_target} bs=1M "
            f">> /tmp/mono_imager_step10_flash.log 2>&1; "
            f"cat /tmp/mono_imager_step10_flash.log"
        )
    else:
        local_fw_path = "/tmp/mono_imager_firmware.img"
        flash_script = (
            f"curl -s -o {local_fw_path} {url} "
            f"> /tmp/mono_imager_step10_flash.log 2>&1; "
            f"dd if={local_fw_path} of={flash_target} bs=1M "
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
    Phase 4: Post-flash — send reboot and say goodbye.

    The flash itself is fully verified by Steps 1-12 (phase1_bootstrap
    + phase2_network + phase3_flash) — that's where "did mono-imager
    do its job correctly" is decided. What the device does after
    reboot (timing, whether the USB-serial port drops, what OS/wizard
    it lands in) depends on the new firmware and this hardware's
    specific USB/UART behavior, which isn't well understood yet and
    isn't this tool's job to verify. Earlier versions of this function
    spent up to 60s trying to detect the device coming back, purely
    informationally — wasted time spent confirming something explicitly
    declared not to matter. Removed. This just sends reboot and exits.
    """
    verbose("=" * 60)
    verbose("Phase 4 — Post-flash")
    verbose("=" * 60)

    verbose("Sending reboot command...")
    d.send_command("reboot", wait_for_prompt=False, timeout=5)

    verbose("Flash complete. Reboot sent — the rest is the new firmware's job.")
    console_logger.info("Rebooting device...")
    return True

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
    verbose(f"📄 Report saved to: {log_file}")

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
        console_logger.info(f"  Full details: {log_file}")

    return verdict == "OK"
