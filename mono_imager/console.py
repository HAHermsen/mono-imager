"""
mono-imager: console rendering.

Pure presentation — every function here takes exactly the data it needs
and returns either nothing (it printed) or a plain value read from the
user. Nothing in this module reads or mutates MonoImager state, holds a
reference to the device, or knows about MenuState. That coupling stays
in tui.py by design: this is the layer that can be unit-tested with a
captured stdout and no hardware.

Author:  H.A. Hermsen
License: GPLv3
"""

import re
import sys
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Sentinel returned by read_line() when the user types the escape word.
# tui.py maps this to its own MenuState transition; console.py never
# touches menu state itself.
ESCAPE = object()


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def read_line(prompt: str, escape_word: str = "exit!"):
    """
    input() wrapper. Returns the stripped string, or the ESCAPE sentinel
    if the user typed the escape word. The caller decides what ESCAPE
    means for menu state — this function has no opinion.
    """
    value = input(prompt).strip()
    if value.lower() == escape_word:
        print("\n  ⚠️  Escaping to main menu...")
        return ESCAPE
    return value


def print_header(version: str, author: str, device_net: Optional[dict]) -> None:
    """
    Application header. Box width is computed from actual content, so it
    stays aligned regardless of version-string length.
    """
    version_line = f"mono-imager {version}"
    subtitle     = "mono gateway firmware flash utility"
    license_line = f"written by {author}, GPLv3 licensed"

    if device_net:
        dns_note = f" (DNS {device_net['dns']})" if device_net.get("dns") else ""
        network_line = (
            f"Device network: {device_net['ip']}/{device_net['prefix']} "
            f"via {device_net['gateway']}{dns_note} - {device_net['source']}"
        )
    else:
        network_line = "Device network: not yet detected"

    inner_width = max(
        len(version_line), len(subtitle), len(license_line), len(network_line)
    ) + 2

    # Plain ASCII box (not Unicode): on a stock Windows console outside a
    # UTF-8 codepage, box-drawing chars render at mismatched widths and
    # the border drifts. ASCII is always one column wide everywhere.
    def left_aligned(text):
        pad = inner_width - len(text) - 1
        return "| " + text + " " * pad + "|"

    print("+" + "-" * inner_width + "+")
    print(left_aligned(version_line))
    print(left_aligned(subtitle))
    print(left_aligned(license_line))
    print(left_aligned(""))
    print(left_aligned(network_line))
    print("+" + "-" * inner_width + "+")
    print()


def check(results: list, label: str, passed: bool, detail: str = "") -> bool:
    """Record pass/fail in results and print a status line."""
    mark = "✓" if passed else "✗"
    print(f"  {mark}  {label}")
    if detail:
        print(f"     {detail}")
    results.append(passed)
    return passed


def show_flash_confirmation(
    *,
    os_name: str,
    port: str,
    firmware_path,
    flash_target: str,
    host_ip: str,
    device_ip: str,
):
    """
    Print the pre-flash summary and NOR/eMMC diagram, then prompt.
    Returns True (confirmed), False (declined), or ESCAPE (exit! escape).
    """
    print()
    print("  About to flash:")
    print(f"    OS:          {os_name}")
    print(f"    Port:        {port}")
    print(f"    Firmware:    {firmware_path}")
    print(f"    Target:      {flash_target}")
    print(f"    Host IP:     {host_ip}:8080  (auto-detected)")
    print(f"    Device IP:   {device_ip}")
    print()
    print("  +--------------------------------------------------+")
    print("  | This writes to eMMC, the device's main storage   |")
    print("  | (32 GB). It does NOT touch NOR flash (64 MB -    |")
    print("  | the bootloader + recovery tool).                 |")
    print("  |                                                  |")
    print("  |    NOR (64 MB)          eMMC (32 GB)             |")
    print("  |   +-------------+      +-------------------+     |")
    print("  |   | Bootloader  |      |  Your OS goes      |    |")
    print("  |   | + Recovery  |      |  here - this is    |    |")
    print("  |   | (untouched) |      |  what gets         |    |")
    print("  |   +-------------+      |  flashed now [OK]  |    |")
    print("  |                        +-------------------+     |")
    print("  |                                                  |")
    if os_name == "OPNsense":
        print("  | After flashing: keep the DIP switch RIGHT        |")
        print("  | (NOR). OPNsense boots via NOR itself, which      |")
        print("  | loads the OS from eMMC — no DIP flip needed.     |")
    else:
        print("  | After flashing, the DIP switch picks which one   |")
        print("  | the board actually boots:                        |")
        print("  |   LEFT  = eMMC  (your new OS boots)              |")
        print("  |   RIGHT = NOR   (boots recovery instead)         |")
    print("  +--------------------------------------------------+")
    print()
    print("  This tool is well tested, but writing firmware is never")
    print("  without risk. Do not unplug power or disconnect the cable")
    print("  while flashing — an interrupted write can leave the")
    print("  device unbootable.")
    print()
    confirm = read_line("  This writes to the device. Proceed? [y/N]: ")
    if confirm is ESCAPE:
        return ESCAPE
    return confirm.lower() == "y"


def show_firmware_output(chunk: str) -> None:
    """Print live firmware-update output; log raw bytes for post-mortem."""
    clean = re.sub(r'\x1b\[[0-9;:]*[mGKHF]', '', chunk)
    print(clean, end="", flush=True)
    logger.debug("[firmware update] %r", chunk)


def show_device_stats(identity: dict, self_test: list) -> None:
    """
    Render parsed U-Boot diagnostics. Parsing stays in the domain layer;
    this function only takes the already-parsed structures.
    """
    if not identity and not self_test:
        print()
        print("  ⚠️  No recognizable U-Boot diagnostic output found.")
        return

    if identity:
        print()
        print("  " + "─" * 56)
        print("  BOARD IDENTITY")
        print("  " + "─" * 56)
        for label, value in identity.items():
            print(f"    {label:<22} {value}")

    if self_test:
        print()
        print("  " + "─" * 56)
        print("  SELF-TEST")
        print("  " + "─" * 56)
        failures = 0
        for label, value, passed in self_test:
            detail = f"  {value}" if value else ""
            mark = "✓" if passed else "✗"
            print(f"    {mark}  {label}{detail}")
            if not passed:
                failures += 1
        if failures:
            print()
            print(f"  ⚠️  {failures} self-test item(s) reported [FAIL] — see above.")

    print()
