"""
mono-imager: shared building blocks for the VyOS journeys (#28).

Leading underscore: not auto-discovered (see journeys/__init__.py); the
two VyOS journey files import it explicitly:

    vyos_net.py      — v1: ISO -> USB stick in the Mono -> install image
    vyos_netemmc.py  — v2 (EXPERIMENTAL): ISO staged on eMMC -> install image

Source of the procedure: the VyOS LS1046A port's own INSTALL.md and
board/scripts/boot.cmd (github.com/mihakralj/vyos-ls1046a-build), plus
upstream vyos-1x src/op_mode/image_installer.py for the exact
`install image` prompts.

mono-imager is a dumb pipe (README "What it does NOT do"): the ISO is a
LOCAL file the user picked and is responsible for. Nothing here judges
or verifies its contents. The only checks are TRANSPORT checks — did the
bytes that left the PC arrive intact, and did they land on the target
intact — plus reading the ISO's own partition table / boot script where
the v2 path needs them to know WHERE things are.

Author:  H.A. Hermsen
License: GPLv3
"""

import getpass
import hashlib
import re
import struct
import time

from mono_imager.flash_orchestrator import verbose

# --- Constants ----------------------------------------------------------------

OS = "VyOS"

ISO_TMP   = "/tmp/mono_imager_vyos.iso"     # staging copy in recovery Linux's RAM
WRITE_LOG = "/tmp/mono_imager_vyos_write.log"
STICK_DEV = "/dev/sda"                       # the USB stick in the Mono
EMMC_DEV  = "/dev/mmcblk0"
SECTOR    = 512

# v2 stages the ISO at 64 MiB: the start of VyOS's own reserved firmware
# zone boundary (INSTALL.md "eMMC Partition Layout": 4 KiB-64 MiB is kept
# free). Everything mono-imager writes for v2 is beyond Mono's eMMC
# firmware region (4 KiB-32 MiB) except the partition table itself in
# the first 4 KiB, which is exactly what every other eMMC OS writes too.
STAGE_BASE_LBA = 64 * 1024 * 1024 // SECTOR

# Recovery Linux's /tmp is a ~3.8 GB tmpfs (see flash_orchestrator.py's
# FLASH_SIZE_CAP comment); same 80 % margin. The VyOS ISO is ~571 MiB.
STAGING_CAP = int(3.8 * 1024**3 * 0.8)

LIVE_USER     = "vyos"     # VyOS live default login (INSTALL.md step 3)
LIVE_PASSWORD = "vyos"

# Shown by tui.py before the ISO path is asked (README: we do not verify).
SOURCE_NOTICE = (
    "  +-----------------------------------------------------+\n"
    "  | CHECK YOUR ISO BEFORE YOU CONTINUE                  |\n"
    "  |                                                     |\n"
    "  | mono-imager writes exactly the file you point it    |\n"
    "  | at. It does NOT download, check or verify it.       |\n"
    "  | Only use a VyOS LS1046A ISO from a source you trust |\n"
    "  | and verify it yourself first (the build publishes a |\n"
    "  | .minisig signature next to every .iso).             |\n"
    "  +-----------------------------------------------------+"
)


# --- Host-side helpers (no device involved) ----------------------------------

def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ask_vyos_password(read=getpass.getpass, out=print) -> str:
    """
    Ask (twice, hidden) for the password `install image` will set for the
    'vyos' user. Asked once, up front, so nobody has to babysit the
    install. Returns "" if the user gives up (three mismatches).
    """
    out("  `install image` sets a new password for the VyOS user 'vyos'.")
    out("  Choose it now — mono-imager types it in for you later.")
    for _ in range(3):
        pw = read("  New password for 'vyos': ")
        if not pw:
            out("  ✗ Empty password — try again.")
            continue
        if not all(32 <= ord(c) < 127 for c in pw):
            out("  ✗ Use plain printable ASCII characters only (it is typed over serial).")
            continue
        if read("  Repeat password: ") != pw:
            out("  ✗ The passwords do not match — try again.")
            continue
        if pw == LIVE_PASSWORD:
            out("  ⚠ That is VyOS's well-known default password — consider a better one.")
        return pw
    return ""


