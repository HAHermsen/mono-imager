#!/usr/bin/env python3
"""
mono-imager: Unit tests for CLI entry point and TUI state machine.

No hardware required. All I/O and device interactions are mocked.

What this tests:

  CLI entry point (cli.py):
    - Normal startup instantiates MonoImager and calls run()
    - KeyboardInterrupt exits cleanly with code 0
    - Unhandled exception exits with code 1
    - No flags -> MONO_DEBUG is left unset (quiet by default)
    - --debug / --verbose (either spelling) -> sets MONO_DEBUG=1
      before mono_imager.tui is ever imported

  State machine (MonoImager.run()):
    - Each menu selection routes to the correct next state
    - 'exit!' escape from any input returns to FLASH_AUTO_OR_MANUAL
    - DONE state exits the loop cleanly
    - Unknown/invalid input stays in current state (no crash)

  Menu navigation paths:
    - Main -> Flash journey (option 1)
    - Main -> Update eMMC firmware (option 2)
    - Main -> Update NOR firmware (option 3)
    - Main -> CLI console (option 4)
    - Main -> Test Serial (option 5)
    - Main -> Test LAN (option 6)
    - Main -> Test USB stick (option 7)
    - Main -> Device stats (option 8)
    - Main -> Quit (option 9)
    - Flash auto/manual -> Auto config
    - Flash auto/manual -> Back to main
    - safe_input() escape hatch ('exit!')

Run: python tests/unit/test_cli.py
"""

import os
import sys
import subprocess
from pathlib import Path
import time
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

# All cli_module.main() calls below patch sys.argv to a clean baseline —
# argparse reads sys.argv[1:] by default, and this file may itself be
# invoked with extra args (e.g. under pytest), which would otherwise leak
# into main()'s own argument parsing and raise SystemExit(2) on an
# unrecognized flag.

# Normal startup: MonoImager is instantiated and run() is called
with patch("mono_imager.tui.MonoImager") as MockApp, \
     patch.object(sys, "argv", ["mono-imager"]):
    instance = MockApp.return_value
    instance.run.side_effect = KeyboardInterrupt  # exit immediately

    import mono_imager.cli as cli_module
    try:
        cli_module.main()
    except SystemExit as e:
        check("KeyboardInterrupt -> sys.exit(0)", e.code == 0)
    else:
        check("KeyboardInterrupt -> sys.exit(0)", False)

    check("MonoImager was instantiated", MockApp.called)
    check("run() was called on the instance", instance.run.called)

# Unhandled exception -> sys.exit(1)
import importlib
with patch("mono_imager.tui.MonoImager") as MockApp, \
     patch.object(sys, "argv", ["mono-imager"]):
    instance = MockApp.return_value
    instance.run.side_effect = RuntimeError("something broke")

    import mono_imager.cli as cli_module
    importlib.reload(cli_module)  # ensure patch is active for this import

    try:
        with patch("builtins.print"):
            cli_module.main()
        check("Unhandled exception -> sys.exit(1)", False)
    except SystemExit as e:
        check("Unhandled exception -> sys.exit(1)", e.code == 1)


# ============================================================================
# --debug / --verbose flags
# ============================================================================

print()
print("=" * 60)
print("CLI entry point — --debug / --verbose flags")
print("=" * 60)

# MONO_DEBUG is process-global env state — save/restore around these
# checks so this test file doesn't leak it into anything that runs after.
_saved_mono_debug = os.environ.pop("MONO_DEBUG", None)

try:
    for flag_name, argv in [
        ("--debug",   ["mono-imager", "--debug"]),
        ("--verbose", ["mono-imager", "--verbose"]),
    ]:
        os.environ.pop("MONO_DEBUG", None)
        with patch("mono_imager.tui.MonoImager") as MockApp, \
             patch.object(sys, "argv", argv):
            instance = MockApp.return_value
            instance.run.side_effect = KeyboardInterrupt

            import mono_imager.cli as cli_module
            importlib.reload(cli_module)
            try:
                cli_module.main()
            except SystemExit:
                pass
        check(f"{flag_name} sets MONO_DEBUG=1", os.environ.get("MONO_DEBUG") == "1")

    # No flag at all -> MONO_DEBUG stays unset (quiet by default)
    os.environ.pop("MONO_DEBUG", None)
    with patch("mono_imager.tui.MonoImager") as MockApp, \
         patch.object(sys, "argv", ["mono-imager"]):
        instance = MockApp.return_value
        instance.run.side_effect = KeyboardInterrupt

        import mono_imager.cli as cli_module
        importlib.reload(cli_module)
        try:
            cli_module.main()
        except SystemExit:
            pass
    check("no flags -> MONO_DEBUG left unset (quiet by default)",
          "MONO_DEBUG" not in os.environ)

    # Unrecognized flag -> argparse exits with code 2, doesn't touch MonoImager.
    # builtins.print/sys.stderr are only patched around the main() call
    # itself (argparse writes its usage/error message there) — check()
    # runs outside that scope so its own PASS/FAIL lines stay visible.
    with patch("mono_imager.tui.MonoImager") as MockApp, \
         patch.object(sys, "argv", ["mono-imager", "--bogus-flag"]):
        import mono_imager.cli as cli_module
        importlib.reload(cli_module)
        exit_code = None
        with patch("builtins.print"), patch("sys.stderr"):
            try:
                cli_module.main()
            except SystemExit as e:
                exit_code = e.code
        check("unrecognized flag -> SystemExit(2)", exit_code == 2)
        check("unrecognized flag never reaches MonoImager", not MockApp.called)

    # REGRESSION: serial_device.py / flash_orchestrator.py must read
    # MONO_DEBUG live (at verbose()-call time), not freeze it into a
    # module-level constant at import time. The installed console-script
    # entry point (`mono-imager = "mono_imager.cli:main"`) does
    # `from mono_imager.cli import main`, which — because Python always
    # initializes parent packages first — imports mono_imager/__init__.py
    # and therefore serial_device.py BEFORE main() ever runs, let alone
    # parses --debug/--verbose. A constant computed at that import time
    # would always see MONO_DEBUG unset, silently ignoring the flag. Both
    # modules are already imported here (via the `mono_imager.tui` import
    # at the top of this file) before this block ever touches argv/env,
    # reproducing that exact ordering.
    import mono_imager.serial_device as sd
    import mono_imager.flash_orchestrator as fo

    os.environ.pop("MONO_DEBUG", None)
    check("serial_device debug reads live: off before flag is set", not sd._debug_enabled())
    check("flash_orchestrator debug reads live: off before flag is set", not fo._debug_enabled())

    os.environ["MONO_DEBUG"] = "1"
    check("serial_device debug reads live: on once MONO_DEBUG is set", sd._debug_enabled())
    check("flash_orchestrator debug reads live: on once MONO_DEBUG is set", fo._debug_enabled())
