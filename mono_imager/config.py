"""
mono-imager: Configuration manager
Persists user preferences (last used port, etc.) across sessions.

Author:  H.A. Hermsen
Version: v1.0.0
License: GPLv3
"""

from mono_imager import __version__  # single source of truth: mono_imager/__init__.py
__author__ = "H.A. Hermsen"

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Known USB-UART chip descriptions to prioritize in port listing
KNOWN_USB_UART_DESCRIPTORS = [
    "cp210",   # Silicon Labs CP210x (very common)
    "ch340",   # WCH CH340 (cheap adapters)
    "ch341",   # WCH CH341
    "ftdi",    # FTDI FT232
    "ft232",   # FTDI FT232
    "prolific", # Prolific PL2303
    "pl2303",
    "cdc",     # Generic CDC ACM
    "uart",
    "serial",
    "usb serial",
]


def get_config_path() -> Path:
    """Return path to config file"""
    return Path.home() / ".config" / "mono-imager" / "config.json"


def load_config() -> dict:
    """Load config from disk, return empty dict if not found"""
    config_path = get_config_path()
    try:
        if config_path.exists():
            return json.loads(config_path.read_text())
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Config file is corrupt or unreadable — resetting to defaults: {e}")
    return {}


def save_config(config: dict):
    """Save config to disk"""
    config_path = get_config_path()
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2))
        logger.debug(f"Config saved to {config_path}")
    except OSError as e:
        logger.warning(f"Could not save config to {config_path}: {e}")


def save_last_port(port: str):
    """Remember last used serial port"""
    config = load_config()
    config["last_port"] = port
    save_config(config)


def get_last_port() -> Optional[str]:
    """Get last used serial port, or None"""
    return load_config().get("last_port")


def is_known_uart(description: str) -> bool:
    """Check if port description matches a known USB-UART chip"""
    desc_lower = description.lower()
    return any(keyword in desc_lower for keyword in KNOWN_USB_UART_DESCRIPTORS)


def detect_serial_ports() -> tuple[list, list]:
    """
    Detect available serial ports, split into known USB-UART and other ports.
    
    Returns:
        (known_ports, other_ports) — both lists of serial.tools.list_ports.ListPortInfo
    """
    try:
        import serial.tools.list_ports
        all_ports = list(serial.tools.list_ports.comports())
        
        known, other = [], []
        for p in all_ports:
            (known if is_known_uart(p.description or "") else other).append(p)
        
        return known, other
        
    except ImportError as e:
        raise RuntimeError(
            "pyserial is not installed — cannot detect serial ports. "
            "Install it with: pip install pyserial"
        ) from e
    except PermissionError as e:
        raise RuntimeError(
            f"Permission denied accessing serial ports: {e}\n"
            "On Linux/macOS add your user to the 'dialout' group:\n"
            "  sudo usermod -a -G dialout $USER  (then log out and back in)\n"
            "On Windows ensure no other application has the port open."
        ) from e