def read_mbr(path) -> list:
    """The 4 primary MBR entries of `path` as dicts, or [] if there is no
    valid MBR signature. Each entry: boot, type, start, sectors."""
    with open(path, "rb") as f:
        mbr = f.read(SECTOR)
    if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa":
        return []
    entries = []
    for i in range(4):
        e = mbr[446 + 16 * i: 446 + 16 * (i + 1)]
        start, sectors = struct.unpack_from("<II", e, 8)
        entries.append({"boot": e[0], "type": e[4], "start": start, "sectors": sectors})
    return entries


_FAT32_TYPES = (0x0B, 0x0C)


def hybrid_layout(path, file_size: int):
    """
    The VyOS LS1046A ISO is a hybrid image (ci-build-iso.sh): partition 1
    is the ISO9660 data (type 0x17, from sector 0), partition 2 an
    appended FAT32 partition (type 0x0C) holding boot.scr, kernel,
    initrd and DTB. v2 needs both locations. Returns (layout, "") or
    (None, reason) — a reason means "not a layout v2 can stage", never
    a judgement of the ISO's contents.
    """
    entries = read_mbr(path)
    if not entries:
        return None, "the file has no MBR partition table (not a hybrid USB/disk image)"
    p1, p2 = entries[0], entries[1]
    if p1["type"] == 0 or p1["sectors"] == 0:
        return None, "partition 1 (the ISO9660 data) is missing from the image's partition table"
    if p2["type"] not in _FAT32_TYPES or p2["sectors"] == 0:
        return None, "partition 2 is not a FAT32 boot partition"
    for name, p in (("1", p1), ("2", p2)):
        if (p["start"] + p["sectors"]) * SECTOR > file_size:
            return None, f"partition {name} runs past the end of the file"
    return {"p1": p1, "p2": p2}, ""


def stage_partition_table(layout, base_lba: int = STAGE_BASE_LBA) -> bytes:
    """
    The 66 bytes (4 partition entries + 0x55AA) for eMMC sector 0 in v2:
    the ISO's own two partitions, shifted to base_lba, so the kernel sees
    the ISO9660 data as mmcblk0p1 (live-boot finds its squashfs there)
    and U-Boot sees the FAT32 boot partition as mmc 0:2.
    """
    def entry(p, boot):
        return struct.pack("<B3sB3sII", boot, b"\xfe\xff\xff", p["type"],
                           b"\xfe\xff\xff", base_lba + p["start"], p["sectors"])
    p1, p2 = layout["p1"], layout["p2"]
    return (entry(p1, 0x00) + entry(p2, 0x80) + b"\x00" * 32 + b"\x55\xaa")


def printf_octal(data: bytes) -> str:
    """`data` as a printf format string of octal escapes (POSIX printf
    and BusyBox printf both understand \\ooo)."""
    return "".join(f"\\{b:03o}" for b in data)


def read_fat32_file(path, part_offset: int, name: str) -> bytes:
    """
    Read one file from the ROOT directory of the FAT32 filesystem that
    starts `part_offset` bytes into `path` (8.3 short name, e.g.
    "BOOT.SCR"; long-name entries are skipped). Just enough FAT32 to
    read the ISO's boot.scr on the PC. Raises ValueError on anything
    unexpected.
    """
    want = name.upper()
    with open(path, "rb") as f:
        def at(off, n):
            f.seek(off)
            data = f.read(n)
            if len(data) != n:
                raise ValueError("FAT32: read past end of file")
            return data

        bs = at(part_offset, 512)
        if bs[510:512] != b"\x55\xaa":
            raise ValueError("FAT32: no boot sector signature")
        bps, spc, rsvd, nfats = struct.unpack_from("<HBHB", bs, 11)
        root_entries = struct.unpack_from("<H", bs, 17)[0]
        fatsz = struct.unpack_from("<I", bs, 36)[0]
        root_clus = struct.unpack_from("<I", bs, 44)[0]
        if bps not in (512, 1024, 2048, 4096) or spc == 0 or root_entries != 0 or fatsz == 0:
            raise ValueError("FAT32: not a FAT32 boot sector")

        fat_off = part_offset + rsvd * bps
        data_off = part_offset + (rsvd + nfats * fatsz) * bps
        cbytes = bps * spc

        def chain(c):
            seen = 0
            while 2 <= c < 0x0FFFFFF8:
                yield c
                seen += 1
                if seen > 1_000_000:
                    raise ValueError("FAT32: cluster chain loop")
                c = struct.unpack("<I", at(fat_off + 4 * c, 4))[0] & 0x0FFFFFFF

        def cluster(c):
            return at(data_off + (c - 2) * cbytes, cbytes)

        for c in chain(root_clus):
            d = cluster(c)
            for i in range(0, len(d), 32):
                e = d[i:i + 32]
                if e[0] == 0x00:
                    raise ValueError(f"FAT32: {name} not found in the root directory")
                if e[0] == 0xE5 or e[11] == 0x0F or e[11] & 0x08:
                    continue
                base = e[0:8].decode("ascii", "replace").rstrip()
                ext = e[8:11].decode("ascii", "replace").rstrip()
                full = f"{base}.{ext}" if ext else base
                if full.upper() != want:
                    continue
                first = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
                size = struct.unpack_from("<I", e, 28)[0]
                out = bytearray()
                for fc in chain(first):
                    out += cluster(fc)
                    if len(out) >= size:
                        break
                if len(out) < size:
                    raise ValueError(f"FAT32: {name} is truncated")
                return bytes(out[:size])
    raise ValueError(f"FAT32: {name} not found in the root directory")