finally:
    if _saved_mono_debug is None:
        os.environ.pop("MONO_DEBUG", None)
    else:
        os.environ["MONO_DEBUG"] = _saved_mono_debug


# ============================================================================
# --version flag
# ============================================================================

print()
print("=" * 60)
print("CLI entry point - --version")
print("=" * 60)

import io
import contextlib
from mono_imager import __version__, __author__, __license__

# --version must print release version, developer and license, each on its
# own line, then exit(0) BEFORE the menu (MonoImager) is ever touched.
with patch("mono_imager.tui.MonoImager") as MockApp, \
     patch.object(sys, "argv", ["mono-imager", "--version"]):
    import mono_imager.cli as cli_module
    importlib.reload(cli_module)
    _buf = io.StringIO()
    _exit_code = None
    with contextlib.redirect_stdout(_buf):
        try:
            cli_module.main()
        except SystemExit as e:
            _exit_code = e.code
    _out = _buf.getvalue()

check("--version exits with code 0", _exit_code == 0)
check("--version never reaches MonoImager", not MockApp.called)
check("--version prints the release version", __version__ in _out)
check("--version prints the developer", __author__ in _out)
check("--version prints the license", __license__ in _out)
check("--version prints three separate lines (newlines preserved)",
      len([ln for ln in _out.splitlines() if ln.strip()]) == 3)


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

check_main_transition("1", MenuState.FLASH_AUTO_OR_MANUAL, "option 1 -> FLASH_AUTO_OR_MANUAL")
check_main_transition("2", MenuState.UPDATE_EMMC,          "option 2 -> UPDATE_EMMC")
check_main_transition("3", MenuState.UPDATE_NOR,           "option 3 -> UPDATE_NOR")
check_main_transition("4", MenuState.CLI_CONSOLE,          "option 4 -> CLI_CONSOLE")
check_main_transition("8", MenuState.DEVICE_STATS,         "option 8 -> DEVICE_STATS")

# Options 5, 6, 7 call menu_test_serial / menu_test_lan / menu_test_usb_mount
# directly (no state change). Verify dispatch by mocking the methods and
# confirming they're called.
for opt, method in [("5", "menu_test_serial"), ("6", "menu_test_lan"), ("7", "menu_test_usb_mount")]:
    app = make_app()
    mock_method = MagicMock()
    with patch.object(app, method, mock_method), \
         patch("builtins.input", return_value=opt), \
         patch("builtins.print"):
        app.menu_main()
    check(f"option {opt} -> {method}() called", mock_method.called)

check_main_transition("9", MenuState.SNAPSHOT,             "option 9 -> SNAPSHOT")
check_main_transition("10", MenuState.RESTORE,             "option 10 -> RESTORE")

# Quit is option 11
app = make_app()
with patch("builtins.input", return_value="11"), patch("builtins.print"):
    try:
        app.menu_main()
    except SystemExit as e:
        check("option 11 -> sys.exit(0)", e.code == 0)
    else:
        check("option 11 -> sys.exit(0)", False)

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

check_flash_am_transition("1", MenuState.NETWORK_AUTO_CONFIG,  "option 1 -> NETWORK_AUTO_CONFIG (auto)")
check_flash_am_transition("2", MenuState.MAIN,                 "option 2 -> MAIN (back)")
check_flash_am_transition("x", MenuState.FLASH_AUTO_OR_MANUAL, "invalid -> stays in FLASH_AUTO_OR_MANUAL")


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
    (MenuState.NETWORK_AUTO_CONFIG,  "menu_network_auto_config"),
    (MenuState.NETWORK_FLASHING,     "menu_network_flashing"),
    (MenuState.UPDATE_EMMC,          "menu_update_emmc"),
    (MenuState.UPDATE_NOR,           "menu_update_nor"),
    (MenuState.DONE,                 "menu_done"),
    (MenuState.CLI_CONSOLE,          "menu_cli_console"),
    (MenuState.DEVICE_STATS,         "device_stats"),
]

for state, method_name in states_and_methods:
    app = make_app()
    app.current_state = state
    mock_method = MagicMock(side_effect=KeyboardInterrupt)
    with patch.object(app, method_name, mock_method), \
         patch.object(app, "_startup_network_setup", lambda: None), \
         patch("builtins.print"):
        try:
            app.run()
        except (KeyboardInterrupt, SystemExit):
            pass
    check(f"run() dispatches {state.value} -> {method_name}()", mock_method.called)

# run() resolves the device network once, before ever dispatching to a menu
app = make_app()
mock_startup = MagicMock(side_effect=KeyboardInterrupt)
with patch.object(app, "_startup_network_setup", mock_startup), \
     patch("builtins.print"):
    try:
        app.run()
    except (KeyboardInterrupt, SystemExit):
        pass
check("run() calls _startup_network_setup() before the dispatch loop", mock_startup.called)


# ============================================================================
# Every menu screen, branch by branch (all I/O and devices mocked)
# ============================================================================

import io
import runpy
import tempfile
import types as _types
from types import SimpleNamespace as NS
import mono_imager.tui as T
from mono_imager import console as CON
from mono_imager import constants as C
from mono_imager.journeys import discovered_journeys


