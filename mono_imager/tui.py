#!/usr/bin/env python3
"""
mono-imager: Automated firmware flashing for Mono Gateway Routers and Dev Kit
Supports serial and networked connections with menu-driven TUI.

Author:  H.A. Hermsen
Version: 0.5.0
License: MIT
"""

__version__ = "0.5.0"
__author__ = "H.A. Hermsen"

import sys
import os
import re
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


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
    Pattern validated against a real capture on this hardware — lines
    of the form "Label (padded) : PASS (optional parenthetical
    value)", e.g.:

        CPU temperature     : PASS (47°C)
        Board temperature   : PASS (44°C)
        USB PD controller   : PASS

    Only matches PASS lines — by design, this is a read-only stats
    display, not a pass/fail diagnostic tool. A failing self-test line
    would print differently (not "PASS") and won't be captured here;
    extending this to surface failures would be a deliberate future
    change, not an accidental omission.

    Returns a list of (label, value) tuples in the order they appeared.
    value is "" for lines with no parenthetical (e.g. "Retimer : PASS").
    """
    pattern = re.compile(r'^([A-Za-z0-9.\s/\-]+?)\s*:\s*PASS(?:\s*\((.*?)\))?\s*$', re.MULTILINE)
    matches = pattern.findall(raw_output)

    # "On-board devices self test: PASS" is a SUMMARY line (confirmed
    # from a real capture: it appears after a blank line, following
    # all the individual component lines) — it matches the same
    # pattern by coincidence but isn't itself a component result.
    # Excluded by exact name rather than a cleverer regex tweak, so a
    # real future component test isn't accidentally excluded too.
    return [
        (label.strip(), value.strip())
        for label, value in matches
        if label.strip().lower() != "on-board devices self test"
    ]


class FlashMode(Enum):
    """Flash target selection"""
    EMMC = "eMMC only"
    NOR  = "NOR only"
    DUAL = "Dual (NOR → eMMC → NOR)"


class ConnectionMode(Enum):
    """Connection type to device"""
    SERIAL  = "Serial (USB/UART)"
    NETWORK = "Network (Ethernet)"


class FirmwareChoice(Enum):
    """Firmware source selection"""
    MONO_OFFICIAL = "Mono Official"
    ARMBIAN       = "Armbian"
    CUSTOM        = "Custom (local file)"


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
    FLASH_MODE            = "flash_mode"
    FIRMWARE_SOURCE       = "firmware_source"
    CONFIRM               = "confirm"
    FLASHING              = "flashing"
    DONE                  = "done"
    CLI_CONSOLE           = "cli_console"
    DEVICE_STATS          = "device_stats"


class MonoImager:
    """Main application controller"""

    def __init__(self, log_file: Path = None):
        self.current_state   = MenuState.MAIN
        self.connection_mode = None
        self.device          = None
        self.flash_mode      = None
        self.firmware_choice = None
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
        print("  1) Flash firmware")
        print("  2) Recover bricked device")
        print("  3) CLI only (console)")
        print("  4) Show Device Stats")
        print("  5) Exit")
        print()

        choice = input("Select [1-5]: ").strip()

        if choice == "1":
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
        elif choice == "2":
            print()
            print("  Recovery mode not yet implemented.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
        elif choice == "3":
            self.current_state = MenuState.CLI_CONSOLE
        elif choice == "4":
            self.current_state = MenuState.DEVICE_STATS
        elif choice == "5":
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
                logger.info(f"Selected serial port: {self.serial_port}")
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
        option existed here but was DELIBERATELY DISABLED: it routes
        to _flash_serial() / flasher.py's Flasher class, which predates
        all of this session's reliability hardening — it still uses
        raw send_command() calls instead of the proven
        run_script()/launch_script() heredoc-write-then-verify pattern
        that exists specifically because naive serial command execution
        was found to be unreliable (see flash_orchestrator.py history).
        It has not been tested on real hardware this session at all.

        Re-enable only after porting flash_emmc_manual()/
        flash_emmc_modern() to the same hardened pattern used
        throughout flash_orchestrator.py, and verifying on real
        hardware — same bar as the Network path was held to.
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

        firmware_raw = input("  Type or paste the full path of the image file: ")
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
        print(f"    Port:        {port}")
        print(f"    Firmware:    {firmware_path}")
        print(f"    Target:      /dev/mmcblk0")
        print(f"    Host IP:     {host_ip}:8080  (auto-detected)")
        print(f"    Device IP:   {device_ip}  (auto-derived)")
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
        self.net_flash_target = "/dev/mmcblk0"
        self.current_state    = MenuState.NETWORK_FLASHING

    # ------------------------------------------------------------------ #
    #  NETWORK FLASH CONFIG — collects the values flash_orchestrator.py  #
    #  actually needs. Deliberately separate from FLASH_MODE/            #
    #  FIRMWARE_SOURCE below: those menus (eMMC/NOR/Dual, Mono Official/ #
    #  Armbian/Custom) map onto _flash_serial()'s Flasher-class API,     #
    #  which flash_orchestrator.py does not use at all — it just takes a#
    #  firmware file path and a flash target string directly. Reusing   #
    #  those menus here would ask questions that don't actually apply.  #
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
        self.clear_screen()
        self.print_header()

        from mono_imager import flash_orchestrator as core

        server = None
        d = None
        try:
            d = core.phase1_bootstrap(self.serial_port, 115200)
            if d is None:
                self.flash_success = core.print_report()
                self.current_state = MenuState.DONE
                return

            server = core.phase2_network(
                d, self.net_host_ip, self.net_device_ip,
                self.net_http_port, self.custom_fw_path
            )
            if server is None:
                self.flash_success = core.print_report()
                self.current_state = MenuState.DONE
                return

            ok = core.phase3_flash(
                d, self.net_host_ip, self.net_http_port, self.net_flash_target
            )
            if not ok:
                self.flash_success = core.print_report()
                self.current_state = MenuState.DONE
                return

            core.phase4_postflash(d)

        finally:
            if server:
                server.shutdown()
                core.logger.info("HTTP server stopped")
            if d:
                d.disconnect()

        self.flash_success = core.print_report()
        self.current_state = MenuState.DONE

    # ------------------------------------------------------------------ #
    #  4. FLASH TARGET                                                     #
    # ------------------------------------------------------------------ #
    def menu_flash_mode(self):
        """Flash target selection"""
        self.clear_screen()
        self.print_header()
        print("What would you like to flash?")
        print()
        print("  1) eMMC only          (safe, single step)")
        print("  2) NOR only           (advanced)")
        print("  3) Dual               (NOR → eMMC → NOR, recommended)")
        print("  4) Back")
        print()

        choice = input("Select [1-4]: ").strip()

        if choice == "1":
            self.flash_mode    = FlashMode.EMMC
            self.current_state = MenuState.FIRMWARE_SOURCE
        elif choice == "2":
            self.flash_mode    = FlashMode.NOR
            self.current_state = MenuState.FIRMWARE_SOURCE
        elif choice == "3":
            self.flash_mode    = FlashMode.DUAL
            self.current_state = MenuState.FIRMWARE_SOURCE
        elif choice == "4":
            self.current_state = MenuState.DEVICE_SELECT
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FLASH_MODE

    # ------------------------------------------------------------------ #
    #  5. FIRMWARE SOURCE                                                  #
    # ------------------------------------------------------------------ #
    def menu_firmware_source(self):
        """Firmware source selection"""
        self.clear_screen()
        self.print_header()
        print("Which firmware would you like to flash?")
        print()
        print("  1) Mono Official      (recommended)")
        print("  2) Armbian")
        print("  3) Custom             (local file)")
        print("  4) Back")
        print()

        choice = input("Select [1-4]: ").strip()

        if choice == "1":
            self.firmware_choice = FirmwareChoice.MONO_OFFICIAL
            self.custom_fw_path  = None
            self.current_state   = MenuState.CONFIRM
        elif choice == "2":
            self.firmware_choice = FirmwareChoice.ARMBIAN
            self.custom_fw_path  = None
            self.current_state   = MenuState.CONFIRM
        elif choice == "3":
            self._select_custom_firmware()
        elif choice == "4":
            self.current_state = MenuState.FLASH_MODE
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FIRMWARE_SOURCE

    def _select_custom_firmware(self):
        """Prompt user for local firmware file path"""
        print()
        path = input("  Enter path to firmware file: ").strip()

        if not path:
            print("  No path entered.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FIRMWARE_SOURCE
            return

        from pathlib import Path
        fw_path = Path(path).expanduser()

        if not fw_path.exists():
            print(f"  ❌ File not found: {fw_path}")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FIRMWARE_SOURCE
            return

        if not fw_path.is_file():
            print(f"  ❌ Path is not a file: {fw_path}")
            input("  Press Enter to continue...")
            self.current_state = MenuState.FIRMWARE_SOURCE
            return

        self.firmware_choice = FirmwareChoice.CUSTOM
        self.custom_fw_path  = fw_path
        self.current_state   = MenuState.CONFIRM

    # ------------------------------------------------------------------ #
    #  6. CONFIRM                                                          #
    # ------------------------------------------------------------------ #
    def menu_confirm(self):
        """Confirmation before flashing"""
        self.clear_screen()
        self.print_header()
        print("⚠️  FLASHING WILL ERASE DATA")
        print()
        print(f"  Device:     {self.serial_port or self.network_host}")
        print(f"  Connection: {self.connection_mode.value}")
        print(f"  Target:     {self.flash_mode.value}")
        print(f"  Firmware:   {self.firmware_choice.value}", end="")
        if self.custom_fw_path:
            print(f" ({self.custom_fw_path})")
        else:
            print()
        print()
        print("  This operation cannot be undone. Ensure you have:")
        print("    ✓ Backed up any important data")
        print("    ✓ Device is connected to stable power")
        print("    ✓ Stable serial or Ethernet connection")
        print()
        print("  This tool is well tested, but writing firmware is never")
        print("  without risk. Do not unplug power or disconnect the cable")
        print("  while a flash is in progress — an interrupted write can")
        print("  leave the device unbootable.")
        print()

        choice = input("Proceed? [y/N]: ").strip().lower()

        if choice == 'y':
            self.current_state = MenuState.FLASHING
        else:
            logger.info("Cancelled by user")
            self.current_state = MenuState.MAIN

    # ------------------------------------------------------------------ #
    #  7. FLASHING                                                         #
    # ------------------------------------------------------------------ #
    def menu_flashing(self):
        """Flashing in progress"""
        self.clear_screen()
        self.print_header()
        print("Starting flashing sequence...")
        print()

        try:
            if self.connection_mode == ConnectionMode.SERIAL:
                self.flash_success = self._flash_serial()
            elif self.connection_mode == ConnectionMode.NETWORK:
                self.flash_success = self._flash_network()
            else:
                self.flash_success = False

            self.current_state = MenuState.DONE

        except Exception as e:
            logger.error(f"Flashing failed: {e}")
            self.flash_success = False
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN

    def _flash_serial(self) -> bool:
        """Execute flashing via serial connection. Returns True on success."""
        from mono_imager.serial_device import SerialDevice
        from mono_imager.flasher import (
            Flasher, FirmwareDownloader, FirmwareSource, create_cache_dir
        )
        
        logger.info(f"Flashing via serial: {self.serial_port}")
        logger.info(f"Target: {self.flash_mode.value}  Firmware: {self.firmware_choice.value}")
        
        device = None
        try:
            # Phase 1: Connect and boot recovery
            print("\n  Connecting to device...")
            device = SerialDevice(self.serial_port, timeout=10)
            if not device.connect():
                print("  ❌ Failed to connect to device")
                logger.error("SerialDevice.connect() failed")
                return False
            
            print("  ⚡ POWER CYCLE YOUR DEVICE NOW (waiting for U-Boot autoboot)...")
            if not device.wait_for_autoboot(timeout=60):
                print("  ❌ Failed to interrupt U-Boot autoboot")
                logger.error("wait_for_autoboot() timed out or failed")
                device.disconnect()
                return False
            
            print("  Booting recovery Linux...")
            if not device.boot_recovery():
                print("  ❌ Failed to boot recovery Linux")
                logger.error("boot_recovery() failed")
                device.disconnect()
                return False
            
            print("  Logging into recovery...")
            if not device.login_recovery(timeout=30):
                print("  ❌ Failed to login to recovery Linux")
                logger.error("login_recovery() failed")
                device.disconnect()
                return False
            
            print("  ✓ Device ready (recovery Linux booted and logged in)")
            
            # Phase 2: Acquire firmware
            print("\n  Preparing firmware...")
            cache_dir = create_cache_dir()
            fw_path = None
            
            if self.firmware_choice == FirmwareChoice.CUSTOM:
                fw_path = Path(self.custom_fw_path)
                if not fw_path.exists():
                    print(f"  ❌ Custom firmware file not found: {fw_path}")
                    logger.error(f"Custom firmware path does not exist: {fw_path}")
                    device.disconnect()
                    return False
                print(f"  Using custom firmware: {fw_path.name}")
            else:
                # Download from official source
                fw_source = (
                    FirmwareSource.MONO_OFFICIAL
                    if self.firmware_choice == FirmwareChoice.MONO_OFFICIAL
                    else FirmwareSource.ARMBIAN
                )
                url = fw_source["eMMC"]
                fw_path = cache_dir / Path(url).name
                
                if not fw_path.exists():
                    print(f"  Downloading {fw_source['name']} firmware...")
                    print(f"    ({url})")
                    downloader = FirmwareDownloader(timeout=60)
                    if not downloader.download(url, fw_path):
                        print("  ❌ Firmware download failed")
                        logger.error(f"Download failed for {url}")
                        device.disconnect()
                        return False
                    print(f"  ✓ Downloaded {fw_path.stat().st_size / (1024*1024):.1f} MB")
                else:
                    print(f"  Using cached firmware: {fw_path.name}")
            
            # Phase 3: Flash
            print(f"\n  Flashing {self.flash_mode.value}...")
            print("  (This may take several minutes. Do not power off device.)")
            
            flasher = Flasher(device)
            
            # Detect firmware tool or use manual method
            tool = flasher.detect_firmware_tool()
            if tool == "firmware":
                # Modern 'firmware update' tool
                success = flasher.flash_emmc_modern()
            else:
                # Manual curl | dd method
                mac = flasher.get_device_mac()
                if not mac:
                    mac = "00:00:00:00:00:00"
                    logger.warning(f"Could not detect device MAC; using fallback {mac}")
                success = flasher.flash_emmc_manual(mac)
            
            if not success:
                print("  ❌ Flash operation failed")
                logger.error("Flash operation did not complete successfully")
                device.disconnect()
                return False
            
            print("  ✓ Flash complete")
            
            # Phase 4: Verify boot source
            print("\n  Verifying boot source...")
            if not flasher.verify_boot_source("eMMC"):
                logger.warning("Boot source verification inconclusive")
                # Don't fail here — device may not have booted yet
            
            device.disconnect()
            return True
            
        except Exception as e:
            logger.error(f"Serial flash error: {type(e).__name__}: {e}", exc_info=True)
            print(f"  ❌ Error: {e}")
            if device:
                try:
                    device.disconnect()
                except Exception:
                    pass
            return False

    def _flash_network(self) -> bool:
        """Execute flashing via network connection. Returns True on success."""
        logger.info(f"Flashing via network: {self.network_host}")
        logger.info(f"Target: {self.flash_mode.value}  Firmware: {self.firmware_choice.value}")
        # TODO: Implement network flashing logic
        print("  Network flashing logic not yet implemented.")
        input("  Press Enter to continue...")
        return False

    # ------------------------------------------------------------------ #
    #  8. DONE                                                             #
    # ------------------------------------------------------------------ #
    def menu_done(self):
        """Flash result screen"""
        self.clear_screen()
        self.print_header()

        if self.flash_success:
            print("✅ Flashing complete!")
            print()
            print("  Next steps:")
            print("    1) Device will reboot automatically")
            print("    2) Wait 30-60 seconds for boot")
            print("    3) Access device via serial console or SSH")
        else:
            print("❌ Flashing did not complete successfully.")
            print()
            print("  Check the log output above for details.")
            print("  You can retry from the main menu.")

        print()
        if self.log_file:
            logger.info(f"Result: {'OK' if self.flash_success else 'NOK'}")
            logger.info(f"📄 Report saved to: {self.log_file}")
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
            logger.error(f"Device stats query failed: {e}")
            print(f"  ❌ Error: {e}")

        print()
        input("  Press Enter to return to main menu...")
        self.current_state = MenuState.MAIN

    def _display_device_stats(self, raw_output: str):
        """
        Parse and display U-Boot's boot-time diagnostics: SoC/board
        identity, clock configuration, and the power-on self-test
        block (voltages, temperatures, fan speed, etc).

        Parses ONLY what U-Boot itself prints before autoboot — no OS,
        no /proc, nothing booted. Confirmed against a real capture on
        this hardware (LS1046A / Mono Gateway Development Kit).

        Fields not present in the captured output are simply omitted
        — this does not guess or fill in values that weren't seen.
        """
        identity = parse_uboot_identity(raw_output)
        self_test = parse_uboot_self_test(raw_output)

        if not identity and not self_test:
            print()
            print("  ⚠️  No recognizable U-Boot diagnostic output found.")
            print("  This can happen if the device didn't fully reach the")
            print("  self-test stage, or U-Boot's output format differs")
            print("  on this board/firmware version.")
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
        print("  " + "─" * 56)
        print("  POWER-ON SELF TEST")
        print("  " + "─" * 56)
        if self_test:
            for label, value in self_test:
                line = f"    {label:<22} PASS"
                if value:
                    line += f"  ({value})"
                print(line)
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
                elif self.current_state == MenuState.FLASH_MODE:
                    self.menu_flash_mode()
                elif self.current_state == MenuState.FIRMWARE_SOURCE:
                    self.menu_firmware_source()
                elif self.current_state == MenuState.CONFIRM:
                    self.menu_confirm()
                elif self.current_state == MenuState.FLASHING:
                    self.menu_flashing()
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
    logger.info(f"mono-imager v{__version__} by {__author__}")
    logger.info(f"Log: {log_file}")

    app = MonoImager(log_file)
    app.run()


if __name__ == "__main__":
    main()
