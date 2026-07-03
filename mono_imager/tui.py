#!/usr/bin/env python3
"""
mono-imager: Automated firmware flashing for Mono Gateway Routers and Dev Kit
Supports serial and networked connections with menu-driven TUI.

Author:  H.A. Hermsen
Version: v1.1.0
License: GPLv3
"""

from mono_imager import __version__  # single source of truth: mono_imager/__init__.py
__author__ = "H.A. Hermsen"

import sys
import os
import re

# U-Boot output regexes — compiled once at module load, not per call.
_RE_SOC       = re.compile(r'^SoC:\s+(.+)$',              re.MULTILINE)
_RE_MODEL     = re.compile(r'^Model:\s+(.+)$',            re.MULTILINE)
_RE_DRAM      = re.compile(r'^DRAM:\s+(.+)$',             re.MULTILINE)
_RE_CPU_CLK   = re.compile(r'CPU\d+\([^)]*\):(\d+)\s*MHz')
_RE_BUS_CLK   = re.compile(r'Bus:\s+(\d+)\s*MHz')
_RE_DDR_CLK   = re.compile(r'DDR:\s+(\d+)\s*MT/s')
_RE_FMAN_CLK  = re.compile(r'FMAN:\s+(\d+)\s*MHz')
import time
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
from mono_imager.spinner import with_spinner, Spinner

logger = logging.getLogger(__name__)

_LOG_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING, "debug": logging.DEBUG}

def verbose(msg: str, level: str = "info"):
    """Print to console immediately AND log it"""
    print(msg, flush=True)
    logger.log(_LOG_LEVELS.get(level, logging.INFO), msg)


def _netmask_to_prefix(value: str) -> Optional[str]:
    """
    Accept either a dotted subnet mask (255.255.255.0) or a bare CIDR
    prefix length (24) from manual entry, and return the CIDR prefix
    string `ip addr add` needs. Returns None on anything unparseable
    or a non-contiguous mask, so the caller can re-prompt rather than
    silently apply a nonsense value.
    """
    value = value.strip()
    if not value:
        return None
    if "." not in value:
        return value if value.isdigit() and 0 <= int(value) <= 32 else None
    try:
        octets = [int(o) for o in value.split(".")]
    except ValueError:
        return None
    if len(octets) != 4 or any(not 0 <= o <= 255 for o in octets):
        return None
    bits = "".join(f"{o:08b}" for o in octets)
    if "01" in bits:  # a 0 followed by a 1 means the mask isn't contiguous
        return None
    return str(bits.count("1"))



_UBOOT_FIELDS = [
    ("SoC",        _RE_SOC,      lambda m: m.group(1).strip()),
    ("Model",      _RE_MODEL,    lambda m: m.group(1).strip()),
    ("DRAM",       _RE_DRAM,     lambda m: m.group(1).strip()),
    ("Bus clock",  _RE_BUS_CLK,  lambda m: f"{m.group(1)} MHz"),
    ("DDR clock",  _RE_DDR_CLK,  lambda m: f"{m.group(1)} MT/s"),
    ("FMAN clock", _RE_FMAN_CLK, lambda m: f"{m.group(1)} MHz"),
]


def parse_uboot_identity(raw_output: str) -> dict:
    """
    Parse SoC/board identity and clock configuration from raw U-Boot
    boot output. Patterns validated against a real capture on this
    hardware (LS1046A / Mono Gateway Development Kit):

        SoC:  LS1046AE Rev1.0 (0x87070010)
        Clock Configuration:
               CPU0(A72):1600 MHz  CPU1(A72):1600 MHz  CPU2(A72):1600 MHz
               CPU3(A72):1600 MHz
               Bus:      600  MHz  DDR:      2100 MT/s  FMAN:     700  MHz
        Model: Mono Gateway Development Kit
        DRAM:  7.9 GiB (DDR4, 64-bit, CL=16, ECC on)

    Returns a dict of only the fields actually found — missing fields
    are simply absent, never guessed or defaulted.
    """
    result = {}
    for label, pattern, fmt in _UBOOT_FIELDS:
        if (m := pattern.search(raw_output)):
            result[label] = fmt(m)
    cpu_clocks = _RE_CPU_CLK.findall(raw_output)
    if cpu_clocks:
        unique = sorted(set(cpu_clocks), key=int)
        if len(unique) == 1:
            result["CPU clock"] = f"{unique[0]} MHz (all cores)"
        else:
            result["CPU clock"] = ", ".join(f"{c} MHz" for c in cpu_clocks)
    return result


_OK_PREFIX = "[ OK ]"

def parse_uboot_self_test(raw_output: str) -> list:
    """
    Parse the power-on self-test block from raw U-Boot boot output.
    Matches lines of the form "[ OK ] Label            value",
    e.g.:

        [ OK ] DDR4 Memory          Bank0: 1982 MB, Bank1: 6144 MB
        [ OK ] USB PD controller    ID 0x25
        [ OK ] Temperatures         CPU 51 °C, Board 46 °C

    Only matches [ OK ] lines — failures are not captured.

    Returns a list of (label, value) tuples in the order they appeared.
    value is "" if only the label is present with no value/detail.
    """
    out = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line.startswith(_OK_PREFIX):
            continue
        label, _, value = line[len(_OK_PREFIX):].strip().partition(" ")
        if label.lower() != "self-test":
            out.append((label, value.strip()))
    return out




class MenuState(Enum):
    """Main menu states"""
    MAIN                   = "main"
    FLASH_AUTO_OR_MANUAL   = "flash_auto_or_manual"
    NETWORK_AUTO_CONFIG    = "network_auto_config"
    NETWORK_FLASHING      = "network_flashing"
    UPDATE_EMMC            = "update_emmc"
    UPDATE_NOR             = "update_nor"
    DONE                  = "done"
    CLI_CONSOLE           = "cli_console"
    DEVICE_STATS          = "device_stats"