def parse_uboot_script(data: bytes) -> str:
    """Text of a mkimage `-T script` image (legacy header, 64 bytes, then
    a length table ending in 0, then the script)."""
    if len(data) < 72 or struct.unpack_from(">I", data, 0)[0] != 0x27051956:
        raise ValueError("boot.scr is not a U-Boot legacy image")
    if data[30] != 6:   # IH_TYPE_SCRIPT
        raise ValueError("boot.scr is not a U-Boot script image")
    length = struct.unpack_from(">I", data, 64)[0]
    body = data[72:72 + length]
    if len(body) != length:
        raise ValueError("boot.scr is truncated")
    return body.decode("utf-8", errors="replace").rstrip("\x00")


_UBOOT_LINE_MAX = 480   # boot.cmd: every line must stay < 500 chars on this U-Boot


def adapt_boot_script_for_emmc(text: str) -> list:
    """
    Turn the ISO's own USB boot script into the U-Boot commands that boot
    the same live system from the eMMC staging copy (v2). The ISO's
    script stays the single source of truth for paths and bootargs; the
    only changes are:
      - `usb start` / `usb stop` dropped, `mmc rescan` added in front
      - `fatload usb 0:2 ...` -> `fatload mmc 0:2 ...`
      - ` toram` appended to bootargs: the live system must copy itself
        into RAM, because `install image` repartitions the same eMMC the
        staging copy lives on (checked again before installing).
    Anything else in the script (if/then, other commands) is refused
    rather than guessed at — raise ValueError, v2 stops before writing.
    """
    lines, booti = ["mmc rescan"], None
    fatloads = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        word = line.split()[0]
        if word == "echo" or line in ("usb start", "usb stop"):
            continue
        if word == "setenv":
            if line.startswith("setenv bootargs ") and " toram" not in f" {line} ":
                line += " toram"
            lines.append(line)
        elif word == "fatload" and " usb 0:2 " in f" {line} ":
            lines.append(line.replace(" usb 0:2 ", " mmc 0:2 ", 1))
            fatloads += 1
        elif word == "booti" and booti is None:
            booti = line
        else:
            raise ValueError(f"unexpected command in the ISO's boot script: {line!r}")
    if booti is None or fatloads == 0:
        raise ValueError("the ISO's boot script has no fatload/booti commands")
    if not any(l.startswith("setenv bootargs ") for l in lines):
        raise ValueError("the ISO's boot script sets no bootargs")
    lines.append(booti)
    too_long = [l for l in lines if len(l) > _UBOOT_LINE_MAX]
    if too_long:
        raise ValueError(f"boot command too long for U-Boot ({len(too_long[0])} chars)")
    return lines


# --- Device-side scripts (run in recovery Linux) -------------------------------

def build_stick_probe_script(dev: str = STICK_DEV) -> str:
    name = dev.rsplit("/", 1)[-1]
    return (
        f'if [ -b {dev} ]; then\n'
        f'  echo "STICK=YES"\n'
        f'  echo "SECTORS=$(cat /sys/block/{name}/size 2>/dev/null)"\n'
        f'  echo "MODEL=$(echo $(cat /sys/block/{name}/device/vendor '
        f'/sys/block/{name}/device/model 2>/dev/null))"\n'
        f'  if grep -q "^{dev}" /proc/mounts; then echo "MOUNTED=YES"; else echo "MOUNTED=NO"; fi\n'
        f'else\n'
        f'  echo "STICK=NO"\n'
        f'fi\n'
    )


