"""mono-imager: Automated firmware flashing for Mono Gateway"""

# Single source of truth for package version.
# All other modules import from here rather than defining their own __version__.
# Pyproject.toml reads this via:  dynamic = ["version"]  +  [tool.setuptools.dynamic]
__version__ = "1.0.0"
__author__  = "H.A. Hermsen"
__license__ = "MIT"

from .serial_device import SerialDevice
from .tui import MonoImager

__all__ = ["SerialDevice", "MonoImager"]
