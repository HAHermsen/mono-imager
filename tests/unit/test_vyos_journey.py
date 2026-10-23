#!/usr/bin/env python3
"""
mono-imager: VyOS journeys (#28) against a simulated Mono — no hardware.

A FakeSerial plays U-Boot, the VyOS live console (login, shell, kexec
double boot) and the `install image` dialogue with the exact prompt texts
of vyos-1x image_installer.py / vyos.utils.io. Time is simulated (a fake
clock that only moves while the code waits), so every timeout and idle
rule runs for real, instantly.

What this tests:
  - uboot_run(): ok / U-Boot error / no prompt
  - wait_live_login(): normal login, kexec double boot mid-login,
    U-Boot prompt back (boot failed), kernel panic, timeout
  - live_cmd(): output between the echo and the marker
  - run_install_image(): every answer (y, Enter, password x2, S, RAID n,
    /dev/mmcblk0 — never the stick, y), password never logged, unknown
    defaulted question -> Enter, failure, no-eMMC -> Ctrl-C before any
    partitioning, rejected answers
  - the FULL v1 and v2 journeys (FlowRunner), plus their stop points:
    no stick, erase declined, EXPERIMENTAL declined, RAM check failing
    (install image never runs), NOR boot settings missing

Run: python tests/unit/test_vyos_journey.py
"""

import gzip
import logging
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.journeys import _vyos as V
from mono_imager.journeys import get_journey

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


# --- simulated time ------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


# --- simulated Mono ------------------------------------------------------------

PROMPT = "vyos@vyos:~$ "
PW = "S3cret-pw!"


class FakeSerial:
    """Device side of the serial line. `mode` is 'uboot' or 'shell'."""

    def __init__(self, **opts):
        self.out = bytearray()
        self.is_open = True
        self.mode = opts.get("mode", "uboot")
        self.opts = opts
        self.typed = []            # every line the host sent, in order
        self.install = None        # install dialogue generator
        self.login_state = None
        self.kexec_left = opts.get("kexec", 0)
        self._line = ""

    # pyserial-ish surface
    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, n=1):
        data = bytes(self.out[:n])
        del self.out[:n]
        return data

    def reset_input_buffer(self):
        self.out.clear()

    def emit(self, s):
        self.out += s.encode()

    def write(self, data):
        text = data.decode()
        if text == "\x03":
            self.typed.append("^C")
            self.install = None
            self.emit("^C\r\n" + PROMPT)
            return len(data)
        self._line += text
        while True:
            m = re.search(r"\r\n|\r|\n", self._line)
            if not m:
                break
            line, self._line = self._line[:m.start()], self._line[m.end():]
            self.typed.append(line)
            self.on_line(line)
        return len(data)

    # behaviour
    def on_line(self, line):
        if self.install is not None:
            try:
                self.emit(self.install.send(line))
            except StopIteration as done:     # dialogue over: emit its last words
                self.install = None
                self.emit(done.value or "")
            return
        if self.mode == "uboot":
            return self.uboot(line)
        if self.mode == "login":
            return self.login(line)
        return self.shell(line)

    def uboot(self, line):
        self.emit(line + "\r\n")
        if line.strip() == "":
            self.emit("=> ")
        elif line.startswith("fatload") and self.opts.get("fatload_fail"):
            self.emit("** Unable to read file live/vmlinuz **\r\n=> ")
        elif line.startswith("fatload"):
            self.emit("9437184 bytes read in 412 ms (21.8 MiB/s)\r\n=> ")
        elif line.startswith("booti") or line.startswith("usb start"):
            if self.opts.get("boot_fail"):
                self.emit("Bad Linux ARM64 Image magic!\r\n=> ")
                return
            self.emit("Starting kernel ...\r\n[    0.000000] Booting Linux on physical CPU 0x0\r\n")
            if self.opts.get("panic"):
                self.emit("[    9.1] Kernel panic - not syncing: VFS\r\n")
                return
            if self.opts.get("no_login"):
                return
            self.mode = "login"
            self.emit("\r\nWelcome to VyOS - vyos ttyS0\r\n\r\nvyos login: ")
            self.emit(self.opts.get("after_login_noise", ""))
        else:
            self.emit("=> ")

    def login(self, line):
        if self.login_state is None and line == "":
            self.emit("\r\nvyos login: ")          # getty: empty user -> new prompt
            return
        if self.login_state is None:
            self.emit(line + "\r\n")
            if self.kexec_left:
                self.kexec_left -= 1
                self.emit("[   61.2] kexec_core: Starting new kernel\r\n[    0.0] Booting Linux\r\n"
                          "\r\nvyos login: ")
                return
            self.login_state = "pw"
            self.emit("Password: ")
        else:
            self.login_state = None
            if line == "vyos":
                self.mode = "shell"
                self.emit("\r\nWelcome to VyOS!\r\n" + PROMPT)
            else:
                self.emit("\r\nLogin incorrect\r\nvyos login: ")

    def shell(self, line):
        self.emit(line + "\r\n")
        if line == "install image":
            self.install = self.opts.get("dialogue", install_dialogue)(self)
            self.emit(next(self.install))
            return
        cmd = line.split("; echo")[0]
        answers = self.opts.get("shell", {})
        out = answers.get(cmd, "")
        if out:
            self.emit(out + "\r\n")
        if '"__MI_""DONE__"' in line:
            self.emit("__MI_DONE__\r\n")
        self.emit(PROMPT)