def parse_kv(text: str) -> dict:
    """KEY=VALUE lines -> dict (first occurrence wins; other lines ignored)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z0-9_]+)=(.*)$", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    return out


def build_write_script(*, url: str, dev: str, iso_bytes: int, iso_sha256: str,
                       report_url: str, seek_lba: int = 0,
                       partition_table: bytes = b"", expect_p2: str = "") -> str:
    """
    One script, launched in recovery Linux, that:
      1. downloads the ISO from the PC into RAM,
      2. writes NOTHING unless the download is complete: size must match,
         and — when the device has sha256sum — the SHA-256 must match the
         PC's copy (a transport check: these are the bytes the user picked),
      3. (v2) writes the staging partition table into sector 0,
      4. dd's the ISO to `dev` at `seek_lba`,
      5. reads it back and compares it with the RAM copy,
      6. (v2) re-reads the partition table and checks `expect_p2` exists,
      7. POSTs its KEY=VALUE log to the PC (TCP/IP report-back, see
         flash_orchestrator._FirmwareHandler for why not serial).
    conv=notrunc is a no-op on block devices; it makes the script behave
    the same against plain files (the unit tests run it for real).
    """
    if iso_bytes % SECTOR:
        raise ValueError("ISO size is not a whole number of 512-byte sectors")
    sectors = iso_bytes // SECTOR
    f, l = ISO_TMP, WRITE_LOG
    s = [
        f'f={f}',
        f'l={l}',
        'rm -f "$f"',
        ': > "$l"',
        f'curl -sk -o "$f" "{url}" >>"$l" 2>&1',
        'echo "DL_RC=$?" >>"$l"',
        'sz=$(wc -c < "$f" 2>/dev/null | tr -d " ")',
        'echo "SIZE=$sz" >>"$l"',
        'ok=1',
        f'[ "$sz" = "{iso_bytes}" ] || ok=0',
        'if command -v sha256sum >/dev/null 2>&1; then',
        '  sum=$(sha256sum "$f" | cut -d" " -f1)',
        '  echo "SHA=$sum" >>"$l"',
        f'  [ "$sum" = "{iso_sha256}" ] || ok=0',
        'else',
        '  echo "SHA=unavailable" >>"$l"',
        'fi',
        'if [ "$ok" = 1 ]; then',
    ]
    if partition_table:
        s += [
            f'  dd if=/dev/zero of={dev} bs=512 count=8 conv=notrunc >>"$l" 2>&1',
            '  echo "PT_ZERO_RC=$?" >>"$l"',
            f"  printf '{printf_octal(partition_table)}' | "
            f'dd of={dev} bs=1 seek=446 conv=notrunc >>"$l" 2>&1',
            '  echo "PT_RC=$?" >>"$l"',
        ]
    seek_mib = seek_lba * SECTOR // (1024 * 1024)
    if seek_mib * 1024 * 1024 != seek_lba * SECTOR:
        raise ValueError("seek_lba must be a whole number of MiB")
    seek = f" seek={seek_mib}" if seek_mib else ""
    s += [
        f'  dd if="$f" of={dev} bs=1M{seek} conv=notrunc >>"$l" 2>&1',
        '  echo "DD_RC=$?" >>"$l"',
        '  sync',
        f'  if dd if={dev} bs=512 skip={seek_lba} count={sectors} 2>/dev/null | cmp -s - "$f"; then',
        '    echo "VERIFY=OK" >>"$l"',
        '  else',
        '    echo "VERIFY=BAD" >>"$l"',
        '  fi',
    ]
    if expect_p2:
        s += [
            f'  blockdev --rereadpt {dev} >>"$l" 2>&1 || partprobe {dev} >>"$l" 2>&1',
            '  sleep 2',
            f'  if [ -b {expect_p2} ]; then echo "P2=OK" >>"$l"; else echo "P2=MISSING" >>"$l"; fi',
        ]
    s += [
        'else',
        '  echo "WRITE=SKIPPED" >>"$l"',
        'fi',
        'rm -f "$f"',
        f'curl -sk -X POST --data-binary @"$l" "{report_url}" >/dev/null 2>&1',
    ]
    return "\n".join(s) + "\n"


def check_write_report(kv: dict, iso_bytes: int, iso_sha256: str,
                       partition_table: bool = False, expect_p2: bool = False):
    """(ok, reason) for build_write_script()'s report."""
    if kv.get("DL_RC") != "0":
        return False, f"download from the PC failed (curl rc={kv.get('DL_RC', '?')})"
    if kv.get("SIZE") != str(iso_bytes):
        return False, f"download incomplete ({kv.get('SIZE', '?')} of {iso_bytes} bytes)"
    sha = kv.get("SHA", "")
    if sha not in ("unavailable", iso_sha256):
        return False, "downloaded copy differs from the file on the PC (SHA-256 mismatch)"
    if kv.get("WRITE") == "SKIPPED":
        return False, "nothing was written (download check failed)"
    if partition_table and (kv.get("PT_ZERO_RC") != "0" or kv.get("PT_RC") != "0"):
        return False, "writing the staging partition table failed"
    if kv.get("DD_RC") != "0":
        return False, f"dd failed (rc={kv.get('DD_RC', '?')})"
    if kv.get("VERIFY") != "OK":
        return False, "read-back does not match what was written"
    if expect_p2 and kv.get("P2") != "OK":
        return False, "the kernel does not see the staged boot partition after re-reading the table"
    return True, ""


