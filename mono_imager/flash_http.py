"""
mono-imager: The host-side HTTP server the device downloads firmware from, and host network helpers

Split out of flash_orchestrator.py; import it through mono_imager.flash_orchestrator,
which re-exports these names. Calls go through `F.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


import shutil
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mono_imager import flash_orchestrator as F


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
    firmware_path: Path | None = None

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
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = self.firmware_path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(self.firmware_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=F.HTTP_COPY_BLOCK)

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
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = self.firmware_path.stat().st_size
        self.send_response(HTTPStatus.OK)
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
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        qs = parse_qs(parsed.query)
        step_id = qs.get("step", [None])[0]

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else ""

        if step_id is not None:
            with self._reports_lock:
                self._reports[step_id] = body
            F.verbose(f"✓ POST /report received: step={step_id} ({len(body)} bytes)", "debug")
            self.send_response(HTTPStatus.OK)
            self.end_headers()
        else:
            F.verbose(f"POST /report received but missing step param: {parsed.query!r}", "debug")
            self.send_response(HTTPStatus.BAD_REQUEST)
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
            F.verbose(f"✓ /report received: step={step_id} result={result!r}", "debug")
            self.send_response(HTTPStatus.OK)
            self.end_headers()
        else:
            F.verbose(f"/report received but missing step/result params: {parsed.query!r}", "debug")
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()

    @classmethod
    def wait_for_report(cls, step_id: str, timeout: float = F.C.REPORT_TIMEOUT) -> str | None:
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
            time.sleep(F.REPORT_POLL_SLEEP)
        return None

    @classmethod
    def peek_report(cls, step_id: str) -> str | None:
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
        F.verbose(f"HTTP: {fmt % args}", "debug")


def wait_for_report(step_id: str, timeout: float = F.C.REPORT_TIMEOUT) -> str | None:
    """
    Module-level convenience wrapper around
    _FirmwareHandler.wait_for_report() — see that method's docstring
    for the full rationale (TCP/IP-based result reporting, replacing
    unreliable serial-echo readback). Call this AFTER launching a
    script (via SerialDevice.launch_script()) that reports its result
    back to this host's /report endpoint.
    """
    return F._FirmwareHandler.wait_for_report(step_id, timeout=timeout)


def peek_report(step_id: str) -> str | None:
    """
    Module-level convenience wrapper around
    _FirmwareHandler.peek_report() — non-destructive read for polling
    in-progress updates (e.g. flash progress percentage) without
    consuming them. See that method's docstring for the full rationale.
    """
    return F._FirmwareHandler.peek_report(step_id)


def start_http_server(host_ip: str, port: int, firmware_path: Path) -> F.HTTPServer | None:
    """Start HTTP server in a daemon thread. Returns server or None on failure."""
    try:
        with F._FirmwareHandler._reports_lock:
            F._FirmwareHandler._reports.clear()
        handler = type("Handler", (F._FirmwareHandler,), {"firmware_path": firmware_path})
        server  = F.HTTPServer((host_ip, port), handler)
        thread  = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        F.verbose(f"✓ HTTP server listening on http://{host_ip}:{port}/", "debug")
        return server
    except OSError as e:
        F.verbose(f"Failed to start HTTP server: {e}", "error")
        return None

# --- Network helpers ---------------------------------------------------------

def detect_host_ip() -> str:
    """Best-effort detection of the host's primary non-loopback IPv4 address.
    Returns "" on failure — callers must handle this and fail loudly rather than
    proceeding with a guessed IP (which wastes minutes before the real error surfaces)."""
    try:
        with F.socket.socket(F.socket.AF_INET, F.socket.SOCK_DGRAM) as s:
            s.connect((F.C.INTERNET_PROBE_IP, F.C.INTERNET_PROBE_PORT))
            return s.getsockname()[0]
    except Exception:
        return ""


def parse_active_eth_ifaces(ip_link_output: str) -> list:
    """
    Parse `ip link show` output and return ALL eth* interfaces that
    report LOWER_UP (a live carrier — a cable actually plugged in),
    in the order they appear. Empty list if none do.

    Recovery Linux boots every eth port administratively DOWN, so
    callers must bring all candidate ports up first before this finds
    anything. More than one entry means multiple cables are live — an
    ambiguous ("complex") topology the caller must resolve (issue #19)
    rather than silently guessing which port is the WAN uplink.
    """
    ifaces = []
    for line in ip_link_output.split('\n'):
        if 'LOWER_UP' in line and ': ' in line:
            parts = line.split(': ')
            if len(parts) >= 2:
                name = parts[1].split()[0]
                if name.startswith('eth') and name not in ifaces:
                    ifaces.append(name)
    return ifaces


def parse_active_eth_iface(ip_link_output: str) -> str | None:
    """
    Backwards-compatible single-port helper: the FIRST eth* interface
    with LOWER_UP, or None. See parse_active_eth_ifaces() for the full
    list (used to detect the multi-cable complex-topology case, #19).
    """
    ifaces = F.parse_active_eth_ifaces(ip_link_output)
    return ifaces[0] if ifaces else None


__all__ = [
    "_FirmwareHandler",
    "detect_host_ip",
    "parse_active_eth_iface",
    "parse_active_eth_ifaces",
    "peek_report",
    "start_http_server",
    "wait_for_report",
]