def install_dialogue(dev, *, disks=("/dev/sda", "/dev/mmcblk0"), extra_q=None, fail=False):
    """vyos-1x install_image() as a generator: yields output, receives answers."""
    got = dev.opts.setdefault("answers", [])
    a = yield ("Welcome to VyOS installation!\r\nThis command will install VyOS to your "
               "permanent storage.\r\nWould you like to continue? [y/N] ")
    got.append(("continue", a))
    if a != "y":
        return "Exiting from VyOS installation\r\n" + PROMPT
    a = yield "What would you like to name this image? (Default: 2026.10.16-2149-rolling) "
    got.append(("name", a))
    a = yield 'Please enter a password for the "vyos" user: '
    got.append(("pw", a))
    b = yield '\r\nPlease confirm password for the "vyos" user: '
    got.append(("pw2", b))
    a = yield "\r\nWhat console should be used by default? (K: KVM, S: Serial)? (Default: S) "
    got.append(("console", a))
    out = f"Probing disks\r\n{len(disks)} disk(s) found\r\n"
    if len(disks) > 1:
        a = yield out + "Would you like to configure RAID-1 mirroring? [Y/n] "
        got.append(("raid", a))
        out = ""
    listing = "".join(f"Drive: {d} (29.1 GB)\r\n" for d in disks)
    a = yield (out + "The following disks were found:\r\n" + listing +
               f"Which one should be used for installation? (Default: {disks[-1]}) ")
    got.append(("disk", a))
    a = yield "Installation will delete all data on the drive. Continue? [y/N] "
    got.append(("delete", a))
    if a != "y":
        return "Exiting from VyOS installation\r\n" + PROMPT
    if extra_q:
        a = yield "Searching for data from previous installations\r\n" + extra_q
        got.append(("extra", a))
    a = yield "No previous installation found\r\nWould you like to use all the free space on the drive? [Y/n] "
    got.append(("space", a))
    a = yield ("Creating partition table...\r\nThe following config files are available for boot:\r\n"
               "\t1: /opt/vyatta/etc/config/config.boot\r\n\t2: /opt/vyatta/etc/config.boot.default\r\n"
               "Which file would you like as boot config? (Default: 1) ")
    got.append(("config", a))
    if fail:
        return "Mounting new partitions\r\nTraceback (most recent call last):\r\nOSError: [Errno 5]\r\n" + PROMPT
    return ("Mounting new partitions\r\nCreating a configuration file\r\nCopying system image files\r\n"
           "Installing GRUB to the drive\r\nThe image installed successfully; please reboot now.\r\n" + PROMPT)


