#!/usr/bin/env python3
"""
mono-imager: Unit tests for config.py — persisted user preferences.

No hardware required. get_config_path() is monkeypatched to a temp file
so tests never touch the real ~/.config/mono-imager/config.json.

What this tests:
  - load_config()/save_config()  — roundtrip, missing file -> {}, corrupt
                                    JSON -> {} (not a crash), OSError on
                                    write is swallowed
  - save_last_port()/get_last_port() — roundtrip, default None
  - is_known_uart()              — matches known USB-UART chip descriptors,
                                    case-insensitively; no false match on
                                    unrelated text

Run: python tests/unit/test_config.py
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import config

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {label}")
        passed += 1
    else:
        print(f"  FAIL: {label}")
        failed += 1


# ============================================================================
# load_config() / save_config() — roundtrip against a real temp file
# ============================================================================

print("=" * 60)
print("load_config() / save_config()")
print("=" * 60)

with tempfile.TemporaryDirectory() as tmp:
    cfg_path = Path(tmp) / "nested" / "config.json"  # parent dir doesn't exist yet

    with patch("mono_imager.config.get_config_path", return_value=cfg_path):
        check("missing file -> empty dict", config.load_config() == {})

        config.save_config({"last_port": "COM5", "nested": {"a": 1}})
        check("save_config() creates parent directories", cfg_path.exists())
        check("save_config()/load_config() roundtrip preserves data",
              config.load_config() == {"last_port": "COM5", "nested": {"a": 1}})

        config.save_config({"overwritten": True})
        check("save_config() overwrites rather than merges",
              config.load_config() == {"overwritten": True})

# ============================================================================
# load_config() — corrupt JSON resets to {} instead of raising
# ============================================================================

print()
print("=" * 60)
print("load_config(): corrupt file handling")
print("=" * 60)

with tempfile.TemporaryDirectory() as tmp:
    cfg_path = Path(tmp) / "config.json"
    cfg_path.write_text("{not valid json!!")

    with patch("mono_imager.config.get_config_path", return_value=cfg_path):
        result = config.load_config()
    check("corrupt JSON -> {} instead of raising", result == {})

# ============================================================================
# save_config() — OSError on write is swallowed, not raised
# ============================================================================

print()
print("=" * 60)
print("save_config(): OSError handling")
print("=" * 60)


class _UnwritablePath:
    """Fake Path whose write_text() always raises, mimicking a
    permission-denied or read-only-filesystem failure."""
    parent = type("P", (), {"mkdir": lambda self, **kw: None})()

    def write_text(self, *_a, **_kw):
        raise OSError("Permission denied")


with patch("mono_imager.config.get_config_path", return_value=_UnwritablePath()):
    try:
        config.save_config({"a": 1})
        check("OSError during save_config() is swallowed, not raised", True)
    except OSError:
        check("OSError during save_config() is swallowed, not raised", False)


# ============================================================================
# save_last_port() / get_last_port()
# ============================================================================

print()
print("=" * 60)
print("save_last_port() / get_last_port()")
print("=" * 60)

with tempfile.TemporaryDirectory() as tmp:
    cfg_path = Path(tmp) / "config.json"
    with patch("mono_imager.config.get_config_path", return_value=cfg_path):
        check("no port saved yet -> None", config.get_last_port() is None)

        config.save_last_port("COM7")
        check("get_last_port() returns what was just saved", config.get_last_port() == "COM7")

        config.save_config({"other_key": "kept"})
        config.save_last_port("COM8")
        check("save_last_port() preserves other existing keys",
              config.load_config() == {"other_key": "kept", "last_port": "COM8"})


# ============================================================================
# is_known_uart()
# ============================================================================

print()
print("=" * 60)
print("is_known_uart()")
print("=" * 60)

check("CP2102 description matches (cp210 keyword)",
      config.is_known_uart("Silicon Labs CP2102 USB to UART Bridge Controller") is True)
check("CH340 description matches", config.is_known_uart("USB-SERIAL CH340") is True)
check("FTDI description matches", config.is_known_uart("FTDI FT232R USB UART") is True)
check("case-insensitive match", config.is_known_uart("SILICON LABS CP210X") is True)
check("unrelated description does not match", config.is_known_uart("Bluetooth Peripheral Device") is False)
check("empty description does not match", config.is_known_uart("") is False)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
