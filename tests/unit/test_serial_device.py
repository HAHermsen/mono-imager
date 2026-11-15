#!/usr/bin/env python3
"""
mono-imager: Unit tests for serial permission handling (issue #15).

No hardware required. serial.Serial is mocked.

What this tests:
  - _is_permission_error(): classify permission-denied vs other open errors
  - wait_for_port(): fail FAST on permission-denied instead of retrying the
    full timeout and reporting a misleading "did not appear"; loop-then-
    timeout on a genuinely absent port; succeed when the port opens.

Run: python tests/unit/test_serial_device.py
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import serial
from mono_imager.serial_device import SerialDevice, _is_permission_error

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


# ============================================================================
# _is_permission_error()
# ============================================================================

print("=" * 60)
print("_is_permission_error()")
print("=" * 60)

check("PermissionError -> True", _is_permission_error(PermissionError("nope")))

_e_msg = serial.SerialException(
    "could not open port /dev/ttyUSB0: [Errno 13] Permission denied"
)
check("SerialException with 'Permission denied' message -> True",
      _is_permission_error(_e_msg))

_e_errno = serial.SerialException("boom")
_e_errno.errno = 13
check("SerialException with errno 13 -> True", _is_permission_error(_e_errno))

check("SerialException 'No such file' -> False",
      not _is_permission_error(serial.SerialException("[Errno 2] No such file or directory")))

check("unrelated exception -> False", not _is_permission_error(ValueError("x")))


# ============================================================================
# wait_for_port(): permission-denied fails fast (#15)
# ============================================================================

print()
print("=" * 60)
print("wait_for_port() permission handling (#15)")
print("=" * 60)

# Permission denied must NOT be retried for the whole timeout — it returns
# immediately so the real cause is surfaced instead of "did not appear".
dev = SerialDevice("/dev/ttyTEST")
with patch("serial.Serial", side_effect=serial.SerialException("[Errno 13] Permission denied")), \
     patch("mono_imager.serial_device.verbose"):
    t0 = time.time()
    _ok = dev.wait_for_port(timeout=30)
    _elapsed = time.time() - t0
check("permission-denied -> returns False", _ok is False)
check("permission-denied -> returns fast (no 30s retry loop)", _elapsed < 1.0)

# A genuinely absent port keeps polling until the (short) timeout, then False.
dev = SerialDevice("/dev/ttyTEST")
with patch("serial.Serial", side_effect=serial.SerialException("[Errno 2] No such file or directory")), \
     patch("mono_imager.serial_device.verbose"):
    t0 = time.time()
    _ok = dev.wait_for_port(timeout=0.2)
    _elapsed = time.time() - t0
check("absent port -> False only after the timeout elapses", _ok is False and _elapsed >= 0.2)

# Port that opens cleanly -> True.
dev = SerialDevice("/dev/ttyTEST")
with patch("serial.Serial", return_value=MagicMock()), \
     patch("mono_imager.serial_device.verbose"):
    check("port opens -> returns True", dev.wait_for_port(timeout=5) is True)


# ============================================================================
# Fake serial port: a scripted device behind the real SerialProxy
# ============================================================================
# Everything below drives the REAL SerialDevice code paths through the real
# SerialProxy -> safe_read/safe_write layer. FakeSerial answers writes via a
# `responder`; reads never block (empty buffer -> b"" after 1 ms). Timing
# constants are shrunk so the poll loops finish in milliseconds.

import subprocess
import shutil as _sh_which

# a real POSIX sh: /bin/sh, or sh.exe from Git for Windows; None -> those sections are skipped
SH = "/bin/sh" if Path("/bin/sh").exists() else _sh_which.which("sh")
import contextlib
import mono_imager.serial_device as SDM
from mono_imager import constants as C
from mono_imager.serial_device import SerialProxy

PROMPT = b"root@recovery:~# "


class FakeSerial:
    def __init__(self, responder=None, initial=b""):
        self.buf = bytearray(initial)
        self.written = []
        self.is_open = True
        self.resets = 0
        self.responder = responder
        self.raise_on = {}          # method -> list of exceptions, raised in order

    def _maybe_raise(self, what):
        q = self.raise_on.get(what)
        if q:
            raise q.pop(0)

    def feed(self, b):
        self.buf += b

    def write(self, data):
        self._maybe_raise("write")
        self.written.append(bytes(data))
        if self.responder:
            self.responder(self, bytes(data))
        return len(data)

    def read(self, n=1):
        self._maybe_raise("read")
        if not self.buf:
            time.sleep(0.001)
            return b""
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_all(self):
        self._maybe_raise("read_all")
        out = bytes(self.buf)
        self.buf.clear()
        return out

    @property
    def in_waiting(self):
        self._maybe_raise("in_waiting")
        return len(self.buf)

    def reset_input_buffer(self):
        self.resets += 1
        self.buf.clear()

    def close(self):
        self.is_open = False


def attach(fake, port="/dev/ttyFAKE"):
    d = SerialDevice(port)
    d.ser = SerialProxy(d, fake)
    d.baud_rate = 115200
    return d


def raw(fake, port="/dev/ttyFAKE"):
    """No proxy: a SerialException reaches the method's own except branch
    (through the proxy, safe_read() absorbs it and reconnects)."""
    d = SerialDevice(port)
    d.ser = fake
    return d


def failing_reads(n=1000, data=b""):
    f = FakeSerial(initial=data)
    f.raise_on["read"] = [serial.SerialException("read failed")] * n
    return f


FAST = {"LINE_SETTLE": 0.01, "POLL_SLEEP": 0.001, "LINE_IDLE_MAX_WAIT": 0.3,
        "LINE_IDLE_SHORT_WAIT": 0.2, "INTERRUPT_KEY_GAP": 0, "INTERRUPT_SETTLE": 0,
        "INTERRUPT_PROMPT_TIMEOUT": 0.3, "BOOT_POLL_TIMEOUT": 1.0, "LOGIN_STEP_TIMEOUT": 0.3,
        "STAGING_LOGIN_SETTLE": 0, "STAGING_PW_SETTLE": 0, "BACKOFF_START": 0.01,
        "BACKOFF_MAX": 0.02, "ENTER_SETTLE": 0, "PORT_POLL_SLEEP": 0.01, "OUTPUT_POLL_SLEEP": 0.001,
        "BAUD_PROBE_SETTLE": 0, "RETRY_PORT_TIMEOUT": 0.05, "RECONNECT_PORT_TIMEOUT": 0.05,
        "PORT_APPEAR_TIMEOUT": 0.05}
said = []


@contextlib.contextmanager
def fast():
    """Shrunk timings + verbose()/logger captured into `said`."""
    said.clear()
    with contextlib.ExitStack() as es:
        for k, v in FAST.items():
            es.enter_context(patch.object(SDM, k, v))
        es.enter_context(patch.object(SDM, "verbose", lambda m, *a, **k: said.append(str(m))))
        es.enter_context(patch.object(SDM.logger, "warning", lambda m, *a, **k: said.append(str(m))))
        es.enter_context(patch.object(SDM.logger, "error", lambda m, *a, **k: said.append(str(m))))
        yield


def heard(text):
    return any(text in s for s in said)


class ShellSim:
    """Recovery shell over serial: heredoc writes land in `files`, `wc -c`
    reports their size, `sh FILE` really runs the file with /bin/sh."""

    def __init__(self, drop=0, wc=None):
        self.files, self.ran, self.drop, self.wc = {}, [], drop, wc

    def __call__(self, fake, data):
        s = data.decode()
        if s.startswith("cat > "):
            head, rest = s.split("\n", 1)
            path, tag = head.split()[2], head.split("<<")[1].strip("'")
            content = rest[:rest.rindex("\n" + tag)] + "\n"
            self.files[path] = content[self.drop:]
            fake.feed(b"> \r\n" * 2 + PROMPT)
        elif s.startswith("wc -c < "):
            path = s.split()[3]
            out = self.wc if self.wc is not None else str(len(self.files[path].encode()))
            fake.feed(s.strip().encode() + b"\r\n" + out.encode() + b"\r\n" + PROMPT)
        elif s.startswith("sh "):
            path = s.split()[1]
            self.ran.append(path)
            r = subprocess.run([SH, "-c", self.files[path]], capture_output=True, text=True)
            fake.feed(s.strip().encode() + b"\r\n" + (r.stdout + r.stderr).replace("\n", "\r\n").encode() + PROMPT)
        elif s.startswith("rm -f "):
            self.files.pop(s.split()[2], None)
            fake.feed(s.strip().encode() + b"\r\n" + PROMPT)
        else:
            fake.feed(s.strip().encode() + b"\r\n" + PROMPT)


def section(t):
    print()
    print("=" * 60)
    print(t)
    print("=" * 60)


# ----------------------------------------------------------------------------
section("_has_prompt / disconnect / SerialProxy")
d = SerialDevice("/dev/ttyFAKE")
check("U-Boot '=>' is a prompt", d._has_prompt(b"foo\r\n=> "))
check("bare '# ' is a prompt", d._has_prompt(b"x # "))
check("recovery prompt is a prompt", d._has_prompt(b"..." + PROMPT))
check("login text is not a prompt", not d._has_prompt(b"gateway login: "))
with fast():
    d.disconnect()
check("disconnect() without a port: no error", True)
f = FakeSerial()
d = attach(f)
with fast():
    d.disconnect()
check("disconnect() closes an open port", f.is_open is False and heard("Serial connection closed"))
f = FakeSerial(initial=b"abc")
d = attach(f)
check("proxy: in_waiting passes through", d.ser.in_waiting == 3)
check("proxy: read() goes through safe_read", d.ser.read(2) == b"ab")
check("proxy: read_all()", d.ser.read_all() == b"c")
check("proxy: write() goes through safe_write", d.ser.write(b"xy") == 2 and f.written == [b"xy"])
check("proxy: unknown attributes forwarded (is_open)", d.ser.is_open is True)
p = SerialProxy.__new__(SerialProxy)
try:
    p.anything
    check("proxy without _ser: AttributeError (no recursion)", False)
except AttributeError as e:
    check("proxy without _ser: AttributeError (no recursion)", "not initialised" in str(e))
f = FakeSerial()
f.raise_on["in_waiting"] = [OSError("gone"), OSError("still gone")]
d = attach(f)
with fast(), patch.object(d, "_attempt_reconnect", return_value=False):
    check("proxy: in_waiting error -> reconnect -> still failing -> 0", d.ser.in_waiting == 0)

# ----------------------------------------------------------------------------
section("safe_write / safe_read / safe_read_all: reconnect on failure")
for meth, call, ok_val, fail_val in (("write", lambda d: d.safe_write(b"hi"), 2, None),
                                     ("read", lambda d: d.safe_read(4), b"data", b""),
                                     ("read_all", lambda d: d.safe_read_all(), b"data", b"")):
    f = FakeSerial(initial=b"data")
    f.raise_on[meth] = [OSError("io")]
    d = attach(f)
    with fast(), patch.object(d, "_attempt_reconnect", return_value=True) as rc:
        check(f"{meth}: error, reconnect OK -> retried", call(d) == ok_val and rc.called)
    f = FakeSerial(initial=b"data")
    f.raise_on[meth] = [OSError("io")]
    d = attach(f)
    with fast(), patch.object(d, "_attempt_reconnect", return_value=False):
        check(f"{meth}: error, reconnect fails -> {fail_val!r}", call(d) == fail_val)
    f = FakeSerial(initial=b"data")
    f.raise_on[meth] = [OSError("io"), OSError("io again")]
    d = attach(f)
    with fast(), patch.object(d, "_attempt_reconnect", return_value=True):
        check(f"{meth}: error, reconnect OK, retry fails -> {fail_val!r}", call(d) == fail_val)

# ----------------------------------------------------------------------------
section("connect / _attempt_reconnect")
d = SerialDevice("/dev/ttyFAKE")
with fast(), patch.object(d, "wait_for_port", return_value=False):
    check("connect(): port never appears -> False", d.connect() is False and heard("did not appear"))
silent, talky = FakeSerial(), FakeSerial(responder=lambda f, b: f.feed(b"=> "))
opened = []


def fake_open(seq):
    it = iter(seq)

    def mk(**kw):
        opened.append(kw["baudrate"])
        x = next(it)
        if isinstance(x, Exception):
            raise x
        return x
    return mk


d = SerialDevice("/dev/ttyFAKE")
with fast(), patch.object(d, "wait_for_port", return_value=True), \
     patch("serial.Serial", side_effect=fake_open([silent, serial.SerialException("busy"), talky])):
    ok = d.connect()
check("connect(): silent rate closed, error skipped, 3rd rate answers",
      ok and opened == C.SERIAL_BAUD_CANDIDATES[:3] and silent.is_open is False
      and d.baud_rate == C.SERIAL_BAUD_CANDIDATES[2] and isinstance(d.ser, SerialProxy))
opened.clear()
d = SerialDevice("/dev/ttyFAKE")
with fast(), patch.object(d, "wait_for_port", return_value=True), \
     patch("serial.Serial", side_effect=fake_open([FakeSerial()])):
    ok = d.connect(baud_rate=57600)
check("connect(baud): only that rate tried; no answer -> False", not ok and opened == [57600])
d = SerialDevice("/dev/ttyFAKE")
with fast(), patch.object(d, "wait_for_port", return_value=False):
    check("_attempt_reconnect(): port missing -> False", d._attempt_reconnect() is False)
d = SerialDevice("/dev/ttyFAKE")
with fast(), patch.object(d, "wait_for_port", return_value=True), \
     patch.object(d, "connect", return_value=True) as cn:
    ok = d._attempt_reconnect()
check("_attempt_reconnect(): unknown baud -> falls back to 115200 with a warning",
      ok and cn.call_args[0][0] == C.SERIAL_BAUD and heard("falling back to 115200"))
d.baud_rate = 9600
with fast(), patch.object(d, "wait_for_port", return_value=True), \
     patch.object(d, "connect", return_value=True) as cn:
    d._attempt_reconnect()
check("_attempt_reconnect(): reuses the detected baud", cn.call_args[0][0] == 9600)

# ----------------------------------------------------------------------------
section("probe_uboot_prompt / verify_recovery_shell")
d = SerialDevice("/dev/ttyFAKE")
check("probe: no port -> False", d.probe_uboot_prompt() is False)
check("verify: no port -> False", d.verify_recovery_shell() is False)
with fast():
    f = FakeSerial(responder=lambda f, b: f.feed(b"\r\n=> "), initial=b"stale")
    d = attach(f)
    check("probe: '=>' answered -> True (stale input flushed first)", d.probe_uboot_prompt(0.5) and f.resets == 1)
    d = attach(FakeSerial(responder=lambda f, b: f.feed(PROMPT)))
    check("probe: recovery shell's '# ' is NOT a U-Boot prompt", d.probe_uboot_prompt(0.2) is False)
    f = FakeSerial()
    f.raise_on["write"] = [serial.SerialException("x"), serial.SerialException("x")]
    d = attach(f)
    with patch.object(d, "_attempt_reconnect", return_value=True):
        check("probe: write keeps failing -> False", d.probe_uboot_prompt(0.2) is False)
    t0 = time.time()
    check("probe: SerialException on read -> False at once",
          raw(failing_reads()).probe_uboot_prompt(2) is False and time.time() - t0 < 1)
    t0 = time.time()
    check("verify: SerialException on read -> False at once",
          raw(failing_reads()).verify_recovery_shell(2) is False and time.time() - t0 < 1)
    f = FakeSerial()
    f.raise_on["write"] = [serial.SerialException("w")]
    check("probe: SerialException on write -> False", raw(f).probe_uboot_prompt(0.5) is False)
    d = attach(FakeSerial(responder=lambda f, b: f.feed(b"\r\n" + PROMPT)))
    check("verify: recovery prompt -> True", d.verify_recovery_shell(0.5) is True)
    d = attach(FakeSerial(responder=lambda f, b: f.feed(b"\r\n=> ")))
    check("verify: U-Boot prompt -> False", d.verify_recovery_shell(0.2) is False)
    d = attach(FakeSerial(responder=lambda f, b: f.feed(b"\r\nroot@OpenWrt:~# ")))
    check("verify: another root shell is not recovery -> False", d.verify_recovery_shell(0.2) is False)

# ----------------------------------------------------------------------------
section("_wait_for_line_idle")
with fast():
    f = FakeSerial(initial=b"leftover")
    d = attach(f)
    check("quiet line -> True, leftover drained", d._wait_for_line_idle(0.02, 0.5) and not f.buf)


class Chatty(FakeSerial):
    @property
    def in_waiting(self):
        self.buf += b"x"
        return len(self.buf)


with fast():
    d = attach(Chatty())
    t0 = time.time()
    check("never quiet -> False at max_wait", d._wait_for_line_idle(0.05, 0.2) is False and time.time() - t0 >= 0.2)
    f = FakeSerial()
    f.raise_on["in_waiting"] = [OSError("x")] * 1000
    d = attach(f)
    with patch.object(d, "_attempt_reconnect", return_value=False):
        check("in_waiting raising counts as quiet -> True", d._wait_for_line_idle(0.02, 0.5) is True)

# ----------------------------------------------------------------------------
section("send_command")
d = SerialDevice("/dev/ttyFAKE")
try:
    d.send_command("ls")
    check("not open -> RuntimeError", False)
except RuntimeError:
    check("not open -> RuntimeError", True)
with fast():
    f = FakeSerial(responder=lambda f, b: f.feed(b"printenv\r\nbootcmd=run x\r\nbootcmd=run x\r\n#\r\nfoo=1\r\n=> "),
                   initial=b"stale")
    d = attach(f)
    out = d.send_command("printenv")
    check("echo stripped, prompt lines dropped, duplicate lines collapsed",
          out == "bootcmd=run x\nfoo=1" and f.resets == 1 and f.written == [b"printenv\r\n"])
    d = attach(FakeSerial(responder=lambda f, b: f.feed(b"uname\r\nLinux\r\n" + PROMPT)))
    check("recovery prompt line stripped", d.send_command("uname") == "Linux")
    d = attach(FakeSerial(responder=lambda f, b: f.feed(b"cat x\r\nline\r\n")))
    t0 = time.time()
    out = d.send_command("cat x", timeout=0.2)
    check("no prompt -> returns what arrived after the timeout", out == "line" and time.time() - t0 >= 0.2)
    d = attach(FakeSerial(responder=lambda f, b: f.feed(b"x\r\n=> more=> ")))
    check("wait_for_prompt=False keeps reading to the timeout", "more" in d.send_command("x", wait_for_prompt=False, timeout=0.1))
    f = FakeSerial(responder=lambda f, b: f.feed(b"x\r\nok\r\n=> "))
    f.raise_on["read"] = [serial.SerialException("a"), serial.SerialException("b")]
    d = attach(f)
    with patch.object(d, "_attempt_reconnect", return_value=True):
        check("read error absorbed by safe_read (reconnect), answer still read", d.send_command("x", timeout=0.5) == "ok")
    d = raw(failing_reads())
    t0 = time.time()
    check("raw SerialException -> loop stops at once, returns ''",
          d.send_command("x", timeout=2) == "" and time.time() - t0 < 1)
    f = FakeSerial(responder=lambda f, b: f.feed(b"x\r\nok\r\n=> "))
    d = attach(f)
    out = d.send_command("x")
    check("tail after the prompt is drained (no bleed into the next command)", out == "ok" and not f.buf)

# ----------------------------------------------------------------------------
if SH:
    section("run_script (heredoc write -> wc -c verify -> sh -> cleanup)")
    d = SerialDevice("/dev/ttyFAKE")
    try:
        d.run_script("echo hi")
        check("not open -> RuntimeError", False)
    except RuntimeError:
        check("not open -> RuntimeError", True)
    with fast():
        sim = ShellSim()
        d = attach(FakeSerial(responder=sim))
        body = 'X=$(echo dollar)\necho "v=$X"\necho \'$HOME stays literal\'\necho `echo back`'
        out = d.run_script(body, marker="t1")
        path = "/tmp/mono_imager_t1.sh"
        check("script ran and output de-echoed", out == "v=dollar\n$HOME stays literal\nback")
        check("ran via 'sh /tmp/mono_imager_<marker>.sh'", sim.ran == [path])
        check("temp script removed afterwards", path not in sim.files)
        sim = ShellSim()
        d = attach(FakeSerial(responder=sim))
        out = d.run_script("echo a")
        check("default marker = time in ms", sim.ran and sim.ran[0].startswith("/tmp/mono_imager_1"))
        sim = ShellSim()
        d = attach(FakeSerial(responder=sim))
        check("output lines '#' and duplicates dropped (echo artifacts)",
              d.run_script("echo one; echo one; echo '#'; echo two", marker="t2") == "one\ntwo")
        for what, sim in (("size mismatch (byte lost on the wire)", ShellSim(drop=1)),
                          ("wc -c unparseable", ShellSim(wc="wc: can't open")),
                          ("wc -c empty", ShellSim(wc=""))):
            d = attach(FakeSerial(responder=sim))
            try:
                d.run_script("echo never", marker="bad")
                check(f"{what} -> RuntimeError, not executed", False)
            except RuntimeError as e:
                check(f"{what} -> RuntimeError, not executed", sim.ran == [] and "Refusing to execute" in str(e))
        sim = ShellSim()
        d = attach(FakeSerial(responder=sim))
        real_send = d.send_command

        def send_rm_fails(cmd, **kw):
            if cmd.startswith("rm -f"):
                raise RuntimeError("port gone")
            return real_send(cmd, **kw)
        with patch.object(d, "send_command", side_effect=send_rm_fails):
            out = d.run_script("echo kept", marker="t3")
        check("cleanup failure is non-fatal", out == "kept" and heard("cleanup of /tmp/mono_imager_t3.sh failed"))

        class Busy(FakeSerial):
            @property
            def in_waiting(self):
                if self.written and self.written[-1].startswith(b"wc -c"):
                    self.buf += b"."      # still echoing after the wc -c answer
                return len(self.buf)
        sim = ShellSim()
        d = attach(Busy(responder=sim))
        with patch.object(SDM.logger, "debug") as dbg:
            out = d.run_script("echo late", marker="t4")
        check("line never settles before exec -> logged, still runs", out.endswith("late") and dbg.called)

        class ExecReadFails(FakeSerial):
            def read(self, n=1):
                if self.written and self.written[-1].startswith(b"sh "):
                    raise serial.SerialException("dropped during exec")
                return FakeSerial.read(self, n)
        sim = ShellSim()
        d = raw(ExecReadFails(responder=sim))
        check("SerialException while reading the exec output -> returns what it has ('')",
              d.run_script("echo x", marker="t5") == "" and sim.ran == ["/tmp/mono_imager_t5.sh"])

        class WriteReadFails(FakeSerial):
            def read(self, n=1):
                if self.written and self.written[-1].startswith(b"cat > "):
                    raise serial.SerialException("dropped during write")
                return FakeSerial.read(self, n)
        sim = ShellSim()
        d = raw(WriteReadFails(responder=sim))
        check("SerialException during the heredoc echo -> still verified by wc -c and run",
              d.run_script("echo y", marker="t6") == "y")
        sim = ShellSim()
        d = raw(WriteReadFails(responder=sim))
        check("launch_script: same for the heredoc echo", d.launch_script("echo z", marker="l6") == "/tmp/mono_imager_l6.sh")

        class DrainFails(FakeSerial):
            def read(self, n=1):
                if self.written and self.written[-1].startswith(b"sh "):
                    raise serial.SerialException("gone after exec")
                return FakeSerial.read(self, n)
        sim = ShellSim()
        d = raw(DrainFails(responder=sim))
        t0 = time.time()
        check("launch_script: SerialException in the post-exec drain -> stops draining, still returns",
              d.launch_script("echo q", marker="l7") == "/tmp/mono_imager_l7.sh" and time.time() - t0 < 1.5)

    # ----------------------------------------------------------------------------
    section("launch_script (fire and forget)")
    d = SerialDevice("/dev/ttyFAKE")
    try:
        d.launch_script("echo hi")
        check("not open -> RuntimeError", False)
    except RuntimeError:
        check("not open -> RuntimeError", True)
    with fast():
        sim = ShellSim()
        f = FakeSerial(responder=sim)
        d = attach(f)
        t0 = time.time()
        r = d.launch_script("echo started", marker="l1")
        check("returns the remote path, exec fired, output drained",
              r == "/tmp/mono_imager_l1.sh" and sim.ran == [r] and not f.buf and heard("post-exec drain"))
        check("post-exec drain lasts ~2 s", 1.9 <= time.time() - t0 < 5)
        check("script left on the device (no cleanup for a running job)", r in sim.files)
        for what, sim in (("size mismatch", ShellSim(drop=3)), ("wc -c unparseable", ShellSim(wc="?"))):
            d = attach(FakeSerial(responder=sim))
            try:
                d.launch_script("echo never", marker="lbad")
                check(f"{what} -> RuntimeError, not launched", False)
            except RuntimeError as e:
                check(f"{what} -> RuntimeError, not launched", sim.ran == [] and "Refusing to launch" in str(e))
else:
    section("run_script / launch_script")
    print("  SKIP: no sh found (install Git for Windows or run on Linux/macOS)")


# ----------------------------------------------------------------------------
section("wait_for_autoboot / capture_boot_diagnostics / interrupt_autoboot")
BANNER = b"U-Boot 2025.04\r\nModel: Mono Gateway\r\n[ OK ] fan\r\nHit any key to stop autoboot:  3 "
with fast():
    f = FakeSerial(initial=BANNER, responder=lambda f, b: f.feed(b"\r\n=> ") if b == b" " else None)
    d = attach(f)
    check("autoboot: banner -> interrupt -> '=>' -> True", d.wait_for_autoboot(timeout=1) is True)
    check("autoboot: boot output kept for the inventory", "Model: Mono Gateway" in (d.last_boot_output or ""))
    d = attach(FakeSerial(initial=BANNER))
    check("autoboot: no prompt after the interrupt -> False",
          d.wait_for_autoboot(timeout=1) is False and heard("U-Boot prompt not seen"))
    d = attach(FakeSerial(initial=b"Starting kernel ..."))
    check("autoboot: no countdown -> False", d.wait_for_autoboot(timeout=0.2) is False and heard("not detected"))
    t0 = time.time()
    check("autoboot: SerialException -> False at once",
          raw(failing_reads()).wait_for_autoboot(timeout=2) is False and time.time() - t0 < 1)
    f = FakeSerial(initial=BANNER)
    d = attach(f)
    out = d.capture_boot_diagnostics(timeout=1)
    check("diagnostics: text up to the marker, autoboot NOT interrupted", out and "[ OK ] fan" in out and f.written == [])
    d = attach(FakeSerial(initial=b"partial boot"))
    check("diagnostics: no marker -> None", d.capture_boot_diagnostics(timeout=0.2) is None and heard("timed out"))
    t0 = time.time()
    check("diagnostics: SerialException -> None at once",
          raw(failing_reads()).capture_boot_diagnostics(timeout=2) is None and time.time() - t0 < 1)
    f = FakeSerial(responder=lambda f, b: f.feed(b"\r\n=> "))
    d = attach(f)
    check("interrupt_autoboot: keys sent, prompt -> True",
          d.interrupt_autoboot() is True and f.written == [b" "] * SDM.INTERRUPT_KEY_COUNT)
    d = attach(FakeSerial())
    check("interrupt_autoboot: no prompt -> False", d.interrupt_autoboot() is False)

# ----------------------------------------------------------------------------
section("boot_recovery")


def uboot(script):
    """U-Boot + recovery Linux: `script` maps a written command to its reply."""
    def resp(f, b):
        if b == b"\r\n" and not any(w.startswith(b"run") or w.startswith(b"boot") for w in f.written[:-1]):
            f.feed(b"\r\n=> ")
            return
        for k, v in script.items():
            if b == k:
                f.feed(v)
                return
    return resp


LOGIN = b"Starting kernel ...\r\n...\r\ngateway login: "
cases = [
    ("login, Password:, blank password -> shell",
     {b"run recovery\r\n": LOGIN, b"root\r": b"root\r\nPassword: ", b"\r": b"\r\n" + PROMPT}, True),
    ("login, passwordless build drops straight to a shell",
     {b"run recovery\r\n": LOGIN, b"root\r": b"root\r\n" + PROMPT}, True),
    ("auto-logged in (shell prompt during boot)",
     {b"run recovery\r\n": b"Starting kernel ...\r\n" + PROMPT}, True),
    ("'recovery' not defined -> falls back to 'boot'",
     {b"run recovery\r\n": b"## Error: \"recovery\" not defined\r\n=> ", b"boot\r\n": LOGIN,
      b"root\r": b"Password: ", b"\r": PROMPT}, True),
    ("FIT without FDT -> DTB loaded from NOR, bootm retried",
     {b"run recovery\r\n": b"ERROR: Did not find a cmdline Flattened Device Tree\r\n=> ",
      b"setenv fdt_high 0xffffffffffffffff;sf probe 0;sf read 0x90000000 0x500000 0x20000;"
      b"sf read 0x82000000 0xa00000 0x2000000;bootm 0x82000000 - 0x90000000\r\n": b"Starting kernel\r\n" + PROMPT}, True),
    ("bootm fails -> U-Boot prompt returns -> False",
     {b"run recovery\r\n": b"Wrong Image Format\r\n=> "}, False),
    ("login, password wrong -> False",
     {b"run recovery\r\n": LOGIN, b"root\r": b"Password: ", b"\r": b"Login incorrect\r\n"}, False),
    ("no output at all -> times out -> False", {}, False),
]
for what, script, want in cases:
    with fast():
        f = FakeSerial(responder=uboot(script))
        d = attach(f)
        got = d.boot_recovery()
    check(f"{what}", got is want)
    if "not defined" in what:
        check("  'boot' sent after 'run recovery'", f.written.index(b"boot\r\n") > f.written.index(b"run recovery\r\n"))
    if what.startswith("no output"):
        check("  timeout reported", heard("timed out"))
with fast():
    f = FakeSerial(responder=uboot({b"run recovery\r\n": PROMPT}))
    d = attach(f)
    d.boot_recovery(boot_medium="nor")
check("prompt primed with a bare Enter before any command", f.written[0] == b"\r\n")
check("boot_medium=nor -> bootargs set before 'run recovery'",
      f.written[1] == f'setenv bootargs "{C.RECOVERY_BOOTARGS} boot_medium=nor"\r\n'.encode()
      and f.written[2] == b"run recovery\r\n")
with fast():
    f = FakeSerial(responder=uboot({b"run recovery\r\n": PROMPT}))
    d = attach(f)
    d.boot_recovery()
check("no boot_medium -> bootargs untouched", not any(w.startswith(b"setenv bootargs") for w in f.written))
with fast():
    f = FakeSerial()
    d = attach(f)
    with patch.object(d, "safe_write", side_effect=RuntimeError("cable pulled")):
        check("unexpected error -> False", d.boot_recovery() is False and heard("Failed to boot recovery"))

with fast():
    f = FakeSerial(responder=lambda f, b: f.feed(b"=") if b == b"\r\n" else
                   (f.feed(b"x" * 3000 + b"\r\n" + PROMPT) if b == b"run recovery\r\n" else None))
    d = attach(f)
    check("partial prime answer (no '=> ') then silence -> continues; long boot log logged in parts",
          d.boot_recovery() is True and heard("Boot output:"))
with fast():
    f = FakeSerial()
    f.raise_on["in_waiting"] = [OSError("x")] * 1000
    check("raw port: in_waiting raising counts as quiet", raw(f)._wait_for_line_idle(0.02, 0.5) is True)

# ----------------------------------------------------------------------------
section("boot_linux_staging (Armbian on eMMC)")
STG = b"Armbian 24.5\r\nmono login: "
cases = [
    ("auto-logged in", {b"boot\r\n": b"..." + b"root@mono:~# "}, True),
    ("login, no password", {b"boot\r\n": STG, b"root\r\n": b"root@mono:~# "}, True),
    ("login, empty password", {b"boot\r\n": STG, b"root\r\n": b"Password: ", b"\r\n": b"root@mono:~# "}, True),
    ("login, Armbian default 1234",
     {b"boot\r\n": STG, b"root\r\n": b"Password: ", b"\r\n": b"Login incorrect\r\nPassword: ",
      b"1234\r\n": b"root@mono:~# "}, True),
    ("login, neither password works", {b"boot\r\n": STG, b"root\r\n": b"Password: "}, False),
    ("login, plain '#' shell", {b"boot\r\n": STG, b"root\r\n": b"# "}, True),
    ("nothing boots -> False", {}, False),
]
for what, script, want in cases:
    with fast():
        f = FakeSerial(responder=lambda f, b, s=script: f.feed(s[b]) if b in s else None)
        d = attach(f)
        got = d.boot_linux_staging()
    check(what, got is want)
    if "neither" in what:
        check("  tried empty, then 1234", b"\r\n" in f.written and b"1234\r\n" in f.written and heard("neither empty"))
with fast():
    d = attach(FakeSerial())
    with patch.object(d, "safe_write", side_effect=RuntimeError("x")):
        check("unexpected error -> False", d.boot_linux_staging() is False)

# ----------------------------------------------------------------------------
section("login_recovery / login_staging (_wait_for_root_shell)")
with fast():
    d = attach(FakeSerial(responder=lambda f, b: f.feed(b"\r\n" + PROMPT)))
    check("login_recovery: prompt on the first Enter", d.login_recovery(timeout=1) and heard("attempt 1"))
    n = {"i": 0}

    def third(f, b):
        n["i"] += 1
        if n["i"] >= 3:
            f.feed(b"root@mono:~# ")
    d = attach(FakeSerial(responder=third))
    check("login_staging: prompt on the 3rd try (back-off retries)", d.login_staging(timeout=2) and heard("attempt 3"))
    d = attach(FakeSerial())
    check("never a shell -> False", d.login_recovery(timeout=0.2) is False and heard("Failed to verify"))
    d = SerialDevice("/dev/ttyFAKE")
    calls = []

    def reconnect(baud):
        calls.append(baud)
        d.ser = SerialProxy(d, FakeSerial(responder=lambda f, b: f.feed(PROMPT)))
        return True
    with patch.object(d, "wait_for_port", side_effect=[False, True]), patch.object(d, "connect", side_effect=reconnect):
        check("port dropped: waits, reconnects at 115200, then logs in",
              d.login_recovery(timeout=2) and calls == [C.SERIAL_BAUD] and heard("Serial disconnected"))
    f = FakeSerial(responder=lambda f, b: f.feed(PROMPT))
    d = attach(f)
    first = {"done": False}

    def glitch_once(data):
        if not first["done"]:
            first["done"] = True
            raise RuntimeError("glitch")
        return f.write(data)
    with patch.object(d, "safe_write", side_effect=glitch_once):
        check("an error in one attempt -> retried", d.login_recovery(timeout=2) and heard("failed: glitch"))

# ----------------------------------------------------------------------------
section("wait_for_any_output")
d = SerialDevice("/dev/ttyFAKE")
check("no port -> False", d.wait_for_any_output(timeout=0.1) is False)
with fast():
    d = attach(FakeSerial(initial=b"[    0.000] Booting Linux"))
    check("any bytes -> True", d.wait_for_any_output(timeout=1) is True)
    d = attach(FakeSerial())
    check("silence -> False", d.wait_for_any_output(timeout=0.1) is False and heard("No serial output"))
    f = FakeSerial(initial=b"late")
    f.raise_on["read"] = [serial.SerialException("x"), serial.SerialException("x")]
    check("SerialException is ignored, keeps polling until bytes arrive", raw(f).wait_for_any_output(timeout=1) is True)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
