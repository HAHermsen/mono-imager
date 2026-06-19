#!/usr/bin/env python3
"""
mono-imager: Automated firmware flashing for Mono Gateway Routers and Dev Kit
Supports serial and networked connections with menu-driven TUI.

Author:  H.A. Hermsen
Version: 0.3.0
License: MIT
"""

__version__ = "0.3.0"
__author__ = "H.A. Hermsen"

import sys
import os
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


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
    MAIN            = "main"
    CONNECTION      = "connection"
    DEVICE_SELECT   = "device_select"
    FLASH_MODE      = "flash_mode"
    FIRMWARE_SOURCE = "firmware_source"
    CONFIRM         = "confirm"
    FLASHING        = "flashing"
    DONE            = "done"


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

    def clear_screen(self):
        """Clear terminal"""
        os.system('clear' if os.name == 'posix' else 'cls')

    def print_header(self):
        """Print application header"""
        print("╔════════════════════════════════════════════╗")
        print(f"║         mono-imager {__version__:<23}║")
        print("║  Mono Gateway Automated Firmware Flasher   ║")
        print(f"║  by {__author__:<39}║")
        print("╚════════════════════════════════════════════╝")
        print()

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
        print("  3) Exit")
        print()

        choice = input("Select [1-3]: ").strip()

        if choice == "1":
            self.current_state = MenuState.CONNECTION
        elif choice == "2":
            print()
            print("  Recovery mode not yet implemented.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN
        elif choice == "3":
            sys.exit(0)
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.MAIN

    # ------------------------------------------------------------------ #
    #  2. CONNECTION MODE                                                  #
    # ------------------------------------------------------------------ #
    def menu_connection(self):
        """Connection mode selection"""
        self.clear_screen()
        self.print_header()
        print("How is your device connected?")
        print()
        print("  1) Serial (USB/UART cable)")
        print("  2) Network (Ethernet)")
        print("  3) Back")
        print()

        choice = input("Select [1-3]: ").strip()

        if choice == "1":
            self.connection_mode = ConnectionMode.SERIAL
            self.current_state   = MenuState.DEVICE_SELECT
        elif choice == "2":
            self.connection_mode = ConnectionMode.NETWORK
            self.current_state   = MenuState.DEVICE_SELECT
        elif choice == "3":
            self.current_state = MenuState.MAIN
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.CONNECTION

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
            self.current_state = MenuState.CONNECTION
            return

        all_ports = known_ports + other_ports

        if not all_ports:
            print("  ❌ No serial devices found.")
            print()
            print("  Please ensure your USB/UART cable is connected.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.CONNECTION
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
            self.current_state = MenuState.FLASH_MODE
            return

        try:
            idx = int(choice) - 1
            if idx == len(all_ports):
                self.current_state = MenuState.CONNECTION
                return
            if 0 <= idx < len(all_ports):
                self.serial_port = all_ports[idx].device
                save_last_port(self.serial_port)
                logger.info(f"Selected serial port: {self.serial_port}")
                self.current_state = MenuState.FLASH_MODE
            else:
                print("  Invalid selection.")
                input("  Press Enter to continue...")
                self.current_state = MenuState.DEVICE_SELECT
        except ValueError:
            print("  Invalid input.")
            input("  Press Enter to continue...")
            self.current_state = MenuState.DEVICE_SELECT

    def _detect_network_devices(self):
        """Network host entry — stub"""
        print("  Network device detection not yet implemented.")
        print()
        input("  Press Enter to continue...")
        self.current_state = MenuState.CONNECTION

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
        print("    ✓ Stable cable or Ethernet connection")
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
        logger.info(f"Flashing via serial: {self.serial_port}")
        logger.info(f"Target: {self.flash_mode.value}  Firmware: {self.firmware_choice.value}")
        # TODO: Implement serial flashing logic
        print("  Serial flashing logic not yet implemented.")
        input("  Press Enter to continue...")
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
    #  MAIN LOOP                                                           #
    # ------------------------------------------------------------------ #
    def run(self):
        """Main event loop"""
        try:
            while True:
                if self.current_state == MenuState.MAIN:
                    self.menu_main()
                elif self.current_state == MenuState.CONNECTION:
                    self.menu_connection()
                elif self.current_state == MenuState.DEVICE_SELECT:
                    self.menu_device_select()
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
