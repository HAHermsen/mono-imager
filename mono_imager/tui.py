#!/usr/bin/env python3
"""
mono-imager: Automated firmware flashing for Mono Gateway Routers and Dev Kit
Supports serial and networked connections with menu-driven TUI.

Author:  H.A. Hermsen
Version: 0.1.0
License: MIT
"""

__version__ = "0.1.0"
__author__ = "H.A. Hermsen"

import sys
import os
import logging
from enum import Enum

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


class FlashMode(Enum):
    """Flash target selection"""
    EMMC = "eMMC"
    NOR = "NOR"
    DUAL = "Dual (NOR → eMMC → NOR)"


class ConnectionMode(Enum):
    """Connection type to device"""
    SERIAL = "Serial (USB/UART)"
    NETWORK = "Network (SSH/Telnet)"


class MenuState(Enum):
    """Main menu states"""
    MAIN = "main"
    CONNECTION = "connection"
    DEVICE_SELECT = "device_select"
    FLASH_MODE = "flash_mode"
    CONFIRM = "confirm"
    FLASHING = "flashing"
    DONE = "done"


class MonoImager:
    """Main application controller"""
    
    def __init__(self):
        self.current_state = MenuState.MAIN
        self.connection_mode = None
        self.device = None
        self.flash_mode = None
        self.serial_port = None
        self.network_host = None
        
    def clear_screen(self):
        """Clear terminal"""
        os.system('clear' if os.name == 'posix' else 'cls')
    
    def print_header(self):
        """Print application header"""
        print("╔════════════════════════════════════════════╗")
        print("║         mono-imager v0.1.0                 ║")
        print("║  Mono Gateway Automated Firmware Flasher   ║")
        print("║  by H.A. Hermsen                           ║")
        print("╚════════════════════════════════════════════╝")
        print()
    
    def menu_main(self):
        """Main menu"""
        self.clear_screen()
        self.print_header()
        print("What would you like to do?")
        print()
        print("  1) Flash Armbian to Mono Gateway")
        print("  2) Flash custom firmware")
        print("  3) Recover bricked device")
        print("  4) Exit")
        print()
        
        choice = input("Select [1-4]: ").strip()
        
        if choice == "1":
            self.current_state = MenuState.CONNECTION
        elif choice == "2":
            logger.info("Custom firmware flashing not yet implemented")
            input("Press Enter to continue...")
            self.current_state = MenuState.MAIN
        elif choice == "3":
            logger.info("Recovery mode not yet implemented")
            input("Press Enter to continue...")
            self.current_state = MenuState.MAIN
        elif choice == "4":
            sys.exit(0)
        else:
            logger.warning("Invalid selection")
            input("Press Enter to continue...")
            self.current_state = MenuState.MAIN
    
    def menu_connection(self):
        """Connection mode selection"""
        self.clear_screen()
        self.print_header()
        print("How is your device connected?")
        print()
        print("  1) Serial (USB/UART cable)")
        print("  2) Network (via Ethernet)")
        print("  3) Back")
        print()
        
        choice = input("Select [1-3]: ").strip()
        
        if choice == "1":
            self.connection_mode = ConnectionMode.SERIAL
            self.current_state = MenuState.DEVICE_SELECT
        elif choice == "2":
            self.connection_mode = ConnectionMode.NETWORK
            self.current_state = MenuState.DEVICE_SELECT
        elif choice == "3":
            self.current_state = MenuState.MAIN
        else:
            logger.warning("Invalid selection")
            input("Press Enter to continue...")
            self.current_state = MenuState.CONNECTION
    
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
        
        known_ports, other_ports = detect_serial_ports()
        all_ports = known_ports + other_ports
        
        if not all_ports:
            print("❌ No serial devices found")
            print()
            print("Please ensure your USB/UART cable is connected")
            input("Press Enter to continue...")
            self.current_state = MenuState.CONNECTION
            return
        
        last_port = get_last_port()
        
        # Show known USB-UART ports first
        if known_ports:
            print("  USB-UART adapters (recommended):")
            for i, port in enumerate(known_ports, 1):
                marker = " ◄ last used" if port.device == last_port else ""
                print(f"  {i}) {port.device} — {port.description}{marker}")
        
        # Show other ports below
        if other_ports:
            print()
            print("  Other ports:")
            offset = len(known_ports)
            for i, port in enumerate(other_ports, offset + 1):
                marker = " ◄ last used" if port.device == last_port else ""
                print(f"  {i}) {port.device} — {port.description}{marker}")
        
        print()
        print(f"  {len(all_ports) + 1}) Back")
        print()
        
        # Pre-select last used port if available
        if last_port:
            devices = [p.device for p in all_ports]
            if last_port in devices:
                print(f"  [Enter] Use last port ({last_port})")
                print()
        
        choice = input(f"Select [1-{len(all_ports) + 1}]: ").strip()
        
        # Handle Enter = use last port
        if choice == "" and last_port:
            devices = [p.device for p in all_ports]
            if last_port in devices:
                self.serial_port = last_port
                logger.info(f"Using last port: {self.serial_port}")
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
                logger.warning("Invalid selection")
                input("Press Enter to continue...")
                self.current_state = MenuState.DEVICE_SELECT
        except ValueError:
            logger.warning("Invalid input")
            input("Press Enter to continue...")
            self.current_state = MenuState.DEVICE_SELECT
    
    def _detect_network_devices(self):
        """Detect devices on network"""
        print("Network device detection not yet implemented")
        print()
        input("Press Enter to continue...")
        self.current_state = MenuState.CONNECTION
    
    def menu_flash_mode(self):
        """Flash target selection"""
        self.clear_screen()
        self.print_header()
        print("What would you like to flash?")
        print()
        print("  1) eMMC only (safe, single step)")
        print("  2) NOR only (advanced)")
        print("  3) Dual flash (NOR → eMMC → NOR, recommended)")
        print("  4) Back")
        print()
        
        choice = input("Select [1-4]: ").strip()
        
        if choice == "1":
            self.flash_mode = FlashMode.EMMC
            self.current_state = MenuState.CONFIRM
        elif choice == "2":
            self.flash_mode = FlashMode.NOR
            self.current_state = MenuState.CONFIRM
        elif choice == "3":
            self.flash_mode = FlashMode.DUAL
            self.current_state = MenuState.CONFIRM
        elif choice == "4":
            self.current_state = MenuState.DEVICE_SELECT
        else:
            logger.warning("Invalid selection")
            input("Press Enter to continue...")
            self.current_state = MenuState.FLASH_MODE
    
    def menu_confirm(self):
        """Confirmation before flashing"""
        self.clear_screen()
        self.print_header()
        print("⚠️  FLASHING WILL ERASE DATA")
        print()
        print(f"Device:     {self.serial_port or self.network_host}")
        print(f"Connection: {self.connection_mode.value}")
        print(f"Flash Mode: {self.flash_mode.value}")
        print()
        print("This operation cannot be undone. Ensure you have:")
        print("  ✓ Backed up any important data")
        print("  ✓ Device is fully charged or connected to power")
        print("  ✓ Stable connection (no WiFi, use Ethernet)")
        print()
        
        choice = input("Proceed? [y/N]: ").strip().lower()
        
        if choice == 'y':
            self.current_state = MenuState.FLASHING
        else:
            logger.info("Cancelled by user")
            self.current_state = MenuState.MAIN
    
    def menu_flashing(self):
        """Flashing in progress"""
        self.clear_screen()
        self.print_header()
        print("Starting flashing sequence...")
        print()
        
        try:
            if self.connection_mode == ConnectionMode.SERIAL:
                self._flash_serial()
            elif self.connection_mode == ConnectionMode.NETWORK:
                self._flash_network()
            
            self.current_state = MenuState.DONE
        except Exception as e:
            logger.error(f"Flashing failed: {e}")
            input("Press Enter to continue...")
            self.current_state = MenuState.MAIN
    
    def _flash_serial(self):
        """Execute flashing via serial connection"""
        logger.info(f"Flashing via serial: {self.serial_port}")
        logger.info(f"Flash mode: {self.flash_mode.value}")
        # TODO: Implement serial flashing logic
        print("Serial flashing logic not yet implemented")
        input("Press Enter to continue...")
    
    def _flash_network(self):
        """Execute flashing via network connection"""
        logger.info(f"Flashing via network: {self.network_host}")
        logger.info(f"Flash mode: {self.flash_mode.value}")
        # TODO: Implement network flashing logic
        print("Network flashing logic not yet implemented")
        input("Press Enter to continue...")
    
    def menu_done(self):
        """Flashing complete"""
        self.clear_screen()
        self.print_header()
        print("✅ Flashing complete!")
        print()
        print("Next steps:")
        print("  1) Device will reboot automatically")
        print("  2) Wait 30-60 seconds for boot")
        print("  3) Access device via serial console or SSH")
        print()
        
        choice = input("Press Enter to return to main menu...")
        self.current_state = MenuState.MAIN
    
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
    app = MonoImager()
    app.run()


if __name__ == "__main__":
    main()
