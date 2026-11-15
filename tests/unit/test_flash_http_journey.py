"""
mono-imager: flash_orchestrator.py — the parts test_flash_orchestrator.py
leaves out: the firmware HTTP server over a real socket, host-IP detection,
the error branches of phase1_uboot()/phase1_recovery(), and every path
through run_flash_journey().

The HTTP tests start the real server on 127.0.0.1 and talk to it with
urllib, so request parsing, status codes and the report store are the real
ones. run_flash_journey() gets fake phases and a fake journey.

Run: python tests/unit/test_flash_http_journey.py
"""

import contextlib
import io
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import flash_orchestrator as FO
from mono_imager import journeys as J
from mono_imager import step_registry as SR

LOCALHOST = "127.0.0.1"
FIRMWARE_BYTES = bytes(range(256)) * 300          # > one HTTP_COPY_BLOCK, not a multiple of it
STEP = "09"
RESULT = "200"
LOG_BODY = "line one\nline two: ✓ ünïcode\n"
REPORT_WAIT_S = 2
NO_REPORT_WAIT_S = 0.1
PORT = "COM5"
OS_NAME = "Armbian"
HOST_IP = "192.168.168.89"
HTTP_PORT = 8080
DEVICE_NET = {"ip": "192.168.168.200", "prefix": 24, "gateway": "192.168.168.1", "source": "dhcp"}
NETWORK_STEP = "Device network ready"
REPORT_VERDICT = "report-verdict"                  # stands in for print_report()'s bool

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {label}")
        passed += 1
    else:
        print(f"  FAIL: {label}")
        failed += 1


def section(title):
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def free_port():
    with socket.socket() as s:
        s.bind((LOCALHOST, 0))
        return s.getsockname()[1]