# --- Serial console helpers (U-Boot and the VyOS live shell) -------------------

_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
LOGIN_RE  = re.compile(rb"login: ?$")
PROMPT_RE = re.compile(rb"[\w.-]+@[\w.-]+:[^\r\n]*\$ ?$")
UBOOT_RE  = re.compile(rb"(^|[\r\n])=> ?$")

_UBOOT_ERRORS = ("Unable to read", "Failed to load", "Unknown command",
                 "Bad Linux ARM64", "Wrong Image", "ERROR", "Error:", "** ")


# Kernel log lines ("[   70.123456] ...") can land on the console at any
# moment — also right after a login or shell prompt, or in the middle of
# one. They are cut out before any prompt is matched.
_KLOG = re.compile(rb"\[\s*\d+\.\d+\][^\r\n]*(\r?\n)?")


def _clean(b: bytes) -> bytes:
    return _KLOG.sub(b"", _ANSI.sub(b"", b))


class Console:
    """
    Minimal expect-style access to SerialDevice.ser, for the parts of the
    VyOS journey that talk to something other than recovery Linux (whose
    prompt SerialDevice.send_command() is built around): U-Boot with our
    own boot commands, and the VyOS live shell (`vyos@vyos:~$ `).
    Non-blocking reads via in_waiting, so idle detection is precise.
    Secrets are never logged.
    """

    def __init__(self, device, clock=time.monotonic, sleep=time.sleep):
        self.d = device
        self.ser = device.ser
        self.clock = clock
        self.sleep = sleep

    def write(self, text: str, secret: bool = False):
        verbose(f">> [console] {'<hidden>' if secret else repr(text)}", "debug")
        self.ser.write(text.encode())

    def read_available(self) -> bytes:
        try:
            n = self.ser.in_waiting
        except Exception:
            n = 0
        if n:
            data = self.ser.read(n)
            if data:
                verbose(f"<< [console] {data[-300:]!r}", "debug")
            return data or b""
        return b""

    def drain(self, idle: float = 0.5, max_wait: float = 5.0) -> bytes:
        """Read until the line has been quiet for `idle` seconds."""
        buf, start, last = b"", self.clock(), self.clock()
        while self.clock() - start < max_wait:
            chunk = self.read_available()
            if chunk:
                buf += chunk
                last = self.clock()
            elif self.clock() - last >= idle:
                break
            else:
                self.sleep(0.05)
        return buf

    def expect(self, patterns, timeout: float):
        """Read until one of the compiled byte regexes matches the cleaned
        tail of what arrived. Returns (index, data) or (-1, data)."""
        buf, start = b"", self.clock()
        while self.clock() - start < timeout:
            chunk = self.read_available()
            if chunk:
                buf += chunk
                tail = _clean(buf[-2048:])
                for i, p in enumerate(patterns):
                    if p.search(tail):
                        return i, buf
            else:
                self.sleep(0.05)
        return -1, buf


def reboot_to_uboot(device) -> bool:
    """From a recovery-Linux shell: reboot and stop at the U-Boot prompt."""
    try:
        device.send_command("reboot", wait_for_prompt=False, timeout=3)
    except Exception as e:
        verbose(f"reboot command failed: {e}", "warning")
        return False
    return device.wait_for_autoboot(timeout=180)