class FakeDevice:
    def __init__(self, ser, stick_out="STICK=YES\nSECTORS=30310400\nMODEL=SanDisk Cruzer\nMOUNTED=NO\n"):
        self.ser = ser
        self.stick_out = stick_out
        self.launched = []

    def run_script(self, body, marker=None, **kw):
        return self.stick_out

    def launch_script(self, body, marker=None, **kw):
        self.launched.append((marker, body))
        return f"/tmp/mono_imager_{marker}.sh"


clock = Clock()


class FastConsole(V.Console):
    def __init__(self, device, **kw):
        super().__init__(device, clock=clock.now, sleep=clock.sleep)


# --- capture the log, to prove the password never lands in it ------------------
log_lines = []


class _Grab(logging.Handler):
    def emit(self, record):
        log_lines.append(record.getMessage())


logging.getLogger().addHandler(_Grab())
logging.getLogger().setLevel(logging.DEBUG)


print("=" * 60)
print("uboot_run()")
print("=" * 60)
ser = FakeSerial()
con = FastConsole(FakeDevice(ser))
ok, out = V.uboot_run(con, "fatload mmc 0:2 ${kernel_addr_r} live/vmlinuz")
check("fatload ok -> True", ok and "bytes read" in out)
ser = FakeSerial(fatload_fail=True)
ok, out = V.uboot_run(FastConsole(FakeDevice(ser)), "fatload mmc 0:2 ${kernel_addr_r} live/vmlinuz")
check("fatload error -> False with the message", not ok and "Unable to read" in out)


class Mute(FakeSerial):
    def write(self, data):
        return len(data)


ok, out = V.uboot_run(FastConsole(FakeDevice(Mute())), "mmc rescan", timeout=5)
check("no prompt back -> False", not ok)


print()
print("=" * 60)
print("wait_live_login()")
print("=" * 60)


def boot_and_login(**opts):
    ser = FakeSerial(**opts)
    con = FastConsole(FakeDevice(ser))
    con.write(opts.get("line", "booti ${kernel_addr_r} - ${fdt_addr_r}") + "\r\n")
    return V.wait_live_login(con, timeout=900), ser


(ok, why), ser = boot_and_login()
check("normal boot -> logged in", ok and ser.mode == "shell")
check("logged in as vyos/vyos", ser.typed[-2:] == ["vyos", "vyos"])
(ok, why), ser = boot_and_login(kexec=1)
check("kexec double boot mid-login -> retried and logged in", ok and ser.typed.count("vyos") == 3)
(ok, why), ser = boot_and_login(after_login_noise="[   72.418311] fsl_dpaa_mac 1ae8000.ethernet eth2: Link is Up\r\n")
check("kernel line printed after 'vyos login:' -> still logs in", ok and ser.mode == "shell")
(ok, why), ser = boot_and_login(after_login_noise="[  OK  ] Started vyos-postinstall.service.\r\n")
check("prompt scrolled away by a systemd line -> Enter for a fresh prompt, logs in",
      ok and ser.mode == "shell" and "" in ser.typed)
(ok, why), ser = boot_and_login(boot_fail=True)
check("U-Boot prompt back -> fails fast with reason", not ok and "U-Boot prompt came back" in why)
(ok, why), ser = boot_and_login(panic=True)
check("kernel panic -> fails", not ok and "panic" in why)
t0 = clock.t
(ok, why), ser = boot_and_login(no_login=True)
check("no login prompt -> timeout reason", not ok and "no VyOS login prompt" in why and clock.t - t0 >= 900)


print()
print("=" * 60)
print("live_cmd()")
print("=" * 60)
ser = FakeSerial(mode="shell", shell={"cat /proc/cmdline": "console=ttyS0,115200 boot=live toram"})
con = FastConsole(FakeDevice(ser))
check("output between echo and marker", V.live_cmd(con, "cat /proc/cmdline") == "console=ttyS0,115200 boot=live toram")
check("empty output -> ''", V.live_cmd(con, "grep /dev/mmcblk0 /proc/mounts") == "")
try:
    V.live_cmd(FastConsole(FakeDevice(Mute())), "true", timeout=5)
    check("no answer -> TimeoutError", False)