def section(t):
    print()
    print("=" * 60)
    print(t)
    print("=" * 60)


class Out:
    """Captures print() output as text."""
    def __init__(self):
        self.lines = []

    def __call__(self, *a, **k):
        self.lines.append(" ".join(str(x) for x in a))

    @property
    def text(self):
        return "\n".join(self.lines)


def drive(fn, inputs, *patches):
    """Run fn() with scripted input(); returns (result, printed text, prompts asked)."""
    out, asked = Out(), []
    it = iter(inputs)

    def inp(prompt=""):
        asked.append(prompt)
        return next(it)
    with contextlib_exit(patches) as _, patch("builtins.input", inp), patch("builtins.print", out):
        try:
            r = fn()
        except SystemExit as e:
            r = ("exit", e.code)
    return r, out.text, asked


import contextlib


@contextlib.contextmanager
def contextlib_exit(patches):
    with contextlib.ExitStack() as es:
        for p in patches:
            es.enter_context(p)
        yield


PORT_A = NS(device="/dev/ttyUSB0", description="FT232R")
PORT_B = NS(device="/dev/ttyS0", description="16550")


def ports(known=(PORT_A,), other=(), last=None, raises=None):
    det = patch("mono_imager.config.detect_serial_ports",
                side_effect=raises) if raises else patch("mono_imager.config.detect_serial_ports",
                                                         return_value=(list(known), list(other)))
    return [det, patch("mono_imager.config.get_last_port", return_value=last),
            patch("mono_imager.config.save_last_port")]


tmpd = Path(tempfile.mkdtemp(prefix="mi_tui_"))
BZ = tmpd / "opnsense.img.bz2"
BZ.write_bytes(b"BZh91AY" + b"\0" * (C.MIB + 10))
RAW = tmpd / "fw.img"
RAW.write_bytes(b"\0" * (C.MIB + 10))

# ----------------------------------------------------------------------------
section("small helpers")
out = Out()
with patch("builtins.print", out), patch.object(T.logger, "log") as lg:
    T.verbose("hello", "warning")
check("verbose(): printed and logged at the right level", "hello" in out.text and lg.call_args.args[0] == T.logging.WARNING)
check("_netmask_to_prefix re-export", T._netmask_to_prefix("255.255.255.0") == "24")
app = make_app()
app.device_net = {"ip": "1.2.3.4"}
app.device_net_verified = True
check("device_net / device_net_verified properties write through to RecoveryNetwork",
      app._net.config == {"ip": "1.2.3.4"} and app._net.verified is True and app.device_net_verified is True)
app = MonoImager()
with patch.object(CON, "clear_screen") as cs, patch.object(CON, "print_header") as ph:
    app.clear_screen()
    app.print_header()
check("clear_screen / print_header delegate to console", cs.called and ph.call_args.args[0] == T.__version__)
with patch.object(CON, "show_firmware_output") as so:
    app._show_firmware_output("chunk")
check("_show_firmware_output delegates", so.call_args.args == ("chunk",))
with patch.object(CON, "show_device_stats") as sd:
    app._display_device_stats("U-Boot 2025.04\nModel: Mono\n")
check("_display_device_stats parses and renders", sd.called and len(sd.call_args.args) == 2)

# ----------------------------------------------------------------------------
section("_validate_firmware_path")
v = MonoImager()._validate_firmware_path
small = tmpd / "small.img"
small.write_bytes(b"x" * 2048)
empty = tmpd / "empty.img"
empty.write_bytes(b"")
check("empty input", v("  ")[1] == "No path entered.")
check("not found", "File not found" in v(str(tmpd / "nope.img"))[1])
check("a folder", "folder, not a file" in v(str(tmpd))[1])
if os.name == "nt":
    print("  SKIP: not a regular file (/dev/null does not exist on Windows)")
else:
    check("not a regular file (/dev/null)", "Not a regular file" in v("/dev/null")[1])
check("0 bytes", "File is empty" in v(str(empty))[1])
check("too small (2 KB)", "only 2 KB" in v(str(small))[1])
p, err = v(f'  "{RAW}"  ')
check("quotes stripped, valid file -> Path", p == RAW and err is None)
p, err = v(f"'{RAW}'")
check("single quotes stripped too", p == RAW and err is None)
fake = MagicMock()
fake.exists.return_value, fake.is_dir.return_value, fake.is_file.return_value = True, False, True
fake.stat.side_effect = OSError("stale handle")
with patch.object(T, "Path", return_value=MagicMock(expanduser=lambda: fake)):
    check("stat() fails -> can't read file info", "Can't read file info" in v("/x")[1])
with patch("builtins.open", side_effect=PermissionError("denied")):
    check("unreadable -> can't read file", "Can't read file: denied" in v(str(RAW))[1])

# ----------------------------------------------------------------------------
section("_soft_reboot_if_possible")
for conn, wrote in ((True, True), (False, False)):
    dev = MagicMock()
    dev.connect.return_value = conn
    with patch("mono_imager.serial_device.SerialDevice", return_value=dev), patch.object(T, "SOFT_REBOOT_SETTLE", 0):
        MonoImager()._soft_reboot_if_possible("/dev/ttyUSB0")
    check(f"connect {conn}: reboot typed={wrote}", dev.ser.write.called is wrote and dev.disconnect.called is wrote)
with patch("mono_imager.serial_device.SerialDevice", side_effect=OSError("busy")):
    MonoImager()._soft_reboot_if_possible("/dev/ttyUSB0")
check("errors are swallowed (best effort)", True)

