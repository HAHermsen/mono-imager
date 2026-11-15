"""
mono-imager: console.py — pure presentation, every branch.

Each function is called with plain data; stdout is captured and input() is
scripted, so the tests check exactly what the user sees and what comes back.

Run: python tests/unit/test_console.py
"""

import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import console
from mono_imager import constants as C

VERSION = "1.7.5"
AUTHOR = "H.A. Hermsen"
NET = {"ip": "192.168.168.200", "prefix": 24, "gateway": "192.168.168.1",
       "dns": "192.168.168.1", "source": "dhcp"}
NET_NO_DNS = {k: v for k, v in NET.items() if k != "dns"}
LONG_VERSION = "1.7.5-rc1+a-very-long-local-build-tag-that-widens-the-box"
CLEAR_SCREEN = "\033[2J\033[H"
ESCAPE_WORD = "exit!"
OTHER_ESCAPE_WORD = "quit"
ANSI_CHUNK = "\x1b[1;32mOK\x1b[0m done\x1b[2K\n"
PLAIN_CHUNK = "OK done\n"
NOR_BOOT_OS = "OPNsense"        # final_dip() == "NOR"
EMMC_BOOT_OS = "Armbian"        # final_dip() == "EMMC"
FLASH_TARGET = "/dev/mmcblk0"
FIRMWARE = "C:/fw/Armbian.img.xz"
IDENTITY = {"Board": "Mono Gateway", "Serial": "MG-0001"}
SELF_TEST_OK = [("DDR", "8 GiB", True), ("eMMC", "", True)]
SELF_TEST_BAD = [("DDR", "8 GiB", True), ("PCIe", "link down", False), ("SATA", "", False)]

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


def shown(fn, *args, answers=(), **kwargs):
    """Call fn with scripted input(); return (result, printed text)."""
    out = io.StringIO()
    it = iter(answers)
    with patch("builtins.input", lambda prompt="": next(it)), contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


# ----------------------------------------------------------------------------
section("clear_screen / read_line")
_, out = shown(console.clear_screen)
check("clear_screen writes the ANSI clear + home sequence", out == CLEAR_SCREEN)

res, out = shown(console.read_line, "> ", answers=["  hello  "])
check("read_line strips the answer", res == "hello" and out == "")
for typed in (ESCAPE_WORD, ESCAPE_WORD.upper(), f"  {ESCAPE_WORD} "):
    res, out = shown(console.read_line, "> ", answers=[typed])
    check(f"read_line {typed!r} -> ESCAPE, with a notice", res is console.ESCAPE and "Escaping" in out)
res, _ = shown(console.read_line, "> ", OTHER_ESCAPE_WORD, answers=[OTHER_ESCAPE_WORD])
check("custom escape word honoured", res is console.ESCAPE)
res, _ = shown(console.read_line, "> ", OTHER_ESCAPE_WORD, answers=[ESCAPE_WORD])
check("default escape word is plain text when another is set", res == ESCAPE_WORD)


# ----------------------------------------------------------------------------
section("print_header / print_status_line")
for what, version, net in (("with DNS", VERSION, NET), ("without DNS", VERSION, NET_NO_DNS),
                           ("no network yet", VERSION, None), ("long version", LONG_VERSION, NET)):
    _, out = shown(console.print_header, version, AUTHOR, net)
    box = [line for line in out.splitlines() if line]
    check(f"header {what}: every box line has the same width", len({len(line) for line in box}) == 1)
    check(f"header {what}: plain ASCII box (Windows console safe)",
          all(line[0] in "+|" and line[-1] in "+|" for line in box) and out.isascii())
    check(f"header {what}: version and author shown", f"mono-imager {version}" in out and AUTHOR in out)
    check(f"header {what}: box hugs the widest line (one space each side)",
          any(line.startswith("| ") and line.endswith(" |") and not line.endswith("  |") for line in box))
_, out = shown(console.print_header, VERSION, AUTHOR, NET)
check("header with DNS: network line complete",
      "Device network: 192.168.168.200/24 via 192.168.168.1 (DNS 192.168.168.1) - dhcp" in out)
_, out = shown(console.print_header, VERSION, AUTHOR, NET_NO_DNS)
check("header without DNS: no DNS note", "(DNS" not in out and "via 192.168.168.1 - dhcp" in out)
_, out = shown(console.print_header, VERSION, AUTHOR, None)
check("header without network: 'not yet detected'", "Device network: not yet detected" in out)