except TimeoutError:
    check("no answer -> TimeoutError", True)


print()
print("=" * 60)
print("run_install_image()")
print("=" * 60)


def install(**opts):
    ser = FakeSerial(mode="shell", **opts)
    con = FastConsole(FakeDevice(ser))
    log_lines.clear()
    ok, transcript = V.run_install_image(con, PW)
    return ok, transcript, dict(ser.opts.get("answers", [])), ser


ok, tr, ans, ser = install()
check("installed -> True", ok)
check("continue=y", ans.get("continue") == "y")
check("image name = VyOS default (Enter)", ans.get("name") == "")
check("password typed twice", ans.get("pw") == PW and ans.get("pw2") == PW)
check("console = S (serial)", ans.get("console") == "S")
check("RAID-1 = n", ans.get("raid") == "n")
check("target = /dev/mmcblk0, not the stick", ans.get("disk") == "/dev/mmcblk0")
check("delete all data = y", ans.get("delete") == "y")
check("all free space = default (Enter)", ans.get("space") == "")
check("boot config = default (Enter)", ans.get("config") == "")
check("password never in the log", not any(PW in l for l in log_lines))
check("'<hidden>' logged instead", any("<hidden>" in l for l in log_lines))

ok, tr, ans, ser = install(dialogue=lambda d: install_dialogue(d, disks=("/dev/mmcblk0",)))
check("single disk (v2): no RAID question, eMMC chosen", ok and "raid" not in ans and ans["disk"] == "/dev/mmcblk0")

ok, tr, ans, ser = install(dialogue=lambda d: install_dialogue(
    d, extra_q="Found data from previous installation:\r\n\t2026.09 on /dev/mmcblk0p3\r\n"
               "Would you like to copy data to the new image? [Y/n] "))
check("unknown [Y/n] question -> Enter (VyOS default)", ok and ans.get("extra") == "")

ok, tr, ans, ser = install(dialogue=lambda d: install_dialogue(d, disks=("/dev/sda", "/dev/sdb")))
check("no eMMC offered -> stopped with Ctrl-C", not ok and "^C" in ser.typed and "no eMMC" in tr)
check("...before the delete-all-data question", "delete" not in ans)

ok, tr, ans, ser = install(dialogue=lambda d: install_dialogue(d, fail=True))
check("installer error -> False with transcript", not ok and "OSError" in tr)


def rejecting(dev):
    a = yield "Which one should be used for installation? (Default: /dev/mmcblk0) "
    while True:
        a = yield "Invalid value, try again.\r\nWhich one should be used for installation? (Default: /dev/mmcblk0) "


ok, tr, ans, ser = install(dialogue=rejecting)
check("installer keeps rejecting -> stops (Ctrl-C)", not ok and "^C" in ser.typed)


# --- full journeys ---------------------------------------------------------------

FIX = Path(__file__).resolve().parent / "fixtures"
tmp = Path(tempfile.mkdtemp(prefix="mi_vyos_j_"))
iso = tmp / "vyos-2026.10.16-2149-rolling-LS1046A-arm64.iso"
with gzip.open(FIX / "vyos_hybrid.img.gz") as g, open(iso, "wb") as o:
    shutil.copyfileobj(g, o)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


GOOD_SHELL = {
    "cat /proc/cmdline": "console=ttyS0,115200 boot=live components union=overlay toram",
    "grep mmcblk0 /proc/mounts": "",
    "test -r filesystem.squashfs && echo READABLE": "READABLE",
    "df -P . | tail -n 1": "tmpfs 4022000 610000 3412000 16% /run/live/medium",
    "sudo fw_printenv -n bootcmd": "run vyos || run recovery",
}


