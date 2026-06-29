"""
mono-imager: Logging initialisation — single source of truth.

Call configure_logging(log_file) exactly once at process startup
(from tui.py's main() or cli.py's main()).

flash_orchestrator.py previously called logging.basicConfig(force=True)
at module import time. tui.py's main() called it again with different
handlers. Whichever imported second silently nuked the first one's
handler config, dropping either file or console output depending on
import order.

Fix: neither module calls basicConfig. tui.py/cli.py call
configure_logging() once at startup. flash_orchestrator.py calls
get_log_file() to find the path for its report footer.
"""


import sys
import logging
from pathlib import Path
from datetime import datetime

_log_file: Path = None


def configure_logging(log_dir: Path = None) -> Path:
    """
    Initialise logging exactly once.

    Sets up two destinations:
      - Root logger → file only (DEBUG level, timestamped format)
        Captures everything including serial byte traces.
      - mono_imager.console logger → stdout only (INFO level, plain)
        User-facing progress messages — no timestamps, no level tags.

    Returns the log file path.
    """
    global _log_file

    if _log_file is not None:
        return _log_file  # already configured — no-op

    if log_dir is None:
        log_dir = Path.cwd() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file = log_dir / f"mono_imager_{timestamp}.log"

    # Root logger: file only, DEBUG, full detail
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[logging.FileHandler(_log_file, encoding="utf-8")],
        force=True,
    )

    # Console logger: stdout only, INFO, plain — no duplication with root
    console = logging.getLogger("mono_imager.console")
    console.setLevel(logging.INFO)
    console.propagate = False
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    console.addHandler(handler)

    return _log_file


def get_log_file() -> Path:
    """Return the current log file path, or None if not yet configured."""
    return _log_file