_, out = shown(console.print_status_line, VERSION, NET)
check("status line with network",
      out == f"mono-imager {VERSION} - 192.168.168.200/24 via 192.168.168.1 (DNS 192.168.168.1) - dhcp\n")
_, out = shown(console.print_status_line, VERSION, NET_NO_DNS)
check("status line without DNS", "(DNS" not in out and out.isascii())
_, out = shown(console.print_status_line, VERSION, None)
check("status line without network", out == f"mono-imager {VERSION} - network not yet detected\n")


# ----------------------------------------------------------------------------
section("check")
results: list[bool] = []
res, out = shown(console.check, results, "Thing works", True)
check("pass -> True, ✓, recorded", res is True and out == "  ✓  Thing works\n" and results == [True])
res, out = shown(console.check, results, "Thing fails", False, "because reasons")
check("fail with detail -> False, ✗ + detail line, recorded",
      res is False and out == "  ✗  Thing fails\n     because reasons\n" and results == [True, False])


# ----------------------------------------------------------------------------
section("show_flash_confirmation")


def confirm(os_name, answer):
    return shown(console.show_flash_confirmation, os_name=os_name, port="COM5", firmware_path=FIRMWARE,
                 flash_target=FLASH_TARGET, host_ip="192.168.168.89", device_ip="192.168.168.200",
                 answers=[answer])


for answer, want in (("y", True), ("Y", True), ("n", False), ("", False), ("yes", False)):
    res, _ = confirm(EMMC_BOOT_OS, answer)
    check(f"answer {answer!r} -> {want}", res is want)
res, out = confirm(EMMC_BOOT_OS, ESCAPE_WORD)
check("exit! -> ESCAPE", res is console.ESCAPE)
check("summary lists OS, port, firmware, target and IPs",
      all(s in out for s in (EMMC_BOOT_OS, "COM5", FIRMWARE, FLASH_TARGET, "192.168.168.89:8080",
                             "192.168.168.200")))
check(f"{EMMC_BOOT_OS} (boots from eMMC): explains the DIP switch", "LEFT  = eMMC" in out and "keep the DIP" not in out)
res, out = confirm(NOR_BOOT_OS, "n")
check(f"{NOR_BOOT_OS} (boots via NOR): keep the DIP RIGHT", "keep the DIP switch RIGHT" in out and
      f"(NOR). {NOR_BOOT_OS}" in out and "LEFT  = eMMC" not in out)
check("risk warning shown", "never" in out and "without risk" in out)


# ----------------------------------------------------------------------------
section("show_firmware_output")
with patch.object(console.logger, "debug") as dbg:
    _, out = shown(console.show_firmware_output, ANSI_CHUNK)
check("ANSI colour/erase codes stripped from the screen", out == PLAIN_CHUNK)
check("raw chunk (with codes) logged for post-mortem", dbg.call_args.args[1] == ANSI_CHUNK)


# ----------------------------------------------------------------------------
section("show_device_stats")
_, out = shown(console.show_device_stats, {}, [])
check("nothing parsed -> one warning, no sections", "No recognizable U-Boot diagnostic output" in out
      and "BOARD IDENTITY" not in out and "SELF-TEST" not in out)

_, out = shown(console.show_device_stats, IDENTITY, [])
check("identity only -> identity section, no self-test",
      "BOARD IDENTITY" in out and "SELF-TEST" not in out and f"    {'Serial':<22} MG-0001" in out)
check("section rules use BOX_RULE_WIDTH", "─" * C.BOX_RULE_WIDTH in out)

_, out = shown(console.show_device_stats, {}, SELF_TEST_OK)
check("all self-tests pass -> ✓ lines, no failure note",
      "✓  DDR  8 GiB" in out and "✓  eMMC\n" in out and "[FAIL]" not in out and "BOARD IDENTITY" not in out)

_, out = shown(console.show_device_stats, IDENTITY, SELF_TEST_BAD)
check("failures marked ✗ with their detail", "✗  PCIe  link down" in out and "✗  SATA\n" in out)
check("failure count summarised", "2 self-test item(s) reported [FAIL]" in out)


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