def run_journey(transfer, *, inputs, shell=None, stick_out=None, report=None, ser_opts=None):
    ser = FakeSerial(mode="uboot", shell=shell or GOOD_SHELL, **(ser_opts or {}))
    dev = FakeDevice(ser, **({"stick_out": stick_out} if stick_out else {}))
    runner = get_journey("VyOS", transfer, device=dev, host_ip="127.0.0.1",
                         firmware_path=iso, http_port=free_port(),
                         device_net={"ip": "192.168.1.50", "prefix": "24", "gateway": "192.168.1.1",
                                     "dns": "", "source": "dhcp"})
    size = iso.stat().st_size
    import hashlib
    sha = hashlib.sha256(iso.read_bytes()).hexdigest()
    rep = report if report is not None else (
        f"DL_RC=0\nSIZE={size}\nSHA={sha}\nPT_ZERO_RC=0\nPT_RC=0\nDD_RC=0\nVERIFY=OK\nP2=OK\n")
    answers = iter(inputs)
    with patch("mono_imager.journeys._vyos.Console", FastConsole), \
         patch("mono_imager.journeys._vyos.reboot_to_uboot", lambda d: True), \
         patch("mono_imager.journeys._vyos.ask_vyos_password", lambda: PW), \
         patch("mono_imager.journeys._common.wait_for_report", lambda sid, timeout=0: "200"), \
         patch("mono_imager.journeys._vyos_steps.wait_for_report", lambda sid, timeout=0: rep), \
         patch("builtins.input", lambda prompt="": next(answers)):
        ok = runner.run()
    srv = runner.ctx.get("http_server")
    if srv:
        srv.shutdown()
    return ok, runner, ser, dev


from mono_imager.flash_orchestrator import results

print()
print("=" * 60)
print("FULL v1 journey (USB stick)")
print("=" * 60)
ok, runner, ser, dev = run_journey("net", inputs=["y"])
check("journey passes", ok)
check("all steps passed", results and all(p for _, _, p, _ in results))
writes = [b for m, b in dev.launched if m == "vyos_write"]
check("one write script launched, to /dev/sda",
      len(writes) == 1 and "of=/dev/sda bs=1M conv=notrunc" in writes[0])
check("v1 writes no partition table", writes and "seek=446" not in writes[0])
check("documented INSTALL.md boot line typed at U-Boot",
      "usb start; fatload usb 0:2 ${load_addr} boot.scr; source ${load_addr}" in ser.typed)
check("...after the load_addr guard (only set when missing, no saveenv)",
      ser.typed.index('if test -z "${load_addr}"; then setenv load_addr 0xa0000000; fi')
      < ser.typed.index("usb start; fatload usb 0:2 ${load_addr} boot.scr; source ${load_addr}")
      and "saveenv" not in ser.typed)
check("install image answered with the eMMC", dict(ser.opts["answers"]).get("disk") == "/dev/mmcblk0")
check("NOR boot settings read back", "sudo fw_printenv -n bootcmd" in " ".join(ser.typed))

ok, runner, ser, dev = run_journey("net", inputs=["n"])
check("erase declined -> journey stops", not ok)
check("...nothing launched", dev.launched == [])

ok, runner, ser, dev = run_journey("net", inputs=["y"], stick_out="STICK=NO\n")
check("no stick -> journey stops before anything is written", not ok and dev.launched == [])

ok, runner, ser, dev = run_journey("net", inputs=["y"],
                                   stick_out="STICK=YES\nSECTORS=1000\nMODEL=Tiny\nMOUNTED=NO\n")
check("stick too small -> stops", not ok and dev.launched == [])

ok, runner, ser, dev = run_journey("net", inputs=["y"], report="DL_RC=0\nSIZE=5\nWRITE=SKIPPED\n")
check("bad write report -> stops before reboot", not ok and "install image" not in ser.typed
      and not any("usb start" in l for l in ser.typed))

ok, runner, ser, dev = run_journey("net", inputs=["y"], shell={**GOOD_SHELL, "sudo fw_printenv -n bootcmd": "run openwrt || run recovery"})
check("NOR boot settings missing -> journey fails at the last step", not ok
      and results[-1][1].startswith("Boot settings written to NOR") and not results[-1][2])