# ----------------------------------------------------------------------------
section("_select_port")
sp = lambda **k: (lambda: MonoImager()._select_port(**k))
r, t, a = drive(sp(), [""], *ports(raises=OSError("udev")))
check("detection fails -> None, message + Enter", r is None and "Port detection failed" in t and a)
r, t, a = drive(sp(quiet=True), [], *ports(raises=OSError("udev")))
check("detection fails, quiet -> None, no prompt", r is None and not a and not t)
r, t, a = drive(sp(), [""], *ports(known=()))
check("no ports -> None, message", r is None and "No serial devices found" in t)
r, t, a = drive(sp(quiet=True), [], *ports(known=()))
check("no ports, quiet -> silent None", r is None and not a)
r, t, a = drive(sp(auto_select_single=True), [], *ports())
check("single port auto-selected, no question", r == "/dev/ttyUSB0" and "Auto-selected" in t and not a)
pp = ports(known=(PORT_A,), other=(PORT_B,), last="/dev/ttyS0")
r, t, a = drive(sp(show_categories=True, allow_enter_last=True, save_on_select=True), [""], *pp)
check("categories listed, last port marked, Enter = last port (saved)",
      r == "/dev/ttyS0" and "USB-UART adapters" in t and "Other ports:" in t and "◄ last used" in t
      and "[Enter] Use last port" in t)
pp = ports(known=(), other=(PORT_B,), last=None)
r, t, a = drive(sp(show_categories=True), ["1"], *pp)
check("only 'other' ports: listed without the adapters heading", r == "/dev/ttyS0" and "USB-UART" not in t)
pp = ports(known=(PORT_A, PORT_B), last="/dev/ttyUSB0")
with pp[2] as save:
    r, t, a = drive(sp(save_on_select=True), ["2"], pp[0], pp[1])
check("numbered choice -> that port, saved", r == "/dev/ttyS0" and save.call_args.args == ("/dev/ttyS0",))
r, t, a = drive(sp(), ["3"], *ports(known=(PORT_A, PORT_B)))
check("'Back' -> None", r is None and "3) Back" in t)
for bad in ("9", "x", "0"):
    r, t, a = drive(sp(), [bad, ""], *ports(known=(PORT_A, PORT_B)))
    check(f"invalid {bad!r} -> None, 'Invalid selection'", r is None and "Invalid selection" in t)
r, t, a = drive(sp(allow_back=False), ["2", ""], *ports(known=(PORT_A,)))
check("no Back entry: N+1 is out of range", r is None and "Back" not in t)

# ----------------------------------------------------------------------------
section("_show_flash_confirmation / _recovery_finish / _setup_recovery_network")
app = make_app()
kw = dict(os_name="X", port="p", firmware_path="f", flash_target="t", host_ip="h", device_ip="d")
with patch.object(CON, "show_flash_confirmation", return_value=CON.ESCAPE):
    check("escape -> None, back to Flash menu", app._show_flash_confirmation(**kw) is None
          and app.current_state == MenuState.FLASH_AUTO_OR_MANUAL)
for v_ in (True, False):
    with patch.object(CON, "show_flash_confirmation", return_value=v_):
        check(f"confirmation {v_} passed through", app._show_flash_confirmation(**kw) is v_)
app = make_app()
app.current_state = MenuState.UPDATE_EMMC
r, t, a = drive(lambda: app._recovery_finish(True), [""])
check("_recovery_finish: result kept, waits for Enter, back to MAIN",
      app.flash_success is True and app.current_state == MenuState.MAIN and a)
fake_tm = _types.SimpleNamespace(TCIFLUSH=0, tcflush=MagicMock())
# msvcrt=None makes "import msvcrt" raise ImportError, so the termios path runs on Windows too
with patch.dict(sys.modules, {"termios": fake_tm, "msvcrt": None}):
    drive(lambda: app._recovery_finish(False), [""])
check("_recovery_finish: stdin flushed via termios (stray Enter can't skip the report)", fake_tm.tcflush.called)
fake_ms = _types.SimpleNamespace(kbhit=MagicMock(side_effect=[True, True, False]), getch=MagicMock())
with patch.dict(sys.modules, {"msvcrt": fake_ms}):
    drive(lambda: app._recovery_finish(False), [""])
check("_recovery_finish: Windows drains pending keys via msvcrt", fake_ms.getch.call_count == 2)
app = make_app()
with patch.object(app._net, "resolve", return_value=True) as rs:
    check("_setup_recovery_network delegates to the session RecoveryNetwork", app._setup_recovery_network("D") and rs.call_args.args == ("D",))

# ----------------------------------------------------------------------------
section("menu_main / menu_flash_auto_or_manual")
want = {"1": MenuState.FLASH_AUTO_OR_MANUAL, "2": MenuState.UPDATE_EMMC, "3": MenuState.UPDATE_NOR,
        "4": MenuState.CLI_CONSOLE, "8": MenuState.DEVICE_STATS, "9": MenuState.SNAPSHOT, "10": MenuState.RESTORE}
for ch, st in want.items():
    app = make_app()
    drive(app.menu_main, [ch], patch.object(CON, "print_status_line"))
    check(f"main {ch} -> {st.value}", app.current_state == st)
for ch, meth in (("5", "menu_test_serial"), ("6", "menu_test_lan"), ("7", "menu_test_usb_mount")):
    app = make_app()
    with patch.object(app, meth) as m:
        drive(app.menu_main, [ch], patch.object(CON, "print_status_line"))
    check(f"main {ch} -> {meth}()", m.called)
app = make_app()
r, t, a = drive(app.menu_main, ["11"], patch.object(CON, "print_status_line"))
check("main 11 -> exit(0)", r == ("exit", 0))
r, t, a = drive(app.menu_main, ["zz", ""], patch.object(CON, "print_status_line"))
check("main invalid -> message, stays on MAIN", "Invalid selection" in t and app.current_state == MenuState.MAIN)
for ch, st in (("1", MenuState.NETWORK_AUTO_CONFIG), ("2", MenuState.MAIN)):
    app = make_app()
    drive(app.menu_flash_auto_or_manual, [ch], patch.object(CON, "print_status_line"))
    check(f"flash menu {ch} -> {st.value}", app.current_state == st)
app = make_app()
r, t, a = drive(app.menu_flash_auto_or_manual, ["7", ""], patch.object(CON, "print_status_line"))
check("flash menu invalid -> stays", app.current_state == MenuState.FLASH_AUTO_OR_MANUAL and "Invalid" in t)

