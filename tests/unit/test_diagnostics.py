"""
mono-imager: diagnostics.py — the menu's "Test Serial", "Test LAN" and
"Test USB stick", every branch, no hardware.

The serial device is a fake with a scripted receive buffer; flash_orchestrator
(bootstrap, host IP, HTTP server, report wait) and usb_utils (mount, size,
image search) are patched where diagnostics imports them. The spinner runs the
wrapped call directly, input() returns at once and printed output is captured,
so each test checks both the return value and what the user sees.

Run: python tests/unit/test_diagnostics.py
"""

import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import constants as C
from mono_imager import diagnostics as D
from mono_imager import flash_orchestrator as FO
from mono_imager.journeys import usb_utils as U

PORT = "COM5"
HOST_IP = "192.168.168.89"
DETECTED_IP = "192.168.168.108"
DEVICE_IP = "192.168.168.200"
HTTP_OK = "200"
HTTP_NOT_FOUND = "404"
SHORT_BOOT_WAIT_S = 0.05          # stands in for RECOVERY_BOOT_WAIT when nothing ever boots
UBOOT_RESPONSE = "ethact=fm1-mac3\n"
USB_PART = "/dev/sda2"
ARMBIAN_IMAGE = "/mnt/usb/Armbian_24.5_mono.img.xz"
SERIAL_CHECKS = 5                 # connect, autoboot, U-Boot responds, booted, shell
LAN_CHECKS = 5                    # recovery, host IP, network, HTTP server, reachability

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


