"""
mono-imager: Flash-side helpers of the TUI: firmware path, soft reboot, port choice, flash confirm and recovery finish

Split out of tui.py. Mixed into MonoImager; module-level names are read
through mono_imager.tui (T.) so the existing patch points keep working.

Author:  H.A. Hermsen
License: GPLv3
"""

from typing import TYPE_CHECKING, Any, Optional

from mono_imager import tui as T


class TuiFlashMixin:
    """Flash-side helpers of the TUI: firmware path, soft reboot, port choice, flash confirm and recovery finish."""

    if TYPE_CHECKING:  # attributes this mixin reads from MonoImager / sibling mixins
        MIN_FIRMWARE_SIZE_BYTES: int
        serial_port: str | None
        def safe_input(self, prompt: str) -> str | None: ...
        def print_screen_divider(self, *args: Any, **kwargs: Any) -> Any: ...

    def _validate_firmware_path(self, raw_input: str):
        """
        Validate a user-typed firmware path. Strips surrounding quotes
        (Windows users naturally type quoted paths with spaces).
        Returns (Path, None) on success, or (None, error_message) on
        failure — error_message is ready to print directly.

        Checks performed: non-empty input, path exists, is a file (not
        a directory), is actually readable, and is at least
        MIN_FIRMWARE_SIZE_BYTES. Does NOT check file contents/format —
        that's intentionally out of scope.
        """
        raw = raw_input.strip().strip('"').strip("'")
        if not raw:
            return None, "No path entered."

        path = T.Path(raw).expanduser()

        if not path.exists():
            return None, f"File not found: {path}"

        if path.is_dir():
            return None, f"That's a folder, not a file: {path}"

        if not path.is_file():
            return None, f"Not a regular file: {path}"

        try:
            size = path.stat().st_size
        except OSError as e:
            return None, f"Can't read file info: {e}"

        if size == 0:
            return None, f"File is empty (0 bytes): {path}"

        if size < self.MIN_FIRMWARE_SIZE_BYTES:
            size_kb = size / T.C.KIB
            return None, (
                f"File is only {size_kb:.0f} KB — too small to be a real "
                f"firmware image. Check the path is correct."
            )

        # Confirm it's actually readable (catches permission issues
        # before they surface mid-flash as a confusing curl/dd error).
        try:
            with open(path, "rb") as f:
                f.read(1)
        except OSError as e:
            return None, f"Can't read file: {e}"

        return path, None

    def _soft_reboot_if_possible(self, port: str):
        """
        Best-effort: if the device is already reachable over serial right
        now (e.g. left sitting in a recovery Linux shell or at the U-Boot
        prompt from earlier this session), trigger its reboot over the
        wire instead of making the user physically power-cycle it.

        CONFIRMED GAP THIS CLOSES: phase1_uboot()'s own skip-the-wait
        check (probe_uboot_prompt()) only fires when the device happens
        to already be sitting AT the U-Boot prompt. If it's one step
        further along — in a Linux shell, as it typically is right after
        a previous recovery-shell operation — that check can't help, and
        every caller of phase1_bootstrap() fell back to asking for a
        manual power cycle even though a plain `reboot` typed into that
        shell would do the exact same job over the wire.

        Purely best-effort and silent: if the device isn't reachable this
        way (freshly powered off, wrong state, whatever), this does
        nothing and phase1_uboot()'s "POWER CYCLE NOW" prompt is still
        there as the fallback — this can only ever remove an unnecessary
        physical step, never break the flow that already existed.
        """
        from mono_imager.serial_device import SerialDevice
        try:
            _d = SerialDevice(port, timeout=T.SOFT_REBOOT_READ_TIMEOUT)
            if _d.connect(T.C.SERIAL_BAUD):
                _d.ser.write(b"\r\nreset\r\nreboot\r\n")
                T.time.sleep(T.SOFT_REBOOT_SETTLE)
                _d.disconnect()
        except Exception:
            pass

    def _select_port(
        self,
        *,
        auto_select_single: bool = False,
        show_categories: bool = False,
        allow_back: bool = True,
        allow_enter_last: bool = False,
        save_on_select: bool = False,
        quiet: bool = False,
    ) -> Optional[str]:
        """
        Detect serial ports, list them, and prompt for a selection.
        Returns the chosen device string, or None if detection failed,
        no ports found, user chose Back, or input was invalid.

        quiet=True suppresses this method's own "Press Enter to
        continue..." messaging on detection failure / no ports found,
        for callers (e.g. startup network detection) that show their
        own message and decide what happens next themselves — avoids
        stacking two different prompts back to back.
        """
        from mono_imager.config import detect_serial_ports, get_last_port, save_last_port

        try:
            known, other = detect_serial_ports()
            all_ports = known + other
        except Exception as e:
            if not quiet:
                print(f"  ❌ Port detection failed: {e}")
                input("  Press Enter to continue...")
            return None

        if not all_ports:
            if not quiet:
                print("  ❌ No serial devices found. Connect the USB-to-UART cable and try again.")
                input("  Press Enter to continue...")
            return None

        if auto_select_single and len(all_ports) == 1:
            port = all_ports[0].device
            print(f"  Auto-selected: {port} ({all_ports[0].description})")
            return port

        last_port = get_last_port()

        if show_categories:
            if known:
                print("  USB-UART adapters (recommended):")
                for i, p in enumerate(known, 1):
                    marker = " ◄ last used" if p.device == last_port else ""
                    print(f"    {i}) {p.device} — {p.description}{marker}")
            if other:
                if known:
                    print()
                print("  Other ports:")
                offset = len(known)
                for i, p in enumerate(other, offset + 1):
                    marker = " ◄ last used" if p.device == last_port else ""
                    print(f"    {i}) {p.device} — {p.description}{marker}")
        else:
            for i, p in enumerate(all_ports, 1):
                marker = " ◄ last used" if p.device == last_port else ""
                print(f"  {i}) {p.device} — {p.description}{marker}")

        if allow_back:
            print()
            print(f"  {len(all_ports) + 1}) Back")

        enter_uses_last = allow_enter_last and last_port and any(p.device == last_port for p in all_ports)
        if enter_uses_last:
            print()
            print(f"  [Enter] Use last port ({last_port})")
        print()

        total = len(all_ports) + (1 if allow_back else 0)
        choice = input(f"Select [1-{total}]: ").strip()

        if enter_uses_last and choice == "":
            if save_on_select:
                save_last_port(last_port)
            return last_port

        try:
            idx = int(choice) - 1
            if allow_back and idx == len(all_ports):
                return None
            if not (0 <= idx < len(all_ports)):
                raise ValueError("out of range")
            port_device = all_ports[idx].device
            if save_on_select:
                save_last_port(port_device)
            return port_device
        except ValueError:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            return None

    def _show_flash_confirmation(
        self,
        *,
        os_name: str,
        port: str,
        firmware_path,
        flash_target: str,
        host_ip: str,
        device_ip: str,
    ) -> Optional[bool]:
        """
        Delegate rendering to console; map its ESCAPE sentinel to the
        None-plus-state-transition contract the callers here expect.
        """
        result = T.console.show_flash_confirmation(
            os_name=os_name, port=port, firmware_path=firmware_path,
            flash_target=flash_target, host_ip=host_ip, device_ip=device_ip,
        )
        if result is T.console.ESCAPE:
            self.current_state = T.MenuState.FLASH_AUTO_OR_MANUAL
            return None
        return result

    def _recovery_finish(self, success: bool) -> None:
        """Set flash result, wait for Enter, and return to main menu."""
        self.flash_success = success
        # Drain buffered stdin (stray newlines from the serial session /
        # the piped 'yes' the flash uses) so this Enter actually blocks.
        # Without it a leftover '\n' satisfies input() instantly and the
        # report is wiped by the next clear_screen() before it can be read.
        try:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
        except ImportError:
            try:
                import termios
                termios.tcflush(T.sys.stdin, termios.TCIFLUSH)
            except Exception:
                pass
        input("  Press Enter to continue...")
        self.current_state = T.MenuState.MAIN

    def _show_firmware_output(self, chunk: str) -> None:
        T.console.show_firmware_output(chunk)