# ----------------------------------------------------------------------------
section("menu_network_auto_config (Fully Auto)")
J_ = discovered_journeys()
OPN_LAN = str(J_.index(("OPNsense", "lan")) + 1)
ARM_USB = str(J_.index(("Armbian", "usb")) + 1)
BACK = str(len(J_) + 1)
QUIET = [patch.object(CON, "print_status_line")]


def auto(app, inputs, host="192.168.168.84", confirm=True, extra=()):
    ps = QUIET + [patch("mono_imager.flash_orchestrator.detect_host_ip", return_value=host),
                  patch.object(CON, "show_flash_confirmation", return_value=confirm)] + list(extra)
    return drive(app.menu_network_auto_config, inputs, *ps)


app = make_app()
with patch.object(app, "_select_port", return_value=None):
    auto(app, [])
check("no session port, picker -> Back -> Flash menu", app.current_state == MenuState.FLASH_AUTO_OR_MANUAL)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
r, t, a = auto(app, ["exit!"])
check("'exit!' at the OS question -> Flash menu", app.current_state == MenuState.FLASH_AUTO_OR_MANUAL
      and "(chosen at launch)" in t)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
auto(app, [BACK])
check("Back -> Flash menu", app.current_state == MenuState.FLASH_AUTO_OR_MANUAL)
for bad in ("0x", "99"):
    app = make_app(); app.serial_port = "/dev/ttyUSB0"; app.current_state = MenuState.MAIN
    r, t, a = auto(app, [bad, ""])
    check(f"invalid OS choice {bad!r} -> asked again", app.current_state == MenuState.NETWORK_AUTO_CONFIG and "Invalid" in t)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
r, t, a = auto(app, [OPN_LAN, "", str(BZ)])
check("OPNsense LAN: Ethernet notice, valid .bz2, confirmed -> NETWORK_FLASHING",
      app.current_state == MenuState.NETWORK_FLASHING and "ETHERNET CABLE REQUIRED" in t
      and app.custom_fw_path == BZ and app.os_name == "OPNsense" and app.transfer_method == "lan"
      and app.net_host_ip == "192.168.168.84" and app.net_flash_target == C.EMMC_DEV)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
r, t, a = auto(app, [ARM_USB])
check("Armbian USB: no Ethernet notice, image auto-detected from the stick",
      app.current_state == MenuState.NETWORK_FLASHING and "ETHERNET" not in t and "auto-detected from USB" in t
      and app.custom_fw_path == Path("."))
VY = [i for i, (o, tr) in enumerate(J_, 1) if __import__("mono_imager.journeys", fromlist=["x"]).get_journey_notice(o, tr)]
if VY:
    app = make_app(); app.serial_port = "/dev/ttyUSB0"
    r, t, a = auto(app, [str(VY[0]), "", "exit!"])
    _o, _tr = J_[VY[0] - 1]
    _note = __import__("mono_imager.journeys", fromlist=["x"]).get_journey_notice(_o, _tr)
    check("journey with a notice (VyOS): notice shown before the firmware question",
          app.current_state == MenuState.FLASH_AUTO_OR_MANUAL and _note.strip() in t)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
auto(app, [OPN_LAN, "", "exit!"])
check("'exit!' at the firmware path -> Flash menu", app.current_state == MenuState.FLASH_AUTO_OR_MANUAL)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
r, t, a = auto(app, [OPN_LAN, "", str(tmpd / "missing.img"), ""])
check("bad firmware path -> error, asked again", app.current_state == MenuState.NETWORK_AUTO_CONFIG and "File not found" in t)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
r, t, a = auto(app, [OPN_LAN, "", str(RAW), "n", ""])
check("image/OS mismatch, declined -> cancelled", app.current_state == MenuState.NETWORK_AUTO_CONFIG
      and "Image/OS mismatch" in t and "Cancelled" in t)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
auto(app, [OPN_LAN, "", str(RAW), "y"])
check("image/OS mismatch, 'y' -> flashes anyway", app.current_state == MenuState.NETWORK_FLASHING)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
r, t, a = auto(app, [OPN_LAN, "", str(BZ), ""], host=None)
check("host IP not detected -> Flash menu", app.current_state == MenuState.FLASH_AUTO_OR_MANUAL and "host IP" in t)
app2 = make_app(); app2.serial_port = "/dev/ttyUSB0"; app2.device_net = {"ip": "192.168.168.222", "source": "dhcp"}
cap = {}


def conf(**k):
    cap.update(k)
    return CON.ESCAPE


with patch.object(CON, "print_status_line"), patch("mono_imager.flash_orchestrator.detect_host_ip", return_value="h"), \
     patch.object(CON, "show_flash_confirmation", side_effect=conf), patch("builtins.print"), \
     patch("builtins.input", side_effect=[OPN_LAN, "", str(BZ)]):
    app2.menu_network_auto_config()
check("known device IP shown in the confirmation; escape -> Flash menu",
      cap.get("device_ip") == "192.168.168.222 (dhcp)" and app2.current_state == MenuState.FLASH_AUTO_OR_MANUAL)
app = make_app(); app.serial_port = "/dev/ttyUSB0"
r, t, a = auto(app, [OPN_LAN, "", str(BZ), ""], confirm=False)
check("confirmation declined -> MAIN", app.current_state == MenuState.MAIN and "Cancelled" in t)

# ----------------------------------------------------------------------------
section("menu_network_flashing")
for res, want in ((None, "kept"), (True, True), (False, False)):
    app = make_app(); app.flash_success = "kept"
    with patch("mono_imager.flash_orchestrator.run_flash_journey", return_value=res) as rj:
        app.menu_network_flashing()
    check(f"journey result {res} -> flash_success {want}, DONE", app.flash_success == want
          and app.current_state == MenuState.DONE and rj.call_args.args[6]() is None)

