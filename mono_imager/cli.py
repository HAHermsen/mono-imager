#!/usr/bin/env python3
"""
mono-imager CLI entry point
"""

import sys
from mono_imager.tui import MonoImager


def main():
    """Main entry point for mono-imager"""
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