def run_spinner(fn, *args, message="", **kwargs):
    """with_spinner() without the thread: (result, error) like the real one."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - mirrors with_spinner(), which captures any error
        return None, e


class FakeSer:
    """pyserial stand-in: read(1) hands out `rx` byte by byte, then b''."""
    def __init__(self, rx=b"", after_enter=b""):
        self.rx, self.after_enter, self.written = bytearray(rx), after_enter, []

    def read(self, n):
        if n == 1:
            return bytes([self.rx.pop(0)]) if self.rx else b""
        out, self.after_enter = self.after_enter[:n], self.after_enter[n:]
        return out

    @property
    def in_waiting(self):
        return len(self.after_enter)

    def write(self, data):
        self.written.append(data)


def fake_device(*, connect=True, autoboot=True, autoboot_raises=False, response=UBOOT_RESPONSE,
                rx=b"", after_enter=b""):
    d = MagicMock()
    d.connect.return_value = connect
    if autoboot_raises:
        d.wait_for_autoboot.side_effect = TimeoutError("no autoboot prompt")
    else:
        d.wait_for_autoboot.return_value = autoboot
    d.send_command.side_effect = lambda cmd, **kw: response if cmd.startswith("printenv") else ""
    d.ser = FakeSer(rx, after_enter)
    return d


PAUSES: list[int] = []                       # "Press Enter" prompts per run: every outcome must pause once


def quiet(prompt):
    """Patches every diagnostics test needs: no spinner thread, no sleeps, no keypress."""
    return [patch.object(D, "with_spinner", run_spinner), patch.object(D, "Spinner", MagicMock()),
            patch.object(D.time, "sleep"), patch("builtins.input", prompt)]


def run(fn, *args, patches=(), **kwargs):
    """Call fn under quiet() + patches; return (result, printed text)."""
    out = io.StringIO()
    prompt = MagicMock(return_value="")
    with contextlib.ExitStack() as es:
        for p in [*quiet(prompt), *patches]:
            es.enter_context(p)
        with contextlib.redirect_stdout(out):
            result = fn(*args, **kwargs)
    PAUSES.append(prompt.call_count)
    return result, out.getvalue()


def serial(d, **kw):
    return run(D.test_serial, PORT, patches=[patch("mono_imager.serial_device.SerialDevice",
                                                   return_value=d)], **kw)


# ----------------------------------------------------------------------------
section("test_serial")
d = fake_device(connect=False)
ok, out = serial(d)
check("connect fails -> False, nothing else tried", ok is False and not d.wait_for_autoboot.called)
check("connect fails -> shown as failed", f"✗  Connect at {C.SERIAL_BAUD} baud" in out)

for what, d in (("autoboot not interrupted", fake_device(autoboot=False)),
                ("autoboot raises", fake_device(autoboot_raises=True))):
    ok, out = serial(d)
    check(f"{what} -> True (port works), stops early", ok is True and not d.send_command.called)
    check(f"{what} -> failure shown, device disconnected",
          "✗  U-Boot autoboot interrupted" in out and d.disconnect.called)

d = fake_device(rx=b"... login:", after_enter=b"root@recovery:~# ")
ok, out = serial(d)
check("login prompt -> logs in as root", d.ser.written[0] == b"root\r\n")
check("full run -> True, all checks pass", ok is True and f"All {SERIAL_CHECKS} checks passed" in out)
check("full run -> 'run recovery' sent", any(c.args[0] == "run recovery" for c in d.send_command.call_args_list))

d = fake_device(rx=b"booting\r\nroot@recovery:~# ")
ok, out = serial(d)
check("shell prompt straight away -> no login typed", b"root\r\n" not in d.ser.written)
check("shell prompt in boot buffer counts as logged in", "✓  Logged into recovery shell" in out)

d = fake_device(response="   ")
with patch.object(D, "RECOVERY_BOOT_WAIT", SHORT_BOOT_WAIT_S):
    ok, out = serial(d)
check("blank U-Boot reply -> 'no response'", "✗  U-Boot responds to commands" in out and "no response" in out)
check("never boots -> boot check fails, shell check skipped",
      "✗  Recovery Linux booted" in out and "Logged into recovery shell" not in out)
check("failures counted in the summary", "2/4 checks failed" in out and ok is True)

d = fake_device(rx=b"login:", after_enter=b"")
ok, out = serial(d)
check("login seen but no shell prompt after Enter -> shell check fails",
      "✗  Logged into recovery shell" in out and "1/5 checks failed" in out)


# ----------------------------------------------------------------------------
section("test_lan")


def lan(*, bootstrap=True, host_ip=HOST_IP, detected=DETECTED_IP, network=True, device_net=None,
        server=True, launch_raises=False, report=HTTP_OK, report_error=None):
    d = MagicMock() if bootstrap else None
    if d is not None and launch_raises:
        d.launch_script.side_effect = OSError("serial write failed")
    srv = MagicMock() if server else None
    served = {}

    def start(ip, port, path):
        served.update(ip=ip, port=port, path=Path(path), existed=Path(path).is_file())
        return srv

    def wait(step, timeout=None):
        if report_error:
            raise report_error
        return report

    net = {"ip": DEVICE_IP} if device_net is None else device_net
    soft = MagicMock()
    patches = [patch.object(FO, "phase1_bootstrap", return_value=d),
               patch.object(FO, "detect_host_ip", return_value=detected),
               patch.object(FO, "start_http_server", side_effect=start),
               patch.object(FO, "wait_for_report", side_effect=wait)]
    res, out = run(D.test_lan, PORT, host_ip, soft, lambda dev: network, lambda: net, patches=patches)
    return res, out, d, srv, served, soft


res, out, d, *_ = lan(bootstrap=False)
check("no recovery shell -> None", res is None and "✗  Device in recovery shell" in out)

res, out, d, srv, served, soft = lan(host_ip=None, detected=None)
check("soft reboot requested for the port", soft.call_args.args == (PORT,))
check("no host IP -> None, device disconnected", res is None and "could not detect" in out and d.disconnect.called)

res, out, d, *_ = lan(network=False)
check("network setup fails -> None", res is None and "✗  Device network ready" in out and d.disconnect.called)

res, out, d, srv, served, _ = lan(server=False)
check("HTTP server fails -> None", res is None and f"✗  HTTP server up on {HOST_IP}" in out)
check("HTTP server fails -> temp file removed", served["existed"] and not served["path"].exists())

res, out, d, srv, served, _ = lan()
check("full run -> port, host IP and device IP returned",
      res == {"serial_port": PORT, "host_ip": HOST_IP, "device_ip": DEVICE_IP})
check("full run -> serves a temp file on the LAN test port",
      served["port"] == C.LANTEST_HTTP_PORT and served["existed"])
check("full run -> server stopped, temp file removed", srv.shutdown.called and not served["path"].exists())
check("full run -> device script curls the host", f"http://{HOST_IP}:{C.LANTEST_HTTP_PORT}/firmware.img"
      in d.launch_script.call_args.args[0])
check(f"full run -> all {LAN_CHECKS} checks pass", "LAN path confirmed end-to-end" in out)

res, out, *_ = lan(host_ip=None)
check("host IP detected when not given", res is not None and res["host_ip"] == DETECTED_IP)

for what, kw in ((f"HTTP {HTTP_NOT_FOUND}", {"report": HTTP_NOT_FOUND}),
                 ("no report", {"report": None}),
                 ("report wait raises", {"report_error": TimeoutError("no report")}),
                 ("launch_script raises", {"launch_raises": True})):
    res, out, d, srv, served, _ = lan(**kw)
    check(f"{what} -> unreachable, values still returned",
          res is not None and "✗  Device can reach host HTTP server" in out and "Firewall" in out)
    check(f"{what} -> server stopped, temp file removed", srv.shutdown.called and not served["path"].exists())

res, out, *_ = lan(device_net={})
check("no device network info -> device_ip None", res is not None and res["device_ip"] is None)


# ----------------------------------------------------------------------------
section("test_usb")


def usb(*, bootstrap=True, mount_part=USB_PART, mount_raises=False, images=None, umount_raises=False):
    d = MagicMock() if bootstrap else None
    if d is not None and umount_raises:
        d.send_command.side_effect = OSError("serial gone")
    images = images or {}

    def mount(dev, device, mnt, os_names):
        if mount_raises:
            raise OSError("mount script failed")
        return (mount_part, bool(images), "")

    size = MagicMock()
    patches = [patch.object(FO, "phase1_bootstrap", return_value=d),
               patch.object(U, "mount_usb_stick", side_effect=mount),
               patch.object(U, "check_usb_size", size),
               patch.object(U, "find_image_on_usb",
                            side_effect=lambda dev, mnt, os_name: (images.get(os_name), "img.xz"))]
    res, out = run(D.test_usb, PORT, patches=patches)
    return res, out, d, size


res, out, d, _ = usb(bootstrap=False)
check("no recovery shell -> False", res is False and "✗  Device in recovery shell" in out)

for what, kw in (("mount raises", {"mount_raises": True}), ("nothing mountable", {"mount_part": None})):
    res, out, d, size = usb(**kw)
    check(f"{what} -> False, explained", res is False and "not FAT32/exFAT" in out and not size.called)
    check(f"{what} -> still unmounts and disconnects",
          "umount /mnt/usb" in d.send_command.call_args.args[0] and d.disconnect.called)

res, out, d, size = usb(images={"Armbian": ARMBIAN_IMAGE})
check("image found -> True, size checked", res is True and size.called)
check("found image listed by file name", f"✓  Armbian image — {Path(ARMBIAN_IMAGE).name}" in out)
check("missing images listed as not found", "·  OPNsense image (not found)" in out)
check("all checks pass", "USB stick mounted and verified" in out)

res, out, d, _ = usb()
check("mounted but no image -> True (stick works), check fails",
      res is True and "✗  At least one recognizable OS image on stick" in out and "1/3 checks failed" in out)

res, out, d, _ = usb(umount_raises=True, images={"Armbian": ARMBIAN_IMAGE})
check("unmount error -> warning shown, still disconnected",
      "USB unmount warning: serial gone" in out and d.disconnect.called and res is True)


# ----------------------------------------------------------------------------
section("every outcome waits for Enter exactly once")
check(f"{len(PAUSES)} runs, each paused once (counts: {sorted(set(PAUSES))})", set(PAUSES) == {1})


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
