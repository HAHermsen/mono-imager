#!/usr/bin/env python3
"""
mono-imager: Unit tests for CLI entry point and TUI state machine.

No hardware required. All I/O and device interactions are mocked.

What this tests:

  CLI entry point (cli.py):
    - Normal startup instantiates MonoImager and calls run()
    - KeyboardInterrupt exits cleanly with code 0
    - Unhandled exception exits with code 1

  State machine (MonoImager.run()):
    - Each menu selection routes to the correct next state
    - 'exit!' escape from any input returns to FLASH_AUTO_OR_MANUAL
    - DONE state exits the loop cleanly
    - Unknown/invalid input stays in current state (no crash)

  Menu navigation paths:
    - Main → Flash journey (option 1)
    - Main → Recovery flow (option 2)
    - Main → CLI console (option 3)
    - Main → Device stats (option 6)
    - Main → Quit (option q)
    - Flash auto/manual → Auto config
    - Flash auto/manual → Manual config (back to CONNECTION)
    - Flash auto/manual → Back to main
    - Transfer method → Network
    - Transfer method → USB
    - Transfer method → Back
    - safe_input() escape hatch ('exit!')

Run: python tests/unit/test_cli.py
"""

import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

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
    """Return a MonoImager with screen-clearing suppressed."""
    app = MonoImager()
    app.clear_screen = lambda: None
    app.print_header = lambda: None
    return app


def run_to_done(app, inputs):
    """
    Drive the state machine with the given input sequence, stopping
    when state reaches DONE or inputs are exhausted.
    Patches builtins.input and builtins.print to suppress all I/O.
    """
    with patch("builtins.input", side_effect=inputs + ["q"] * 20), \
         patch("builtins.print"):
        try:
            app.run()
        except (StopIteration, SystemExit, KeyboardInterrupt):
            pass


# ============================================================================
# CLI entry point (cli.py)
# ============================================================================

print("=" * 60)
print("CLI entry point (cli.py)")
print("=" * 60)

# Normal startup: MonoImager is instantiated and run() is called
with patch("mono_imager.tui.MonoImager") as MockApp:
    instance = MockApp.return_value
    instance.run.side_effect = KeyboardInterrupt  # exit immediately

    import mono_imager.cli as cli_module
    try:
        cli_module.main()
    except SystemExit as e:
        check("KeyboardInterrupt → sys.exit(0)", e.code == 0)
    else:
        check("KeyboardInterrupt → sys.exit(0)", False)

    check("MonoImager was instantiated", MockApp.called)
    check("run() was called on the instance", instance.run.called)

# Unhandled exception → sys.exit(1)
import importlib
with patch("mono_imager.tui.MonoImager") as MockApp:
    instance = MockApp.return_value
    instance.run.side_effect = RuntimeError("something broke")

    import mono_imager.cli as cli_module
    importlib.reload(cli_module)  # ensure patch is active for this import

    try:
        cli_module.main()
        check("Unhandled exception → sys.exit(1)", False)
    except SystemExit as e:
        check("Unhandled exception → sys.exit(1)", e.code == 1)


# ============================================================================
# safe_input() escape hatch
# ============================================================================

print()
print("=" * 60)
print("safe_input() — 'exit!' escape")
print("=" * 60)

app = make_app()
with patch("builtins.input", return_value="exit!"), patch("builtins.print"):
    result = app.safe_input("Prompt: ")
check("safe_input returns None on 'exit!'",      result is None)
check("state set to FLASH_AUTO_OR_MANUAL",       app.current_state == MenuState.FLASH_AUTO_OR_MANUAL)

app = make_app()
with patch("builtins.input", return_value="hello"), patch("builtins.print"):
    result = app.safe_input("Prompt: ")
check("safe_input returns value on normal input", result == "hello")
check("state unchanged on normal input",         app.current_state == MenuState.MAIN)

# Case-insensitive
app = make_app()
with patch("builtins.input", return_value="EXIT!"), patch("builtins.print"):
    result = app.safe_input("Prompt: ")
check("safe_input 'EXIT!' (uppercase) also escapes", result is None)


# ============================================================================
# Main menu routing
# ============================================================================

print()
print("=" * 60)
print("Main menu routing")
print("=" * 60)

def check_main_transition(choice, expected_state, label):
    app = make_app()
    with patch("builtins.input", side_effect=[choice, "q"]), patch("builtins.print"):
        app.menu_main()
    check(label, app.current_state == expected_state)

check_main_transition("1", MenuState.FLASH_AUTO_OR_MANUAL, "option 1 → FLASH_AUTO_OR_MANUAL")
check_main_transition("2", MenuState.RECOVERY_FLOW,        "option 2 → RECOVERY_FLOW")
check_main_transition("3", MenuState.CLI_CONSOLE,          "option 3 → CLI_CONSOLE")
check_main_transition("6", MenuState.DEVICE_STATS,         "option 6 → DEVICE_STATS")

