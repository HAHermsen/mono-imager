#!/usr/bin/env python3
"""
mono-imager: Automated firmware flashing for Mono Gateway Routers and Dev Kit
Supports serial and networked connections with menu-driven TUI.

Author:  H.A. Hermsen
Version: v1.0.0
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

        inner_width = max(len(version_line), len(subtitle), len(license_line)) + 2

        def left_aligned(text):
            pad = inner_width - len(text) - 1
            return "║ " + text + " " * pad + "║"

        print("╔" + "═" * inner_width + "╗")
        print(left_aligned(version_line))
        print(left_aligned(subtitle))
        print(left_aligned(license_line))
        print("╚" + "═" * inner_width + "╝")
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

    def _select_port(
        self,
        *,
        auto_select_single: bool = False,
        show_categories: bool = False,
        allow_back: bool = True,
        allow_enter_last: bool = False,
        save_on_select: bool = False,
    ) -> Optional[str]:
        """
        Detect serial ports, list them, and prompt for a selection.
        Returns the chosen device string, or None if detection failed,
        no ports found, user chose Back, or input was invalid.
        """
        from mono_imager.config import detect_serial_ports, get_last_port, save_last_port

        try:
            known, other = detect_serial_ports()
            all_ports = known + other
        except Exception as e:
            print(f"  ❌ Port detection failed: {e}")
            input("  Press Enter to continue...")
            return None

        if not all_ports:
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
        print(f"    Device IP:   {device_ip}  (auto-derived)")
        print()
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  This writes to eMMC, the device's main storage  │")
        print("  │  (32 GB). It does NOT touch NOR flash (64 MB —   │")
        print("  │  the bootloader + recovery tool).                │")
        print("  │                                                   │")
        print("  │     NOR (64 MB)          eMMC (32 GB)            │")
        print("  │   ┌─────────────┐      ┌─────────────────┐      │")
        print("  │   │ Bootloader  │      │  Your OS goes    │      │")
        print("  │   │ + Recovery  │      │  here — this is  │      │")
        print("  │   │ (untouched) │      │  what gets       │      │")
        print("  │   └─────────────┘      │  flashed now ✓   │      │")
        print("  │                        └─────────────────┘      │")
        print("  │                                                   │")
        print("  │  After flashing, the DIP switch picks which one  │")
        print("  │  the board actually boots:                       │")
        print("  │    LEFT  = eMMC  (your new OS boots)             │")
        print("  │    RIGHT = NOR   (boots recovery instead)        │")
        print("  └─────────────────────────────────────────────────┘")
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
        print("  2) Update eMMC FW")
        print("  3) Update NOR FW")
        print("  4) CLI only (serial)")
        print("  5) Test Serial connection")
        print("  6) Test LAN connection")
        print("  7) Show Device Stats")
        print("  8) Exit")
        print()

        choice = input("Select [1-8]: ").strip()

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
            self.current_state = MenuState.DEVICE_STATS
        elif choice == "8":
            sys.exit(0)
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN

    def menu_flash_auto_or_manual(self):
        self.clear_screen()
        self.print_header()
        print("  ⚠️  ETHERNET: Plug into RIGHTMOST 1 Gig RJ-45 jack (not SFP+ cages)")
        print()
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
        print("  ⚠️  ETHERNET: Plug into RIGHTMOST 1 Gig RJ-45 jack (not SFP+ cages)")
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

        if os_name == "OpenWRT":
            print()
            print("  ┌─────────────────────────────────────────────────────┐")
            print("  │  ETHERNET CABLE REQUIRED — OpenWRT                  │")
            print("  │                                                     │")
            if transfer == "usb":
                print("  │  The firmware update step (flashes the eMMC         │")
                print("  │  bootloader) needs internet access from the device. │")
                print("  │  Plug an ethernet cable into the RIGHTMOST          │")
                print("  │  1 Gig RJ-45 jack before proceeding.               │")
                print("  │                                                     │")
                print("  │  The cable must be connected to a router/switch     │")
                print("  │  that provides DHCP and internet access.            │")
            else:
                print("  │  The firmware update step routes internet traffic   │")
                print("  │  through the host machine. Ensure the host has      │")
                print("  │  internet sharing / NAT enabled on its ethernet     │")
                print("  │  interface, or connect the device to a router       │")
                print("  │  instead and use the USB flash method.              │")
            print("  └─────────────────────────────────────────────────────┘")
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

        device_ip = core.pick_device_ip(host_ip)
        if not device_ip:
            print(f"  ❌ Could not derive a device IP from host IP {host_ip}.")
            print("  Use Manual mode instead to set it yourself.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
            return

        confirmed = self._show_flash_confirmation(
            os_name=os_name,
            port=port,
            firmware_path=firmware_display,
            flash_target=flash_target,
            host_ip=host_ip,
            device_ip=device_ip,
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
        self.net_device_ip    = device_ip
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
                device_ip     = self.net_device_ip,
                firmware_path = Path(self.custom_fw_path),
                http_port     = self.net_http_port,
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

            print("=" * 60)
            print("PHASE 2+: Flashing Firmware")
            print("=" * 60)
            fw_display = "auto-detected from USB" if self.custom_fw_path == Path(".") else str(self.custom_fw_path)
            print(f"OS:          {self.os_name}")
            print(f"Firmware:    {fw_display}")
            print(f"Host IP:     {self.net_host_ip}:{self.net_http_port}")
            print(f"Device IP:   {self.net_device_ip}")
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
        """
        from mono_imager import recovery_orchestrator as rec

        print()
        print("  Network setup — REQUIRED before 'firmware update' will work.")
        print("  'firmware update' needs the device to reach the internet directly.")
        print()
        
        # Bring up all eth ports first, THEN check for LOWER_UP.
        # BUG FIXED: this previously checked for LOWER_UP without ever
        # bringing any interface up first — recovery Linux boots with
        # all eth ports administratively DOWN, so LOWER_UP was never
        # set regardless of whether a cable was plugged in. Confirmed
        # on real hardware: 'No active Ethernet port detected' even
        # with a cable connected, both before and after the retry
        # prompt. Same root cause and same fix already applied to
        # flash_orchestrator.py's phase2_network() earlier — this is
        # the matching fix for this separate code path.
        #
        # SPEEDUP: previously 5 separate 'eth up' calls + 1 'ip link
        # show' call = 6 run_script() round trips. Each round trip on
        # this device/link costs ~15s (write+verify+exec, each step
        # waiting for the line to settle) — confirmed via real log
        # timestamps (clean 5s jumps between each sub-step). Combining
        # all six commands into ONE script body cuts that to a single
        # round trip. Same commands, same write-verify-exec safety
        # checks, same idle-wait logic — just one trip instead of six.
        try:
            # BUG FIXED: checking 'ip link show' immediately after
            # 'ip link set up' can miss interfaces whose link partner
            # hasn't finished autonegotiating yet. Confirmed on real
            # Force eth0 only — it is the only working port
            d.run_script("ip link set eth0 up 2>/dev/null", marker="recovery_eth0_up", exec_timeout=10)
        except Exception as e:
            print(f"  ❌ Failed to bring up eth0: {e}")
            return False
        
        try:
            ip_output, _eth0_err = with_spinner(
                d.run_script, "sleep 2; ip link show eth0",
                marker="recovery_eth0_check", exec_timeout=10,
                message="Checking eth0 carrier..."
            )
            if _eth0_err:
                raise _eth0_err
            if 'LOWER_UP' not in ip_output:
                print("  ❌ eth0 has no carrier.")
                print("     Plug an Ethernet cable into the RIGHTMOST 1 Gig RJ-45 jack (not the SFP+ cages).")
                print()
                input("  Press Enter once the cable is plugged in...")
                ip_output = d.run_script("ip link show eth0", marker="recovery_eth0_check_retry", exec_timeout=5)
                if 'LOWER_UP' not in ip_output:
                    print("  ❌ eth0 still has no carrier.")
                    print("     Verify the cable is in the RIGHTMOST 1 Gig RJ-45 jack.")
                    return False
            print("  ✓ eth0 is ready.")
        except Exception as e:
            print(f"  ❌ Failed to check eth0 carrier: {e}")
            return False
        
        device_ip = input(
            "  Pick an unused IP address for the device on that same network "
            "(e.g. 192.168.1.50). Check your own machine's network adapter "
            "settings first if you're unsure of the IP/subnet/gateway on that "
            "network.\n  Device IP to assign: "
        ).strip()
        if not device_ip:
            print("  ❌ Device IP is required.")
            return False
        prefix = input("  Prefix [24]: ").strip() or "24"
        gateway = input("  Gateway (your router's IP on that network, e.g. 192.168.1.1): ").strip()
        if not gateway:
            print("  ❌ Gateway is required.")
            return False



        iface = "eth0"
        print(f"  Configuring {iface} = {device_ip}/{prefix}, gateway {gateway}...")
        net_cmd = (
            f"ip link set {iface} up && "
            f"ip addr add {device_ip}/{prefix} dev {iface} && "
            f"ip route add default via {gateway} dev {iface}; "
            f"echo RC=$?"
        )
        try:
            output = d.run_script(net_cmd, marker="recovery_net_setup_eth0", exec_timeout=20)
        except RuntimeError as e:
            print(f"  ❌ Network setup failed on eth0: {e}")
            return False

        if "RC=0" not in output:
            print(f"  ❌ Network setup did not report success on eth0.")
            return False

        print("  ✓ Local network config applied.", end=" ", flush=True)

        result, error = with_spinner(
            rec.check_internet_reachable, d, gateway=gateway,
            message="Verifying real connectivity..."
        )

        if error is not None:
            raise error

        if result:
            print(f"  ✓ Internet reachable via eth0 — network is ready.")
            return True

        print(f"  ❌ eth0 has link but could not reach the internet.")
        print("     Check the gateway IP, cable, and network configuration.")
        return False

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
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  START HERE: DIP switch → RIGHT (NOR)            │")
        print("  │                                                   │")
        print("  │  Booting from NOR recovery ensures 'firmware     │")
        print("  │  update' targets eMMC automatically.             │")
        print("  │                                                   │")
        print("  │  If DIP is LEFT (eMMC), flip it RIGHT and        │")
        print("  │  power-cycle before continuing.                  │")
        print("  └─────────────────────────────────────────────────┘")
        print()
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  ETHERNET: RIGHTMOST 1 Gig RJ-45 jack           │")
        print("  │  Device needs internet to download firmware.     │")
        print("  └─────────────────────────────────────────────────┘")
        print()

        port = self._select_port(allow_back=True, save_on_select=True)
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
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  ETHERNET: RIGHTMOST 1 Gig RJ-45 jack           │")
        print("  │  Device needs internet to download firmware.     │")
        print("  └─────────────────────────────────────────────────┘")
        print()

        port = self._select_port(allow_back=True, save_on_select=True)
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
          3. Detect host IP, derive device IP
          4. Assign IP to eth0 on device, ping from host
          5. Start HTTP server on host
          6. Device curls the server and reports back — confirms full path
        """
        from mono_imager.flash_orchestrator import (
            phase1_bootstrap, detect_host_ip, pick_device_ip,
            start_http_server, wait_for_report
        )
        from mono_imager.serial_device import SerialDevice
        import time, tempfile, pathlib, socket

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
        try:
            _d = SerialDevice(port, timeout=2)
            if _d.connect(115200):
                _d.ser.write(b"\r\nreset\r\nreboot\r\n")
                time.sleep(0.5)
                _d.disconnect()
        except Exception:
            pass  # best-effort — bootstrap will catch it either way

        d = phase1_bootstrap(port, 115200)
        if not self._check(results, "Device in recovery shell", d is not None):
            input("\n  Press Enter to return to main menu...")
            self.current_state = MenuState.MAIN
            return

        try:
            # Step 3: host IP + device IP
            host_ip   = self.net_host_ip or detect_host_ip()
            device_ip = self.net_device_ip or pick_device_ip(host_ip)

            if not self._check(results, "Host IP detected", bool(host_ip), host_ip or "could not detect"):
                return
            if not self._check(results, "Device IP", bool(device_ip), device_ip or "could not derive — use Manual mode"):
                return

            # Step 4: auto-detect active ethernet port, assign IP, ping from host.
            # Recovery Linux boots with all eth ports DOWN — bring them all up first,
            # then find the one with carrier (LOWER_UP). Hardcoding eth0 was wrong:
            # the cable may be on a different port, and an unchecked ip addr add to a
            # no-carrier interface would silently assign the IP to a dead port.
            eth_out = ""
            with Spinner("Detecting active Ethernet port..."):
                for _n in range(5):
                    try:
                        d.send_command(f"ip link set eth{_n} up 2>/dev/null", timeout=5)
                    except Exception:
                        pass
                eth_out = d.send_command("sleep 2; ip link show", timeout=15)
            iface = None
            for _line in eth_out.split('\n'):
                if 'LOWER_UP' in _line and ': ' in _line:
                    _parts = _line.split(': ')
                    if len(_parts) >= 2:
                        _name = _parts[1].split()[0]
                        if _name.startswith('eth'):
                            iface = _name
                            break

            if not self._check(results, "Ethernet port with carrier detected", iface is not None,
                               "plug cable into the RIGHTMOST 1 Gig RJ-45 jack" if iface is None else iface):
                print()
                print("  " + "─" * 56)
                print("  ✗  1 check failed.")
                input("\n  Press Enter to return to main menu...")
                return

            d.send_command(f"ip addr add {device_ip}/24 dev {iface}", timeout=10)

            # Ping HOST from DEVICE via serial — tests the actual path curl will use.
            # This is the useful direction: host->device ICMP needs admin on Windows
            # and tests the wrong direction anyway.
            ping_out, _ping_err = with_spinner(
                d.send_command, f"ping -c 2 -W 2 {host_ip} 2>&1", timeout=15,
                message=f"Testing device -> host connectivity..."
            )
            if _ping_err:
                ping_out = ""
            device_can_ping_host = "1 received" in ping_out or "2 received" in ping_out
            if device_can_ping_host:
                print(f"  ✓  Device can reach host {host_ip}")
            else:
                print(f"  ⚠  Device cannot ping host (ICMP likely blocked by host firewall — non-fatal)")

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
                f"curl -s -I -o /dev/null -w '%{{http_code}}' {url} "
                f"> /tmp/lantest_code.txt; "
                f"curl -s -X POST --data-binary @/tmp/lantest_code.txt "
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

            if not device_sees_host:
                _curl_detail = (
                    "host reachable but port 18080 blocked — allow Python.exe through Windows Defender Firewall"
                    if device_can_ping_host else
                    "device cannot reach host — verify cable connects to the SAME router/switch as the host"
                )
            else:
                _curl_detail = ""
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



    # ------------------------------------------------------------------ #
    #  MAIN LOOP                                                           #
    # ------------------------------------------------------------------ #
    def run(self):
        """Main event loop"""
        try:
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