def uboot_run(con: Console, line: str, timeout: float = 120.0):
    """Run one U-Boot command; (ok, output). ok=False on a U-Boot error
    message or when the prompt does not come back."""
    con.drain(idle=0.3, max_wait=2.0)
    con.write(line + "\r\n")
    idx, data = con.expect([UBOOT_RE], timeout)
    text = _clean(data).decode("utf-8", errors="replace")
    body = text.split("\n", 1)[1] if "\n" in text else ""   # drop the echo
    if idx < 0:
        return False, text[-300:]
    bad = [e for e in _UBOOT_ERRORS if e in body]
    return not bad, body.strip()[-300:]


def uboot_prime(con: Console):
    """U-Boot can swallow the first byte right after autoboot was
    interrupted (see SerialDevice.boot_recovery) — send a bare Enter
    onto an empty line first."""
    con.write("\r\n")
    con.drain(idle=0.3, max_wait=2.0)


def wait_live_login(con: Console, timeout: float = 900.0,
                    user: str = LIVE_USER, password: str = LIVE_PASSWORD):
    """
    After the boot command: wait for the VyOS live login prompt and log
    in. Copes with the build's documented kexec double boot (a login
    attempt that the reboot interrupts is simply retried) and fails fast
    if U-Boot's prompt comes back (boot command failed) or the kernel
    panics. Returns (ok, reason).
    """
    buf, start = b"", con.clock()
    last_rx = con.clock()
    booted = False
    nudged_at = -1e9
    while con.clock() - start < timeout:
        chunk = con.read_available()
        if chunk:
            buf = (buf + chunk)[-8192:]
            booted = booted or b"Starting kernel" in buf
            last_rx = con.clock()
            continue
        con.sleep(0.05)
        idle = con.clock() - last_rx
        tail = _clean(buf[-400:])
        if b"Kernel panic" in buf:
            return False, "kernel panic while booting VyOS"
        if idle >= 1.5 and not booted and UBOOT_RE.search(tail):
            return False, "U-Boot prompt came back — the boot command failed: " + \
                tail.decode("utf-8", "replace")[-200:]
        if idle >= 5.0 and b"login:" in tail and not LOGIN_RE.search(tail) \
                and con.clock() - nudged_at > 15.0:
            # the prompt scrolled away under later console output: an empty
            # line makes getty print a fresh "vyos login: "
            con.write("\r")
            nudged_at = con.clock()
            continue
        if idle >= 2.0 and LOGIN_RE.search(tail):
            con.write(user + "\r")
            i, data = con.expect([re.compile(rb"assword: ?$")], 15)
            if i < 0:
                # e.g. the build's kexec reboot hit mid-login: whatever came
                # in instead (possibly the next login prompt) stays in buf
                buf, last_rx = data[-8192:], con.clock()
                continue
            con.write(password + "\r", secret=True)
            i, data = con.expect([PROMPT_RE, LOGIN_RE], 60)
            if i == 0:
                return True, ""
            verbose("VyOS live login did not complete — waiting for the next login prompt", "warning")
            buf, last_rx = data[-8192:], con.clock()
    return False, f"no VyOS login prompt within {int(timeout)} s"


_DONE = "__MI_DONE__"


def live_cmd(con: Console, cmd: str, timeout: float = 60.0) -> str:
    """
    Run a SHORT command in the VyOS live shell and return its output.
    The end marker is typed as "__MI_""DONE__" so only the command's own
    output (not the echoed command line) contains __MI_DONE__.
    """
    con.drain(idle=0.3, max_wait=3.0)
    con.write(f'{cmd}; echo "__MI_""DONE__"\r')
    i, data = con.expect([re.compile(_DONE.encode() + rb"\r?\n")], timeout)
    text = _clean(data).decode("utf-8", errors="replace")
    if i < 0:
        raise TimeoutError(f"no answer to {cmd!r} within {int(timeout)} s")
    con.expect([PROMPT_RE], 10)
    echo_end = text.rfind('"__MI_""DONE__"')
    if echo_end >= 0:
        nl = text.find("\n", echo_end)
        text = text[nl + 1:] if nl >= 0 else ""
    return text[:text.rfind(_DONE)].strip()


