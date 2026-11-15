"""
mono-imager: TUI device console and statistics screens

Split out of tui.py. Mixed into MonoImager; module-level names are read
through mono_imager.tui (T.) so the existing patch points keep working.

Author:  H.A. Hermsen
License: GPLv3
"""

from mono_imager import tui as T


class TuiConsoleMixin:
    """TUI device console and statistics screens."""

    def menu_cli_console(self):
        """Serial console session — raw pass-through"""
        import threading

        from mono_imager.serial_device import SerialDevice

        self.print_screen_divider()
        print("  CLI Console — Serial")
        print()

        port = self._select_port(
            auto_select_single=True,
            show_categories=True,
            allow_back=True,
            allow_enter_last=True,
            save_on_select=True,
        )
        if port is None:
            self.current_state = T.MenuState.MAIN
            return

        self.print_screen_divider()
        print(f"  Connecting to {port} at {T.C.SERIAL_BAUD} baud...")
        print()

        d = SerialDevice(port, timeout=T.CONSOLE_READ_TIMEOUT)
        if not d.connect(T.C.SERIAL_BAUD):
            print(f"  ❌ Failed to connect to {port}")
            input("  Press Enter to continue...")
            self.current_state = T.MenuState.MAIN
            return

        print("  ✓ Connected — you are now in raw serial console.")
        print("  Type Ctrl+] to exit the console.")
        print()

        # Drain any leftover bytes from connect()'s own internal probe,
        # then request a fresh prompt so the screen isn't blank.
        try:
            T.time.sleep(T.CONSOLE_DRAIN_SETTLE)
            d.safe_read_all()
            T.time.sleep(T.CONSOLE_PROMPT_SETTLE)
            d.safe_write(b"\r")
        except Exception:
            pass

        stop_event = threading.Event()

        def reader():
            """Read from device, print to stdout"""
            while not stop_event.is_set():
                try:
                    data = d.safe_read_all()
                    if data:
                        T.sys.stdout.write(data.decode("utf-8", errors="replace"))
                        T.sys.stdout.flush()
                except Exception:
                    break

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        try:
            import termios
            import tty
            fd = T.sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setraw(fd)
            try:
                while True:
                    ch = T.sys.stdin.read(1)
                    if ch == T.CONSOLE_EXIT_KEY:
                        break
                    d.safe_write(ch.encode())
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except ImportError:
            # Windows — use msvcrt
            import msvcrt
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == T.CONSOLE_EXIT_KEY:
                        break
                    d.safe_write(ch.encode("utf-8", errors="replace"))

        stop_event.set()
        d.disconnect()
        print()
        print("  Console session ended.")
        input("  Press Enter to continue...")
        self.current_state = T.MenuState.MAIN

    def device_stats(self):
        """Query and display Mono Gateway hardware statistics"""
        from mono_imager.serial_device import SerialDevice

        self.print_screen_divider()

        print("  Select device to query:")
        print()
        port = self._select_port(
            auto_select_single=True,
            show_categories=True,
            allow_back=True,
            allow_enter_last=True,
            save_on_select=True,
        )
        if port is None:
            self.current_state = T.MenuState.MAIN
            return

        # Query device
        self.print_screen_divider()
        print(f"  Connecting to {port}...")
        print()

        try:
            device = SerialDevice(port, timeout=T.STATS_READ_TIMEOUT)
            if not device.connect():
                print("  ❌ Failed to connect")
                input("  Press Enter to continue...")
                self.current_state = T.MenuState.MAIN
                return

            print("=" * T.C.DIVIDER_WIDTH)
            print("  ⚡ POWER CYCLE YOUR DEVICE NOW ⚡")
            print("=" * T.C.DIVIDER_WIDTH)
            print()
            print("  Reading boot output (no need to interrupt autoboot)...")

            raw_output, _diag_err = T.with_spinner(
                device.capture_boot_diagnostics, timeout=T.STATS_CAPTURE_TIMEOUT,
                message="Reading boot diagnostics..."
            )
            device.disconnect()

            if raw_output is None:
                print()
                print("  ❌ Timed out waiting for boot output.")
                print("  Make sure the device was power-cycled after connecting.")
                input("  Press Enter to continue...")
                self.current_state = T.MenuState.MAIN
                return

            self._display_device_stats(raw_output)

        except Exception as e:
            T.verbose(f"Device stats query failed: {e}", "error")
            print(f"  ❌ Error: {e}")

        print()
        input("  Press Enter to continue...")
        self.current_state = T.MenuState.MAIN

    def _display_device_stats(self, raw_output: str):
        """Parse U-Boot boot-time diagnostics (domain), render via console."""
        T.console.show_device_stats(
            T.parse_uboot_identity(raw_output),
            T.parse_uboot_self_test(raw_output),
        )
