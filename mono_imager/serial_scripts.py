"""
mono-imager: SerialDevice script execution — write a script to the device
over a verified heredoc and run it (run_script) or start it in the
background (launch_script). Mixed into SerialDevice (serial_device.py).

Author:  H.A. Hermsen
License: GPLv3
"""


import time
from typing import TYPE_CHECKING, Any

import serial

from mono_imager import constants as C
from mono_imager import serial_timing as T
from mono_imager.serial_timing import logger, verbose


class ScriptMixin:
    """run_script / launch_script and their helpers (needs SerialDevice's
    ser, send_command, _has_prompt, _wait_for_line_idle, RECOVERY_PROMPT)."""

    if TYPE_CHECKING:  # what SerialDevice provides; keeps mypy honest about the mixin
        ser: Any
        baud_rate: int | None
        RECOVERY_PROMPT: bytes

        def connect(self, baud_rate: int | None = None) -> bool: ...
        def wait_for_port(self, timeout: float = ...) -> bool: ...
        def _has_prompt(self, response: bytes) -> bool: ...
        def _wait_for_line_idle(self, settle_time: float = ..., max_wait: float = ...) -> bool: ...
        def send_command(self, command: str, wait_for_prompt: bool = True, *args: Any, **kwargs: Any) -> Any: ...

    def _write_verified_script(self, script_body: str, marker: str | None,
                               write_timeout: float, who: str, verb: str) -> str:
        """
        Write `script_body` to /tmp/mono_imager_<marker>.sh on the device via a
        quoted heredoc and verify its byte count with `wc -c`. Returns the
        remote path. `who` (the calling method) and `verb` ("execute" /
        "launch") only shape the log and error text.

        Raises:
            RuntimeError: serial not open, or the size cannot be verified.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial connection not open")

        marker = marker or str(int(time.time() * T.MS_PER_S))
        remote_path = f"/tmp/mono_imager_{marker}.sh"
        heredoc_tag = f"MONOIMAGER_EOF_{marker}"

        # Single-quoted heredoc tag ('TAG', not TAG) disables shell
        # expansion of $, `, \ inside the body — required here since
        # script_body may itself contain $variables or backticks
        # (e.g. curl's -w '%{http_code}') that must be written literally,
        # not expanded by the shell doing the writing.
        write_cmd = f"cat > {remote_path} <<'{heredoc_tag}'\n{script_body}\n{heredoc_tag}"

        verbose(f">> [{who}:write] {len(write_cmd)} bytes to {remote_path}", "debug")
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
                chunk = self.ser.read(T.READ_CMD)
                if chunk:
                    write_response += chunk
                    if self._has_prompt(write_response):
                        break
            except serial.SerialException:
                break
        verbose(f"<< [{who}:write raw] {len(write_response)} bytes: {write_response[:C.LOG_TAIL_LONG]!r}", "debug")

        # Real hardware showed this shell actively redrawing the heredoc
        # echo with ANSI escape sequences (cursor-up, clear-line) — i.e.
        # genuinely still rendering output after the prompt-match fired.
        # Same idle-wait applied here as before the exec step, for the
        # same reason: don't assume "prompt seen" means "device settled."
        # A long heredoc keeps echoing after the prompt pattern first shows
        # (confirmed on hardware with a 13 KB script): big scripts get
        # proportionally longer to go quiet. Small scripts: unchanged.
        self._wait_for_line_idle(settle_time=T.LINE_SETTLE,
                                 max_wait=max(T.LINE_IDLE_MAX_WAIT, len(write_cmd) / T.ECHO_BYTES_PER_S))

        # Verify the file landed with the expected byte count before
        # trusting it enough to execute. expected_size = body + newline
        # (the heredoc adds exactly one trailing \n after script_body
        # before the closing tag, from the f-string above).
        expected_size = len(script_body.encode()) + 1
        size_check = self.send_command(
            f"wc -c < {remote_path}", wait_for_prompt=True, timeout=C.SHELL_QUICK_TIMEOUT
        )
        verbose(f"<< [{who}:wc-c parsed] {size_check!r}", "debug")
        try:
            actual_size = int(size_check.strip().splitlines()[0].strip())
        except (ValueError, IndexError):
            raise RuntimeError(
                f"{who}(): could not verify remote file size for "
                f"{remote_path} — wc -c returned unparseable output: "
                f"{size_check!r}. Refusing to {verb} unverified script."
            )

        if actual_size != expected_size:
            raise RuntimeError(
                f"{who}(): remote file {remote_path} size mismatch — "
                f"expected {expected_size} bytes, got {actual_size}. "
                f"Heredoc write likely corrupted over serial. "
                f"Refusing to {verb} unverified script."
            )

        verbose(f"✓ {who}: verified {remote_path} = {actual_size} bytes", "debug")
        return remote_path

    def run_script(self, script_body: str, marker: str = None,
                    write_timeout: float = T.SCRIPT_WRITE_TIMEOUT, exec_timeout: float = C.SCRIPT_EXEC_TIMEOUT) -> str:
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
        remote_path = self._write_verified_script(script_body, marker, write_timeout,
                                                  "run_script", "execute")

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
        line_was_idle = self._wait_for_line_idle(settle_time=T.LINE_SETTLE, max_wait=T.LINE_IDLE_MAX_WAIT)
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

        exec_response = self._read_until_prompt(exec_timeout)
        verbose(f"<< [run_script:exec raw total] {len(exec_response)} bytes: {exec_response!r}", "debug")

        result = self._clean_response(exec_response, exec_cmd)
        verbose(f"<< [run_script:exec parsed] {result!r}", "debug")

        # Best-effort cleanup; failure to remove the temp file is not
        # fatal to the caller and is not worth raising over.
        try:
            self.send_command(f"rm -f {remote_path}", wait_for_prompt=True, timeout=C.SHELL_QUICK_TIMEOUT)
        except Exception as e:
            verbose(f"run_script: cleanup of {remote_path} failed (non-fatal): {e}", "debug")

        return result

    def _read_until_prompt(self, timeout: float) -> bytes:
        """Raw bytes until a shell/U-Boot prompt shows up (or `timeout`); each chunk is logged."""
        exec_start = time.time()
        exec_response = b""
        while time.time() - exec_start < timeout:
            try:
                chunk = self.ser.read(T.READ_CMD)
                if chunk:
                    exec_response += chunk
                    verbose(f"<< [run_script:exec raw chunk] {chunk!r}", "debug")
                    if self._has_prompt(exec_response):
                        break
            except serial.SerialException:
                break
        return exec_response

    def _clean_response(self, exec_response: bytes, exec_cmd: str) -> str:
        """De-echo `exec_cmd`, drop prompt-only and blank lines, and de-duplicate lines."""
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
        return result

    def launch_script(self, script_body: str, marker: str = None,
                       write_timeout: float = T.SCRIPT_WRITE_TIMEOUT) -> str:
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
        remote_path = self._write_verified_script(script_body, marker, write_timeout,
                                                  "launch_script", "launch")

        self._wait_for_line_idle(settle_time=T.LINE_SETTLE, max_wait=T.LINE_IDLE_MAX_WAIT)

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
                chunk = self.ser.read(T.READ_CMD)
                if chunk:
                    drained += len(chunk)
            except serial.SerialException:
                break
        verbose(f"<< [launch_script:post-exec drain] {drained} bytes discarded", "debug")

        return remote_path