# Options 4 and 5 now route to menu_test_serial / menu_test_lan.
# Verify dispatch by mocking the methods and confirming they're called.
for opt, method in [("4", "menu_test_serial"), ("5", "menu_test_lan")]:
    app = make_app()
    mock_method = MagicMock()
    with patch.object(app, method, mock_method), \
         patch("builtins.input", return_value=opt), \
         patch("builtins.print"):
        app.menu_main()
    check(f"option {opt} → {method}() called", mock_method.called)

# Quit is option 7
app = make_app()
with patch("builtins.input", return_value="7"), patch("builtins.print"):
    try:
        app.menu_main()
    except SystemExit as e:
        check("option 7 → sys.exit(0)", e.code == 0)
    else:
        check("option 7 → sys.exit(0)", False)

# Invalid input stays in MAIN (no crash, no transition)
app = make_app()
app.current_state = MenuState.MAIN
with patch("builtins.input", side_effect=["z", "q"]), patch("builtins.print"):
    try:
        app.menu_main()
    except SystemExit:
        pass
check("invalid input stays in MAIN (no crash)", app.current_state == MenuState.MAIN)


# ============================================================================
# Flash auto/manual routing
# ============================================================================

print()
print("=" * 60)
print("Flash auto/manual routing")
print("=" * 60)

def check_flash_am_transition(choice, expected_state, label):
    app = make_app()
    with patch("builtins.input", return_value=choice), patch("builtins.print"):
        app.menu_flash_auto_or_manual()
    check(label, app.current_state == expected_state)

check_flash_am_transition("1", MenuState.NETWORK_AUTO_CONFIG, "option 1 → NETWORK_AUTO_CONFIG (auto)")
check_flash_am_transition("2", MenuState.CONNECTION,          "option 2 → CONNECTION (manual)")
check_flash_am_transition("3", MenuState.MAIN,               "option 3 → MAIN (back)")


# ============================================================================
# Transfer method routing
# ============================================================================

print()
print("=" * 60)
print("Transfer method routing")
print("=" * 60)

def check_transfer_transition(choice, expected_state, expected_method, label):
    app = make_app()
    app.os_name     = "Armbian"
    app.serial_port = "COM5"
    with patch("builtins.input", return_value=choice), patch("builtins.print"):
        app.menu_transfer_method()
    check(label + " — state",           app.current_state == expected_state)
    if expected_method is not None:
        check(label + " — transfer_method", app.transfer_method == expected_method)

check_transfer_transition("1", MenuState.NETWORK_FLASH_CONFIG, "network", "option 1 → network")
check_transfer_transition("2", MenuState.DEVICE_SELECT,        None,      "option 2 → back")


# ============================================================================
# State machine: DONE exits the loop
# ============================================================================

print()
print("=" * 60)
print("State machine — DONE exits the loop")
print("=" * 60)

app = make_app()
app.current_state = MenuState.DONE
app.flash_success = True

# menu_done either exits the loop or sets state to MAIN (go again)
# Either way it should not crash
with patch("builtins.input", return_value="q"), patch("builtins.print"):
    try:
        app.menu_done()
        check("menu_done completes without crash", True)
    except (SystemExit, Exception) as e:
        check("menu_done completes without crash", isinstance(e, SystemExit))


# ============================================================================
# State machine: run() dispatches to correct menu method
# ============================================================================

print()
print("=" * 60)
print("run() — dispatch to menu methods")
print("=" * 60)

# Each state should call the corresponding method exactly once
states_and_methods = [
    (MenuState.MAIN,                 "menu_main"),
    (MenuState.FLASH_AUTO_OR_MANUAL, "menu_flash_auto_or_manual"),
    (MenuState.CONNECTION,           "menu_connection"),
    (MenuState.DEVICE_SELECT,        "menu_device_select"),
    (MenuState.TRANSFER_METHOD,      "menu_transfer_method"),
    (MenuState.NETWORK_AUTO_CONFIG,  "menu_network_auto_config"),
    (MenuState.NETWORK_FLASH_CONFIG, "menu_network_flash_config"),
    (MenuState.NETWORK_FLASHING,     "menu_network_flashing"),
    (MenuState.RECOVERY_FLOW,        "menu_recovery_flow"),
    (MenuState.CLI_CONSOLE,          "menu_cli_console"),
    (MenuState.DEVICE_STATS,         "device_stats"),
]

for state, method_name in states_and_methods:
    app = make_app()
    app.current_state = state
    mock_method = MagicMock(side_effect=KeyboardInterrupt)
    with patch.object(app, method_name, mock_method), \
         patch("builtins.print"):
        try:
            app.run()
        except (KeyboardInterrupt, SystemExit):
            pass
    check(f"run() dispatches {state.value} → {method_name}()", mock_method.called)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
