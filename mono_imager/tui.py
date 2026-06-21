#!/usr/bin/env python3
"""
mono-imager: Automated firmware flashing for Mono Gateway Routers and Dev Kit
Supports serial and networked connections with menu-driven TUI.

Author:  H.A. Hermsen
Version: 0.6.0
License: MIT
"""

__version__ = "0.6.0"
__author__ = "H.A. Hermsen"

import sys
import os
import re
import time
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

def verbose(msg: str, level: str = "info"):
    """Print to console immediately AND log it"""
    print(msg, flush=True)
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "debug":
        logger.debug(msg)
    else:
        logger.info(msg)



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

    soc = re.search(r'^SoC:\s+(.+)$', raw_output, re.MULTILINE)
    if soc:
        result["SoC"] = soc.group(1).strip()

    model = re.search(r'^Model:\s+(.+)$', raw_output, re.MULTILINE)
    if model:
        result["Model"] = model.group(1).strip()

    dram = re.search(r'^DRAM:\s+(.+)$', raw_output, re.MULTILINE)
    if dram:
        result["DRAM"] = dram.group(1).strip()

    cpu_clocks = re.findall(r'CPU\d+\([^)]*\):(\d+)\s*MHz', raw_output)
    if cpu_clocks:
        unique = sorted(set(cpu_clocks), key=int)
        if len(unique) == 1:
            result["CPU clock"] = f"{unique[0]} MHz (all cores)"
        else:
            result["CPU clock"] = ", ".join(f"{c} MHz" for c in cpu_clocks)

    bus = re.search(r'Bus:\s+(\d+)\s*MHz', raw_output)
    if bus:
        result["Bus clock"] = f"{bus.group(1)} MHz"

    ddr = re.search(r'DDR:\s+(\d+)\s*MT/s', raw_output)
    if ddr:
        result["DDR clock"] = f"{ddr.group(1)} MT/s"

    fman = re.search(r'FMAN:\s+(\d+)\s*MHz', raw_output)
    if fman:
        result["FMAN clock"] = f"{fman.group(1)} MHz"

    return result


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
    results = []
    
    for line in raw_output.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        # Match [ OK ] Label    value
        if line.startswith('[ OK ]'):
            rest = line[6:].strip()  # Skip "[ OK ] "
            
            # Split on first run of whitespace
            parts = rest.split(None, 1)
            if parts:
                label = parts[0]
                value = parts[1] if len(parts) > 1 else ""
                
                # Skip summary line
                if label.lower() != "self-test":
                    results.append((label, value))
    
    return results




class ConnectionMode(Enum):
    """Connection type to device"""
    SERIAL  = "Serial (USB/UART)"
    NETWORK = "Network (Ethernet)"


class MenuState(Enum):
    """Main menu states"""
    MAIN                   = "main"
    FLASH_AUTO_OR_MANUAL   = "flash_auto_or_manual"
    CONNECTION             = "connection"
    DEVICE_SELECT          = "device_select"
    TRANSFER_METHOD        = "transfer_method"
    NETWORK_AUTO_CONFIG    = "network_auto_config"
    NETWORK_FLASH_CONFIG  = "network_flash_config"
    NETWORK_FLASHING      = "network_flashing"
    RECOVERY_FLOW         = "recovery_flow"
    DONE                  = "done"
    CLI_CONSOLE           = "cli_console"
    DEVICE_STATS          = "device_stats"


