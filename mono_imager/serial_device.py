#!/usr/bin/env python3
"""
mono-imager: Serial I/O and boot-control layer
Provides UART autodetect, USB presence polling, U‑Boot automation, 
recovery boot handling, and firmware flashing utilities

Author:  H.A. Hermsen
Version: v.0.9.9 RC1
License: MIT
"""

from mono_imager import __version__  # single source of truth: mono_imager/__init__.py
__author__ = "H.A. Hermsen"

import os
import serial
import time
import logging
import sys
from typing import Optional, List

logger = logging.getLogger(__name__)

_DEBUG = os.environ.get("MONO_DEBUG", "").lower() in ("1", "true", "yes")


def verbose(msg: str, level: str = "info"):
    """Log always; print to console only in debug mode or for errors/warnings."""
    if _DEBUG or level in ("error", "warning"):
        print(msg, flush=True)
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "debug":
        logger.debug(msg)
    else:
        logger.info(msg)


class SerialDevice:
    """Wrapper for serial communication with Mono Gateway"""
    
    # Standard baud rates to try
    BAUD_RATES = [115200, 9600, 57600, 38400]
    
    # U-Boot prompt patterns
    UBOOT_PROMPTS = [b"=>", b"# "]
    
    # Recovery Linux prompt
    RECOVERY_PROMPT = b"root@recovery:~# "
    
    def __init__(self, port: str, timeout: float = 10.0):
        """
        Initialize serial device
        
        Args:
            port: Serial port path (e.g., /dev/ttyUSB0, COM3)
            timeout: Read timeout in seconds
        """
        self.port = port
        self.timeout = timeout
        self.ser = None
        self.baud_rate = None
    
    def connect(self, baud_rate: Optional[int] = None) -> bool:
        """
        Connect to device with automatic baud rate detection
        """
        if not self.wait_for_port(timeout=30):
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
                time.sleep(0.5)
                response = self.ser.read_all()

                if response:
                    verbose(f"Response at {rate} baud: {response[:100]}", "debug")

                if self._has_prompt(response) or len(response) > 0:
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

        if not self.wait_for_port(timeout=20):
            return False

        baud = self.baud_rate
        if baud is None:
            verbose("Baud rate was never successfully detected — falling back to 115200", "warning")
            baud = 115200

        return self.connect(baud)
    
    def _has_prompt(self, response: bytes) -> bool:
        """Check if response contains a known prompt"""
        for prompt in self.UBOOT_PROMPTS + [self.RECOVERY_PROMPT]:
            if prompt in response:
                return True
        return False

    def verify_recovery_shell(self, timeout: float = 5.0) -> bool:
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
                chunk = self.ser.read(1024)
                if chunk:
                    response += chunk
                    if self.RECOVERY_PROMPT in response:
                        return True
            except serial.SerialException:
                break

        return self.RECOVERY_PROMPT in response

    def _wait_for_line_idle(self, settle_time: float = 0.3, max_wait: float = 5.0) -> bool:
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

            time.sleep(0.02)  # short poll interval, not a settling sleep

        return False
    
    def send_command(self, command: str, wait_for_prompt: bool = True,
                    timeout: Optional[float] = None) -> str:
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

        timeout = timeout or 5.0

        # Flush stale input before sending
        self.ser.reset_input_buffer()
        
        # Send command with newline
        verbose(f">> {command}", "debug")
        self.ser.write((command + "\r\n").encode())

        # Prompt-driven read — exit the moment a known prompt appears,
        # no fixed sleeps; serial.read() already blocks up to self.timeout
        response = b""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                chunk = self.ser.read(1024)
                if chunk:
                    response += chunk
                    if wait_for_prompt and self._has_prompt(response):
                        break
            except serial.SerialException:
                break
        
        response_str = response.decode('utf-8', errors='replace').strip()
        
        # Strip command echo (device echoes back what we sent).
        #
        # NOTE: for long commands, the terminal can line-wrap the echo at
        # its column width and in doing so DUPLICATE characters at the
        # wrap boundary (observed: "...8080..." became "...808" + \r\n +
        # "80..." — note the duplicated "8"). This means a wrapped echo
        # cannot be reliably reconstructed character-by-character; do not
        # attempt to "skip injected whitespace" here, it was tried and
        # disproven against real captured device output. For any command
        # at risk of exceeding the terminal width, use run_script() below
        # instead, which avoids the problem entirely by keeping the
        # echoed line short.
        if command and response_str.startswith(command):
            response_str = response_str[len(command):].strip()
        
        # Remove trailing prompt lines.
        #
        # Previously only matched bare U-Boot-style markers ("=>", "#"),
        # never the recovery shell's actual prompt ("root@recovery:~# ",
        # defined as RECOVERY_PROMPT above) — so recovery-shell command
        # output always carried a trailing "root@recovery:~#" line that
        # callers had to tolerate or strip themselves. Now strips lines
        # matching either bare U-Boot markers or the recovery prompt
        # (stripped of trailing whitespace, since splitlines() already
        # separates on \r\n and the trailing space in RECOVERY_PROMPT
        # would otherwise prevent an exact match).
        recovery_prompt_bare = self.RECOVERY_PROMPT.decode().strip()
        prompt_markers = ("=>", "#", recovery_prompt_bare)
        lines = [l for l in response_str.splitlines() if l.strip() not in prompt_markers and l.strip() != ""]
        
        # Remove duplicate lines (echo artifact)
        seen = []
        for line in lines:
            if line not in seen:
                seen.append(line)
        response_str = "\n".join(seen).strip()
        
        verbose(f"<< {response_str[:200]}", "debug")
        return response_str

    def run_script(self, script_body: str, marker: str = None,
                    write_timeout: float = 10.0, exec_timeout: float = 60.0) -> str:
        """
        Execute a (potentially long) shell command safely over a serial
        link that line-wraps and corrupts long echoed lines.

        Long single-line commands sent via send_command() can exceed the
        recovery shell's terminal width; the device's echo of that line
        then gets wrapped by the terminal, and the wrap can DUPLICATE
        characters at the boundary (confirmed: "8080" -> "808" + "\\r\\n"
        + "80"). That corruption makes the echo unrecoverable, and
        send_command()'s de-echo logic then returns the mangled echo
        instead of the command's real output.

        run_script() avoids the problem at its root: it writes the real
        (long) command to a temp file on the device via a quoted heredoc,
        verifies the file landed with the expected byte count, then runs
        that file with a SHORT invocation ("sh /tmp/mono_imager_NNNN.sh")
        whose echo can never wrap. Only that short command's output is
        parsed, so long/complex commands are no longer at risk.

        Args:
            script_body: The shell command(s) to run. Can be long/complex.
            marker: Optional unique string to name the temp script
                (default: derived from current time). Useful for log
                correlation across multiple run_script() calls.
            write_timeout: Timeout for the heredoc write step (seconds)
            exec_timeout: Timeout for executing the script (seconds)

        Returns:
            The script's real stdout/stderr, de-echoed and clean.

        Raises:
            RuntimeError: If serial connection is not open, or if the
                remote file write could not be verified (byte count
                mismatch), since proceeding on an unverified file write
                risks executing a corrupted/incomplete script.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial connection not open")

        marker = marker or str(int(time.time() * 1000))
        remote_path = f"/tmp/mono_imager_{marker}.sh"
        heredoc_tag = f"MONOIMAGER_EOF_{marker}"

        # Single-quoted heredoc tag ('TAG', not TAG) disables shell
        # expansion of $, `, \ inside the body — required here since
        # script_body may itself contain $variables or backticks
        # (e.g. curl's -w '%{http_code}') that must be written literally,
        # not expanded by the shell doing the writing.
        write_cmd = f"cat > {remote_path} <<'{heredoc_tag}'\n{script_body}\n{heredoc_tag}"

        verbose(f">> [run_script:write] {len(write_cmd)} bytes to {remote_path}", "debug")
        self.ser.reset_input_buffer()
        self.ser.write((write_cmd + "\r\n").encode())

        # We deliberately do NOT try to parse this response — the
        # heredoc write itself can still get its echo wrapped/corrupted
        # over serial, same as any long line. We don't need to parse it;
        # we only need the prompt to return, then we verify the file
        # independently via wc -c (a short command, safe to parse).
        # RAW bytes logged here (not just whether prompt was seen) so
        # any silent failure at this stage is visible, not inferred.
        start_time = time.time()
        write_response = b""
        while time.time() - start_time < write_timeout:
            try:
                chunk = self.ser.read(1024)
                if chunk:
                    write_response += chunk
                    if self._has_prompt(write_response):
                        break
            except serial.SerialException:
                break
        verbose(f"<< [run_script:write raw] {len(write_response)} bytes: {write_response[:300]!r}", "debug")

        # Real hardware showed this shell actively redrawing the heredoc
        # echo with ANSI escape sequences (cursor-up, clear-line) — i.e.
        # genuinely still rendering output after the prompt-match fired.
        # Same idle-wait applied here as before the exec step, for the
        # same reason: don't assume "prompt seen" means "device settled."
        self._wait_for_line_idle(settle_time=0.3, max_wait=5.0)

        # Verify the file landed with the expected byte count before
        # trusting it enough to execute. expected_size = body + newline
        # (the heredoc adds exactly one trailing \n after script_body
        # before the closing tag, from the f-string above).
        expected_size = len(script_body.encode()) + 1
        size_check = self.send_command(
            f"wc -c < {remote_path}", wait_for_prompt=True, timeout=5
        )
        verbose(f"<< [run_script:wc-c parsed] {size_check!r}", "debug")
        try:
            actual_size = int(size_check.strip().splitlines()[0].strip())
        except (ValueError, IndexError):
            raise RuntimeError(
                f"run_script(): could not verify remote file size for "
                f"{remote_path} — wc -c returned unparseable output: "
                f"{size_check!r}. Refusing to execute unverified script."
            )

        if actual_size != expected_size:
            raise RuntimeError(
                f"run_script(): remote file {remote_path} size mismatch — "
                f"expected {expected_size} bytes, got {actual_size}. "
                f"Heredoc write likely corrupted over serial. "
                f"Refusing to execute unverified script."
            )

        verbose(f"✓ run_script: verified {remote_path} = {actual_size} bytes", "debug")

        # ROOT CAUSE FIX: real hardware showed a 5-second gap between
        # sending "sh <script>" and even its OWN ECHO arriving — despite
        # curl (inside the script) completing its HTTP request in 25ms
        # once it did start. That points to the device/terminal still
        # settling from the previous wc -c exchange (line-editing shell
        # redraw, trailing bytes) when the next write hit the wire.
        #
        # Confirm the line is genuinely idle before sending the next
        # command, rather than assuming send_command()'s prompt-match
        # return meant the device was fully done. Not a fixed sleep —
        # actively polls in_waiting and only proceeds once truly quiet.
        line_was_idle = self._wait_for_line_idle(settle_time=0.3, max_wait=5.0)
        if not line_was_idle:
            logger.debug(
                "run_script: line did not settle within 5s before exec — "
                "proceeding anyway, but next read may race with trailing "
                "output from the wc -c step."
            )

        # Execute via a SHORT command — "sh /tmp/mono_imager_NNNN.sh" is
        # well under any realistic terminal width, so its echo cannot
        # wrap and de-echoing via send_command()'s simple startswith()
        # check works correctly.
        #
        # Raw bytes (not just the parsed result) are logged here so any
        # future regression is visible as ground truth, not inferred.
        exec_cmd = f"sh {remote_path}"
        verbose(f">> [run_script:exec] {exec_cmd}", "debug")

        # Bypass send_command's own reset_input_buffer() + opaque
        # parsing for this one call — read raw here first so we see
        # EXACTLY what arrives, then run the same string through the
        # normal de-echo path afterward for the actual return value.
        self.ser.reset_input_buffer()
        self.ser.write((exec_cmd + "\r\n").encode())

        exec_start = time.time()
        exec_response = b""
        while time.time() - exec_start < exec_timeout:
            try:
                chunk = self.ser.read(1024)
                if chunk:
                    exec_response += chunk
                    verbose(f"<< [run_script:exec raw chunk] {chunk!r}", "debug")
                    if self._has_prompt(exec_response):
                        break
            except serial.SerialException:
                break
        verbose(f"<< [run_script:exec raw total] {len(exec_response)} bytes: {exec_response!r}", "debug")

        exec_response_str = exec_response.decode('utf-8', errors='replace').strip()
        if exec_cmd and exec_response_str.startswith(exec_cmd):
            exec_response_str = exec_response_str[len(exec_cmd):].strip()
        recovery_prompt_bare = self.RECOVERY_PROMPT.decode().strip()
        prompt_markers = ("=>", "#", recovery_prompt_bare)
        lines = [l for l in exec_response_str.splitlines() if l.strip() not in prompt_markers and l.strip() != ""]
        seen = []
        for line in lines:
            if line not in seen:
                seen.append(line)
        result = "\n".join(seen).strip()
        verbose(f"<< [run_script:exec parsed] {result!r}", "debug")

        # Best-effort cleanup; failure to remove the temp file is not
        # fatal to the caller and is not worth raising over.
        try:
            self.send_command(f"rm -f {remote_path}", wait_for_prompt=True, timeout=5)
        except Exception as e:
            verbose(f"run_script: cleanup of {remote_path} failed (non-fatal): {e}", "debug")

        return result

    def launch_script(self, script_body: str, marker: str = None,
                       write_timeout: float = 10.0) -> str:
        """
        Write a script to the device and START it running, WITHOUT
        waiting for or parsing any serial response from the exec step.

        This exists because waiting for a script's result over serial
        echo was found to be intermittently unreliable on real
        hardware — roughly a 50% failure rate across many real test
        runs, with the device's own action (e.g. curl succeeding,
        confirmed via the host's HTTP access log) genuinely completing
        but its output never arriving back over the serial read within
        any reasonable timeout. No single root cause was confirmed
        despite testing several theories (terminal line-wrapping,
        shell line-editor interference, stdout buffering).

        Once a TCP/IP link to the device is confirmed working (which,
        in every test run all session, has been 100% reliable), the
        device should report its OWN results back via a second HTTP
        request to the host's server (see _FirmwareHandler /report
        endpoint and wait_for_report() in flash_orchestrator.py),
        rather than the host trying to read results back over serial.
        launch_script() is the serial half of that pattern: it does
        the SAME proven-reliable heredoc-write + byte-verification as
        run_script() (only the final exec-readback step has been
        flaky, not the write/verify), then fires the exec command and
        returns immediately without blocking on its serial output.

        Args:
            script_body: The shell command(s) to run. Should itself
                include a curl call back to the host's /report
                endpoint if the caller needs to know the result —
                this method does not return one.
            marker: Optional unique string to name the temp script.
            write_timeout: Timeout for the heredoc write step (seconds)

        Returns:
            The remote script path that was launched (for logging /
            debugging — not a result of the script's execution).

        Raises:
            RuntimeError: If serial connection is not open, or if the
                remote file write could not be verified (byte count
                mismatch) — same safety check as run_script(), since
                launching an unverified/corrupted script is still
                unsafe even though we don't wait for its output.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial connection not open")

        marker = marker or str(int(time.time() * 1000))
        remote_path = f"/tmp/mono_imager_{marker}.sh"
        heredoc_tag = f"MONOIMAGER_EOF_{marker}"

        write_cmd = f"cat > {remote_path} <<'{heredoc_tag}'\n{script_body}\n{heredoc_tag}"

        verbose(f">> [launch_script:write] {len(write_cmd)} bytes to {remote_path}", "debug")
        self.ser.reset_input_buffer()
        self.ser.write((write_cmd + "\r\n").encode())

        start_time = time.time()
        write_response = b""
        while time.time() - start_time < write_timeout:
            try:
                chunk = self.ser.read(1024)
                if chunk:
                    write_response += chunk
                    if self._has_prompt(write_response):
                        break
            except serial.SerialException:
                break
        verbose(f"<< [launch_script:write raw] {len(write_response)} bytes: {write_response[:300]!r}", "debug")

        self._wait_for_line_idle(settle_time=0.3, max_wait=5.0)

        # Same byte-count verification as run_script() — still refuse
        # to launch an unverified/corrupted script, even though we
        # won't wait for its output afterward.
        expected_size = len(script_body.encode()) + 1
        size_check = self.send_command(
            f"wc -c < {remote_path}", wait_for_prompt=True, timeout=5
        )
        verbose(f"<< [launch_script:wc-c parsed] {size_check!r}", "debug")
        try:
            actual_size = int(size_check.strip().splitlines()[0].strip())
        except (ValueError, IndexError):
            raise RuntimeError(
                f"launch_script(): could not verify remote file size for "
                f"{remote_path} — wc -c returned unparseable output: "
                f"{size_check!r}. Refusing to launch unverified script."
            )

        if actual_size != expected_size:
            raise RuntimeError(
                f"launch_script(): remote file {remote_path} size mismatch "
                f"— expected {expected_size} bytes, got {actual_size}. "
                f"Heredoc write likely corrupted over serial. "
                f"Refusing to launch unverified script."
            )

        verbose(f"✓ launch_script: verified {remote_path} = {actual_size} bytes", "debug")

        self._wait_for_line_idle(settle_time=0.3, max_wait=5.0)

        # Fire the exec command and DO NOT wait for or parse its
        # response — that's the entire point of this method. The
        # caller is expected to learn the result via a separate
        # TCP/IP channel (e.g. wait_for_report()), not from this
        # method's return value.
        exec_cmd = f"sh {remote_path}"
        verbose(f">> [launch_script:exec, fire-and-forget] {exec_cmd}", "debug")
        self.ser.reset_input_buffer()
        self.ser.write((exec_cmd + "\r\n").encode())

        # DRAIN, don't parse: launch_script() previously returned
        # immediately after writing the exec command, never reading a
        # single byte back. Comparing against run_script() (proven
        # 100% reliable across 5 real hardware runs via
        # test_run_script_reliability.py --mode step09) — the one
        # structural difference is that run_script() ALWAYS actively
        # reads in a loop right after firing exec, while launch_script()
        # never did. A real hardware test of the EXACT SAME simplified
        # script via launch_script() still failed (report never
        # arrived), ruling out script complexity as the cause and
        # pointing at this difference instead.
        #
        # Theory: with nothing reading the serial port, the device's
        # UART transmit side can fill up (command echo + shell prompt
        # fragments + the script's own output all queue up with nowhere
        # to drain) and block — which can stall the device's shell
        # process entirely, including curl calls that haven't even
        # started yet. A short drain here (read and discard for a
        # couple seconds) gives the device somewhere to write to,
        # without reintroducing the original problem this method
        # exists to avoid: we still don't PARSE this content or trust
        # it for the result — the result still comes via TCP/IP
        # (wait_for_report()), not from here.
        drain_start = time.time()
        drained = 0
        while time.time() - drain_start < 2.0:
            try:
                chunk = self.ser.read(1024)
                if chunk:
                    drained += len(chunk)
            except serial.SerialException:
                break
        verbose(f"<< [launch_script:post-exec drain] {drained} bytes discarded", "debug")

        return remote_path
    
    def wait_for_autoboot(self, timeout: float = 30) -> bool:
        """
        Wait for U-Boot autoboot countdown and auto-interrupt

        Args:
            timeout: Max time to wait for autoboot message

        Returns:
            True if interrupted and U-Boot prompt reached, False otherwise
        """
        verbose("Waiting for U-Boot autoboot countdown...")

        start_time = time.time()
        buffer = b""

        while time.time() - start_time < timeout:
            try:
                # Read one byte at a time so we trigger ASAP
                byte = self.ser.read(1)
                if byte:
                    buffer += byte

                    if b"Hit any key to stop autoboot" in buffer:
                        verbose("✓ Detected autoboot — interrupting and polling for prompt...")

                        # Send interrupt keypress and poll for U-Boot prompt
                        # rather than blindly spamming for a fixed duration
                        interrupt_start = time.time()
                        interrupt_timeout = 5.0
                        interrupt_buf = b""

                        while time.time() - interrupt_start < interrupt_timeout:
                            self.ser.write(b" ")
                            chunk = self.ser.read(64)
                            if chunk:
                                interrupt_buf += chunk
                                if b"=>" in interrupt_buf:
                                    verbose("✓ U-Boot prompt confirmed")
                                    return True

                        logger.error(
                            f"U-Boot prompt not seen within {interrupt_timeout}s after interrupt — "
                            f"last bytes: {repr(interrupt_buf[-60:])}"
                        )
                        return False

            except serial.SerialException:
                break

        verbose("Autoboot countdown not detected within timeout", "warning")
        return False

    def capture_boot_diagnostics(self, timeout: float = 30) -> Optional[str]:
        """
        Capture U-Boot's boot-time diagnostic output (SoC/board identity,
        clock configuration, and the power-on self-test block with
        voltages/temperatures/fan speed) WITHOUT interrupting autoboot.

        Everything printed before "Hit any key to stop autoboot" on this
        hardware (confirmed from a real boot capture — SoC/Model/DRAM/Clock
        Configuration lines, then a self-test block of "[ OK ]" and "[FAIL]"
        lines). This just reads until that marker appears, then returns
        everything captured before it — no interrupt, no recovery boot needed.

        Args:
            timeout: Max time to wait for the autoboot marker

        Returns:
            The captured boot text (str) if the autoboot marker was
            seen, or None if the timeout was hit first.
        """
        verbose("Capturing U-Boot diagnostic output (waiting for boot)...")

        start_time = time.time()
        buffer = b""

        while time.time() - start_time < timeout:
            try:
                chunk = self.ser.read(4096)
                if chunk:
                    buffer += chunk
                    if b"Hit any key to stop autoboot" in buffer:
                        verbose("✓ Captured boot diagnostics output")
                        return buffer.decode("utf-8", errors="replace")
            except serial.SerialException:
                break

        logger.warning(
            f"Boot diagnostics capture timed out after {timeout}s — "
            f"never saw autoboot marker. Captured {len(buffer)} bytes so far."
        )
        return None

    def interrupt_autoboot(self) -> bool:
        """
        Interrupt U-Boot autoboot countdown (manual)
        
        Returns:
            True if successfully interrupted, False otherwise
        """
        verbose("Interrupting U-Boot autoboot...")
        
        # Send multiple spaces/enters to interrupt
        for _ in range(5):
            self.ser.write(b" ")
            time.sleep(0.1)
        
        time.sleep(0.5)
        response = self.ser.read_all().decode('utf-8', errors='replace')
        
        if self._has_prompt(response.encode()):
            verbose("✓ U-Boot interrupted")
            return True
        
        verbose("Failed to interrupt autoboot", "warning")
        return False
    
    def boot_recovery(self) -> bool:
        """
        Boot into recovery Linux from U-Boot and auto-login as root (no password).

        Accepts any Linux login prompt and any root shell — the recovery Linux
        hostname may differ from "recovery" depending on the firmware build.

        Returns:
            True if a root shell was reached, False otherwise
        """
        verbose("Booting into recovery Linux...")

        try:
            verbose("Sending 'run recovery' command...", "debug")
            self.ser.write(b"run recovery\r\n")

            poll_start = time.time()
            poll_timeout = 120.0
            poll_buf = b""
            _last_log_len = 0

            while time.time() - poll_start < poll_timeout:
                chunk = self.ser.read(256)
                if chunk:
                    poll_buf += chunk
                    tail = poll_buf.decode("utf-8", errors="replace")

                    # Periodic debug: show what the device is sending
                    if len(poll_buf) - _last_log_len > 2048:
                        verbose(f"  Boot output: {tail[-300:]!r}", "debug")
                        _last_log_len = len(poll_buf)

                    # U-Boot prompt returned — check why before failing.
                    if b"\n=> " in poll_buf or b"\r=> " in poll_buf:
                        if b"not defined" in poll_buf:
                            # 'recovery' env var missing (e.g. Armbian U-Boot on
                            # eMMC boot0, or NOR env wiped). Fall back to running
                            # the default bootcmd via 'boot'. On Armbian U-Boot
                            # this loads Armbian via extlinux.conf; on the old SPI
                            # U-Boot it falls through to the SPI recovery kernel.
                            verbose("  'recovery' not defined — falling back to 'boot' (bootcmd)...", "warning")
                            poll_buf = b""
                            self.ser.write(b"boot\r\n")
                            continue
                        verbose("✗ bootm failed — U-Boot prompt returned", "error")
                        verbose(f"  Output: {tail[-500:]!r}", "error")
                        return False

                    # Any Linux login prompt (hostname may vary)
                    if " login:" in tail:
                        for line in tail.splitlines():
                            if " login:" in line:
                                verbose(f"✓ Login prompt: '{line.strip()}' — logging in as root...")
                                break
                        self.ser.write(b"root\r\n")
                        time.sleep(0.5)
                        self.ser.write(b"\r\n")
                        time.sleep(0.5)
                        response = self.ser.read_all().decode("utf-8", errors="replace")
                        if "root@" in response and "#" in response:
                            verbose("✓ Recovery Linux booted and logged in")
                            return True
                        verbose(f"Login response: {response!r}", "warning")
                        return False

                    # Already at root shell (auto-login or no password)
                    if "root@" in tail and "#" in tail:
                        verbose("✓ Recovery Linux booted (auto-logged in)")
                        return True

            verbose(
                f"✗ Recovery boot timed out after {poll_timeout}s — "
                f"last output: {poll_buf.decode('utf-8', errors='replace')[-300:]!r}",
                "error"
            )
            logger.warning(
                f"Recovery boot prompt not seen within {poll_timeout}s — "
                f"last {len(poll_buf)} bytes: {repr(poll_buf[-120:])}"
            )
            return False

        except Exception as e:
            verbose(f"Failed to boot recovery: {e}", "error")
            return False
    
    def boot_linux_staging(self) -> bool:
        """
        Boot into a staging Linux environment (e.g., Armbian on eMMC) by
        issuing 'boot' at the U-Boot prompt and waiting for any login prompt.

        Used when the 'recovery' U-Boot env variable has been lost (e.g.,
        after 'env default -a') and the recovery Linux cannot be booted via
        'run recovery'. Armbian on eMMC has the same tools (curl, dd, ip)
        as recovery Linux and serves as an equivalent staging environment.

        The caller's U-Boot step must have already set bootcmd to the right
        target (e.g., sysboot mmc 0:1 ... extlinux.conf) and saved it to NOR
        before this is called — 'boot' here just executes that bootcmd.
        """
        verbose("Booting staging Linux (issuing 'boot' at U-Boot prompt)...")
        try:
            self.ser.write(b"boot\r\n")

            poll_start = time.time()
            poll_timeout = 120.0
            poll_buf = b""

            while time.time() - poll_start < poll_timeout:
                chunk = self.ser.read(256)
                if chunk:
                    poll_buf += chunk
                    tail = poll_buf.decode("utf-8", errors="replace")

                    # Auto-login: already at root shell
                    if "root@" in tail and "#" in tail:
                        verbose("✓ Staging Linux booted (auto-logged in as root)")
                        return True

                    if "login:" in tail:
                        verbose("✓ Staging Linux login prompt — logging in as root...")
                        self.ser.write(b"root\r\n")
                        time.sleep(1.0)
                        resp = self.ser.read(512).decode("utf-8", errors="replace")

                        if "root@" in resp and "#" in resp:
                            verbose("✓ Logged in to staging Linux (empty password)")
                            return True

                        if "Password" in resp or "password" in resp.lower():
                            # Try empty password first
                            self.ser.write(b"\r\n")
                            time.sleep(0.5)
                            r2 = self.ser.read(256).decode("utf-8", errors="replace")
                            if "root@" in r2 and "#" in r2:
                                verbose("✓ Logged in to staging Linux (empty password)")
                                return True

                            # Fall back to Armbian default: 1234
                            self.ser.write(b"1234\r\n")
                            time.sleep(0.5)
                            r3 = self.ser.read(256).decode("utf-8", errors="replace")
                            if "root@" in r3 and "#" in r3:
                                verbose("✓ Logged in to staging Linux (password: 1234)")
                                return True

                            verbose(
                                "Staging login failed — neither empty password nor '1234' worked",
                                "warning"
                            )
                            return False

                        if "#" in resp:
                            verbose("✓ Logged in to staging Linux")
                            return True

            logger.warning(
                f"Staging Linux boot prompt not seen within {poll_timeout}s — "
                f"last {len(poll_buf)} bytes: {repr(poll_buf[-120:])}"
            )
            return False

        except Exception as e:
            verbose(f"Failed to boot staging Linux: {e}", "error")
            return False

    def login_staging(self, timeout: float = 30) -> bool:
        """
        Confirm we are at a root shell in the staging Linux environment.
        Checks for any 'root@<hostname>#' prompt — not locked to 'root@recovery'.
        Called after boot_linux_staging() in the staging-boot path.
        """
        verbose("Verifying staging Linux login...")

        start_time = time.time()
        attempt = 0
        backoff = 0.5

        while time.time() - start_time < timeout:
            attempt += 1
            try:
                if not self.ser or not self.ser.is_open:
                    verbose("Serial disconnected — waiting for device to reappear...", "warning")
                    if not self.wait_for_port(timeout=10):
                        time.sleep(min(backoff, 5.0))
                        backoff = min(backoff * 2, 5.0)
                        continue
                    self.connect(self.baud_rate or 115200)

                self.ser.write(b"\r\n")
                time.sleep(0.3)
                response = self.ser.read_all().decode("utf-8", errors="replace")

                if "root@" in response and "#" in response:
                    verbose(f"✓ Logged into staging Linux shell (attempt {attempt})")
                    return True

                if self.RECOVERY_PROMPT in response.encode():
                    verbose(f"✓ Recovery Linux available (attempt {attempt})")
                    return True

                verbose(f"Staging login attempt {attempt} — retrying in {backoff:.1f}s", "debug")
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

            except Exception as e:
                verbose(f"Staging login attempt {attempt} failed: {e}", "debug")
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

        verbose(f"Failed to verify staging Linux login after {attempt} attempts", "error")
        return False

    def login_recovery(self, timeout: float = 30) -> bool:
        """
        Verify we are logged into recovery Linux (root@recovery prompt).
        Handles the case where boot_recovery already auto-logged in.
        
        Returns:
            True if root@recovery prompt confirmed, False otherwise
        """
        verbose("Verifying recovery Linux login...")

        start_time = time.time()
        attempt = 0
        backoff = 0.5

        while time.time() - start_time < timeout:
            attempt += 1
            try:
                # If serial dropped, try to reconnect
                if not self.ser or not self.ser.is_open:
                    verbose("Serial disconnected — waiting for device to reappear...", "warning")
                    if not self.wait_for_port(timeout=10):
                        time.sleep(min(backoff, 5.0))
                        backoff = min(backoff * 2, 5.0)
                        continue
                    self.connect(self.baud_rate or 115200)

                # Send Enter to get a prompt
                self.ser.write(b"\r\n")
                time.sleep(0.3)
                response = self.ser.read_all().decode("utf-8", errors="replace")

                if "root@" in response and "#" in response:
                    verbose(f"✓ Logged into recovery Linux (attempt {attempt})")
                    return True

                verbose(f"Login attempt {attempt} — retrying in {backoff:.1f}s", "debug")
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

            except Exception as e:
                verbose(f"Login attempt {attempt} failed: {e} — retrying in {backoff:.1f}s", "debug")
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

        verbose(f"Failed to verify recovery Linux login after {attempt} attempts", "error")
        return False

               
    def wait_for_port(self, timeout: float = 30.0) -> bool:
        """
        Wait until the serial port appears (device plugged in or rebooted).
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
            except serial.SerialException:
                time.sleep(0.5)

        verbose(f"Device on {self.port} did not appear within {timeout}s", "error")
        return False

    def wait_for_any_output(self, timeout: float = 60.0) -> bool:
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
                chunk = self.ser.read(256)
                if chunk:
                    verbose(f"✓ Device responsive — received {len(chunk)} bytes")
                    return True
            except serial.SerialException:
                pass
            time.sleep(0.2)

        verbose(f"No serial output received within {timeout}s", "error")
        return False

    def safe_write(self, data: bytes) -> Optional[int]:
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
            # auto‑reconnect
            self._parent._attempt_reconnect()
            return self._ser.in_waiting

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