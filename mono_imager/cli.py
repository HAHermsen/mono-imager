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
        # Must be set before the first `import mono_imager` anywhere in
        # this process. serial_device.py and flash_orchestrator.py each
        # read MONO_DEBUG once, at their own import time, into a
        # module-level _DEBUG constant — and mono_imager/__init__.py
        # imports serial_device eagerly. Setting the env var here, before
        # mono_imager.tui is imported below, is what makes both pick up
        # the flag instead of a stale (unset) env snapshot.
        os.environ["MONO_DEBUG"] = "1"

    from mono_imager.tui import MonoImager

    try:
        app = MonoImager()
        app.run()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
