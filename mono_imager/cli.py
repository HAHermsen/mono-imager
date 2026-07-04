#!/usr/bin/env python3
"""
mono-imager CLI entry point
"""

import sys
import os
import argparse


def main():
    """Main entry point for mono-imager"""
    parser = argparse.ArgumentParser(
        prog="mono-imager",
        description="Automated firmware flashing tool for Mono Gateway "
                     "Routers and the Mono Development Kit.",
    )
    parser.add_argument(
        "--debug", "--verbose",
        action="store_true",
        dest="debug",
        help="Print verbose console output — every serial command sent "
             "and received. Quiet by default; the log file always gets "
             "full detail regardless of this flag. Equivalent to setting "
             "MONO_DEBUG=1.",
    )
    args = parser.parse_args()

    if args.debug:
        # logging_setup.debug_enabled() reads MONO_DEBUG live on every
        # call, not once at import time — so strictly this could also be
        # set after importing mono_imager. It's set here anyway, before
        # anything else runs, so the very first log lines (module imports,
        # startup) are already covered rather than only calls made after
        # some later point in main().
        os.environ["MONO_DEBUG"] = "1"

    from mono_imager.tui import MonoImager
    from mono_imager.logging_setup import configure_logging
    from pathlib import Path

    log_file = configure_logging(Path(__file__).parent.parent / "logs")

    try:
        app = MonoImager(log_file)
        app.run()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