# --- `install image` dialogue -------------------------------------------------
# Prompt texts: vyos-1x src/op_mode/image_installer.py (ask_yes_no adds
# " [y/N] " / " [Y/n] ", ask_input adds " (Default: X) "). Matched on the
# distinctive part only, so small wording changes between builds still
# land on the safe generic rule: accept VyOS's own default (Enter) — the
# same "accept defaults for the rest" INSTALL.md tells people to do.

_Q_CONTINUE  = "Would you like to continue?"
_Q_RAID      = "RAID-1"
_Q_DISK      = "Which one should be used for installation?"
_Q_DELETE    = "Installation will delete all data on the drive"
_Q_PW        = 'Please enter a password for the "vyos" user'
_Q_PW2       = 'Please confirm password for the "vyos" user'
_Q_CONSOLE   = "What console should be used by default?"
_SUCCESS     = "The image installed successfully"
_INVALID     = "Invalid value, try again"
# Only questions that HAVE a default get a blind Enter. A stray Enter
# typed while the installer is busy would sit in the tty buffer and be
# eaten by the next question (e.g. "delete all data? [y/N]" -> N -> abort).
_GENERIC_Q   = re.compile(r"(\[[yY]/[nN]\]|\(Default: [^)]*\))\s*$")
_OPEN_Q      = re.compile(r"[?:]\s*$")
_STUCK_AFTER = 300.0   # s of silence on an unanswerable question -> give up


def _pick_emmc(text: str, prefix: str = "mmcblk"):
    drives = re.findall(r"Drive:\s+(/dev/\S+)", text)
    for d in drives:
        if re.fullmatch(rf"/dev/{prefix}\d+", d):
            return d
    return None


def run_install_image(con: Console, password: str, timeout: float = 2400.0,
                      max_answers: int = 40):
    """
    Drive `install image` in the VyOS live shell. Explicit answers:
    continue=y, RAID-1=n, target=the eMMC (/dev/mmcblk*, never the USB
    stick), delete-all-data=y, password x2, console=S (serial). Any other
    question gets Enter = VyOS's own default. Returns (ok, transcript).
    """
    con.drain(idle=0.5, max_wait=3.0)
    con.write("install image\r")
    buf = b""
    answered_at = 0
    answers = 0
    invalid = 0
    last_rx = con.clock()
    start = con.clock()
    while con.clock() - start < timeout:
        chunk = con.read_available()
        if chunk:
            buf += chunk
            last_rx = con.clock()
            continue
        con.sleep(0.05)
        if con.clock() - last_rx < 0.8:
            continue
        pending = _clean(buf[answered_at:]).decode("utf-8", errors="replace")
        full = _clean(buf).decode("utf-8", errors="replace")
        if not pending.strip():
            continue
        if PROMPT_RE.search(pending.encode()):
            ok = _SUCCESS in full
            return ok, full[-3000:]
        if answers >= max_answers:
            con.write("\x03")
            return False, "too many questions — stopped: " + full[-1500:]
        if _INVALID in pending:
            invalid += 1
            if invalid > 3:
                con.write("\x03")
                return False, "the installer kept rejecting answers: " + full[-1500:]

        last_line = pending.rstrip().splitlines()[-1] if pending.strip() else ""
        answer, secret = None, False
        if _Q_PW2 in last_line or _Q_PW in last_line:
            answer, secret = password, True
        elif _Q_DELETE in last_line:
            answer = "y"
        elif _Q_CONTINUE in last_line:
            answer = "y"
        elif _Q_RAID in last_line:
            answer = "n"
        elif _Q_DISK in last_line:
            answer = _pick_emmc(full)
            if answer is None:
                con.write("\x03")
                return False, "the installer offered no eMMC (/dev/mmcblk*) — stopped before partitioning"
        elif _Q_CONSOLE in last_line:
            answer = "S"
        elif _GENERIC_Q.search(last_line):
            verbose(f"  install image asked: {last_line.strip()!r} — accepting VyOS's default", "debug")
            answer = ""
        elif _OPEN_Q.search(last_line) and con.clock() - last_rx >= _STUCK_AFTER:
            con.write("\x03")
            return False, f"the installer is waiting for an unexpected question: {last_line.strip()!r}"
        else:
            continue   # still printing (e.g. copying files) — keep waiting

        con.write(answer + "\r", secret=secret)
        answers += 1
        answered_at = len(buf)
    return False, f"install image did not finish within {int(timeout)} s"
