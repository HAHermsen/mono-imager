#!/usr/bin/env python3
"""
mono-imager: Unit tests for issue #27 — Fully Auto must use the serial
port chosen at launch, not the first port in the list.

No hardware required. Port detection and the device are mocked.

What this tests:
  - _startup_network_setup() stores the chosen port in self.serial_port
  - menu_network_auto_config() uses self.serial_port when it is set,
    even when another adapter is listed first (the reporter's CH340/FTDI)
  - with no launch choice, it falls back to _select_port() (which asks
    when several ports exist) instead of taking all_ports[0]
  - Back from that picker returns to the Flash menu
  - Phase 1 step 1 says "Port X present", not "Device detected"

Run: python tests/unit/test_serial_port_choice.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager.tui import MonoImager, MenuState

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


def make_app():
    app = MonoImager()
    app.clear_screen = lambda: None
    app.print_screen_divider = lambda: None
    return app


# The reporter's setup: KVM's CH340 listed first, the Mono's FTDI second.
CH340 = SimpleNamespace(device="COM4", description="USB-SERIAL CH340 (COM4)")
FTDI  = SimpleNamespace(device="COM5", description="USB Serial Port (COM5)")


def run_fully_auto(app, select_port_result=None):
    """
    Drive menu_network_auto_config() up to the confirmation screen:
    pick journey 1 via USB-free answers, accept the confirmation.
    Returns (captured _show_flash_confirmation kwargs, _select_port mock).
    """
    captured = {}

    def fake_confirm(**kw):
        captured.update(kw)
        return True

    journeys = [("OpenWRT", "usb")]
    with patch("mono_imager.config.detect_serial_ports", return_value=([CH340, FTDI], [])), \
         patch("mono_imager.journeys.discovered_journeys", return_value=journeys), \
         patch("mono_imager.step_registry.list_journey", return_value=[]), \
         patch("mono_imager.flash_orchestrator.detect_host_ip", return_value="10.0.0.2"), \
         patch.object(app, "safe_input", return_value="1"), \
         patch.object(app, "_select_port", return_value=select_port_result) as mock_select, \
         patch.object(app, "_show_flash_confirmation", side_effect=fake_confirm), \
         patch("builtins.input", return_value=""), \
         patch("builtins.print"):
        app.menu_network_auto_config()
    return captured, mock_select


# ============================================================================
# _startup_network_setup() remembers the chosen port
# ============================================================================

print("=" * 60)
print("_startup_network_setup(): chosen port is stored in self.serial_port")
print("=" * 60)

app = make_app()
with patch.object(app, "_select_port", return_value="COM5"), \
     patch("serial.Serial", return_value=MagicMock()), \
     patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=None), \
     patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    app._startup_network_setup()

check("serial_port == COM5 after launch pick (even if bootstrap fails)", app.serial_port == "COM5")

app = make_app()
import serial as _serial
with patch.object(app, "_select_port", return_value="COM5"), \
     patch("serial.Serial", side_effect=_serial.SerialException("busy")), \
     patch("builtins.print"):
    try:
        app._startup_network_setup()
    except SystemExit:
        pass
check("serial_port stays unset when the port cannot be opened", app.serial_port is None)


# ============================================================================
# Fully Auto uses the launch choice
# ============================================================================

print()
print("=" * 60)
print("Fully Auto: launch choice COM5 wins over COM4 listed first")
print("=" * 60)

app = make_app()
app.serial_port = "COM5"
captured, mock_select = run_fully_auto(app)

check("confirmation shows COM5", captured.get("port") == "COM5")
check("flash runs on COM5", app.serial_port == "COM5")
check("no port picker shown when launch choice exists", not mock_select.called)
check("proceeds to NETWORK_FLASHING", app.current_state == MenuState.NETWORK_FLASHING)


# ============================================================================
# No launch choice -> port picker, never all_ports[0]
# ============================================================================

print()
print("=" * 60)
print("Fully Auto: no launch choice -> uses _select_port(), not first port")
print("=" * 60)

app = make_app()
captured, mock_select = run_fully_auto(app, select_port_result="COM5")

check("_select_port() called", mock_select.called)
check("_select_port() auto-selects only a single port",
      mock_select.call_args.kwargs.get("auto_select_single") is True)
check("confirmation shows the picked COM5, not COM4", captured.get("port") == "COM5")
check("flash runs on COM5", app.serial_port == "COM5")

app = make_app()
captured, mock_select = run_fully_auto(app, select_port_result=None)
check("Back in picker -> Flash menu", app.current_state == MenuState.FLASH_AUTO_OR_MANUAL)
check("Back in picker -> no confirmation shown", captured == {})


# ============================================================================
# Phase 1 step 1 label
# ============================================================================

print()
print("=" * 60)
print("phase1_uboot(): step 1 says 'Port X present'")
print("=" * 60)

from mono_imager import flash_orchestrator as core

labels = []
with patch("mono_imager.flash_orchestrator.detect_serial_ports", return_value=([CH340], [])), \
     patch("mono_imager.flash_orchestrator.step",
           side_effect=lambda n, label, ok, *a, **k: labels.append(label) or ok), \
     patch("mono_imager.flash_orchestrator.SerialDevice") as mock_dev, \
     patch("builtins.print"):
    mock_dev.return_value.connect.return_value = False
    core.phase1_uboot("COM4", 115200)

check("step 1 label is 'Port COM4 present'", labels and labels[0] == "Port COM4 present")
check("no 'Device detected' wording left", not any("Device detected" in l for l in labels))


print()
print(f"Results: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