def request(base, path, *, method="GET", body=None):
    """(status, headers, body) — HTTP errors returned, not raised."""
    req = urllib.request.Request(base + path, data=body, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def run_spinner(fn, *args, message="", **kwargs):
    """with_spinner() without the thread: (result, error) like the real one."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - mirrors with_spinner(), which captures any error
        return None, e


# ----------------------------------------------------------------------------
section("firmware HTTP server (real socket)")
with tempfile.TemporaryDirectory() as tmp:
    fw = Path(tmp) / "firmware.img"
    fw.write_bytes(FIRMWARE_BYTES)
    port = free_port()
    srv = FO.start_http_server(LOCALHOST, port, fw)
    assert srv is not None, "HTTP server did not start"
    base = f"http://{LOCALHOST}:{port}"
    try:
        status, headers, body = request(base, "/firmware.img")
        check("GET firmware -> 200, whole file", status == HTTPStatus.OK and body == FIRMWARE_BYTES)
        check("GET firmware -> Content-Length = file size", headers["Content-Length"] == str(len(FIRMWARE_BYTES)))
        status, headers, body = request(base, "/firmware.img", method="HEAD")
        check("HEAD firmware -> 200, size, no body",
              status == HTTPStatus.OK and headers["Content-Length"] == str(len(FIRMWARE_BYTES)) and body == b"")

        status, _, _ = request(base, f"/report?step={STEP}&result={RESULT}")
        check("GET /report with step + result -> 200", status == HTTPStatus.OK)
        check("peek_report sees it without consuming", FO.peek_report(STEP) == RESULT == FO.peek_report(STEP))
        check("wait_for_report returns and consumes it",
              FO.wait_for_report(STEP, timeout=REPORT_WAIT_S) == RESULT and FO.peek_report(STEP) is None)
        for what, path in (("no result", f"/report?step={STEP}"), ("no step", f"/report?result={RESULT}")):
            status, _, _ = request(base, path)
            check(f"GET /report, {what} -> 400, nothing stored",
                  status == HTTPStatus.BAD_REQUEST and FO.peek_report(STEP) is None)

        status, _, _ = request(base, f"/report?step={STEP}", method="POST", body=LOG_BODY.encode())
        check("POST /report with a body -> 200, body stored as text",
              status == HTTPStatus.OK and FO.wait_for_report(STEP, timeout=REPORT_WAIT_S) == LOG_BODY)
        status, _, _ = request(base, f"/report?step={STEP}", method="POST", body=b"")
        check("POST /report, empty body -> stored as ''", status == HTTPStatus.OK and FO.peek_report(STEP) == "")
        status, _, _ = request(base, "/report", method="POST", body=b"x")
        check("POST /report, no step -> 400", status == HTTPStatus.BAD_REQUEST)
        status, _, _ = request(base, "/elsewhere", method="POST", body=b"x")
        check("POST to another path -> 404", status == HTTPStatus.NOT_FOUND)
        check("no report -> wait_for_report gives None after the timeout",
              FO.wait_for_report("never", timeout=NO_REPORT_WAIT_S) is None)

        fw.unlink()
        status, _, _ = request(base, "/firmware.img")
        check("GET, firmware file gone -> 404", status == HTTPStatus.NOT_FOUND)
        status, _, _ = request(base, "/firmware.img", method="HEAD")
        check("HEAD, firmware file gone -> 404", status == HTTPStatus.NOT_FOUND)
    finally:
        srv.shutdown()
        srv.server_close()


# ----------------------------------------------------------------------------
section("detect_host_ip")
fake_sock = MagicMock()
fake_sock.__enter__.return_value.getsockname.return_value = (HOST_IP, 0)
with patch.object(FO.socket, "socket", return_value=fake_sock):
    check("route found -> that interface's IPv4", FO.detect_host_ip() == HOST_IP)
fake_sock.__enter__.return_value.connect.side_effect = OSError("network unreachable")
with patch.object(FO.socket, "socket", return_value=fake_sock):
    check("no route -> '' (caller must fail loudly)", FO.detect_host_ip() == "")


# ----------------------------------------------------------------------------
section("phase1_uboot / phase1_recovery error branches")
d = MagicMock()
d.connect.return_value = True
d.probe_uboot_prompt.return_value = False
d.wait_for_autoboot.side_effect = TimeoutError("no autoboot")
with patch.object(FO, "detect_serial_ports", return_value=([SimpleNamespace(device=PORT)], [])), \
     patch.object(FO, "SerialDevice", return_value=d), patch.object(FO, "with_spinner", run_spinner), \
     contextlib.redirect_stdout(io.StringIO()):
    res = FO.phase1_uboot(PORT)
check("autoboot wait raises -> None, disconnected", res is None and d.disconnect.called)
check("step 3 recorded as failed", FO.results[-1][1] == "U-Boot autoboot interrupted" and FO.results[-1][2] is False)

d = MagicMock()
d.boot_recovery.return_value = True
d.login_recovery.side_effect = TimeoutError("no shell")
with patch.object(FO, "with_spinner", run_spinner):
    res = FO.phase1_recovery(d)
check("login raises -> None, disconnected", res is None and d.disconnect.called)
check("step 5 recorded as failed", FO.results[-1][2] is False and "Logged into recovery shell" in FO.results[-1][1])


# ----------------------------------------------------------------------------
section("run_flash_journey")


class FakeCtx:
    """The journey context: a .get() store plus attributes."""
    def __init__(self, store=None, raises=False):
        self.store, self.raises, self.device_ip, self.device_net = store or {}, raises, "", None

    def get(self, key):
        if self.raises:
            raise KeyError(key)
        return self.store.get(key)


def journey_run(*, uboot=True, uboot_steps=True, recovery=True, steps=(NETWORK_STEP,), network=True,
                run_ok=True, firmware=".", store=None, ctx_raises=False):
    d = MagicMock()
    journey = MagicMock()
    journey.run_uboot_steps.return_value = uboot_steps
    journey.run.return_value = run_ok
    journey.ctx = FakeCtx(store, ctx_raises)
    setup = MagicMock(return_value=network)
    out = io.StringIO()
    with patch.object(FO, "phase1_uboot", return_value=d if uboot else None), \
         patch.object(FO, "phase1_recovery", return_value=d if recovery else None) as rec, \
         patch.object(FO, "print_report", return_value=REPORT_VERDICT), \
         patch.object(J, "get_journey", return_value=journey) as gj, \
         patch.object(SR, "get_staging_boot_methods", return_value={"boot_method": "boot_recovery"}), \
         patch.object(SR, "list_journey", return_value=list(steps)), contextlib.redirect_stdout(out):
        res = FO.run_flash_journey(PORT, OS_NAME, "LAN", HOST_IP, HTTP_PORT, firmware,
                                   lambda: DEVICE_NET, setup)
    return SimpleNamespace(res=res, out=out.getvalue(), d=d, journey=journey, setup=setup, rec=rec, gj=gj)


r = journey_run(uboot=False)
check("no U-Boot -> None, journey never built", r.res is None and "Bootstrap FAILED" in r.out and not r.gj.called)

r = journey_run(uboot_steps=False)
check("U-Boot steps fail -> None, recovery not booted", r.res is None and "U-Boot setup FAILED" in r.out
      and not r.rec.called and r.d.disconnect.called)
check("journey built with the device network IP", r.gj.call_args.kwargs["device_ip"] == DEVICE_NET["ip"])

r = journey_run(recovery=False)
check("staging boot fails -> None", r.res is None and "Bootstrap FAILED" in r.out and not r.journey.run.called)
check("staging boot uses the journey's boot methods", r.rec.call_args.kwargs == {"boot_method": "boot_recovery"})

r = journey_run(network=False)
check("network needed but fails -> None, nothing flashed",
      r.res is None and "network setup FAILED" in r.out and not r.journey.run.called)

r = journey_run(firmware="C:/fw/Armbian.img.xz")
check("full run -> print_report() verdict returned", r.res == REPORT_VERDICT)
check("network resolved after boot is forwarded into the journey",
      r.journey.ctx.device_net == DEVICE_NET and r.journey.ctx.device_ip == DEVICE_NET["ip"])
check("summary shows firmware path and device IP",
      "Firmware:    C:/fw/Armbian.img.xz" in r.out and f"Device IP:   {DEVICE_NET['ip']}" in r.out)
check("success message, device disconnected", "Flashing completed successfully" in r.out and r.d.disconnect.called)

r = journey_run(steps=(), run_ok=False)
check("no network step -> network setup skipped", not r.setup.called)
check("USB journey: firmware shown as auto-detected, no device IP",
      "auto-detected from USB" in r.out and "(not needed for this journey)" in r.out)
check("journey fails -> failure message, verdict still returned",
      "did not complete successfully" in r.out and r.res == REPORT_VERDICT)

with tempfile.TemporaryDirectory() as tmp:
    rootfs = Path(tmp) / "rootfs.img"
    rootfs.write_bytes(b"x")
    server = MagicMock()
    r = journey_run(store={"http_server": server, "extracted_rootfs": str(rootfs)})
    check("cleanup: journey's HTTP server stopped", server.shutdown.called)
    check("cleanup: extracted rootfs deleted", not rootfs.exists())

r = journey_run(ctx_raises=True)
check("cleanup: errors reading the context are swallowed", r.res == REPORT_VERDICT and r.d.disconnect.called)


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