# ----------------------------------------------------------------------------
section("menu_update_emmc / menu_update_nor")
for meth, runner in (("menu_update_emmc", "run_emmc_update"), ("menu_update_nor", "run_nor_update")):
    app = make_app()
    with patch.object(app, "_select_port", return_value=None):
        drive(getattr(app, meth), [], *QUIET)
    check(f"{meth}: no port -> MAIN", app.current_state == MenuState.MAIN)
    app = make_app()
    with patch.object(app, "_select_port", return_value="p"), \
         patch(f"mono_imager.recovery_orchestrator.{runner}") as run:
        r, t, a = drive(getattr(app, meth), ["n", ""], *QUIET)
    check(f"{meth}: not confirmed -> cancelled, nothing written", not run.called and "Cancelled" in t)
    for ok in (True, False):
        app = make_app()
        with patch.object(app, "_select_port", return_value="p"), \
             patch(f"mono_imager.recovery_orchestrator.{runner}") as run, \
             patch("mono_imager.recovery_orchestrator.print_report", return_value=ok):
            r, t, a = drive(getattr(app, meth), ["y", ""], *QUIET)
        check(f"{meth}: confirmed, result {ok} -> runner called, result kept",
              run.called and run.call_args.args[0] == "p" and app.flash_success is ok
              and app.current_state == MenuState.MAIN)
        if meth == "menu_update_emmc":
            check(f"  eMMC update: DIP-flip instruction only on success ({ok})", ("FLIP DIP SWITCH TO LEFT" in t) is ok)

# ----------------------------------------------------------------------------
section("menu_test_serial / menu_test_lan / menu_test_usb_mount")
for meth, diag in (("menu_test_serial", "test_serial"), ("menu_test_usb_mount", "test_usb")):
    app = make_app()
    with patch.object(app, "_select_port", return_value=None):
        drive(getattr(app, meth), [], *QUIET)
    check(f"{meth}: no port -> MAIN", app.current_state == MenuState.MAIN)
    for ok in (True, False):
        app = make_app()
        with patch.object(app, "_select_port", return_value="/dev/ttyUSB1"), \
             patch(f"mono_imager.diagnostics.{diag}", return_value=ok):
            drive(getattr(app, meth), [], *QUIET)
        check(f"{meth}: test {ok} -> port remembered: {ok}", (app.serial_port == "/dev/ttyUSB1") is ok)
    app = make_app(); app.serial_port = "/dev/ttyS9"
    with patch(f"mono_imager.diagnostics.{diag}", return_value=True) as dg:
        drive(getattr(app, meth), [], *QUIET)
    check(f"{meth}: session port used without asking", dg.call_args.args[0] == "/dev/ttyS9")
app = make_app()
with patch.object(app, "_select_port", return_value=None):
    drive(app.menu_test_lan, [], *QUIET)
check("menu_test_lan: no port -> MAIN", app.current_state == MenuState.MAIN)
app = make_app()
with patch.object(app, "_select_port", return_value="p"), \
     patch("mono_imager.diagnostics.test_lan", return_value={"serial_port": "p", "host_ip": "h", "device_ip": "d"}) as tl:
    drive(app.menu_test_lan, [], *QUIET)
check("menu_test_lan: saved values kept", (app.serial_port, app.net_host_ip, app.net_device_ip) == ("p", "h", "d")
      and tl.call_args.args[4]() is None)
app = make_app(); app.serial_port = "s"
with patch("mono_imager.diagnostics.test_lan", return_value=None):
    drive(app.menu_test_lan, [], *QUIET)
check("menu_test_lan: failed test -> nothing saved", app.net_host_ip is None)

# ----------------------------------------------------------------------------
section("snapshot / restore menus")
STORE = tmpd / "store"
cfg = {}
SNAP = QUIET + [patch("mono_imager.config.load_config", side_effect=lambda: dict(cfg)),
                patch("mono_imager.config.save_config", side_effect=lambda c: cfg.update(c)),
                patch("mono_imager.flash_orchestrator.detect_host_ip", return_value="h")]
app = make_app()
with patch.object(app, "_select_port", return_value=None):
    r, t, a = drive(lambda: app._snapshot_common("T"), [], *SNAP)
check("no port -> None", r is None)
app = make_app(); app.serial_port = "p"
r, t, a = drive(lambda: app._snapshot_common("T"), ["3"], *SNAP)
check("Back -> None", r is None)
app = make_app(); app.serial_port = "p"
r, t, a = drive(lambda: app._snapshot_common("T"), ["1", ""], *SNAP[:3],
                patch("mono_imager.flash_orchestrator.detect_host_ip", return_value=None))
check("LAN, no host IP -> None", r is None and "host IP" in t)
app = make_app(); app.serial_port = "p"
r, t, a = drive(lambda: app._snapshot_common("T"), ["1", f'"{STORE}"'], *SNAP)
check("LAN: folder typed (quotes stripped) and remembered in the config",
      r == ("p", "lan", "h", STORE) and cfg.get("snapshot_dir") == str(STORE))
app = make_app(); app.serial_port = "p"
r, t, a = drive(lambda: app._snapshot_common("T"), ["1", ""], *SNAP)
check("LAN: Enter keeps the remembered folder", r[3] == STORE)
app = make_app()
with patch.object(app, "_select_port", return_value="q"):
    r, t, a = drive(lambda: app._snapshot_common("T"), ["2"], *SNAP)
check("USB: no host IP / folder question", r[0] == "q" and r[1] == "usb" and r[2] == "" and len(a) == 1
      and app.serial_port == "q")
app = make_app()
with patch.object(app, "_snapshot_common", return_value=None):
    app.menu_snapshot()
check("snapshot: back -> MAIN", app.current_state == MenuState.MAIN)
app = make_app()
with patch.object(app, "_snapshot_common", return_value=("p", "lan", "h", STORE)), \
     patch("mono_imager.snapshot_orchestrator.run_snapshot", return_value=True) as rs:
    drive(app.menu_snapshot, [""])
check("snapshot: run_snapshot with the picked values, result kept",
      rs.call_args.kwargs["store"] == STORE and rs.call_args.kwargs["transfer"] == "lan" and app.flash_success is True)