class MonoImager:
    """Main application controller"""

    def __init__(self, log_file: Optional[Path] = None):
        self.current_state   = MenuState.MAIN
        self.device          = None
        self.custom_fw_path  = None
        self.serial_port     = None
        self.flash_success   = False
        self.log_file        = log_file
        self.transfer_method = None
        self.net_host_ip     = None
        self.net_device_ip   = None
        self.net_http_port   = 8080
        self.net_flash_target = None
        self.os_name         = None
        # Device's own recovery-shell network (DHCP or manual) — resolved
        # once per session by _setup_recovery_network() and reused by every
        # later eMMC/NOR operation rather than re-detected/re-asked.
        # {"ip", "prefix", "gateway", "dns", "source": "dhcp"|"manual"}
        self.device_net       = None
        # True once real internet reachability (not just local config)
        # has been proven for self.device_net at least once this session.
        # Recovery Linux forgets its network config every reboot, so
        # _setup_recovery_network() still has to re-apply the IP each
        # time — but the path itself (cable, switch, gateway, upstream
        # route) doesn't change between reboots of the same device in
        # the same session, so once it's proven reachable there's no
        # need to pay for a fresh ping-based re-verification (several
        # seconds of serial round trips) on every subsequent recovery
        # boot. Reset to False whenever device_net itself is invalidated
        # and re-resolved (see _setup_recovery_network()).
        self.device_net_verified = False

    def clear_screen(self):
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    
    def safe_input(self, prompt: str) -> Optional[str]:
        """
        Wrapper around input() that checks for 'exit!' escape sequence.
        If user types 'exit!', return None and set state to FLASH_AUTO_OR_MANUAL.
        Otherwise return the user's input.
        """
        user_input = input(prompt).strip()
        if user_input.lower() == "exit!":
            print("\n  ⚠️  Escaping to main menu...")
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
            return None
        return user_input

    def print_header(self):
        """
        Print application header. Box width is computed from the
        actual content rather than hardcoded, so it stays correctly
        aligned regardless of version string length (e.g. "0.5.0" vs
        a future "0.10.0") instead of needing manual padding upkeep.
        """
        version_line = f"mono-imager {__version__}"
        subtitle      = "mono gateway firmware flash utility"
        license_line  = f"written by {__author__}, GPLv3 licensed"

        net = self.device_net
        if net:
            dns_note = f" (DNS {net['dns']})" if net.get("dns") else ""
            network_line = (
                f"Device network: {net['ip']}/{net['prefix']} via {net['gateway']}"
                f"{dns_note} - {net['source']}"
            )
        else:
            network_line = "Device network: not yet detected"

        inner_width = max(len(version_line), len(subtitle), len(license_line), len(network_line)) + 2

        # Plain ASCII box-drawing, not Unicode (+/-/| instead of the
        # box-drawing block). CONFIRMED BUG THIS AVOIDS: on a stock
        # Windows console (not in UTF-8 codepage — the default), Unicode
        # box-drawing characters and the em dash render as mismatched
        # widths, making the border drift out of alignment even though
        # every line here is the same computed length. Plain ASCII is
        # always exactly one column wide everywhere, so it can't drift.
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

    # Minimum plausible firmware size. This is a sanity check against
    # obviously wrong paths (a README, a 0-byte placeholder, a text
    # file typo) — NOT a content/format check. mono-imager deliberately
    # never inspects what's inside the file (dumb-pipe by design); this
    # only catches "that can't possibly be a real image" before
    # spending several minutes of a flash attempt on it.
    MIN_FIRMWARE_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB

    def _validate_firmware_path(self, raw_input: str):
        """
        Validate a user-typed firmware path. Strips surrounding quotes
        (Windows users naturally type quoted paths with spaces).
        Returns (Path, None) on success, or (None, error_message) on
        failure — error_message is ready to print directly.

        Checks performed: non-empty input, path exists, is a file (not
        a directory), is actually readable, and is at least
        MIN_FIRMWARE_SIZE_BYTES. Does NOT check file contents/format —
        that's intentionally out of scope.
        """
        raw = raw_input.strip().strip('"').strip("'")
        if not raw:
            return None, "No path entered."

        path = Path(raw).expanduser()

        if not path.exists():
            return None, f"File not found: {path}"

        if path.is_dir():
            return None, f"That's a folder, not a file: {path}"

        if not path.is_file():
            return None, f"Not a regular file: {path}"

        try:
            size = path.stat().st_size
        except OSError as e:
            return None, f"Can't read file info: {e}"

        if size == 0:
            return None, f"File is empty (0 bytes): {path}"

        if size < self.MIN_FIRMWARE_SIZE_BYTES:
            size_kb = size / 1024
            return None, (
                f"File is only {size_kb:.0f} KB — too small to be a real "
                f"firmware image. Check the path is correct."
            )

        # Confirm it's actually readable (catches permission issues
        # before they surface mid-flash as a confusing curl/dd error).
        try:
            with open(path, "rb") as f:
                f.read(1)
        except OSError as e:
            return None, f"Can't read file: {e}"

        return path, None

    # All imports inside methods are intentional — deferred to keep
    # startup fast and avoid circular import issues at module load time.

    def _soft_reboot_if_possible(self, port: str):
        """
        Best-effort: if the device is already reachable over serial right
        now (e.g. left sitting in a recovery Linux shell or at the U-Boot
        prompt from earlier this session), trigger its reboot over the
        wire instead of making the user physically power-cycle it.

        CONFIRMED GAP THIS CLOSES: phase1_uboot()'s own skip-the-wait
        check (probe_uboot_prompt()) only fires when the device happens
        to already be sitting AT the U-Boot prompt. If it's one step
        further along — in a Linux shell, as it typically is right after
        a previous recovery-shell operation — that check can't help, and
        every caller of phase1_bootstrap() fell back to asking for a
        manual power cycle even though a plain `reboot` typed into that
        shell would do the exact same job over the wire.

        Purely best-effort and silent: if the device isn't reachable this
        way (freshly powered off, wrong state, whatever), this does
        nothing and phase1_uboot()'s "POWER CYCLE NOW" prompt is still
        there as the fallback — this can only ever remove an unnecessary
        physical step, never break the flow that already existed.
        """
        from mono_imager.serial_device import SerialDevice
        import time
        try:
            _d = SerialDevice(port, timeout=2)
            if _d.connect(115200):
                _d.ser.write(b"\r\nreset\r\nreboot\r\n")
                time.sleep(0.5)
                _d.disconnect()
        except Exception:
            pass

    def _select_port(
        self,
        *,
        auto_select_single: bool = False,
        show_categories: bool = False,
        allow_back: bool = True,
        allow_enter_last: bool = False,
        save_on_select: bool = False,
        quiet: bool = False,
    ) -> Optional[str]:
        """
        Detect serial ports, list them, and prompt for a selection.
        Returns the chosen device string, or None if detection failed,
        no ports found, user chose Back, or input was invalid.

        quiet=True suppresses this method's own "Press Enter to
        continue..." messaging on detection failure / no ports found,
        for callers (e.g. startup network detection) that show their
        own message and decide what happens next themselves — avoids
        stacking two different prompts back to back.
        """
        from mono_imager.config import detect_serial_ports, get_last_port, save_last_port

        try:
            known, other = detect_serial_ports()
            all_ports = known + other
        except Exception as e:
            if not quiet:
                print(f"  ❌ Port detection failed: {e}")
                input("  Press Enter to continue...")
            return None

        if not all_ports:
            if not quiet:
                print("  ❌ No serial devices found. Connect the USB-to-UART cable and try again.")
                input("  Press Enter to continue...")
            return None

        if auto_select_single and len(all_ports) == 1:
            port = all_ports[0].device
            print(f"  Auto-selected: {port} ({all_ports[0].description})")
            return port

        last_port = get_last_port()

        if show_categories:
            if known:
                print("  USB-UART adapters (recommended):")
                for i, p in enumerate(known, 1):
                    marker = " ◄ last used" if p.device == last_port else ""
                    print(f"    {i}) {p.device} — {p.description}{marker}")
            if other:
                if known:
                    print()
                print("  Other ports:")
                offset = len(known)
                for i, p in enumerate(other, offset + 1):
                    marker = " ◄ last used" if p.device == last_port else ""
                    print(f"    {i}) {p.device} — {p.description}{marker}")
        else:
            for i, p in enumerate(all_ports, 1):
                marker = " ◄ last used" if p.device == last_port else ""
                print(f"  {i}) {p.device} — {p.description}{marker}")

        if allow_back:
            print()
            print(f"  {len(all_ports) + 1}) Back")

        enter_uses_last = allow_enter_last and last_port and any(p.device == last_port for p in all_ports)
        if enter_uses_last:
            print()
            print(f"  [Enter] Use last port ({last_port})")
        print()

        total = len(all_ports) + (1 if allow_back else 0)
        choice = input(f"Select [1-{total}]: ").strip()

        if enter_uses_last and choice == "":
            if save_on_select:
                save_last_port(last_port)
            return last_port

        try:
            idx = int(choice) - 1
            if allow_back and idx == len(all_ports):
                return None
            if not (0 <= idx < len(all_ports)):
                raise ValueError("out of range")
            port_device = all_ports[idx].device
            if save_on_select:
                save_last_port(port_device)
            return port_device
        except ValueError:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            return None

    def _check(self, results: list, label: str, passed: bool, detail: str = "") -> bool:
        """Record pass/fail in results and print a status line."""
        mark = "✓" if passed else "✗"
        print(f"  {mark}  {label}")
        if detail:
            print(f"     {detail}")
        results.append(passed)
        return passed

    def _show_flash_confirmation(
        self,
        *,
        os_name: str,
        port: str,
        firmware_path,
        flash_target: str,
        host_ip: str,
        device_ip: str,
    ) -> Optional[bool]:
        """
        Print the pre-flash summary and NOR/eMMC diagram, then prompt.
        Returns True (confirmed), False (declined), or None (exit! escape).
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
        confirm = self.safe_input("  This writes to the device. Proceed? [y/N]: ")
        if confirm is None:
            return None
        return confirm.lower() == "y"

    def _recovery_finish(self, success: bool) -> None:
        """Set flash result, wait for Enter, and return to main menu."""
        self.flash_success = success
        input("  Press Enter to continue...")
        self.current_state = MenuState.MAIN

    def _show_firmware_output(self, chunk: str) -> None:
        """Print live firmware-update output; log raw bytes to file for post-mortem."""
        clean = re.sub(r'\x1b\[[0-9;:]*[mGKHF]', '', chunk)
        print(clean, end="", flush=True)
        logger.debug("[firmware update] %r", chunk)

    # ------------------------------------------------------------------ #
    #  1. MAIN MENU                                                        #
    # ------------------------------------------------------------------ #
    def menu_main(self):
        """Main menu — action first"""
        self.clear_screen()
        self.print_header()
        print("What would you like to do?")
        print()
        print("  1) Flash OS")
        print("  2) Update eMMC firmware")
        print("  3) Update NOR firmware")
        print("  4) CLI only (serial)")
        print("  5) Test Serial connection")
        print("  6) Test LAN connection")
        print("  7) Test USB stick")
        print("  8) Show Device Stats")
        print("  9) Exit")
        print()

        choice = input("Select [1-9]: ").strip()

        if choice == "1":
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
        elif choice == "2":
            self.current_state = MenuState.UPDATE_EMMC
        elif choice == "3":
            self.current_state = MenuState.UPDATE_NOR
        elif choice == "4":
            self.current_state = MenuState.CLI_CONSOLE
        elif choice == "5":
            self.menu_test_serial()
        elif choice == "6":
            self.menu_test_lan()
        elif choice == "7":
            self.menu_test_usb_mount()
        elif choice == "8":
            self.current_state = MenuState.DEVICE_STATS
        elif choice == "9":
            sys.exit(0)
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN

    def menu_flash_auto_or_manual(self):
        self.clear_screen()
        self.print_header()
        print("  1) Fully Auto — flash via LAN or USB")
        print("  2) Back")
        print()

        choice = input("Select [1-2]: ").strip()

        if choice == "1":
            self.current_state = MenuState.NETWORK_AUTO_CONFIG
        elif choice == "2":
            self.current_state = MenuState.MAIN
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL

    def menu_network_auto_config(self):
        """
        Fully-auto path — matches tests/test_verify_flash_auto.py
        exactly. Only the firmware file is asked; the serial port is
        auto-detected (first port found), host IP is auto-detected,
        device IP is auto-derived from it, HTTP port defaults to 8080,
        flash target defaults to /dev/mmcblk0.
        """
        from mono_imager import flash_orchestrator as core
        from mono_imager.config import detect_serial_ports

        self.clear_screen()
        self.print_header()
        print("  Fully Auto")
        print()

        try:
            known, other = detect_serial_ports()
            all_ports = known + other
        except Exception as e:
            print(f"  ❌ Port auto-detection failed: {e}")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        if not all_ports:
            print("  ❌ No serial port detected. Connect the device, or")
            print("  use Manual mode to specify one.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
            return

        port = all_ports[0].device
        print(f"  Auto-detected port: {port} ({all_ports[0].description})")
        print()
        print("  ℹ️  Images over ~3GB auto-switch to streaming flash mode (slower, no local copy).")
        print()

        # OS selection — built dynamically from discovered journey files
        from mono_imager.journeys import discovered_journeys

        journeys = discovered_journeys()  # [(os_name, transfer), ...]

        print("  Select OS and flash method:")
        for i, (os_name, transfer) in enumerate(journeys, 1):
            print(f"    {i}) {os_name} via {transfer}")
        back_idx = len(journeys) + 1
        print(f"    {back_idx}) Back")
        print("  (Type 'exit!' at any prompt to escape to main menu)")
        print()
        os_choice = self.safe_input(f"  Select [1-{back_idx}]: ")
        if os_choice is None:
            return

        if os_choice == str(back_idx):
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
            return

        try:
            os_name, transfer = journeys[int(os_choice) - 1]
        except (ValueError, IndexError):
            print("  ❌ Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.NETWORK_AUTO_CONFIG
            return

        self.transfer_method = transfer

        # Flash target is owned by the journeys package.
        from mono_imager.journeys import _FLASH_TARGETS
        flash_target = _FLASH_TARGETS.get(os_name, "/dev/mmcblk0")

        self.os_name = os_name

        # Any journey whose step list includes "Device network ready"
        # needs the device's OWN Ethernet connection — either for the
        # LAN flash transfer itself, or for a post-flash step that needs
        # real internet access (OpenWRT/OPNsense's firmware update).
        # Driven by the step registry rather than a hardcoded os_name
        # check, so this stays correct as journeys are added/changed.
        from mono_imager.step_registry import list_journey
        if "Device network ready" in list_journey(os_name, transfer):
            print()
            print("  +-----------------------------------------------------+")
            print("  | ETHERNET CABLE REQUIRED                             |")
            print("  |                                                     |")
            print("  | The device needs its own network connection -       |")
            print("  | both for the flash transfer itself, and for any     |")
            print("  | post-flash 'firmware update' step, which needs      |")
            print("  | direct internet access.                             |")
            print("  | Connect an Ethernet cable to a router/switch that   |")
            print("  | provides DHCP and internet access - the active      |")
            print("  | port is auto-detected, no specific jack required.   |")
            print("  | If no DHCP response comes back, you'll be           |")
            print("  | prompted to enter the network settings manually.    |")
            print("  +-----------------------------------------------------+")
            print()
            input("  Press Enter once the cable is plugged in...")

        print()
        if transfer == "usb":
            print("  Image will be auto-detected from USB stick.")
            print(f"  Recommended: 16 GB minimum (holds all three OS images simultaneously).")
            print()
            firmware_path     = Path(".")
            firmware_display  = "auto-detected from USB stick (16 GB min. recommended)"
        else:
            from mono_imager.journeys import get_firmware_prompt
            firmware_prompt = get_firmware_prompt(os_name, transfer)
            firmware_raw = self.safe_input(f"  {firmware_prompt} ")
            if firmware_raw is None:
                return
            firmware_path, error = self._validate_firmware_path(firmware_raw)
            if error:
                print(f"  ❌ {error}")
                input("  Press Enter to continue...")
                self.current_state = MenuState.NETWORK_AUTO_CONFIG
                return
            firmware_display = str(firmware_path)

        host_ip = core.detect_host_ip()
        if not host_ip:
            print("  ❌ Could not auto-detect host IP.")
            print("  Use Manual mode instead to set it yourself.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
            return

        # The device's own IP comes from _startup_network_setup() (already
        # run once, before the main menu) or gets (re-)resolved fresh once
        # this journey actually bootstraps into the recovery shell — see
        # menu_network_flashing(). Nothing to derive here; just preview
        # whatever's already known.
        if self.device_net:
            device_ip_preview = f"{self.device_net['ip']} ({self.device_net['source']})"
        else:
            device_ip_preview = "resolved via DHCP once the device is connected"

        confirmed = self._show_flash_confirmation(
            os_name=os_name,
            port=port,
            firmware_path=firmware_display,
            flash_target=flash_target,
            host_ip=host_ip,
            device_ip=device_ip_preview,
        )
        if confirmed is None:
            return
        if not confirmed:
            print("  Cancelled.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        self.serial_port      = port
        self.net_host_ip      = host_ip
        self.net_http_port    = 8080
        self.custom_fw_path   = firmware_path
        self.net_flash_target = flash_target
        self.current_state    = MenuState.NETWORK_FLASHING

    # ------------------------------------------------------------------ #
    #  NETWORK FLASH CONFIG — collects the values flash_orchestrator.py  #
    #  actually needs: just a firmware file path and a flash target      #
    #  string directly. (The eMMC/NOR/Dual + Mono Official/Armbian/      #
    #  Custom menus that used to feed the old Flasher-class API have     #
    #  been removed — they were unreachable and flash_orchestrator.py    #
    #  never used them anyway.)                                         #
    # ------------------------------------------------------------------ #
    def menu_network_flashing(self):
        """
        Run the actual flash via flash_orchestrator.py — the same
        proven functions used by tests/test_verify_flash_auto.py and
        tests/test_verify_flash_manual.py, confirmed working on real
        hardware (12/12 steps, both auto and manual paths).
        """
        # DON'T clear screen here — keep all output visible for debugging

        from mono_imager.flash_orchestrator import phase1_uboot, phase1_recovery
        from mono_imager import flash_orchestrator as core
        from mono_imager.journeys import get_journey
        from mono_imager.step_registry import get_staging_boot_methods

        d = None
        journey = None
        try:
            print()
            print("=" * 60)
            print("PHASE 1: Bootstrap (Serial Connection)")
            print("=" * 60)
            print(f"Port: {self.serial_port}")
            print()

            # Step 1: Connect and interrupt U-Boot — no OS awareness
            d = phase1_uboot(self.serial_port, 115200)
            if d is None:
                print("❌ Bootstrap FAILED")
                self.current_state = MenuState.DONE
                return

            # Step 2: Journey-specific U-Boot commands (eMMC erase, bootcmd, etc.)
            # Delegated entirely to the journey file via run_uboot_steps()
            print()
            print("  Configuring U-Boot...")
            journey = get_journey(
                os_name       = self.os_name,
                transfer      = getattr(self, "transfer_method", "lan"),
                device        = d,
                host_ip       = self.net_host_ip,
                device_ip     = (self.device_net or {}).get("ip", ""),
                firmware_path = Path(self.custom_fw_path),
                http_port     = self.net_http_port,
                device_net    = self.device_net,
            )
            if not journey.run_uboot_steps():
                print("❌ U-Boot setup FAILED")
                d.disconnect()
                self.current_state = MenuState.DONE
                return

            # Step 3: Boot staging Linux (recovery or alternative, per journey)
            staging = get_staging_boot_methods(
                self.os_name, getattr(self, "transfer_method", "lan")
            )
            d = phase1_recovery(d, **staging)
            if d is None:
                print("❌ Bootstrap FAILED")
                self.current_state = MenuState.DONE
                return

            print("✓ Bootstrap successful")
            print()

            # Step 3b: resolve the device's own network — same DHCP-first,
            # verified, manual-fallback mechanism used everywhere else
            # (self.device_net). Only needed by journeys whose step list
            # actually depends on it (LAN transfer, or a post-flash
            # internet-requiring step like OpenWRT/OPNsense's firmware
            # update) — skip it otherwise so e.g. Armbian-via-USB never
            # prompts for network settings it will never use.
            from mono_imager.step_registry import list_journey
            needs_network = "Device network ready" in list_journey(
                self.os_name, getattr(self, "transfer_method", "lan")
            )
            if needs_network:
                if not self._setup_recovery_network(d):
                    print("❌ Device network setup FAILED — cannot continue without it.")
                    d.disconnect()
                    self.current_state = MenuState.DONE
                    return
                # get_journey() was called earlier (before recovery boot,
                # for run_uboot_steps()) with a placeholder device_net —
                # now that it's actually resolved, forward it into the
                # already-built ctx rather than rebuilding the journey.
                journey.ctx.device_net = self.device_net
                journey.ctx.device_ip  = self.device_net["ip"]

            print()
            print("=" * 60)
            print("PHASE 2+: Flashing Firmware")
            print("=" * 60)
            fw_display = "auto-detected from USB" if self.custom_fw_path == Path(".") else str(self.custom_fw_path)
            print(f"OS:          {self.os_name}")
            print(f"Firmware:    {fw_display}")
            print(f"Host IP:     {self.net_host_ip}:{self.net_http_port}")
            print(f"Device IP:   {journey.ctx.device_ip or '(not needed for this journey)'}")
            print()

            ok = journey.run()

            if not ok:
                print("❌ Flashing did not complete successfully")
            else:
                print("✓ Flashing completed successfully")

            self.flash_success = ok
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
                core.verbose("HTTP server stopped")
            if d:
                d.disconnect()

        self.flash_success = core.print_report()
        self.current_state = MenuState.DONE

    # ------------------------------------------------------------------ #
    #  UPDATE eMMC / NOR — split FW update journeys.                    #
    #  eMMC journey: DIP RIGHT (NOR), flashes eMMC via firmware update. #
    #  NOR journey:  DIP LEFT (eMMC), flashes NOR via firmware update.  #
    #  Both detect modern vs. legacy automatically and fall back to      #
    #  curl+dd / curl+flashcp when the modern tool is unavailable.       #
    # ------------------------------------------------------------------ #
    def _setup_recovery_network(self, d) -> bool:
        """
        Prompt for and configure networking on the device's current
        recovery shell session, then VERIFY it actually works.

        BUG THIS FIXES (part 1): both the modern 'firmware update'
        command and the legacy curl-based path require real internet
        access on the device (documented hard prerequisite) — but
        recovery Linux has no DHCP and nothing was ever configuring
        an IP before attempting either. Confirmed on real hardware:
        without it, 'firmware update' prints its confirmation prompt
        then aborts itself within seconds (no internet to reach) —
        faster than recovery_orchestrator's auto-confirm could land,
        so the late-arriving 'yes' got typed into the now-idle shell
        as a literal command instead.

        BUG THIS FIXES (part 2): local 'ip addr'/'ip route' commands
        reporting RC=0 only proves the LOCAL config was accepted, not
        that the device can actually reach anything — confirmed on
        real hardware: a run reported network setup success, then
        'firmware update' still failed within seconds, because the
        physical port in use had no real path to the internet. This
        now calls recovery_orchestrator.check_internet_reachable()
        afterward to catch that before ever touching 'firmware update'.

        Recovery Linux also doesn't persist config across a reboot,
        so this must be called again after every fresh boot into a
        recovery shell, not just once at the start.

        DHCP is tried automatically first. Manual entry (IP, subnet
        mask, gateway, DNS) is only used as a fallback when DHCP fails
        or the lease it gets isn't actually reachable, and re-prompts
        on failure rather than aborting the whole operation. Whatever
        is resolved (DHCP or manual) is cached on self.device_net for
        the rest of the session — later calls in the same run just
        re-apply the known values instead of prompting again.
        """
        from mono_imager import recovery_orchestrator as rec
        from mono_imager import flash_orchestrator as core

        print()
        print("  Network setup — REQUIRED before 'firmware update' will work.")
        print("  'firmware update' needs the device to reach the internet directly.")
        print()

        # Bring up every eth* port, then auto-detect which one actually
        # has a cable (LOWER_UP) instead of assuming one specific
        # physical jack. Recovery Linux boots with all eth ports
        # administratively DOWN, so LOWER_UP is never set until each
        # candidate port has been brought up first. Uses
        # flash_orchestrator.parse_active_eth_iface().
        #
        # SPEEDUP: bring-up for all candidate ports is combined into a
        # single run_script() round trip rather than one call per port
        # — each round trip on this device/link costs real seconds
        # (write+verify+exec, waiting for the line to settle).
        try:
            bring_up_cmd = "; ".join(f"ip link set eth{n} up 2>/dev/null" for n in range(5))
            d.run_script(bring_up_cmd, marker="recovery_eth_up", exec_timeout=10)
        except Exception as e:
            print(f"  ❌ Failed to bring up Ethernet ports: {e}")
            return False

        try:
            # SPEEDUP: was a blind "sleep 2" before every single check —
            # a fixed 2s tax paid even when the cable/switch raises
            # carrier almost immediately (the common case). Poll for
            # LOWER_UP instead, returning as soon as it appears; still
            # caps out at 2s total for a genuinely slow link (e.g. a
            # managed switch's port negotiation), so the worst case is
            # unchanged — only the common case gets faster.
            ip_output, _eth_err = with_spinner(
                d.run_script,
                "for i in 1 2 3 4; do ip link show | grep -q LOWER_UP && break; sleep 0.5; done; ip link show",
                marker="recovery_eth_check", exec_timeout=10,
                message="Detecting active Ethernet port..."
            )
            if _eth_err:
                raise _eth_err
            iface = core.parse_active_eth_iface(ip_output)
            if iface is None:
                print("  ❌ No Ethernet port has a cable plugged in.")
                print("     Plug an Ethernet cable into any RJ-45 jack (not the SFP+ cages).")
                print()
                input("  Press Enter once the cable is plugged in...")
                ip_output = d.run_script("ip link show", marker="recovery_eth_check_retry", exec_timeout=5)
                iface = core.parse_active_eth_iface(ip_output)
                if iface is None:
                    print("  ❌ Still no Ethernet port with a cable detected.")
                    return False
            print(f"  ✓ {iface} is ready.")
        except Exception as e:
            print(f"  ❌ Failed to check Ethernet carrier: {e}")
            return False

        # Already resolved earlier this session. Recovery Linux doesn't
        # persist config across a reboot, so the known values still have
        # to be re-applied on this fresh shell — but we don't ask again.
        if self.device_net:
            net = self.device_net
            print(f"  Re-applying known network config: {net['ip']}/{net['prefix']} via {net['gateway']}...")
            if self._apply_device_network(d, iface, net):
                # The path itself (cable, switch, gateway, upstream route)
                # already proved reachable once this session — re-running
                # the several-second ping-based check on every subsequent
                # recovery boot re-verifies something that hasn't changed.
                # Only pay for it again if it's never been proven yet.
                reachable = True
                if not self.device_net_verified:
                    reachable = self._verify_device_network(d, net["gateway"])
                    self.device_net_verified = reachable
                if reachable:
                    print(f"  ✓ Internet reachable via {iface} — network is ready.")
                    # Refresh iface in case port enumeration differs on this boot
                    # (same cable, but not guaranteed to be identical every time).
                    self.device_net = {**net, "iface": iface}
                    return True
            print("  ⚠ Previously-working network config is no longer reachable — re-resolving...")
            self.device_net = None
            self.device_net_verified = False

        # First time this session — try DHCP before ever asking the user.
        lease, _dhcp_err = with_spinner(
            rec.try_dhcp, d, iface,
            message="Attempting DHCP..."
        )
        if _dhcp_err:
            lease = None

        if lease:
            dns_note = f", DNS {lease['dns']}" if lease["dns"] else ""
            print(f"  DHCP lease: {lease['ip']}/{lease['prefix']} via {lease['gateway']}{dns_note}")
            if self._verify_device_network(d, lease["gateway"]):
                print(f"  ✓ Internet reachable via {iface} — network is ready.")
                self.device_net = {**lease, "source": "dhcp"}
                self.device_net_verified = True
                return True
            print("  ❌ DHCP lease obtained but the internet is not reachable through it.")
        else:
            print("  ❌ No DHCP response.")

        print()
        print("  Falling back to manual network entry.")

        while True:
            net = self._prompt_manual_network()
            if net is None:
                return False

            print(f"  Configuring {iface} = {net['ip']}/{net['prefix']}, gateway {net['gateway']}...")
            if self._apply_device_network(d, iface, net):
                print("  ✓ Local network config applied.", end=" ", flush=True)
                if self._verify_device_network(d, net["gateway"]):
                    print(f"  ✓ Internet reachable via {iface} — network is ready.")
                    self.device_net = {**net, "source": "manual", "iface": iface}
                    self.device_net_verified = True
                    return True
                print(f"  ❌ {iface} has link but could not reach the internet.")
                print("     Check the gateway IP, cable, and network configuration.")

            retry = input("  Try entering the network settings again? [Y/n]: ").strip().lower()
            if retry == "n":
                return False

    def _apply_device_network(self, d, iface: str, net: dict) -> bool:
        """
        Statically (re-)apply a known ip/prefix/gateway/dns to iface.
        Used for cache-reuse (config lost on reboot) and manual entry —
        NOT for a fresh DHCP lease, which udhcpc's own bound script
        already applies as part of obtaining it. Flushes any existing
        address/route first so this is safe to call again in the same
        boot after a failed attempt (retry loop), not just once.
        """
        dns_cmd = f" && echo nameserver {net['dns']} > /etc/resolv.conf" if net.get("dns") else ""
        net_cmd = (
            f"ip addr flush dev {iface} 2>/dev/null; "
            f"ip link set {iface} up && "
            f"ip addr add {net['ip']}/{net['prefix']} dev {iface} && "
            f"ip route replace default via {net['gateway']} dev {iface}"
            f"{dns_cmd}; echo RC=$?"
        )
        try:
            output = d.run_script(net_cmd, marker="recovery_net_setup", exec_timeout=20)
        except RuntimeError as e:
            print(f"  ❌ Network setup failed on {iface}: {e}")
            return False

        if "RC=0" not in output:
            print(f"  ❌ Network setup did not report success on {iface}.")
            return False
        return True

    def _verify_device_network(self, d, gateway: str) -> bool:
        from mono_imager import recovery_orchestrator as rec
        result, error = with_spinner(
            rec.check_internet_reachable, d, gateway=gateway,
            message="Verifying real connectivity..."
        )
        if error is not None:
            return False
        return bool(result)

    def _prompt_manual_network(self) -> Optional[dict]:
        """
        Prompt for Device IP, subnet mask, gateway, and DNS. Returns
        None (caller should abort) if the user leaves a required field
        blank — DNS is optional since some networks resolve fine
        without one being explicitly set.
        """
        device_ip = input(
            "  Pick an unused IP address for the device on that same network "
            "(e.g. 192.168.1.50). Check your own machine's network adapter "
            "settings first if you're unsure of the IP/subnet/gateway on that "
            "network.\n  Device IP to assign: "
        ).strip()
        if not device_ip:
            print("  ❌ Device IP is required.")
            return None

        mask_raw = input("  Subnet mask (e.g. 255.255.255.0) [255.255.255.0]: ").strip() or "255.255.255.0"
        prefix = _netmask_to_prefix(mask_raw)
        if prefix is None:
            print(f"  ❌ Invalid subnet mask: {mask_raw}")
            return None

        gateway = input("  Gateway (your router's IP on that network, e.g. 192.168.1.1): ").strip()
        if not gateway:
            print("  ❌ Gateway is required.")
            return None

        dns = input("  DNS server [8.8.8.8]: ").strip() or "8.8.8.8"

        return {"ip": device_ip, "prefix": prefix, "gateway": gateway, "dns": dns}

    def menu_update_emmc(self):
        """
        Flash eMMC firmware only. Device must be in NOR recovery (DIP RIGHT).
        The 'firmware update' command auto-targets eMMC when booted from NOR.
        Falls back to legacy curl+dd if the modern tool is unavailable.
        """
        from mono_imager import flash_orchestrator as core
        from mono_imager import recovery_orchestrator as rec

        self.clear_screen()
        self.print_header()
        print("Update eMMC Firmware")
        print()
        print("  +-----------------------------------------------+")
        print("  | START HERE: DIP switch -> RIGHT (NOR)         |")
        print("  |                                               |")
        print("  | Booting from NOR recovery ensures 'firmware   |")
        print("  | update' targets eMMC automatically.           |")
        print("  |                                               |")
        print("  | If DIP is LEFT (eMMC), flip it RIGHT and      |")
        print("  | power-cycle before continuing.                |")
        print("  +-----------------------------------------------+")
        print()

        port = self._select_port(auto_select_single=True, allow_back=True, save_on_select=True)
        if port is None:
            self.current_state = MenuState.MAIN
            return

        print()
        print("⚠️  This writes firmware to eMMC.")
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Cancelled.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        d = None
        try:
            self._soft_reboot_if_possible(port)
            d = core.phase1_bootstrap(port, 115200)
            if d is None:
                print()
                print("  ❌ Could not bootstrap into the recovery shell.")
                self._recovery_finish(core.print_report())
                return

            rec.reset_results()
            is_modern, _fw_err = with_spinner(
                rec.detect_modern_firmware_tool, d,
                message="Detecting firmware tool type..."
            )
            if _fw_err:
                is_modern = None

            if is_modern is None:
                print()
                print("  ❌ Could not determine the device's firmware tool type.")
                self._recovery_finish(rec.print_report())
                return

            if not self._setup_recovery_network(d):
                self._recovery_finish(rec.print_report())
                return

            if is_modern:
                print()
                print("  Modern firmware tool detected.")
                print()
                emmc_ok = rec.phase_modern_flash_emmc(d, on_output=self._show_firmware_output)
                if not emmc_ok:
                    print()
                    print("  ⚠ Modern 'firmware update' failed — falling back to legacy curl+dd...")
                    emmc_ok, _leg_err = with_spinner(
                        rec.phase_legacy_flash_emmc, d,
                        message="Flashing eMMC (legacy curl+dd)..."
                    )
                    if _leg_err:
                        emmc_ok = False
                    if not emmc_ok:
                        print("  ❌ Legacy fallback also failed for eMMC.")
                        self._recovery_finish(rec.print_report())
                        return
                    print("  ✓ Legacy fallback succeeded.")
            else:
                print()
                print("  Legacy firmware tool detected — using curl+dd directly.")
                print()
                emmc_ok, _leg_err = with_spinner(
                    rec.phase_legacy_flash_emmc, d,
                    message="Flashing eMMC (legacy curl+dd)..."
                )
                if _leg_err:
                    emmc_ok = False
                if not emmc_ok:
                    self._recovery_finish(rec.print_report())
                    return

        finally:
            if d:
                d.disconnect()

        success = rec.print_report()
        if success:
            print()
            print("=" * 60)
            print("  ⚡ FLIP DIP SWITCH TO LEFT (eMMC), THEN POWER-CYCLE ⚡")
            print("=" * 60)
            print()
        self._recovery_finish(success)

    def menu_update_nor(self):
        """
        Flash NOR firmware only. Device must be in eMMC recovery (DIP LEFT).
        The 'firmware update' command auto-targets NOR when booted from eMMC.
        Falls back to legacy curl+flashcp if the modern tool is unavailable.
        Requires eMMC to already have the official Mono firmware with recovery.
        """
        from mono_imager import flash_orchestrator as core
        from mono_imager import recovery_orchestrator as rec

        self.clear_screen()
        self.print_header()
        print("Update NOR Firmware")
        print()
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  START HERE: DIP switch → LEFT (eMMC)            │")
        print("  │                                                   │")
        print("  │  Booting from eMMC recovery ensures 'firmware    │")
        print("  │  update' targets NOR automatically.              │")
        print("  │                                                   │")
        print("  │  ⚠️  eMMC must already have the official Mono     │")
        print("  │  Gateway firmware (with recovery partition).      │")
        print("  └─────────────────────────────────────────────────┘")
        print()

        port = self._select_port(auto_select_single=True, allow_back=True, save_on_select=True)
        if port is None:
            self.current_state = MenuState.MAIN
            return

        print()
        print("⚠️  This writes firmware to NOR flash (bootloader + recovery).")
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Cancelled.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        d = None
        try:
            self._soft_reboot_if_possible(port)
            d = core.phase1_bootstrap(port, 115200)
            if d is None:
                print()
                print("  ❌ Could not bootstrap into the recovery shell.")
                self._recovery_finish(core.print_report())
                return

            rec.reset_results()
            is_modern, _fw_err = with_spinner(
                rec.detect_modern_firmware_tool, d,
                message="Detecting firmware tool type..."
            )
            if _fw_err:
                is_modern = None

            if is_modern is None:
                print()
                print("  ❌ Could not determine the device's firmware tool type.")
                self._recovery_finish(rec.print_report())
                return

            if not self._setup_recovery_network(d):
                self._recovery_finish(rec.print_report())
                return

            if is_modern:
                print()
                print("  Modern firmware tool detected.")
                print()
                nor_ok = rec.phase_modern_flash_nor(d, on_output=self._show_firmware_output)
                if not nor_ok:
                    print()
                    print("  ⚠ Modern 'firmware update' failed — falling back to legacy curl+flashcp...")
                    nor_ok, _leg_err = with_spinner(
                        rec.phase_legacy_flash_nor, d,
                        message="Flashing NOR (legacy curl+flashcp)..."
                    )
                    if _leg_err:
                        nor_ok = False
                    if not nor_ok:
                        print("  ❌ Legacy fallback also failed for NOR.")
                        self._recovery_finish(rec.print_report())
                        return
                    print("  ✓ Legacy fallback succeeded.")

                print()
                print("=" * 60)
                print("  ⚡ FLIP THE DIP SWITCH BACK TO NOR (RIGHT), THEN POWER-CYCLE ⚡")
                print("=" * 60)
                input("  Press Enter once you've done that...")

                rec.phase_modern_verify_nor_boot(d)

            else:
                print()
                print("  Legacy firmware tool detected — using curl+flashcp directly.")
                print("  (No DIP-switch flip needed for this path.)")
                print()
                nor_ok, _leg_err = with_spinner(
                    rec.phase_legacy_flash_nor, d,
                    message="Flashing NOR (legacy curl+flashcp)..."
                )
                if _leg_err:
                    nor_ok = False

        finally:
            if d:
                d.disconnect()

        self._recovery_finish(rec.print_report())

    # ------------------------------------------------------------------ #
    #  TEST SERIAL — option 4 from main menu                             #
    # ------------------------------------------------------------------ #
    def menu_test_serial(self):
        """
        Run the serial connection test inline.

        Reuses test logic from tests/hardware/test_serial_connect.py
        directly — same steps, same pass/fail output, no subprocess.

        If self.serial_port is already set (user connected earlier in
        this session), it is used automatically. Otherwise the user is
        prompted to pick a port first.
        """
        from mono_imager.serial_device import SerialDevice
        import time

        self.clear_screen()
        self.print_header()
        print("  Test Serial Connection")
        print("  " + "─" * 56)
        print()

        # Resolve port
        port = self.serial_port
        if not port:
            port = self._select_port(auto_select_single=True, allow_back=False)
            if port is None:
                self.current_state = MenuState.MAIN
                return

        print()
        print(f"  Port:  {port}")
        print(f"  Baud:  115200")
        print()

        results = []

        # Step 1: connect
        d = SerialDevice(port, timeout=5)
        if not self._check(results, "Connect at 115200 baud", d.connect(115200)):
            input("\n  Press Enter to return to main menu...")
            self.current_state = MenuState.MAIN
            return

        try:
            # Step 2: interrupt U-Boot
            print()
            print("  " + "─" * 56)
            print("  ⚡  POWER CYCLE YOUR DEVICE NOW  ⚡")
            print("  " + "─" * 56)
            print()
            _autoboot_ok, _autoboot_err = with_spinner(
                d.wait_for_autoboot, timeout=60,
                message="Waiting for U-Boot autoboot interrupt..."
            )
            if _autoboot_err:
                _autoboot_ok = False
            self._check(results, "U-Boot autoboot interrupted", bool(_autoboot_ok))
            if not results[-1]:
                input("\n  Press Enter to return to main menu...")
                self.current_state = MenuState.MAIN
                return

            # Step 3: U-Boot responds to a command
            response = d.send_command("printenv ethact", timeout=5)
            self._check(results, "U-Boot responds to commands",
                        bool(response.strip()),
                        response.strip() if response.strip() else "no response")

            # Step 4: boot recovery
            booted = False
            buffer = b""
            with Spinner("Booting recovery Linux..."):
                d.send_command("run recovery", wait_for_prompt=False, timeout=3)
                start = time.time()
                while time.time() - start < 60:
                    byte = d.ser.read(1)
                    if byte:
                        buffer += byte
                        if b"root@recovery" in buffer or b"login:" in buffer:
                            if b"login:" in buffer and b"root@recovery" not in buffer:
                                d.ser.write(b"root\r\n")
                                time.sleep(1)
                            booted = True
                            break
            self._check(results, "Recovery Linux booted", booted)

            # Step 5: login confirmed
            if booted:
                d.ser.write(b"\r\n")
                time.sleep(0.5)
                waiting  = d.ser.in_waiting
                response = d.ser.read(waiting) if waiting else b""
                at_shell = b"root@recovery" in buffer or b"root@recovery" in response
                self._check(results, "Logged into recovery shell", at_shell)

        finally:
            d.disconnect()

        # Summary
        print()
        print("  " + "─" * 56)
        total  = len(results)
        passed = sum(results)
        if passed == total:
            print(f"  ✓  All {total} checks passed — serial connection is healthy.")
        else:
            print(f"  ✗  {total - passed}/{total} checks failed.")

        # Remember the working port for subsequent operations
        if results and results[0]:  # connected successfully
            self.serial_port = port

        input("\n  Press Enter to return to main menu...")
        self.current_state = MenuState.MAIN


    # ------------------------------------------------------------------ #
    #  TEST LAN — option 5 from main menu                                #
    # ------------------------------------------------------------------ #
    def menu_test_lan(self):
        """
        Full end-to-end LAN test — boots device into recovery, sets up
        networking, and confirms the device can reach the host HTTP server.

        Steps:
          1. Resolve serial port (use known port or auto-detect)
          2. Bootstrap device via serial (soft reboot → U-Boot → recovery)
          3. Detect host IP
          4. Resolve the device's own network (DHCP-first, verified,
             manual fallback — same mechanism as everywhere else, and
             reused rather than re-implemented here)
          5. Start HTTP server on host
          6. Device curls the server and reports back — confirms full path
        """
        from mono_imager.flash_orchestrator import (
            phase1_bootstrap, detect_host_ip,
            start_http_server, wait_for_report
        )
        import tempfile, pathlib, socket

        self.clear_screen()
        self.print_header()
        print("  Test LAN Connection")
        print("  " + "─" * 56)
        print()

        results = []

        # Step 1: resolve serial port
        port = self.serial_port
        if not port:
            port = self._select_port(auto_select_single=True, allow_back=False)
            if port is None:
                self.current_state = MenuState.MAIN
                return

        print()

        # Step 2: soft reboot then bootstrap into recovery
        print("  Rebooting device into recovery Linux...")
        self._soft_reboot_if_possible(port)

        d = phase1_bootstrap(port, 115200)
        if not self._check(results, "Device in recovery shell", d is not None):
            input("\n  Press Enter to return to main menu...")
            self.current_state = MenuState.MAIN
            return

        try:
            # Step 3: host IP (still detected/derived — separate concern
            # from the device's own network, since this is the address
            # the device will curl toward, not the address it configures
            # on itself).
            host_ip = self.net_host_ip or detect_host_ip()
            if not self._check(results, "Host IP detected", bool(host_ip), host_ip or "could not detect"):
                return

            # Step 4: device network — reuses the same DHCP-first, verified,
            # manual-fallback resolution as everywhere else (self.device_net),
            # instead of re-implementing ethernet-port detection and a bare
            # `ip addr add` here. Replaces the old ICMP ping-from-device
            # check too: that check was non-fatal and unused by anything
            # (informational only), while _setup_recovery_network() already
            # does a real reachability check as part of resolving the
            # network, and the curl-based check in step 6 below is the
            # actually meaningful "can the device reach the host" test.
            if not self._check(results, "Device network ready", self._setup_recovery_network(d)):
                input("\n  Press Enter to return to main menu...")
                return
            device_ip = self.device_net["ip"]

            # Step 5: HTTP server
            http_port = 18080
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as _tf:
                _tf.write(b"LAN_TEST")
                tmp = pathlib.Path(_tf.name)
            server = start_http_server(host_ip, http_port, tmp)
            if not self._check(results, f"HTTP server up on {host_ip}:{http_port}", server is not None):
                tmp.unlink(missing_ok=True)
                input("\n  Press Enter to return to main menu...")
                self.current_state = MenuState.MAIN
                return

            # Step 6: device curls the server and reports back
            url = f"http://{host_ip}:{http_port}/firmware.img"
            check_script = (
                f"curl -sk -I -o /dev/null -w '%{{http_code}}' {url} "
                f"> /tmp/lantest_code.txt; "
                f"curl -sk -X POST --data-binary @/tmp/lantest_code.txt "
                f"\"http://{host_ip}:{http_port}/report?step=lantest\" >/dev/null 2>&1"
            )
            try:
                d.launch_script(check_script, marker="lantest")
                report, _rep_err = with_spinner(
                    wait_for_report, "lantest", timeout=15.0,
                    message="Waiting for device HTTP report..."
                )
                device_sees_host = report is not None and "200" in report
            except Exception as e:
                device_sees_host = False
            finally:
                server.shutdown()
                tmp.unlink(missing_ok=True)

            _curl_detail = (
                "" if device_sees_host else
                "device's network is up but can't reach the host — either port 18080 is "
                "blocked (allow Python.exe through Windows Defender Firewall) or the cable "
                "doesn't connect to the SAME router/switch as the host"
            )
            self._check(results, "Device can reach host HTTP server", device_sees_host, _curl_detail)

            # Save working config
            self.serial_port   = port
            self.net_host_ip   = host_ip
            self.net_device_ip = device_ip

        finally:
            if d:
                d.disconnect()

        # Summary
        print()
        print("  " + "─" * 56)
        passed_count = sum(results)
        total        = len(results)
        if passed_count == total:
            print("  ✓  LAN path confirmed end-to-end.")
        else:
            print(f"  ✗  {total - passed_count}/{total} checks failed.")

        input("\n  Press Enter to return to main menu...")
        self.current_state = MenuState.MAIN


    # ------------------------------------------------------------------ #
    #  TEST USB MOUNT — option 7 from main menu                          #
    # ------------------------------------------------------------------ #
    def menu_test_usb_mount(self):
        """
        Verify a USB stick is connected, mountable, and (optionally)
        already staged with recognizable OS images — before starting a
        real USB flash journey. Same standalone-diagnostic principle as
        menu_test_lan(): boot into recovery, exercise the exact path a
        real journey would use, report pass/fail, return to the main menu.

        Mount attempt mirrors the real USB journeys' step_mount_usb
        (usb_device + "1" first, falling back to the bare usb_device for
        unpartitioned sticks) — same reasoning, not reinvented here.

        Unlike a real journey (which only looks for the one OS you
        picked), this scans for all three known image patterns and
        reports what's actually present, since a 16 GB+ stick is meant
        to hold all three simultaneously.
        """
        from mono_imager.flash_orchestrator import phase1_bootstrap
        from mono_imager.journeys.usb_utils import check_usb_size, find_image_on_usb

        self.clear_screen()
        self.print_header()
        print("  Test USB Stick")
        print("  " + "─" * 56)
        print()

        results = []

        # Step 1: resolve serial port
        port = self.serial_port
        if not port:
            port = self._select_port(auto_select_single=True, allow_back=False)
            if port is None:
                self.current_state = MenuState.MAIN
                return

        print()

        # Step 2: bootstrap into recovery
        d = phase1_bootstrap(port, 115200)
        if not self._check(results, "Device in recovery shell", d is not None):
            input("\n  Press Enter to return to main menu...")
            self.current_state = MenuState.MAIN
            return

        usb_device = "/dev/sda"
        usb_mount  = "/mnt/usb"

        try:
            # Step 3: mount (partition first, bare device as fallback)
            try:
                d.send_command(f"mkdir -p {usb_mount}", timeout=5)
                response, _mnt_err = with_spinner(
                    d.send_command, f"mount {usb_device}1 {usb_mount} 2>&1; echo RC=$?",
                    timeout=15, message="Mounting USB stick..."
                )
                if _mnt_err:
                    raise _mnt_err
                mounted = "RC=0" in response
                if not mounted:
                    response = d.send_command(f"mount {usb_device} {usb_mount} 2>&1; echo RC=$?", timeout=15)
                    mounted = "RC=0" in response
            except Exception as e:
                mounted, response = False, str(e)

            if not self._check(results, f"USB mounted ({usb_device} -> {usb_mount})", mounted,
                                "" if mounted else "no USB stick detected, or it's not FAT32/exFAT formatted"):
                input("\n  Press Enter to return to main menu...")
                return

            # Step 4: capacity (informational — warns below 16 GB, doesn't fail the test)
            check_usb_size(d, usb_mount)

            # Step 5: scan for all three known OS image patterns
            print()
            found = {}
            for os_name in ["OPNsense", "OpenWRT", "Armbian"]:
                path, _fmt = find_image_on_usb(d, usb_mount, os_name)
                found[os_name] = path
                mark = "✓" if path else "·"
                detail = f" — {Path(path).name}" if path else " (not found)"
                print(f"  {mark}  {os_name} image{detail}")

            any_found = any(found.values())
            self._check(results, "At least one recognizable OS image on stick", any_found,
                        "" if any_found else "stick mounts fine but no armbian*/openwrt*/opnsense* "
                                              "image found — see README for expected filenames")

        finally:
            # Step 6: unmount
            try:
                d.send_command(f"umount {usb_mount} 2>&1; sync", timeout=15)
            except Exception as e:
                verbose(f"⚠ USB unmount warning: {e}", "warning")
            d.disconnect()

        # Summary
        print()
        print("  " + "─" * 56)
        passed_count = sum(results)
        total        = len(results)
        if passed_count == total:
            print("  ✓  USB stick mounted and verified.")
        else:
            print(f"  ✗  {total - passed_count}/{total} checks failed.")

        self.serial_port = port

        input("\n  Press Enter to return to main menu...")
        self.current_state = MenuState.MAIN


    # ------------------------------------------------------------------ #
    #  8. DONE                                                             #
    # ------------------------------------------------------------------ #
    def menu_done(self):
        """Flash result screen"""
        # DON'T clear screen — keep all debug output visible
        
        print()
        print("=" * 60)
        if self.flash_success:
            print("✅ Flashing complete!")
            print()
            print("  ⚡ NEXT STEP: Set DIP Switch ⚡")
            print("  Move the DIP switch to: LEFT (eMMC)")
            print()
            print("  Then power-cycle the device.")
            print("  It will boot with the new firmware.")
            print()
            print("  💡 Tip: You can watch it boot live via")
            print("     option 3 (CLI / raw serial console) from the main menu.")
        else:
            print("❌ Flashing did not complete successfully.")
            print()
            print("  Check the log output above for details.")
            print("  You can retry from the main menu.")

        print("=" * 60)
        print()
        if self.log_file:
            verbose(f"Result: {'OK' if self.flash_success else 'NOK'}")
            verbose(f"📄 Report saved to: {self.log_file}")
            print(f"  📄 Report: {self.log_file}")
            print()
        input("Press Enter to return to main menu...")
        self.flash_success = False
        self.current_state = MenuState.MAIN

    # ------------------------------------------------------------------ #
    #  CLI CONSOLE                                                         #
    # ------------------------------------------------------------------ #
    def menu_cli_console(self):
        """Serial console session — raw pass-through"""
        from mono_imager.serial_device import SerialDevice
        import threading

        self.clear_screen()
        self.print_header()
        print("  CLI Console — Serial")
        print()

        port = self._select_port(
            auto_select_single=True,
            show_categories=True,
            allow_back=True,
            allow_enter_last=True,
            save_on_select=True,
        )
        if port is None:
            self.current_state = MenuState.MAIN
            return

        self.clear_screen()
        self.print_header()
        print(f"  Connecting to {port} at 115200 baud...")
        print()

        d = SerialDevice(port, timeout=0.1)
        if not d.connect(115200):
            print(f"  ❌ Failed to connect to {port}")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        print("  ✓ Connected — you are now in raw serial console.")
        print("  Type Ctrl+] to exit.")
        print()

        # Drain any leftover bytes from connect()'s own internal probe,
        # then request a fresh prompt so the screen isn't blank.
        try:
            time.sleep(0.2)
            d.safe_read_all()
            time.sleep(0.1)
            d.safe_write(b"\r")
        except Exception:
            pass

        stop_event = threading.Event()

        def reader():
            """Read from device, print to stdout"""
            while not stop_event.is_set():
                try:
                    data = d.safe_read_all()
                    if data:
                        sys.stdout.write(data.decode("utf-8", errors="replace"))
                        sys.stdout.flush()
                except Exception:
                    break

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        try:
            import tty, termios
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setraw(fd)
            try:
                while True:
                    ch = sys.stdin.read(1)
                    if ch == "\x1d":  # Ctrl+]
                        break
                    d.safe_write(ch.encode())
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except ImportError:
            # Windows — use msvcrt
            import msvcrt
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ord(ch) == 29:  # Ctrl+]
                        break
                    d.safe_write(ch.encode("utf-8", errors="replace"))

        stop_event.set()
        d.disconnect()
        print()
        print("  Console session ended.")
        input("  Press Enter to continue...")
        self.current_state = MenuState.MAIN
        

    def device_stats(self):
        """Query and display Mono Gateway hardware statistics"""
        from mono_imager.serial_device import SerialDevice

        self.clear_screen()
        self.print_header()

        print("  Select device to query:")
        print()
        port = self._select_port(
            auto_select_single=True,
            show_categories=True,
            allow_back=True,
            allow_enter_last=True,
            save_on_select=True,
        )
        if port is None:
            self.current_state = MenuState.MAIN
            return

        # Query device
        self.clear_screen()
        self.print_header()
        print(f"  Connecting to {port}...")
        print()

        try:
            device = SerialDevice(port, timeout=10)
            if not device.connect():
                print("  ❌ Failed to connect")
                input("  Press Enter to continue...")
                self.current_state = MenuState.MAIN
                return

            print("=" * 60)
            print("  ⚡ POWER CYCLE YOUR DEVICE NOW ⚡")
            print("=" * 60)
            print()
            print("  Reading boot output (no need to interrupt autoboot)...")

            raw_output, _diag_err = with_spinner(
                device.capture_boot_diagnostics, timeout=60,
                message="Reading boot diagnostics..."
            )
            device.disconnect()

            if raw_output is None:
                print()
                print("  ❌ Timed out waiting for boot output.")
                print("  Make sure the device was power-cycled after connecting.")
                input("  Press Enter to continue...")
                self.current_state = MenuState.MAIN
                return

            self._display_device_stats(raw_output)

        except Exception as e:
            verbose(f"Device stats query failed: {e}", "error")
            print(f"  ❌ Error: {e}")

        print()
        input("  Press Enter to return to main menu...")
        self.current_state = MenuState.MAIN

    def _display_device_stats(self, raw_output: str):
        """Parse and display U-Boot boot-time diagnostics."""
        identity  = parse_uboot_identity(raw_output)
        self_test = parse_uboot_self_test(raw_output)

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
            for label, value in self_test:
                detail = f"  {value}" if value else ""
                print(f"    ✓  {label}{detail}")

        print()



    def _startup_network_setup(self):
        """
        Resolve the device's own recovery-shell network (DHCP, falling
        back to manual entry) once, right at launch, before the main
        menu is ever shown — so self.device_net is already populated
        by the time any journey or firmware operation needs it, and
        every one of them just reuses it via get_journey()'s
        device_net= forwarding / _setup_recovery_network()'s cache
        check, instead of re-detecting or re-asking.

        Reaching a recovery shell doesn't require knowing which
        journey the user will pick yet or which DIP-switch position
        it's currently in — whichever recovery Linux the device is
        already sitting in works identically for this purpose.

        If no serial port is found at all, there's nothing this tool can
        do (every menu option needs one), so it exits cleanly rather
        than dropping into a main menu that would fail on every choice.
        If a port IS found but the device can't be bootstrapped into a
        recovery shell yet (not connected/powered on — a transient,
        recoverable state), resolution is postponed instead of exiting
        — it happens lazily on the first eMMC/NOR/firmware operation,
        via the same cache check.
        """
        from mono_imager import flash_orchestrator as core

        self.clear_screen()
        self.print_header()
        print("  🔧 Setting up your device's network — this happens once per launch.")
        print("  You'll be asked to power-cycle in a moment, then the tool scans")
        print("  for a DHCP connection automatically — this needs a router or")
        print("  switch actually handing out DHCP on the connected Ethernet port.")
        print("  If none responds, you'll be prompted to enter the network")
        print("  settings manually instead. Takes a minute or two.")
        print()
        print("  💡 Every menu after this already knows your device's network —")
        print("  nothing to configure again this session.")
        print()

        port = self._select_port(auto_select_single=True, allow_back=False, save_on_select=True, quiet=True)
        if port is None:
            print("  ❌ No serial device found — connect the USB-to-UART cable and restart mono-imager.")
            print()
            input("  Press Enter to exit...")
            sys.exit(0)

        d = core.phase1_bootstrap(port, 115200)
        if d is None:
            print()
            print("  Could not reach a recovery shell — network settings will be resolved")
            print("  on the first eMMC/NOR update or firmware flash instead.")
            input("  Press Enter to continue...")
            return

        try:
            self._setup_recovery_network(d)
        finally:
            d.disconnect()

    # ------------------------------------------------------------------ #
    #  MAIN LOOP                                                           #
    # ------------------------------------------------------------------ #
    def run(self):
        """Main event loop"""
        try:
            self._startup_network_setup()
            while True:
                if self.current_state == MenuState.MAIN:
                    self.menu_main()
                elif self.current_state == MenuState.FLASH_AUTO_OR_MANUAL:
                    self.menu_flash_auto_or_manual()
                elif self.current_state == MenuState.NETWORK_AUTO_CONFIG:
                    self.menu_network_auto_config()
                elif self.current_state == MenuState.NETWORK_FLASHING:
                    self.menu_network_flashing()
                elif self.current_state == MenuState.UPDATE_EMMC:
                    self.menu_update_emmc()
                elif self.current_state == MenuState.UPDATE_NOR:
                    self.menu_update_nor()
                elif self.current_state == MenuState.DONE:
                    self.menu_done()
                elif self.current_state == MenuState.CLI_CONSOLE:
                    self.menu_cli_console()
                elif self.current_state == MenuState.DEVICE_STATS:
                    self.device_stats()
        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
            sys.exit(0)


def main():
    """Entry point"""
    from mono_imager.logging_setup import configure_logging
    log_dir = Path(__file__).parent.parent / "logs"
    log_file = configure_logging(log_dir)
    verbose(f"mono-imager {__version__} by {__author__}")
    verbose(f"Log: {log_file}")
    app = MonoImager(log_file)
    app.run()


if __name__ == "__main__":
    main()
