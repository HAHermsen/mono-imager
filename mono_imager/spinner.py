#!/usr/bin/env python3
"""
mono-imager: Shared terminal progress spinner.

De-facto standard progress indicator for the entire application —
any blocking call (serial I/O, downloads, flashing, etc.) that needs
visual feedback during a wait should use with_spinner() from this
module rather than reimplementing spinner logic locally.

Design constraints:
  - Spinner runs in its own thread, purely for visual feedback. It
    never touches the actual operation being waited on, and adds no
    measurable delay to its timing.
  - Braille frames + green color are used only after confirming the
    terminal can actually render them — legacy Windows console code
    pages (437/850) cannot display Braille, and ANSI color codes only
    render correctly on Windows if Virtual Terminal Processing has
    been enabled on that console (not guaranteed by default on every
    configuration). Both checks run once at import time and fail
    safe to plain ASCII / no color rather than ever risk printing
    garbled escape codes or boxes into the user's terminal.

Author:  H.A. Hermsen
Version: 0.4.0
License: MIT
"""

import sys
import os
import time
import threading
from typing import Any, Callable, Tuple, Optional


def _try_enable_windows_vt_processing() -> bool:
    """
    On Windows, ANSI escape codes (used for color) only render correctly
    if "Virtual Terminal Processing" is enabled on the console — it is
    NOT guaranteed on by default on every Windows configuration, even on
    modern builds. This attempts to enable it via the Win32 API.

    Returns True if the attempt succeeded (or we're not on Windows, in
    which case it's not needed), False if it failed — caller should
    disable color output on False rather than risk printing raw garbled
    escape codes into the terminal.
    """
    if os.name != "nt":
        return True  # not Windows, ANSI generally works (Linux/macOS terminals)

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if not kernel32.SetConsoleMode(handle, new_mode):
            return False
        return True
    except Exception:
        # Any failure here (missing ctypes, no console attached, etc.)
        # means we cannot safely assume color will render — fall back.
        return False


def _try_encode_braille() -> bool:
    """
    Confirms the current stdout encoding can actually represent Braille
    spinner characters before committing to using them. Legacy Windows
    console code pages (437/850) cannot render these and would print
    garbled boxes/question marks instead.

    Returns True if safe to use Braille frames, False if we should fall
    back to plain ASCII frames instead.
    """
    try:
        test_char = "⠋"
        encoding = sys.stdout.encoding or "utf-8"
        test_char.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# Resolved once at import time — cheap checks, and the result doesn't
# change mid-run, so no need to re-check on every spinner frame.
_COLOR_SUPPORTED = _try_enable_windows_vt_processing()
_BRAILLE_SUPPORTED = _try_encode_braille()

_GREEN = "\x1b[32m" if _COLOR_SUPPORTED else ""
_RESET = "\x1b[0m" if _COLOR_SUPPORTED else ""


class Spinner:
    """
    Spinner for the terminal, running in its own thread purely for
    visual feedback. Does NOT touch whatever operation it's wrapping,
    or any timing-sensitive code — it only animates a character in
    place while a blocking call runs on the caller's behalf in a
    worker thread (see with_spinner() below for the common usage
    pattern; using Spinner directly as a context manager is also
    supported for custom wrapping needs).

    Uses Braille frames + green color when the terminal can safely
    support them (checked once at module load), and falls back to
    plain ASCII / no color otherwise — never assumes support and
    risks printing garbled output.
    """
    BRAILLE_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    ASCII_FRAMES = ["|", "/", "-", "\\"]

    FRAMES = BRAILLE_FRAMES if _BRAILLE_SUPPORTED else ASCII_FRAMES

    def __init__(self, message: str):
        self.message = message
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        # Clear the spinner line
        sys.stdout.write("\r" + " " * (len(self.message) + 4) + "\r")
        sys.stdout.flush()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r{self.message} {_GREEN}{frame}{_RESET}")
            sys.stdout.flush()
            i += 1
            time.sleep(0.15)


def with_spinner(fn: Callable, *args, message: str = "Working", **kwargs) -> Tuple[Any, Optional[Exception]]:
    """
    Run any blocking callable on a worker thread while a Spinner
    animates on the main thread. The wrapped call's actual execution
    and timing are entirely unaffected — this only gives the terminal
    something to show instead of sitting silent during a wait.

    This is the standard way to add spinner feedback to any blocking
    operation in the application (serial I/O, downloads, flashing,
    etc.) — wrap the call here rather than reimplementing the
    threading/spinner boilerplate at each call site.

    Args:
        fn: The blocking callable to run (e.g. a SerialDevice method).
        *args: Positional arguments to pass to fn.
        message: Text shown next to the spinner.
        **kwargs: Keyword arguments to pass to fn.

    Returns:
        (result, error) — exactly one will be None. If fn raised an
        exception, it is captured in error rather than re-raised
        immediately, so the spinner can clean up its terminal line
        first; callers should check error and handle/re-raise as
        appropriate for their context.

    Example:
        result, error = with_spinner(
            device.run_script, "echo hello", marker="diag",
            message="Waiting for device response"
        )
        if error is not None:
            raise error
    """
    box = {"result": None, "error": None}

    def worker():
        try:
            box["result"] = fn(*args, **kwargs)
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    with Spinner(message):
        t.start()
        t.join()

    return box["result"], box["error"]