app = make_app()
with patch.object(app, "_snapshot_common", return_value=None):
    app.menu_restore()
check("restore: back -> MAIN", app.current_state == MenuState.MAIN)
app = make_app()
with patch.object(app, "_snapshot_common", return_value=("p", "lan", "h", STORE)), \
     patch("mono_imager.snapshot.list_snapshots", return_value=[]):
    r, t, a = drive(app.menu_restore, [""])
check("restore LAN: no snapshots -> message, MAIN", "No snapshots" in t and app.current_state == MenuState.MAIN)
snaps = [("prod", {"label": "prod"}), ("auto-1", {"label": "auto-1"})]
app = make_app()
with patch.object(app, "_snapshot_common", return_value=("p", "lan", "h", STORE)), \
     patch("mono_imager.snapshot.list_snapshots", return_value=snaps), \
     patch("mono_imager.snapshot_orchestrator.pick_snapshot", return_value=None):
    drive(app.menu_restore, [])
check("restore LAN: none picked -> MAIN", app.current_state == MenuState.MAIN)
for ans, auto_ in (("", True), ("n", False)):
    app = make_app()
    with patch.object(app, "_snapshot_common", return_value=("p", "lan", "h", STORE)), \
         patch("mono_imager.snapshot.list_snapshots", return_value=snaps), \
         patch("mono_imager.snapshot_orchestrator.pick_snapshot", return_value=1), \
         patch("mono_imager.snapshot_orchestrator.run_restore", return_value=True) as rr:
        drive(app.menu_restore, [ans, ""])
    check(f"restore LAN: picked #2, auto-snapshot answer {ans!r} -> {auto_}",
          rr.call_args.kwargs["folder"] == STORE / "auto-1" and rr.call_args.kwargs["manifest"] == snaps[1][1]
          and rr.call_args.kwargs["auto_snapshot"] is auto_)
app = make_app()
with patch.object(app, "_snapshot_common", return_value=("p", "usb", "", STORE)), \
     patch("mono_imager.snapshot_orchestrator.run_restore", return_value=False) as rr:
    drive(app.menu_restore, ["", ""])
check("restore USB: snapshot picked on the device (folder/manifest None)",
      rr.call_args.kwargs["folder"] is None and rr.call_args.kwargs["manifest"] is None and app.flash_success is False)

# ----------------------------------------------------------------------------
section("menu_done")
for osn, ok, needle in (("OPNsense", True, "do NOT flip to eMMC"), ("OpenWRT", True, "Then power-cycle"),
                        ("OpenWRT", False, "did not complete")):
    app = make_app(); app.flash_success, app.os_name = ok, osn
    r, t, a = drive(app.menu_done, [""])
    check(f"{osn} {'OK' if ok else 'failed'}: '{needle}', reset, MAIN",
          needle in t and app.flash_success is False and app.current_state == MenuState.MAIN)
app = make_app(); app.flash_success, app.os_name, app.log_file = True, "Armbian", tmpd / "x.log"
with patch.object(T, "verbose") as vb:
    r, t, a = drive(app.menu_done, [""])
check("with a log file: result + path logged and shown", "Report:" in t and any("Result: OK" in c.args[0] for c in vb.call_args_list))

# ----------------------------------------------------------------------------
section("menu_cli_console (raw serial pass-through)")
app = make_app()
with patch.object(app, "_select_port", return_value=None):
    drive(app.menu_cli_console, [], *QUIET)
check("no port -> MAIN", app.current_state == MenuState.MAIN)
dev = MagicMock()
dev.connect.return_value = False
app = make_app()
with patch.object(app, "_select_port", return_value="p"), patch("mono_imager.serial_device.SerialDevice", return_value=dev):
    r, t, a = drive(app.menu_cli_console, [""], *QUIET)
check("connect fails -> message, MAIN", "Failed to connect" in t and app.current_state == MenuState.MAIN)


class Stdin:
    def __init__(self, keys):
        self.keys = list(keys)

    def fileno(self):
        return 0

    def read(self, n):
        time.sleep(0.05)
        return self.keys.pop(0)


def console_run(keys, reads, drain_raises=False):
    dev = MagicMock()
    dev.connect.return_value = True
    seq = list(reads)

    def ra():
        if drain_raises and not hasattr(ra, "done"):
            ra.done = True
            raise OSError("drain")
        if seq:
            x = seq.pop(0)
            if isinstance(x, Exception):
                raise x
            return x
        return b""
    dev.safe_read_all.side_effect = ra
    stdout = io.StringIO()
    tm = _types.SimpleNamespace(tcgetattr=MagicMock(return_value="old"), tcsetattr=MagicMock(), TCSADRAIN=1)
    app = make_app()
    with patch.object(app, "_select_port", return_value="p"), patch("mono_imager.serial_device.SerialDevice", return_value=dev), \
         patch.object(T, "CONSOLE_DRAIN_SETTLE", 0), patch.object(T, "CONSOLE_PROMPT_SETTLE", 0), \
         patch.dict(sys.modules, {"termios": tm, "tty": _types.SimpleNamespace(setraw=MagicMock())}), \
         patch.object(T.sys, "stdin", Stdin(keys)), patch.object(T.sys, "stdout", stdout):
        r, t, a = drive(app.menu_cli_console, [""], *QUIET)
    return app, dev, stdout.getvalue(), tm, t


app, dev, shown, tm, t = console_run(["l", "s", T.CONSOLE_EXIT_KEY], [b"", b"root@recovery:~# "])
typed = [c.args[0] for c in dev.safe_write.call_args_list]
check("Enter sent for a fresh prompt, keys typed through, Ctrl+] ends", typed == [b"\r", b"l", b"s"])
check("device output relayed to the screen", "root@recovery" in shown)
check("terminal restored, port closed, MAIN", tm.tcsetattr.call_args.args[2] == "old" and dev.disconnect.called
      and "Console session ended" in t and app.current_state == MenuState.MAIN)