class MonoImager:
    """Main application controller"""

    def __init__(self, log_file: Path = None):
        self.current_state   = MenuState.MAIN
        self.connection_mode = None
        self.device          = None
        self.custom_fw_path  = None
        self.serial_port     = None
        self.network_host    = None
        self.flash_success   = False
        self.log_file        = log_file
        self.cli_port        = None

        # Transfer method (asked after serial port is selected — serial
        # is always mandatory for bootstrap; this chooses how the actual
        # firmware bytes get transferred): "network" or "serial"
        self.transfer_method = None
        # Network transfer config, only used when transfer_method == "network"
        self.net_host_ip      = None
        self.net_device_ip    = None
        self.net_http_port    = 8080
        self.net_flash_target = None

    def clear_screen(self):
        """Clear terminal"""
        os.system('clear' if os.name == 'posix' else 'cls')

    def print_header(self):
        """
        Print application header. Box width is computed from the
        actual content rather than hardcoded, so it stays correctly
        aligned regardless of version string length (e.g. "0.5.0" vs
        a future "0.10.0") instead of needing manual padding upkeep.
        """
        version_line = f"mono-imager {__version__}"
        subtitle      = "mono gateway firmware flash utility"
        license_line  = f"written by {__author__}, MIT licensed"

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
        print("  2) Update / Recover Firmware")
        print("  3) CLI only (serial)")
        print("  4) Test Serial connection")
        print("  5) Test LAN connection")
        print("  6) Show Device Stats")
        print("  7) Exit")
        print()

        choice = input("Select [1-7]: ").strip()

        if choice == "1":
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
        elif choice == "2":
            self.current_state = MenuState.RECOVERY_FLOW
        elif choice == "3":
            self.current_state = MenuState.CLI_CONSOLE
        elif choice == "4":
            print()
            print("  Test Serial connection not yet implemented.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
        elif choice == "5":
            print()
            print("  Test LAN connection not yet implemented.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
        elif choice == "6":
            self.current_state = MenuState.DEVICE_STATS
        elif choice == "7":
            sys.exit(0)
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN

    # ------------------------------------------------------------------ #
    #  2. CONNECTION (serial is always required — go straight to port    #
    #     selection; the network-vs-serial choice happens AFTER connecting,#
    #     and is about transfer method, not connection method)            #
    # ------------------------------------------------------------------ #
    def menu_connection(self):
        """
        Entry point into the connect-to-device flow. Serial is mandatory
        for every flash (it's how mono-imager bootstraps the device and
        interrupts U-Boot) — there is no connection-method choice here
        anymore. This just hands off straight to serial port detection.
        """
        self.connection_mode = ConnectionMode.SERIAL
        self.current_state   = MenuState.DEVICE_SELECT

    # ------------------------------------------------------------------ #
    #  3. DEVICE / PORT SELECTION                                          #
    # ------------------------------------------------------------------ #
    def menu_device_select(self):
        """Device detection and selection"""
        self.clear_screen()
        self.print_header()

        if self.connection_mode == ConnectionMode.SERIAL:
            self._detect_serial_devices()
        elif self.connection_mode == ConnectionMode.NETWORK:
            self._detect_network_devices()

    def _detect_serial_devices(self):
        """Detect available serial ports with USB-UART filtering and last-used memory"""
        from mono_imager.config import detect_serial_ports, get_last_port, save_last_port

        print("Scanning for serial devices...")
        print()

        try:
            known_ports, other_ports = detect_serial_ports()
        except RuntimeError as e:
            print(f"  ❌ {e}")
            print()
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        all_ports = known_ports + other_ports

        if not all_ports:
            print("  ❌ No serial devices found.")
            print()
            print("  Please ensure your USB/UART cable is connected.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        last_port = get_last_port()

        if known_ports:
            print("  USB-UART adapters (recommended):")
            for i, port in enumerate(known_ports, 1):
                marker = " ◄ last used" if port.device == last_port else ""
                print(f"    {i}) {port.device} — {port.description}{marker}")

        if other_ports:
            print()
            print("  Other ports:")
            offset = len(known_ports)
            for i, port in enumerate(other_ports, offset + 1):
                marker = " ◄ last used" if port.device == last_port else ""
                print(f"    {i}) {port.device} — {port.description}{marker}")

        print()
        print(f"  {len(all_ports) + 1}) Back")
        print()

        if last_port and last_port in [p.device for p in all_ports]:
            print(f"  [Enter] Use last port ({last_port})")
            print()

        choice = input(f"Select [1-{len(all_ports) + 1}]: ").strip()

        if choice == "" and last_port and last_port in [p.device for p in all_ports]:
            self.serial_port   = last_port
            self.current_state = MenuState.TRANSFER_METHOD
            return

        try:
            idx = int(choice) - 1
            if idx == len(all_ports):
                self.current_state = MenuState.MAIN
                return
            if 0 <= idx < len(all_ports):
                self.serial_port = all_ports[idx].device
                save_last_port(self.serial_port)
                verbose(f"Selected serial port: {self.serial_port}")
                self.current_state = MenuState.TRANSFER_METHOD
            else:
                print("  Invalid selection.")
                input("  Press Enter to continue...")
                self.current_state = MenuState.DEVICE_SELECT
        except ValueError:
            print("  Invalid input.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.DEVICE_SELECT

    def _detect_network_devices(self):
        """Network host entry — stub, no longer reachable from the menu
        (kept only because removing it wasn't part of the agreed scope
        for this change; transfer-method selection replaced its role)."""
        print("  Network device detection not yet implemented.")
        print()
        input("  Press Enter to continue...")
        self.current_state = MenuState.CONNECTION

    # ------------------------------------------------------------------ #
    #  3b. TRANSFER METHOD (how the firmware bytes actually get moved —  #
    #      serial connection above is always required for bootstrap;     #
    #      this only affects the data transfer, not the connection)      #
    # ------------------------------------------------------------------ #
    def menu_transfer_method(self):
        """
        Ask how to transfer the firmware after serial bootstrap.

        Only Network is currently offered. A Serial-only transfer
        option (_flash_serial() / flasher.py's Flasher class) existed
        here but was removed: it predated this session's reliability
        hardening — it still used raw send_command() calls instead of
        the proven run_script()/launch_script() heredoc-write-then-
        verify pattern that exists specifically because naive serial
        command execution was found to be unreliable (see
        flash_orchestrator.py history) — and had never been tested on
        real hardware. The whole menu chain it routed through
        (FLASH_MODE/FIRMWARE_SOURCE/CONFIRM/FLASHING) was unreachable
        anyway, so it was deleted outright rather than left disabled.
        """
        self.clear_screen()
        self.print_header()
        print(f"  Connected: {self.serial_port}")
        print()
        print("How should the firmware be transferred to the device?")
        print()
        print("  1) Serial, then Network — fast (~2.7 MB/s, ~2.5 min for 400MB)")
        print("  2) Back")
        print()

        choice = input("Select [1-2]: ").strip()

        if choice == "1":
            self.transfer_method = "network"
            self.current_state   = MenuState.NETWORK_FLASH_CONFIG
        elif choice == "2":
            self.current_state = MenuState.DEVICE_SELECT
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.TRANSFER_METHOD

    # ------------------------------------------------------------------ #
    #  1b. FLASH: AUTO OR MANUAL — the real fork. Fully Auto matches      #
    #      tests/test_verify_flash_auto.py exactly: port is              #
    #      auto-detected (first port found), host/device IP auto-        #
    #      derived, only the firmware file is asked. Manual goes through #
    #      the existing port-selection screen, then the full config form.#
    # ------------------------------------------------------------------ #
    def menu_flash_auto_or_manual(self):
        self.clear_screen()
        self.print_header()
        print("  ⚠️  ETHERNET: Plug into RIGHTMOST 1 Gig RJ-45 jack (not SFP+ cages)")
        print()
        print("  1) Fully Auto — just point it at a firmware file")
        print("  2) Manual     — choose port, network settings yourself")
        print("  3) Back")
        print()

        choice = input("Select [1-3]: ").strip()

        if choice == "1":
            self.current_state = MenuState.NETWORK_AUTO_CONFIG
        elif choice == "2":
            self.current_state = MenuState.CONNECTION
        elif choice == "3":
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

        # OS selection to determine flash target
        print("  Select the OS you're flashing:")
        print("    1) Armbian (Debian/Ubuntu)")
        print("    2) OPNsense")
        print("    3) OpenWRT")
        print("    4) VyOS")
        print("    5) Other (manual target)")
        print("    6) Back")
        print()
        os_choice = input("  Select [1-6]: ").strip()

        flash_target = "/dev/mmcblk0"  # Default for most OSes
        if os_choice == "1":
            os_name = "Armbian"
        elif os_choice == "2":
            os_name = "OPNsense"
        elif os_choice == "3":
            os_name = "OpenWRT"
            flash_target = "/dev/mmcblk0p1"  # OpenWRT uses partition 1
        elif os_choice == "4":
            os_name = "VyOS"
        elif os_choice == "5":
            os_name = "Other"
            flash_target = input("  Enter flash target (e.g., /dev/mmcblk0 or /dev/mmcblk0p1): ").strip()
            if not flash_target:
                print("  ❌ Flash target is required.")
                input("  Press Enter to continue...")
                self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
                return
        elif os_choice == "6":
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
            return
        else:
            print("  ❌ Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.NETWORK_AUTO_CONFIG
            return

        print()
        firmware_raw = input("  Type the full path (or paste or drag-n-drop) of the image file: ")
        firmware_path, error = self._validate_firmware_path(firmware_raw)
        if error:
            print(f"  ❌ {error}")
            input("  Press Enter to continue...")
            self.current_state = MenuState.NETWORK_AUTO_CONFIG
            return

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
        confirm = input("  This writes to the device. Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
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
    def menu_network_flash_config(self):
        """
        Collect network flash parameters. Prompts mirror
        tests/test_verify_flash_manual.py, which is proven working on
        real hardware — including the quote-stripping fix for Windows
        paths typed with quotes (e.g. "C:\\path with spaces\\file.img"),
        which previously caused a silent failure if omitted.
        """
        self.clear_screen()
        self.print_header()
        print(f"  Connected: {self.serial_port}  |  Transfer: Network")
        print()
        print("Network configuration:")

        host_ip = input("  Host IP (this machine's IP on the device's network): ").strip()
        if not host_ip:
            print("  Host IP is required.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.TRANSFER_METHOD
            return

        device_ip = input("  Device IP to assign (must be on host's subnet): ").strip()
        if not device_ip:
            print("  Device IP is required.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.TRANSFER_METHOD
            return

        http_port_raw = input("  HTTP server port [8080]: ").strip()
        try:
            http_port = int(http_port_raw) if http_port_raw else 8080
        except ValueError:
            print(f"  '{http_port_raw}' is not a number, using default 8080")
            http_port = 8080

        firmware_raw = input("  Type or paste the full path of the image file: ")
        firmware_path, error = self._validate_firmware_path(firmware_raw)
        if error:
            print(f"  ❌ {error}")
            input("  Press Enter to continue...")
            self.current_state = MenuState.NETWORK_FLASH_CONFIG
            return

        flash_target = input("  Flash target device (e.g. /dev/mmcblk0): ").strip()
        if not flash_target:
            print("  Flash target is required.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.NETWORK_FLASH_CONFIG
            return

        print()
        print("  About to flash:")
        print(f"    Firmware:    {firmware_path}")
        print(f"    Target:      {flash_target}")
        print(f"    Device IP:   {device_ip}")
        print(f"    Host IP:     {host_ip}:{http_port}")
        print()
        print("  This tool is well tested, but writing firmware is never")
        print("  without risk. Do not unplug power or disconnect the cable")
        print("  while flashing — an interrupted write can leave the")
        print("  device unbootable.")
        print()
        confirm = input("  This writes to the device. Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Cancelled.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        self.net_host_ip    = host_ip
        self.net_device_ip  = device_ip
        self.net_http_port  = http_port
        self.custom_fw_path = firmware_path
        self.net_flash_target = flash_target
        self.current_state  = MenuState.NETWORK_FLASHING

    def menu_network_flashing(self):
        """
        Run the actual flash via flash_orchestrator.py — the same
        proven functions used by tests/test_verify_flash_auto.py and
        tests/test_verify_flash_manual.py, confirmed working on real
        hardware (12/12 steps, both auto and manual paths).
        """
        # DON'T clear screen here — keep all output visible for debugging

        from mono_imager import flash_orchestrator as core

        server = None
        d = None
        try:
            print()
            print("=" * 60)
            print("PHASE 1: Bootstrap (Serial)")
            print("=" * 60)
            print(f"Port: {self.serial_port}")
            print(f"Baud: 115200")
            print()
            d = core.phase1_bootstrap(self.serial_port, 115200)
            if d is None:
                print("❌ Phase 1 FAILED")
                print()
                self.flash_success = core.print_report()
                self.current_state = MenuState.DONE
                return

            print("✓ Phase 1 PASSED")
            print()
            
            print("=" * 60)
            print("PHASE 2: Network Setup")
            print("=" * 60)
            print(f"Host IP: {self.net_host_ip}")
            print(f"Device IP: {self.net_device_ip}")
            print(f"HTTP Port: {self.net_http_port}")
            print(f"Firmware: {self.custom_fw_path}")
            print()
            server = core.phase2_network(
                d, self.net_host_ip, self.net_device_ip,
                self.net_http_port, self.custom_fw_path
            )
            if server is None:
                print("❌ Phase 2 FAILED")
                print()
                self.flash_success = core.print_report()
                self.current_state = MenuState.DONE
                return

            print("✓ Phase 2 PASSED")
            print()
            print("=" * 60)
            print("PHASE 3: Flash (curl | dd)")
            print("=" * 60)
            print(f"Target: {self.net_flash_target}")
            print()
            ok = core.phase3_flash(
                d, self.net_host_ip, self.net_http_port, self.net_flash_target,
                firmware_size=os.path.getsize(self.custom_fw_path)
            )
            if not ok:
                print("❌ Phase 3 FAILED")
                print()
                self.flash_success = core.print_report()
                self.current_state = MenuState.DONE
                return

            print("✓ Phase 3 PASSED")
            print()
            print("=" * 60)
            print("PHASE 4: Post-Flash (Reboot)")
            print("=" * 60)
            print()
            core.phase4_postflash(d)
            print("✓ Phase 4 PASSED")
            print()

        finally:
            if server:
                server.shutdown()
                core.verbose("HTTP server stopped")
            if d:
                d.disconnect()

        self.flash_success = core.print_report()
        self.current_state = MenuState.DONE

    # ------------------------------------------------------------------ #
    #  RECOVERY — Update / Recover Firmware. Guided (guardrailed)         #
    #  semi-automated: device/firmware-type detection is automatic, but #
    #  the physical DIP-switch flips pause for an explicit Enter rather #
    #  than blindly polling immediately — the user stays in control of  #
    #  timing while not needing to know modern-vs-legacy device details.#
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
            ip_output = d.run_script("sleep 2; ip link show eth0", marker="recovery_eth0_check", exec_timeout=10)
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

        from mono_imager.spinner import with_spinner

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

    def menu_recovery_flow(self):
        """
        Run the documented modern recovery sequence (NOR -> flash eMMC
        -> [DIP flip] -> eMMC -> flash NOR -> [DIP flip back]), or the
        legacy curl-based sequence on older devices — detected live,
        per device, via recovery_orchestrator.detect_modern_firmware_tool().

        Reuses flash_orchestrator.phase1_bootstrap() for the initial
        connect/interrupt/boot-recovery/login sequence (same proven
        bootstrap used by the Flash OS path), then hands off to
        recovery_orchestrator.py's phase_* functions, which already
        do the actual device-talking and result tracking.
        """
        from mono_imager import flash_orchestrator as core
        from mono_imager import recovery_orchestrator as rec
        from mono_imager.config import detect_serial_ports, get_last_port, save_last_port

        self.clear_screen()
        self.print_header()
        print("Update / Recover Firmware")
        print()
        print("This walks you through recovering or refreshing your device's")
        print("firmware, using the device's own 'firmware update' tool (or the")
        print("legacy curl-based method on older devices, detected automatically).")
        print("You'll be asked to flip the DIP switch and power-cycle the device")
        print("at the right moments — nothing happens without your confirmation.")
        print()
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  START HERE: DIP switch → RIGHT (NOR)            │")
        print("  │                                                   │")
        print("  │  This makes the board boot straight into         │")
        print("  │  recovery on its own — you don't have to catch   │")
        print("  │  the countdown or type anything.                 │")
        print("  │                                                   │")
        print("  │  If your DIP is on LEFT (eMMC) right now, flip   │")
        print("  │  it to RIGHT and power-cycle before continuing.  │")
        print("  └─────────────────────────────────────────────────┘")
        print()
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  ETHERNET CABLE REQUIREMENT                      │")
        print("  │                                                   │")
        print("  │  Plug an Ethernet cable into the RIGHTMOST       │")
        print("  │  1 Gig RJ-45 jack (not the SFP+ cages).          │")
        print("  │                                                   │")
        print("  │  The device needs internet access to download    │")
        print("  │  firmware during the update process.             │")
        print("  └─────────────────────────────────────────────────┘")
        print()

        # --- Port selection ---
        try:
            known, other = detect_serial_ports()
            all_ports = known + other
        except Exception as e:
            print(f"  ❌ Port detection failed: {e}")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        if not all_ports:
            print("  ❌ No serial device found. Connect the device and try again.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        last_port = get_last_port()
        for i, p in enumerate(all_ports, 1):
            marker = " ◄ last used" if p.device == last_port else ""
            print(f"  {i}) {p.device} — {p.description}{marker}")
        print(f"  {len(all_ports) + 1}) Back")
        print()

        choice = input(f"Select [1-{len(all_ports) + 1}]: ").strip()
        try:
            idx = int(choice) - 1
            if idx == len(all_ports):
                self.current_state = MenuState.MAIN
                return
            if not (0 <= idx < len(all_ports)):
                raise ValueError("out of range")
            port = all_ports[idx].device
        except ValueError:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        save_last_port(port)

        print()
        print("⚠️  This can write firmware to your device's NOR and/or eMMC.")
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Cancelled.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        def show_progress(chunk):
            print(chunk, end="", flush=True)
            # BUG THIS FIXES: live 'firmware update' output only ever
            # went to print() — if the process gets killed (e.g. a
            # runaway prompt requiring Ctrl+C) before _stream_command()
            # returns and logs the full buffer, none of what was on
            # screen ever made it into the log file. Logging each
            # chunk as it arrives means a forced kill still leaves a
            # real trail to diagnose from.
            verbose(f"[firmware update output] {chunk!r}", "debug")

        def finish(success: bool):
            self.flash_success = success
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN

        d = None
        try:
            d = core.phase1_bootstrap(port, 115200)
            if d is None:
                print()
                print("  ❌ Could not bootstrap into the recovery shell.")
                finish(core.print_report())
                return

            rec.reset_results()
            is_modern = rec.detect_modern_firmware_tool(d)

            if is_modern is None:
                print()
                print("  ❌ Could not determine the device's firmware tool type.")
                finish(rec.print_report())
                return

            if not self._setup_recovery_network(d):
                finish(rec.print_report())
                return

            if is_modern:
                print()
                print("  Modern firmware tool detected.")
                print()
                emmc_ok = rec.phase_modern_flash_emmc(d, on_output=show_progress)
                if not emmc_ok:
                    print()
                    print("  ⚠ Modern 'firmware update' failed for eMMC — falling back")
                    print("    to the legacy curl+dd method (same session, no reboot needed)...")
                    emmc_ok = rec.phase_legacy_flash_emmc(d)
                    if not emmc_ok:
                        print("  ❌ Legacy fallback also failed for eMMC. Nothing more to try automatically.")
                        finish(rec.print_report())
                        return
                    print("  ✓ Legacy fallback succeeded for eMMC.")
                    print("  Backend that serves the modern tool looks broken for this device —")
                    print("  continuing with the legacy method for NOR too (no DIP flip needed).")
                    rec.phase_legacy_flash_nor(d)
                    finish(rec.print_report())
                    return

                print()
                print("=" * 60)
                print("  ⚡ FLIP THE DIP SWITCH TO eMMC, THEN POWER-CYCLE ⚡")
                print("=" * 60)
                input("  Press Enter once you've done that...")

                if not rec.phase_modern_verify_emmc_boot(d):
                    print("  ❌ Could not confirm the device booted from eMMC.")
                    finish(rec.print_report())
                    return

                print("  Re-entering the recovery shell on eMMC...")
                reentered = (
                    d.wait_for_autoboot(timeout=60)
                    and d.boot_recovery()
                    and d.login_recovery(timeout=60)
                )
                if not reentered:
                    print("  ❌ Could not re-enter the recovery shell after the eMMC boot.")
                    finish(rec.print_report())
                    return

                if not self._setup_recovery_network(d):
                    finish(rec.print_report())
                    return

                nor_ok = rec.phase_modern_flash_nor(d, on_output=show_progress)
                if not nor_ok:
                    print()
                    print("  ⚠ Modern 'firmware update' failed for NOR — falling back")
                    print("    to the legacy curl+flashcp method...")
                    nor_ok = rec.phase_legacy_flash_nor(d)
                    if not nor_ok:
                        print("  ❌ Legacy fallback also failed for NOR. Nothing more to try automatically.")
                        finish(rec.print_report())
                        return
                    print("  ✓ Legacy fallback succeeded for NOR.")

                print()
                print("=" * 60)
                print("  ⚡ FLIP THE DIP SWITCH BACK TO NOR, THEN POWER-CYCLE ⚡")
                print("=" * 60)
                input("  Press Enter once you've done that...")

                rec.phase_modern_verify_nor_boot(d)

            else:
                print()
                print("  Legacy firmware tool detected — using curl/dd/flashcp directly.")
                print("  (No DIP-switch flips needed for this path.)")
                print()
                if not rec.phase_legacy_flash_emmc(d):
                    finish(rec.print_report())
                    return
                rec.phase_legacy_flash_nor(d)

        finally:
            if d:
                d.disconnect()

        finish(rec.print_report())

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
        from mono_imager.config import detect_serial_ports, get_last_port, save_last_port
        from mono_imager.serial_device import SerialDevice
        import threading

        self.clear_screen()
        self.print_header()
        print("  CLI Console — Serial")
        print()

        try:
            known_ports, other_ports = detect_serial_ports()
        except RuntimeError as e:
            print(f"  ❌ {e}")
            print()
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        all_ports = known_ports + other_ports

        if not all_ports:
            print("  ❌ No serial devices found.")
            print()
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        last_port = get_last_port()

        if known_ports:
            print("  USB-UART adapters (recommended):")
            for i, port in enumerate(known_ports, 1):
                marker = " ◄ last used" if port.device == last_port else ""
                print(f"    {i}) {port.device} — {port.description}{marker}")
        if other_ports:
            print()
            print("  Other ports:")
            offset = len(known_ports)
            for i, port in enumerate(other_ports, offset + 1):
                marker = " ◄ last used" if port.device == last_port else ""
                print(f"    {i}) {port.device} — {port.description}{marker}")

        print()
        print(f"  {len(all_ports) + 1}) Back")
        print()

        if last_port and last_port in [p.device for p in all_ports]:
            print(f"  [Enter] Use last port ({last_port})")
            print()

        choice = input(f"Select [1-{len(all_ports) + 1}]: ").strip()

        if choice == "" and last_port and last_port in [p.device for p in all_ports]:
            port = last_port
        else:
            try:
                idx = int(choice) - 1
                if idx == len(all_ports):
                    self.current_state = MenuState.MAIN
                    return
                if 0 <= idx < len(all_ports):
                    port = all_ports[idx].device
                    save_last_port(port)
                else:
                    print("  Invalid selection.")
                    input("  Press Enter to continue...")
                    self.current_state = MenuState.CLI_CONSOLE
                    return
            except ValueError:
                print("  Invalid input.")
                input("  Press Enter to continue...")
                self.current_state = MenuState.CLI_CONSOLE
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
        from mono_imager.config import detect_serial_ports, get_last_port, save_last_port
        from mono_imager.serial_device import SerialDevice
        
        self.clear_screen()
        self.print_header()
        
        # Port selection
        try:
            known_ports, other_ports = detect_serial_ports()
        except RuntimeError as e:
            print(f"  ❌ {e}")
            print()
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        all_ports = known_ports + other_ports

        if not all_ports:
            print("  ❌ No serial devices found.")
            print()
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
            return

        last_port = get_last_port()

        print("  Select device to query:")
        print()
        if known_ports:
            print("  USB-UART adapters (recommended):")
            for i, port in enumerate(known_ports, 1):
                marker = " ◄ last used" if port.device == last_port else ""
                print(f"    {i}) {port.device} — {port.description}{marker}")
        if other_ports:
            print()
            print("  Other ports:")
            offset = len(known_ports)
            for i, port in enumerate(other_ports, offset + 1):
                marker = " ◄ last used" if port.device == last_port else ""
                print(f"    {i}) {port.device} — {port.description}{marker}")

        print()
        print(f"  {len(all_ports) + 1}) Back")
        print()

        if last_port and last_port in [p.device for p in all_ports]:
            print(f"  [Enter] Use last port ({last_port})")
            print()

        choice = input(f"Select [1-{len(all_ports) + 1}]: ").strip()

        if choice == "" and last_port and last_port in [p.device for p in all_ports]:
            port = last_port
        else:
            try:
                idx = int(choice) - 1
                if idx == len(all_ports):
                    self.current_state = MenuState.MAIN
                    return
                if 0 <= idx < len(all_ports):
                    port = all_ports[idx].device
                    save_last_port(port)
                else:
                    print("  Invalid selection.")
                    input("  Press Enter to continue...")
                    self.current_state = MenuState.DEVICE_STATS
                    return
            except ValueError:
                print("  Invalid input.")
                input("  Press Enter to continue...")
                self.current_state = MenuState.DEVICE_STATS
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

            raw_output = device.capture_boot_diagnostics(timeout=60)
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
        """
        Parse and display U-Boot's boot-time diagnostics: SoC/board identity.

        Parses identity fields from U-Boot output.
        """
        identity = parse_uboot_identity(raw_output)

        if not identity:
            print()
            print("  ⚠️  No recognizable U-Boot diagnostic output found.")
            return

        print()
        print("  " + "─" * 56)
        print("  BOARD IDENTITY")
        print("  " + "─" * 56)
        if identity:
            for label, value in identity.items():
                print(f"    {label:<22} {value}")
        else:
            print("    (none captured)")
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
                elif self.current_state == MenuState.CONNECTION:
                    self.menu_connection()
                elif self.current_state == MenuState.DEVICE_SELECT:
                    self.menu_device_select()
                elif self.current_state == MenuState.TRANSFER_METHOD:
                    self.menu_transfer_method()
                elif self.current_state == MenuState.NETWORK_AUTO_CONFIG:
                    self.menu_network_auto_config()
                elif self.current_state == MenuState.NETWORK_FLASH_CONFIG:
                    self.menu_network_flash_config()
                elif self.current_state == MenuState.NETWORK_FLASHING:
                    self.menu_network_flashing()
                elif self.current_state == MenuState.RECOVERY_FLOW:
                    self.menu_recovery_flow()
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
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"flash_{timestamp}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
        force=True
    )
    verbose(f"mono-imager v{__version__} by {__author__}")
    verbose(f"Log: {log_file}")

    app = MonoImager(log_file)
    app.run()


if __name__ == "__main__":
    main()