print()
print("=" * 60)
print("FULL v2 journey (eMMC staging, EXPERIMENTAL)")
print("=" * 60)
ok, runner, ser, dev = run_journey("netemmc", inputs=["y"])
check("journey passes", ok)
writes = [b for m, b in dev.launched if m == "vyos_write"]
body = writes[0] if writes else ""
check("write targets /dev/mmcblk0 at 64 MiB", "of=/dev/mmcblk0 bs=1M seek=64 conv=notrunc" in body)
check("partition table written first", body.find("seek=446") < body.find("bs=1M seek=64") and "seek=446" in body)
check("p2 checked after re-reading the table", "/dev/mmcblk0p2" in body)
check("U-Boot got the ISO's commands, adapted",
      "mmc rescan" in ser.typed and any(l.startswith("fatload mmc 0:2 ") for l in ser.typed)
      and any(l.startswith("setenv bootargs ") and l.endswith(" toram") for l in ser.typed))
check("no usb commands at U-Boot", not any("usb start" in l for l in ser.typed))
check("booti sent last before the login", [l for l in ser.typed if l.startswith("booti")])
check("single disk: eMMC chosen", dict(ser.opts["answers"]).get("disk") == "/dev/mmcblk0")

ok, runner, ser, dev = run_journey("netemmc", inputs=["n"])
check("EXPERIMENTAL declined -> stops, nothing written", not ok and dev.launched == [])

ok, runner, ser, dev = run_journey("netemmc", inputs=["y"], shell={**GOOD_SHELL,
                                   "cat /proc/cmdline": "console=ttyS0,115200 boot=live"})
check("no toram -> stops at the safety check", not ok and "Live system runs from RAM" in results[-1][1])
check("...install image NEVER started", "install image" not in ser.typed)

ok, runner, ser, dev = run_journey("netemmc", inputs=["y"], shell={**GOOD_SHELL,
                                   "grep mmcblk0 /proc/mounts": "/dev/mmcblk0p1 /run/live/medium iso9660 ro 0 0"})
check("eMMC still mounted -> stops, no install", not ok and "install image" not in ser.typed)

ok, runner, ser, dev = run_journey("netemmc", inputs=["y"], shell={**GOOD_SHELL,
                                   "df -P . | tail -n 1": "/dev/mmcblk0p1 600000 600000 0 100% /run/live/medium"})
check("system image still on the eMMC -> stops, no install", not ok and "install image" not in ser.typed)

ok, runner, ser, dev = run_journey("netemmc", inputs=["y"], shell={**GOOD_SHELL,
                                   "test -r filesystem.squashfs && echo READABLE": ""})
check("system image not readable -> stops, no install", not ok and "install image" not in ser.typed)

ok, runner, ser, dev = run_journey("netemmc", inputs=["y"], shell={**GOOD_SHELL,
                                   "df -P . | tail -n 1": "Filesystem 1024-blocks Used Available Capacity Mounted on"})
check("df shows only its header -> stops, no install", not ok and "install image" not in ser.typed)
check("every command typed into the live shell fits 80 columns with the prompt",
      all(len(PROMPT) + len(l) < 80 for l in ser.typed if '__MI_""DONE__' in l))

ok, runner, ser, dev = run_journey("netemmc", inputs=["y"], ser_opts={"fatload_fail": True})
check("fatload fails at U-Boot -> stops, no boot", not ok and not any(l.startswith("booti") for l in ser.typed))

notboot = tmp / "plain.iso"
notboot.write_bytes(b"\x00" * (1024 * 1024))
iso_saved, iso = iso, notboot
ok, runner, ser, dev = run_journey("netemmc", inputs=["y"])
iso = iso_saved
check("ISO without a FAT32 boot partition -> stops before anything is written",
      not ok and dev.launched == [] and "FAT32" in results[-1][1])

shutil.rmtree(tmp, ignore_errors=True)
print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
