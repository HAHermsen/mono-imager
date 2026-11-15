#!/usr/bin/env python3
"""
mono-imager: Automated firmware flashing for Mono Gateway Routers and Dev Kit
Supports serial and networked connections with menu-driven TUI.

Author:  H.A. Hermsen
License: GPLv3
"""

from mono_imager import __version__  # single source of truth: mono_imager/__init__.py

__author__ = "H.A. Hermsen"

import logging
import sys
import time  # noqa: F401  (read via T.time by the mixins; kept for patch points)
from enum import Enum
from pathlib import Path

from mono_imager import console
from mono_imager import constants as C
from mono_imager.device_net import RecoveryNetwork
from mono_imager.spinner import with_spinner  # noqa: F401  (tests patch T.with_spinner)
from mono_imager.uboot_parse import (  # noqa: F401  (patch points)
    parse_uboot_identity,
    parse_uboot_self_test,
)

# --- Tuning (seconds) ---------------------------------------------------------
SOFT_REBOOT_READ_TIMEOUT = 2         # best-effort `reboot` over serial
SOFT_REBOOT_SETTLE = 0.5
CONSOLE_READ_TIMEOUT = 0.1           # raw serial console: short reads keep it responsive
CONSOLE_DRAIN_SETTLE = 0.2
CONSOLE_PROMPT_SETTLE = 0.1
STATS_READ_TIMEOUT = 10
STATS_CAPTURE_TIMEOUT = 60
CONSOLE_EXIT_KEY = "\x1d"            # Ctrl+]

logger = logging.getLogger(__name__)

_LOG_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING, "debug": logging.DEBUG}

def verbose(msg: str, level: str = "info"):
    """Print to console immediately AND log it"""
    print(msg, flush=True)
    logger.log(_LOG_LEVELS.get(level, logging.INFO), msg)


def _netmask_to_prefix(value: str) -> str | None:
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
    SNAPSHOT              = "snapshot"
    RESTORE               = "restore"


from mono_imager.tui_console import TuiConsoleMixin
from mono_imager.tui_flash import TuiFlashMixin
from mono_imager.tui_menus import TuiMenusMixin
from mono_imager.tui_snapshot import TuiSnapshotMixin


class MonoImager(TuiFlashMixin, TuiMenusMixin, TuiSnapshotMixin, TuiConsoleMixin):
    """Main application controller"""

    def __init__(self, log_file: Path | None = None):
        self.current_state   = MenuState.MAIN
        self.device          = None
        self.custom_fw_path  = None
        self.serial_port: str | None = None
        self.flash_success   = False
        self.log_file        = log_file
        self.transfer_method = None
        self.net_host_ip     = None
        self.net_device_ip   = None
        self.net_http_port   = C.HTTP_PORT
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

    def safe_input(self, prompt: str) -> str | None:
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

    def print_screen_divider(self):
        """
        Scrolling-transcript equivalent of clear_screen()+print_header()
        (branch poc/continous_cli_menu): appends a divider + a one-line
        status recap instead of wiping the screen, so prior output —
        including earlier steps' results — stays visible and scrollable.
        Every menu screen uses this now; see menu_main()'s original PoC
        docstring in git history for the rationale. menu_network_flashing()
        and menu_done() already didn't clear the screen even before this
        (deliberately, for debugging visibility) and are unaffected.
        """
        print()
        print("-" * C.DIVIDER_WIDTH)
        console.print_status_line(__version__, self.device_net)

    # Minimum plausible firmware size. This is a sanity check against
    # obviously wrong paths (a README, a 0-byte placeholder, a text
    # file typo) — NOT a content/format check. mono-imager deliberately
    # never inspects what's inside the file (dumb-pipe by design); this
    # only catches "that can't possibly be a real image" before
    # spending several minutes of a flash attempt on it.
    MIN_FIRMWARE_SIZE_BYTES = 1 * C.MIB


    # All imports inside methods are intentional — deferred to keep
    # startup fast and avoid circular import issues at module load time.


    # ------------------------------------------------------------------ #
    #  1. MAIN MENU                                                        #
    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    #  NETWORK FLASH CONFIG — collects the values flash_orchestrator.py  #
    #  actually needs: just a firmware file path and a flash target      #
    #  string directly. (The eMMC/NOR/Dual + Mono Official/Armbian/      #
    #  Custom menus that used to feed the old Flasher-class API have     #
    #  been removed — they were unreachable and flash_orchestrator.py    #
    #  never used them anyway.)                                         #
    # ------------------------------------------------------------------ #



    # ------------------------------------------------------------------ #
    #  UPDATE eMMC / NOR — split FW update journeys.                    #
    #  eMMC journey: DIP RIGHT (NOR), flashes eMMC via firmware update. #
    #  NOR journey:  DIP LEFT (eMMC), flashes NOR via firmware update.  #
    #  Both detect modern vs. legacy automatically and fall back to      #
    #  curl+dd / curl+flashcp when the modern tool is unavailable.       #
    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    #  TEST SERIAL — option 5 from main menu                             #
    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    #  TEST LAN — option 6 from main menu                                #
    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    #  SNAPSHOT / RESTORE eMMC — options 9 and 10 (#29). Menu chrome     #
    #  only; the journeys live in snapshot_orchestrator.py.              #
    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    #  TEST USB MOUNT — option 7 from main menu                          #
    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    #  8. DONE                                                             #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  CLI CONSOLE                                                         #
    # ------------------------------------------------------------------ #
        


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

        self.print_screen_divider()
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

        # Remember the launch choice for this session, so later menus
        # (Fully Auto, tests) use it instead of re-picking (#27).
        self.serial_port = port

        d = core.phase1_bootstrap(port, C.SERIAL_BAUD)
        if d is None:
            print()
            print("  Could not reach a recovery shell — network settings will be resolved")
            print("  on the first eMMC/NOR update or firmware flash instead.")
            input("  Press Enter to continue...")
            return

        try:
            if not self._setup_recovery_network(d):
                print()
                print("  ⚠ Network setup was not completed. You can still use 'CLI only")
                print("    (serial)' (option 4) from the main menu, or flash an OS image")
                print("    over USB, without the device's own network being set up.")
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
                elif self.current_state == MenuState.SNAPSHOT:
                    self.menu_snapshot()
                elif self.current_state == MenuState.RESTORE:
                    self.menu_restore()
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
