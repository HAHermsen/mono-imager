#!/usr/bin/env python3
"""
mono-imager: Serial I/O and boot-control layer
Provides UART autodetect, USB presence polling, U‑Boot automation, 
recovery boot handling, and firmware flashing utilities

Author:  H.A. Hermsen
License: GPLv3
"""

__author__ = "H.A. Hermsen"

import time
from typing import Any

import serial

from mono_imager.logging_setup import debug_enabled as _debug_enabled  # noqa: F401  (kept for callers/tests)
from mono_imager.serial_timing import logger  # noqa: F401  (re-export: tests patch SDM.logger)
from mono_imager import constants as C
from mono_imager import serial_timing as T
from mono_imager.serial_boot import BootMixin
from mono_imager.serial_scripts import ScriptMixin
from mono_imager.serial_timing import verbose


def _is_permission_error(exc) -> bool:
    """
    True if `exc` is an OS 'permission denied' opening the serial port
    (a Linux tty needs sudo or 'dialout' membership). pyserial wraps the
    OS error in SerialException, so check the type, errno 13, and message.
    """
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "errno", None) == T.EACCES:
        return True
    return "permission denied" in str(exc).lower()


class SerialDevice(ScriptMixin, BootMixin):
    """Wrapper for serial communication with Mono Gateway"""
    
    # Standard baud rates to try
    BAUD_RATES = C.SERIAL_BAUD_CANDIDATES
    
    # U-Boot prompt patterns
    UBOOT_PROMPTS = [b"=>", b"# "]
    
    # Recovery Linux prompt
    RECOVERY_PROMPT = b"root@recovery:~# "
    
    def __init__(self, port: str, timeout: float = T.DEFAULT_READ_TIMEOUT):
        """
        Initialize serial device
        
        Args:
            port: Serial port path (e.g., /dev/ttyUSB0, COM3)
            timeout: Read timeout in seconds
        """
        self.port = port
        self.timeout = timeout
        self.ser: Any = None  # SerialProxy once connected
        self.baud_rate = None
        # U-Boot env snapshot from the last phase1_uboot() call — see
        # flash_orchestrator.capture_uboot_env()/restore_uboot_env().
        self.captured_uboot_env: dict | None = None
        # Serial output of the last boot that wait_for_autoboot() caught
        # (U-Boot banner + power-on self-test) — for the snapshot inventory.
        self.last_boot_output: str | None = None
    
    def connect(self, baud_rate: int | None = None) -> bool:
        """
        Connect to device with automatic baud rate detection
        """
        if not self.wait_for_port(timeout=T.PORT_APPEAR_TIMEOUT):
            verbose(f"Device on {self.port} did not appear — cannot connect", "error")
            return False

        rates_to_try = [baud_rate] if baud_rate else self.BAUD_RATES

        for rate in rates_to_try:
            try:
                verbose(f"Attempting connection at {rate} baud...")

                # Create the REAL serial port
                real = serial.Serial(
                    port=self.port,
                    baudrate=rate,
                    timeout=self.timeout,
                    write_timeout=self.timeout
                )

                # Wrap it in the proxy
                self.ser = SerialProxy(self, real)

                # Test communication
                self.ser.write(b"\r\n")
                time.sleep(T.BAUD_PROBE_SETTLE)
                response = self.ser.read_all()

                if response:
                    verbose(f"Response at {rate} baud: {response[:C.LOG_TAIL_SHORT]}", "debug")

                if self._has_prompt(response) or response:
                    self.baud_rate = rate
                    verbose(f"✓ Connected at {rate} baud")
                    return True

                # Close real port if no response
                real.close()

            except serial.SerialException as e:
                verbose(f"Failed to connect at {rate} baud: {e}", "debug")
                continue

        verbose(f"Failed to connect to {self.port} at any baud rate", "error")
        return False

    def disconnect(self):
        """Close serial connection"""
        if self.ser and self.ser.is_open:
            self.ser.close()
            verbose("Serial connection closed")
            
    def _attempt_reconnect(self) -> bool:
        """
        Wait for port to reappear and reconnect at the last known baud rate.
        Warns explicitly if baud rate was never detected and falls back to 115200.
        """
        verbose("Attempting auto‑reconnect...")

        if not self.wait_for_port(timeout=T.RECONNECT_PORT_TIMEOUT):
            return False

        baud = self.baud_rate
        if baud is None:
            verbose("Baud rate was never successfully detected — falling back to 115200", "warning")
            baud = C.SERIAL_BAUD

        return self.connect(baud)
    
    def _has_prompt(self, response: bytes) -> bool:
        """Check if response contains a known prompt"""
        for prompt in self.UBOOT_PROMPTS + [self.RECOVERY_PROMPT]:
            if prompt in response:
                return True
        return False

    def probe_uboot_prompt(self, timeout: float = 2.0) -> bool:
        """
        Check whether the device is ALREADY sitting at the U-Boot prompt
        (autoboot already interrupted in a previous attempt this
        session), so phase1_uboot() can skip the "POWER CYCLE NOW" wait
        entirely instead of forcing a fresh power cycle every time.

        Safe because it can only ever SKIP THE WAIT — every step after
        it (journey-specific U-Boot commands, boot_recovery(),
        login_recovery()) still runs exactly as before; nothing
        downstream is skipped or altered by a True result here.

        Sends a bare Enter and checks for the U-Boot prompt marker
        ("=>") specifically, not any known prompt — a recovery Linux
        shell prompt also matches the bare "# " in UBOOT_PROMPTS via
        _has_prompt(), which would be a WRONG positive here (recovery
        Linux is past U-Boot, not at it), so this checks the literal
        "=>" bytes only.

        Returns:
            True if the U-Boot prompt is confirmed present right now,
            False otherwise (including on any read error — caller
            should fall back to the normal power-cycle wait, not
            assume anything).
        """
        if not self.ser or not self.ser.is_open:
            return False

        try:
            self.ser.reset_input_buffer()
            self.ser.write(b"\r\n")
        except serial.SerialException:
            return False

        start = time.time()
        response = b""
        while time.time() - start < timeout:
            try:
                chunk = self.ser.read(T.READ_PROMPT)
                if chunk:
                    response += chunk
                    if b"=>" in response:
                        return True
            except serial.SerialException:
                break

        return b"=>" in response

    def verify_recovery_shell(self, timeout: float = T.VERIFY_SHELL_TIMEOUT) -> bool:
        """
        Explicitly confirm the device is CURRENTLY sitting at the
        recovery shell prompt (root@recovery:~#), rather than assuming
        it from connect() succeeding or from manual timing.

        This exists because connect() only confirms SOME response came
        back at the time of connection — it does not confirm WHICH
        prompt. A device can be connected at the recovery shell when a
        test starts, then boot onward to its normal OS by the time a
        later command runs (or vice versa — already past recovery by
        the time a script connects at all). Scripts that assumed
        "connected" meant "at recovery shell" produced deeply
        misleading failures: e.g. run_script()'s byte-count
        verification correctly detected garbage (a full Armbian/
        systemd boot log, or a different login prompt) and refused to
        execute — which is the right safety behavior — but the
        resulting error looked like a script-write corruption bug,
        when the real cause was simply being in the wrong state
        entirely.

        Sends a bare Enter and checks whether RECOVERY_PROMPT
        specifically (not any U-Boot prompt, not a login prompt)
        appears in the response.

        Returns:
            True if the recovery shell prompt is confirmed present,
            False otherwise (caller should not proceed with
            run_script()/launch_script() calls if False — results
            will be meaningless).
        """
        if not self.ser or not self.ser.is_open:
            return False

        self.ser.reset_input_buffer()
        self.ser.write(b"\r\n")

        start = time.time()
        response = b""
        while time.time() - start < timeout:
            try:
                chunk = self.ser.read(T.READ_CMD)
                if chunk:
                    response += chunk
                    if self.RECOVERY_PROMPT in response:
                        return True
            except serial.SerialException:
                break

        return self.RECOVERY_PROMPT in response

    def _wait_for_line_idle(self, settle_time: float = T.LINE_SETTLE, max_wait: float = T.LINE_IDLE_MAX_WAIT) -> bool:
        """
        Block until the serial line has been silent (no incoming bytes)
        for at least `settle_time` seconds, or until `max_wait` elapses.

        Root cause this addresses: send_command() returns as soon as it
        SEES a recognized prompt pattern in the buffer, but the device
        can still be flushing trailing bytes (terminal redraw, shell
        echo of a previous heredoc, etc.) for a short window afterward.
        Firing the next command immediately can then race with those
        trailing bytes, corrupting or delaying the next exchange —
        confirmed on real hardware: a wc -c command immediately followed
        by sh <script> exec saw a 5-second gap before even the echo of
        the next command arrived, despite curl completing its HTTP
        request in 25ms once it did start.

        This is intentionally NOT a fixed sleep (the codebase already
        moved away from blind sleeps toward prompt/state-driven reads).
        It actively polls in_waiting and only returns once the line has
        genuinely gone quiet, or gives up after max_wait as a hard
        ceiling so a misbehaving device can't hang the caller forever.

        Args:
            settle_time: Required duration of silence (seconds) before
                considering the line idle.
            max_wait: Hard ceiling (seconds) — returns False if the line
                never goes quiet for settle_time within this window.

        Returns:
            True if the line went idle for settle_time within max_wait,
            False if max_wait was reached first (caller should proceed
            with caution — this is a ceiling, not a guarantee of safety,
            and the codebase has no purely-blocking alternative since
            the line legitimately may be busy with real device output).
        """
        start = time.time()
        quiet_since = None

        while time.time() - start < max_wait:
            try:
                waiting = self.ser.in_waiting
            except Exception:
                waiting = 0

            if waiting > 0:
                # Drain whatever is sitting there so it doesn't bleed
                # into the next command's read, and reset the quiet timer.
                try:
                    self.ser.read(waiting)
                except Exception:
                    pass
                quiet_since = None
            else:
                if quiet_since is None:
                    quiet_since = time.time()
                elif time.time() - quiet_since >= settle_time:
                    return True

            time.sleep(T.POLL_SLEEP)

        return False
    
    def send_command(self, command: str, wait_for_prompt: bool = True,
                    timeout: float | None = None) -> str:
        """
        Send a command and return the response.

        Reads are prompt-driven — returns as soon as a known prompt is seen,
        or when timeout expires. No fixed sleeps.

        Args:
            command: Command to send (without newline)
            wait_for_prompt: If True, return as soon as a known prompt appears
            timeout: Override default timeout (seconds)

        Returns:
            Response text, stripped and de-echoed

        Raises:
            RuntimeError: If serial connection is not open
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial connection not open")

        timeout = timeout or T.DEFAULT_CMD_TIMEOUT

        # Flush stale input before sending
        self.ser.reset_input_buffer()
        
        # Send command with newline
        verbose(f">> {command}", "debug")
        self.ser.write((command + "\r\n").encode())

        # Prompt-driven read — exit the moment a known prompt appears,
        # no fixed sleeps; serial.read() already blocks up to self.timeout
        response = b""
        start_time = time.time()
        matched_prompt = False
        while time.time() - start_time < timeout:
            try:
                chunk = self.ser.read(T.READ_CMD)
                if chunk:
                    response += chunk
                    if wait_for_prompt and self._has_prompt(response):
                        matched_prompt = True
                        break
            except serial.SerialException:
                break

        # CONFIRMED BUG THIS GUARDS AGAINST: _has_prompt() matches as soon
        # as a prompt-like substring appears ANYWHERE in the accumulated
        # response — e.g. a bare "# " that happens to occur mid-dump in a
        # long reply (a real-hardware printenv dump can contain "# " inside
        # a script-style env var). That fires this loop's break while the
        # device is still actively transmitting the rest of its reply
        # (including its own real trailing prompt). Without draining that
        # straggling output here, it sits in the OS serial buffer and
        # bleeds into whatever the NEXT command reads — confirmed on real
        # hardware: a leftover "...Environment size: .../8188 bytes\r\n=> "
        # from a preceding printenv call landed in boot_recovery()'s read
        # loop and was misread as "bootm failed — U-Boot prompt returned",
        # when the device was still genuinely mid-boot. Same race class
        # already fixed in run_script() via _wait_for_line_idle() — this
        # was the one call site (send_command(), used by capture_uboot_env())
        # still missing it.
        if matched_prompt:
            self._wait_for_line_idle(settle_time=T.LINE_SETTLE, max_wait=T.LINE_IDLE_SHORT_WAIT)

        response_str = self._clean_response(response, command)

        verbose(f"<< {response_str[:C.LOG_TAIL]}", "debug")
        return response_str


    


    


    def wait_for_port(self, timeout: float = T.PORT_WAIT_DEFAULT_TIMEOUT) -> bool:
        """
        Wait until the serial port appears (device plugged in or rebooted).

        A 'permission denied' open is NOT transient: retrying for the full
        timeout only hides it behind a misleading "did not appear" message
        (issue #15). Detect it and fail fast with the actionable fix.
        """
        verbose(f"Waiting for device on {self.port}...")

        start = time.time()
        while time.time() - start < timeout:
            try:
                # Try opening the port non‑blocking
                test = serial.Serial(self.port)
                test.close()
                verbose("✓ Device detected")
                return True
            except serial.SerialException as e:
                if _is_permission_error(e):
                    verbose(
                        f"Cannot open {self.port}: permission denied. On Linux, "
                        "run with sudo, or add your user to the 'dialout' group "
                        "(sudo usermod -a -G dialout $USER, then log out/in).",
                        "error",
                    )
                    return False
                time.sleep(T.PORT_POLL_SLEEP)

        verbose(
            f"Device on {self.port} did not appear within {timeout}s. If it is "
            "connected, power-cycle/reset the device (it may be stuck at a "
            "U-Boot or shell prompt) and try again.",
            "error",
        )
        return False

    def wait_for_any_output(self, timeout: float = T.OUTPUT_DEFAULT_TIMEOUT) -> bool:
        """
        Wait for the device to send ANY serial output at all, without
        caring what it is — boot log lines, a login prompt, a
        first-boot setup wizard, anything. Confirms the device is
        alive and producing output post-reboot.

        This exists because the previous post-reboot check
        (wait_for_port()) waited for the COM port to disappear and
        reappear, which assumes the USB-serial bridge itself power-
        cycles along with the device's OS. On this hardware, confirmed
        via live testing, that assumption is WRONG: the FTDI bridge is
        a separate chip from the SoC being reset and stays continuously
        enumerated across an OS-level reboot — the port never drops at
        all, so waiting for it to reappear always timed out, regardless
        of whether the device had actually rebooted successfully.

        What we actually care about — and the only thing mono-imager's
        job requires — is "did the new firmware boot and is it alive",
        not which specific state/prompt it landed in. A first-boot
        setup wizard, a normal login prompt, or active boot log
        scrolling by are all valid signs of life; what the new firmware
        does after that point is outside this tool's scope.
        """
        if not self.ser or not self.ser.is_open:
            return False

        verbose("Waiting for device to send any output after reboot...")
        start = time.time()
        while time.time() - start < timeout:
            try:
                chunk = self.ser.read(T.READ_BOOT)
                if chunk:
                    verbose(f"✓ Device responsive — received {len(chunk)} bytes")
                    return True
            except serial.SerialException:
                pass
            time.sleep(T.OUTPUT_POLL_SLEEP)

        verbose(f"No serial output received within {timeout}s", "error")
        return False

    def safe_write(self, data: bytes) -> int | None:
        """
        Write data to the serial port with auto‑reconnect on failure.

        Returns the number of bytes written (from serial.Serial.write()),
        or None on unrecoverable failure.

        NOTE: reaches through SerialProxy to self.ser._ser directly.
        This bypasses the proxy's delegation layer. Acceptable for now
        since safe_write is the only writer and the proxy is thin, but
        worth consolidating if SerialProxy grows.
        """
        try:
            return self.ser._ser.write(data)
        except Exception as e:
            verbose(f"Write failed ({e}) — attempting reconnect...", "warning")

            if not self._attempt_reconnect():
                return None

            try:
                return self.ser._ser.write(data)
            except Exception as e2:
                verbose(f"Retry write failed: {e2}", "error")
                return None
            
    def safe_read(self, size: int = 1) -> bytes:
        """
        Read from serial port with auto‑reconnect on failure.
        """
        try:
            return self.ser._ser.read(size)   # <-- REAL serial port
        except Exception as e:
            verbose(f"Read failed ({e}) — attempting reconnect...", "warning")

            if not self._attempt_reconnect():
                return b""

            try:
                return self.ser._ser.read(size)
            except Exception as e2:
                verbose(f"Retry read failed: {e2}", "error")
                return b""

    def safe_read_all(self) -> bytes:
        """
        Read all available bytes with auto‑reconnect on failure.
        """
        try:
            return self.ser._ser.read_all()
        except Exception as e:
            verbose(f"Read-all failed ({e}) — attempting reconnect...", "warning")

            if not self._attempt_reconnect():
                return b""

            try:
                return self.ser._ser.read_all()
            except Exception as e2:
                verbose(f"Retry read-all failed: {e2}", "error")
                return b""


class SerialProxy:
    def __init__(self, parent, ser):
        self._parent = parent   # SerialDevice instance
        self._ser = ser         # real serial.Serial object

    def write(self, data):
        return self._parent.safe_write(data)

    def read(self, size=1):
        return self._parent.safe_read(size)

    def read_all(self):
        return self._parent.safe_read_all()

    @property
    def in_waiting(self):
        try:
            return self._ser.in_waiting
        except Exception:
            try:
                self._parent._attempt_reconnect()
                return self._ser.in_waiting
            except Exception:
                return 0

    # Fallback: forward any unknown attribute to real serial object.
    # Guard against missing _ser to prevent infinite recursion:
    # if _ser is absent, __getattr__ would be called again to look it up,
    # triggering RecursionError. object.__getattribute__ bypasses __getattr__.
    def __getattr__(self, name):
        try:
            ser = object.__getattribute__(self, "_ser")
        except AttributeError:
            raise AttributeError(
                f"SerialProxy has no attribute {name!r} and _ser is not initialised"
            )
        return getattr(ser, name)