app, dev, shown, tm, t = console_run([T.CONSOLE_EXIT_KEY], [OSError("unplugged")], drain_raises=True)
check("drain error ignored; reader stops on a read error", app.current_state == MenuState.MAIN and dev.disconnect.called)

real_import = __import__


def no_tty(name, *a, **k):
    if name in ("tty", "termios"):
        raise ImportError(name)
    return real_import(name, *a, **k)


dev = MagicMock()
dev.connect.return_value = True
dev.safe_read_all.return_value = b""
ms = _types.SimpleNamespace(kbhit=MagicMock(side_effect=[False, True, True]), getwch=MagicMock(side_effect=["é", T.CONSOLE_EXIT_KEY]))
app = make_app()
with patch.object(app, "_select_port", return_value="p"), patch("mono_imager.serial_device.SerialDevice", return_value=dev), \
     patch.object(T, "CONSOLE_DRAIN_SETTLE", 0), patch.object(T, "CONSOLE_PROMPT_SETTLE", 0), \
     patch.dict(sys.modules, {"msvcrt": ms}), patch("builtins.__import__", side_effect=no_tty):
    drive(app.menu_cli_console, [""], *QUIET)
check("Windows (no tty/termios): msvcrt keys typed (UTF-8), Ctrl+] ends",
      [c.args[0] for c in dev.safe_write.call_args_list] == [b"\r", "é".encode()] and app.current_state == MenuState.MAIN)

# ----------------------------------------------------------------------------
section("device_stats")
app = make_app()
with patch.object(app, "_select_port", return_value=None):
    drive(app.device_stats, [], *QUIET)
check("no port -> MAIN", app.current_state == MenuState.MAIN)
for what, conn, diag, needle in (("connect fails", False, None, "Failed to connect"),
                                 ("no boot output", True, (None, None), "Timed out"),
                                 ("boot output parsed", True, ("U-Boot 2025.04\nModel: Mono", None), None)):
    dev = MagicMock()
    dev.connect.return_value = conn
    app = make_app()
    with patch.object(app, "_select_port", return_value="p"), patch("mono_imager.serial_device.SerialDevice", return_value=dev), \
         patch.object(T, "with_spinner", return_value=diag), patch.object(app, "_display_device_stats") as ds:
        r, t, a = drive(app.device_stats, ["", ""], *QUIET)
    check(f"{what} -> {'shown' if needle is None else needle}",
          (ds.called if needle is None else needle in t) and app.current_state == MenuState.MAIN)
app = make_app()
with patch.object(app, "_select_port", return_value="p"), \
     patch("mono_imager.serial_device.SerialDevice", side_effect=OSError("port busy")), patch.object(T, "verbose") as vb:
    r, t, a = drive(app.device_stats, [""], *QUIET)
check("error -> shown and logged, MAIN", "port busy" in t and vb.called and app.current_state == MenuState.MAIN)

# ----------------------------------------------------------------------------
section("_startup_network_setup")
app = make_app()
with patch.object(app, "_select_port", return_value=None):
    r, t, a = drive(app._startup_network_setup, [""], *QUIET)
check("no serial port -> message, exit(0)", r == ("exit", 0) and "No serial device found" in t)
app = make_app()
with patch.object(app, "_select_port", return_value="/dev/ttyUSB0"), \
     patch("serial.Serial", side_effect=__import__('serial').SerialException("[Errno 13] Permission denied")):
    r, t, a = drive(app._startup_network_setup, [], *QUIET)
check("port can't be opened -> sudo hint, exit(1)", r == ("exit", 1) and "sudo" in t)
app = make_app()
with patch.object(app, "_select_port", return_value="/dev/ttyUSB0"), patch("serial.Serial"), \
     patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=None):
    r, t, a = drive(app._startup_network_setup, [""], *QUIET)
check("no recovery shell -> postponed, port remembered", app.serial_port == "/dev/ttyUSB0" and "Could not reach" in t)
for ok in (False, True):
    d = MagicMock()
    app = make_app()
    with patch.object(app, "_select_port", return_value="/dev/ttyUSB0"), patch("serial.Serial"), \
         patch("mono_imager.flash_orchestrator.phase1_bootstrap", return_value=d), \
         patch.object(app, "_setup_recovery_network", return_value=ok):
        r, t, a = drive(app._startup_network_setup, [], *QUIET)
    check(f"network setup {ok} -> {'no warning' if ok else 'warning'}, port closed",
          ("Network setup was not completed" in t) is (not ok) and d.disconnect.called)

# ----------------------------------------------------------------------------
section("run() / main() / __main__")
for st, meth in ((MenuState.SNAPSHOT, "menu_snapshot"), (MenuState.RESTORE, "menu_restore")):
    app = make_app(); app.current_state = st
    with patch.object(app, "_startup_network_setup"), patch.object(app, meth, side_effect=KeyboardInterrupt) as m:
        r, t, a = drive(app.run, [])
    check(f"run() dispatches {st.value}; Ctrl-C -> 'Interrupted', exit(0)", m.called and r == ("exit", 0) and "Interrupted by user" in t)
with patch("mono_imager.logging_setup.configure_logging", return_value=tmpd / "l.log") as cl, \
     patch.object(T, "MonoImager") as M, patch.object(T, "verbose") as vb:
    T.main()
check("main(): logging set up, app built with the log file and run", cl.called and M.call_args.args == (tmpd / "l.log",)
      and M.return_value.run.called and "Log:" in vb.call_args_list[-1].args[0])
with patch("mono_imager.logging_setup.configure_logging", return_value=tmpd / "l.log"), \
     patch("mono_imager.config.detect_serial_ports", return_value=([], [])), patch("builtins.input", return_value=""), \
     patch("builtins.print"):
    try:
        runpy.run_module("mono_imager.tui", run_name="__main__")
        check("python -m mono_imager.tui runs main()", False)
    except SystemExit as e:
        check("python -m mono_imager.tui runs main() (no port -> exit 0)", e.code == 0)

import shutil as _sh
_sh.rmtree(tmpd, ignore_errors=True)


# ============================================================================
# Result
# ============================================================================

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed else 0)
