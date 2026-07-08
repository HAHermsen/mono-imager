#!/usr/bin/env python3
"""
mono-imager: Automated firmware flashing for Mono Gateway Routers and Dev Kit
Supports serial and networked connections with menu-driven TUI.

Author:  H.A. Hermsen
Version: v1.2.7
License: GPLv3
"""

from mono_imager import __version__  # single source of truth: mono_imager/__init__.py
__author__ = "H.A. Hermsen"

import sys
import time
import logging
from enum import Enum
from pathlib import Path
from typing import Optional
from mono_imager.spinner import with_spinner
from mono_imager import console
from mono_imager.device_net import RecoveryNetwork
from mono_imager.uboot_parse import parse_uboot_identity, parse_uboot_self_test

logger = logging.getLogger(__name__)

_LOG_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING, "debug": logging.DEBUG}

def verbose(msg: str, level: str = "info"):
    """Print to console immediately AND log it"""
    print(msg, flush=True)
    logger.log(_LOG_LEVELS.get(level, logging.INFO), msg)


def _netmask_to_prefix(value: str) -> Optional[str]:
    # Moved to mono_imager.device_net.netmask_to_prefix; kept as a
    # re-export so any external caller importing it from here still works.
    from mono_imager.device_net import netmask_to_prefix
    return netmask_to_prefix(value)






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
        # Device's own recovery-shell network (DHCP or manual). The
        # session cache (config + verified) lives in RecoveryNetwork;
        # MonoImager exposes it via the device_net / device_net_verified
        # properties below so existing readers stay unchanged.
        self._net = RecoveryNetwork()

    @property
    def device_net(self):
        return self._net.config

    @device_net.setter
    def device_net(self, value):
        self._net.config = value

    @property
    def device_net_verified(self):
        return self._net.verified

    @device_net_verified.setter
    def device_net_verified(self, value):
        self._net.verified = value

    def clear_screen(self):
        console.clear_screen()

    def safe_input(self, prompt: str) -> Optional[str]:
        """
        Wrapper around console.read_line() that owns the escape-to-menu
        state transition. Rendering/reading lives in console; the
        MenuState side-effect stays here where menu state belongs.
        """
        value = console.read_line(prompt)
        if value is console.ESCAPE:
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
            return None
        return value

    def print_header(self):
        console.print_header(__version__, __author__, self.device_net)

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
        Delegate rendering to console; map its ESCAPE sentinel to the
        None-plus-state-transition contract the callers here expect.
        """
        result = console.show_flash_confirmation(
            os_name=os_name, port=port, firmware_path=firmware_path,
            flash_target=flash_target, host_ip=host_ip, device_ip=device_ip,
        )
        if result is console.ESCAPE:
            self.current_state = MenuState.FLASH_AUTO_OR_MANUAL
            return None
        return result

    def _recovery_finish(self, success: bool) -> None:
        """Set flash result, wait for Enter, and return to main menu."""
        self.flash_success = success
        # Drain buffered stdin (stray newlines from the serial session /
        # the piped 'yes' the flash uses) so this Enter actually blocks.
        # Without it a leftover '\n' satisfies input() instantly and the
        # report is wiped by the next clear_screen() before it can be read.
        try:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
        except ImportError:
            try:
                import termios
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
            except Exception:
                pass
        input("  Press Enter to continue...")
        self.current_state = MenuState.MAIN

    def _show_firmware_output(self, chunk: str) -> None:
        console.show_firmware_output(chunk)

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
            print("  Recommended: 16 GB minimum (holds all three OS images simultaneously).")
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
        Run the actual flash journey. Bootstrap, U-Boot steps, recovery
        boot, network setup, and journey.run() all live in
        flash_orchestrator.run_flash_journey() now — this method only
        calls it and updates flash_success/current_state, restoring
        the get_journey()+.run() contract JOURNEYS.md documents for
        tui.py.

        flash_success is only touched when run_flash_journey() returns
        non-None (i.e. it got far enough to actually call
        print_report()) — matching the original's behavior of leaving
        flash_success at its previous value on an early bootstrap/
        U-Boot-steps/network-setup failure.
        """
        # DON'T clear screen here — keep all output visible for debugging

        from mono_imager import flash_orchestrator as core

        result = core.run_flash_journey(
            self.serial_port,
            self.os_name,
            getattr(self, "transfer_method", "lan"),
            self.net_host_ip,
            self.net_http_port,
            self.custom_fw_path,
            lambda: self.device_net,
            self._setup_recovery_network,
        )
        if result is not None:
            self.flash_success = result
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
        Resolve+verify the recovery shell's network. DHCP-first,
        real-reachability-verified, manual fallback; caches on the
        session RecoveryNetwork and re-applies (not re-prompts) on
        each fresh recovery boot. See mono_imager.device_net for the
        full rationale and the hardware bugs this guards against.
        """
        return self._net.resolve(d)

    def menu_update_emmc(self):
        """
        Flash eMMC firmware only. Device must be in NOR recovery (DIP RIGHT).

        Bootstrap, modern/legacy detection, flashing, and legacy
        fallback all live in recovery_orchestrator.run_emmc_update()
        now (it used to be duplicated almost verbatim here and in
        menu_update_nor()). This method only owns the menu chrome
        (instructions, port selection, confirmation) and the
        MenuState/flash_success bookkeeping via _recovery_finish().
        """
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

        rec.run_emmc_update(
            port, self._soft_reboot_if_possible, self._setup_recovery_network,
            on_output=self._show_firmware_output,
        )

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
        Requires eMMC to already have the official Mono firmware with recovery.

        Bootstrap, modern/legacy detection, flashing, and legacy
        fallback all live in recovery_orchestrator.run_nor_update()
        now. This method only owns the menu chrome and MenuState/
        flash_success bookkeeping. No DIP-flip-back or boot
        verification is prompted after flashing — the tool leaves the
        device as-is once the write is confirmed.
        """
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

        rec.run_nor_update(
            port, self._soft_reboot_if_possible, self._setup_recovery_network,
            on_output=self._show_firmware_output,
        )

        self._recovery_finish(rec.print_report())

    # ------------------------------------------------------------------ #
    #  TEST SERIAL — option 4 from main menu                             #
    # ------------------------------------------------------------------ #
    def menu_test_serial(self):
        """
        Run the serial connection test. Hardware interaction, live
        progress, and the pass/fail summary all live in
        diagnostics.test_serial() now; this method only resolves the
        port (a menu-navigation decision) and drives MenuState.

        If self.serial_port is already set (user connected earlier in
        this session), it is used automatically. Otherwise the user is
        prompted to pick a port first.
        """
        from mono_imager import diagnostics

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

        if diagnostics.test_serial(port):
            self.serial_port = port

        self.current_state = MenuState.MAIN


    # ------------------------------------------------------------------ #
    #  TEST LAN — option 5 from main menu                                #
    # ------------------------------------------------------------------ #
    def menu_test_lan(self):
        """
        Full end-to-end LAN test — boots device into recovery, sets up
        networking, and confirms the device can reach the host HTTP
        server. Hardware interaction, live progress, and the summary
        all live in diagnostics.test_lan(); this method only resolves
        the port (menu-navigation), passes through the session-scoped
        callables (soft reboot, device-network resolve/read — both
        owned by MonoImager so every caller shares the same cache),
        and persists whatever diagnostics.test_lan() says is safe to
        keep.
        """
        from mono_imager import diagnostics

        self.clear_screen()
        self.print_header()

        port = self.serial_port
        if not port:
            port = self._select_port(auto_select_single=True, allow_back=False)
            if port is None:
                self.current_state = MenuState.MAIN
                return

        saved = diagnostics.test_lan(
            port,
            self.net_host_ip,
            self._soft_reboot_if_possible,
            self._setup_recovery_network,
            lambda: self.device_net,
        )
        if saved:
            self.serial_port   = saved["serial_port"]
            self.net_host_ip   = saved["host_ip"]
            self.net_device_ip = saved["device_ip"]

        self.current_state = MenuState.MAIN


    # ------------------------------------------------------------------ #
    #  TEST USB MOUNT — option 7 from main menu                          #
    # ------------------------------------------------------------------ #
    def menu_test_usb_mount(self):
        """
        Verify a USB stick is connected, mountable, and (optionally)
        already staged with recognizable OS images — before starting a
        real USB flash journey. Hardware interaction, live progress, and
        the summary all live in diagnostics.test_usb(); this method
        only resolves the port (menu-navigation) and persists it once
        the mount is proven.
        """
        from mono_imager import diagnostics

        self.clear_screen()
        self.print_header()

        port = self.serial_port
        if not port:
            port = self._select_port(auto_select_single=True, allow_back=False)
            if port is None:
                self.current_state = MenuState.MAIN
                return

        if diagnostics.test_usb(port):
            self.serial_port = port

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
            import tty
            import termios
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
        """Parse U-Boot boot-time diagnostics (domain), render via console."""
        console.show_device_stats(
            parse_uboot_identity(raw_output),
            parse_uboot_self_test(raw_output),
        )



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

        # Detection only lists the port; it does not confirm we can OPEN
        # it. On Linux the tty usually needs sudo / dialout-group access,
        # so opening can fail with PermissionError. No usable tty -> no
        # point going to the main menu; every option needs the device.
        import serial
        try:
            _probe = serial.Serial(port)
            _probe.close()
        except (serial.SerialException, OSError):
            print()
            print("  tty device could not be opened, did you run this script with sudo?")
            sys.exit(1)

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
