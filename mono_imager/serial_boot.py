"""
mono-imager: SerialDevice boot control — catch autoboot, boot recovery
Linux or the eMMC OS, and log in. Mixed into SerialDevice (serial_device.py).

Author:  H.A. Hermsen
License: GPLv3
"""

import time
from typing import TYPE_CHECKING, Any

import serial

from mono_imager import constants as C
from mono_imager import serial_timing as T
from mono_imager.serial_timing import logger, verbose


class BootMixin:
    """Autoboot interrupt, recovery / staging boot and login (needs
    SerialDevice's ser, connection helpers and prompt handling)."""

    if TYPE_CHECKING:  # what SerialDevice provides; keeps mypy honest about the mixin
        ser: Any
        baud_rate: int | None
        RECOVERY_PROMPT: bytes

        def connect(self, baud_rate: int | None = None) -> bool: ...
        def wait_for_port(self, timeout: float = ...) -> bool: ...
        def _has_prompt(self, response: bytes) -> bool: ...
        def _wait_for_line_idle(self, settle_time: float = ..., max_wait: float = ...) -> bool: ...
        def send_command(self, command: str, wait_for_prompt: bool = True, *args: Any, **kwargs: Any) -> Any: ...

    def wait_for_autoboot(self, timeout: float = T.AUTOBOOT_DEFAULT_TIMEOUT) -> bool:
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
                        self.last_boot_output = buffer.decode("utf-8", errors="replace")

                        # Send interrupt keypress and poll for U-Boot prompt
                        # rather than blindly spamming for a fixed duration
                        interrupt_start = time.time()
                        interrupt_timeout = T.INTERRUPT_PROMPT_TIMEOUT
                        interrupt_buf = b""

                        while time.time() - interrupt_start < interrupt_timeout:
                            self.ser.write(b" ")
                            chunk = self.ser.read(T.READ_PROMPT)
                            if chunk:
                                interrupt_buf += chunk
                                if b"=>" in interrupt_buf:
                                    verbose("✓ U-Boot prompt confirmed")
                                    return True

                        logger.error(
                            f"U-Boot prompt not seen within {interrupt_timeout}s after interrupt — "
                            f"last bytes: {interrupt_buf[-T.LOG_RAW_TAIL:]!r}"
                        )
                        return False

            except serial.SerialException:
                break

        verbose("Autoboot countdown not detected within timeout", "warning")
        return False

    def capture_boot_diagnostics(self, timeout: float = T.DIAG_DEFAULT_TIMEOUT) -> str | None:
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
                chunk = self.ser.read(T.READ_DIAG)
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
        for _ in range(T.INTERRUPT_KEY_COUNT):
            self.ser.write(b" ")
            time.sleep(T.INTERRUPT_KEY_GAP)
        
        time.sleep(T.INTERRUPT_SETTLE)
        response = self.ser.read_all().decode('utf-8', errors='replace')
        
        if self._has_prompt(response.encode()):
            verbose("✓ U-Boot interrupted")
            return True
        
        verbose("Failed to interrupt autoboot", "warning")
        return False

    def boot_recovery(self, boot_medium: str | None = None) -> bool:
        """
        Boot into recovery Linux from U-Boot and auto-login as root (no password).

        Accepts any Linux login prompt and any root shell — the recovery Linux
        hostname may differ from "recovery" depending on the firmware build.

        boot_medium: if set ("nor" or "emmc"), the kernel is booted with
            boot_medium=<value> appended to bootargs. The device's modern
            `firmware update` tool reads this to know which medium it booted
            from (and flashes the OTHER one); without it the tool aborts with
            "Cannot detect boot medium" because this board's `recovery` U-Boot
            command sets no bootargs of its own. Confirmed on real hardware
            (U-Boot 2025.04). Default None keeps the original behaviour for
            every other caller (OS flash, LAN test, etc.) unchanged.

        Returns:
            True if a root shell was reached, False otherwise
        """
        verbose("Booting into recovery Linux...")

        try:
            verbose("Sending 'run recovery' command...", "debug")
            # Flush stale bytes before sending. Without this, leftover
            # output from a previous command (e.g. capture_uboot_env()'s
            # printenv, whose own trailing "=> " prompt can still be
            # unread in the OS serial buffer if send_command() returned
            # early on a false prompt match) gets swept into poll_buf
            # below and falsely triggers the "bootm failed" check —
            # confirmed on real hardware: the "failure" output was
            # actually a stale printenv tail + old prompt, not a real
            # bootm failure. Every other command-sending method in this
            # class already does this (send_command, probe_uboot_prompt,
            # verify_recovery_shell, _stream_command); this was the one
            # missing it.
            #
            # BELT AND SUSPENDERS: reset_input_buffer() only discards bytes
            # already sitting in the OS buffer at this instant — it cannot
            # stop a still-transmitting previous command (e.g. printenv)
            # from delivering MORE straggling bytes a moment later, which
            # would land right back in poll_buf below. Confirm the line is
            # genuinely silent first (same _wait_for_line_idle() pattern
            # used elsewhere in this file for this exact race), then reset,
            # so nothing still in flight can sneak in between the flush and
            # the write.
            self._wait_for_line_idle(settle_time=T.LINE_SETTLE, max_wait=T.LINE_IDLE_SHORT_WAIT)
            self.ser.reset_input_buffer()

            self._prime_uboot_prompt()
            self._set_recovery_boot_medium(boot_medium)
            self.ser.write(b"run recovery\r\n")

            poll_start = time.time()
            poll_timeout = T.BOOT_POLL_TIMEOUT
            poll_buf = b""
            _last_log_len = 0

            while time.time() - poll_start < poll_timeout:
                chunk = self.ser.read(T.READ_BOOT)
                if chunk:
                    poll_buf += chunk
                    tail = poll_buf.decode("utf-8", errors="replace")

                    # Periodic debug: show what the device is sending
                    if len(poll_buf) - _last_log_len > T.BOOT_LOG_EVERY_BYTES:
                        verbose(f"  Boot output: {tail[-C.LOG_TAIL_LONG:]!r}", "debug")
                        _last_log_len = len(poll_buf)

                    # U-Boot prompt returned — check why before failing.
                    if b"\n=> " in poll_buf or b"\r=> " in poll_buf:
                        if self._recovery_returned_to_uboot(poll_buf, tail) == "retry":
                            poll_buf = b""
                            continue
                        return False

                    # Any Linux login prompt (hostname may vary)
                    if " login:" in tail:
                        return self._login_root_at_prompt(tail)

                    # Already at root shell (auto-login or no password)
                    if "root@" in tail and "#" in tail:
                        verbose("✓ Recovery Linux booted (auto-logged in)")
                        return True

            verbose(
                f"✗ Recovery boot timed out after {poll_timeout}s — "
                f"last output: {poll_buf.decode('utf-8', errors='replace')[-C.LOG_TAIL_LONG:]!r}",
                "error"
            )
            logger.warning(
                f"Recovery boot prompt not seen within {poll_timeout}s — "
                f"last {len(poll_buf)} bytes: {poll_buf[-T.LOG_RAW_TAIL_LONG:]!r}"
            )
            return False

        except Exception as e:
            verbose(f"Failed to boot recovery: {e}", "error")
            return False

    def _prime_uboot_prompt(self) -> None:
        """Absorb the byte(s) U-Boot eats right after autoboot is interrupted."""
        # PRIME THE PROMPT before sending the real command. CONFIRMED
        # ON HARDWARE: immediately after autoboot is interrupted, U-Boot
        # eats the first byte(s) written to it while it's still settling
        # the prompt — "run recovery" arrived as "un recovery" -> Unknown
        # command 'un'. Sending a bare newline first absorbs that dropped
        # leading character on an empty line (a stray newline at "=> "
        # just yields another "=> ", harmless), then we flush and send
        # the actual command onto a confirmed-clean prompt. Doesn't
        # affect the already-at-prompt path — an extra newline there is
        # a no-op.
        self.ser.write(b"\r\n")
        prime_start = time.time()
        prime_buf = b""
        while time.time() - prime_start < 2.0:
            chunk = self.ser.read(T.READ_PROMPT)
            if chunk:
                prime_buf += chunk
                if b"=> " in prime_buf:
                    break
            elif prime_buf:
                break
        self._wait_for_line_idle(settle_time=T.LINE_SETTLE, max_wait=T.LINE_IDLE_SHORT_WAIT)
        self.ser.reset_input_buffer()

    def _set_recovery_boot_medium(self, boot_medium: str | None) -> None:
        """Append boot_medium=<value> to bootargs (no-op when None)."""
        # If a boot_medium was requested, set bootargs so the kernel
        # cmdline carries boot_medium=<value>. This board's `recovery`
        # U-Boot command boots without setting any bootargs, so the
        # modern `firmware update` tool (which reads boot_medium= off
        # /proc/cmdline) otherwise aborts with "Cannot detect boot
        # medium". Base cmdline confirmed on real hardware (U-Boot
        # 2025.04). The prompt was just primed+flushed above, so this
        # lands on a clean "=> ".
        if boot_medium:
            bootargs = (
                f"{C.RECOVERY_BOOTARGS} boot_medium={boot_medium}"
            )
            self.ser.write(f'setenv bootargs "{bootargs}"\r\n'.encode())
            self._wait_for_line_idle(settle_time=T.LINE_SETTLE, max_wait=T.LINE_IDLE_SHORT_WAIT)
            self.ser.reset_input_buffer()

    def _recovery_returned_to_uboot(self, poll_buf: bytes, tail: str) -> str:
        """
        U-Boot's prompt came back after `run recovery`. "retry" after sending
        a fallback boot command ('boot', or bootm with the NOR DTB), "fail"
        otherwise.
        """
        if b"not defined" in poll_buf:
            # 'recovery' env var missing (e.g. Armbian U-Boot on
            # eMMC boot0, or NOR env wiped). Fall back to running
            # the default bootcmd via 'boot'. On Armbian U-Boot
            # this loads Armbian via extlinux.conf; on the old SPI
            # U-Boot it falls through to the SPI recovery kernel.
            verbose("  'recovery' not defined — falling back to 'boot' (bootcmd)...", "warning")
            self.ser.write(b"boot\r\n")
            return "retry"
        if (b"cmdline Flattened Device Tree" in poll_buf or
                b"valid device tree" in poll_buf):
            # bootm succeeded in loading the FIT kernel but the FIT
            # has no embedded FDT. The DTB is stored separately in
            # NOR at 0x500000 (confirmed layout). Load it and retry.
            verbose("  bootm: no embedded FDT — loading DTB from NOR 0x500000 and retrying...", "warning")
            self.ser.write(
                b"setenv fdt_high 0xffffffffffffffff;"
                b"sf probe 0;"
                b"sf read 0x90000000 0x500000 0x20000;"
                b"sf read 0x82000000 0xa00000 0x2000000;"
                b"bootm 0x82000000 - 0x90000000\r\n"
            )
            return "retry"
        verbose("✗ bootm failed — U-Boot prompt returned", "error")
        verbose(f"  Output: {tail[-T.LOG_ERROR_TAIL:]!r}", "error")
        return "fail"

    def _login_root_at_prompt(self, tail: str) -> bool:
        """A Linux login prompt is in `tail`: log in as root (blank password)."""
        for line in tail.splitlines():
            if " login:" in line:
                verbose(f"✓ Login prompt: '{line.strip()}' — logging in as root...")
                break
        # Send the username with a bare CR (not CR+LF). A
        # trailing LF gets eaten by the "Password:" prompt as a
        # premature empty entry, desyncing the exchange so the
        # real blank password lands out of order -> "Login
        # incorrect" even though root has no password. Confirmed
        # on hardware (gateway-dk eMMC OS login).
        self.ser.write(b"root\r")

        # Wait for the actual "Password:" prompt before
        # answering, instead of a blind sleep, so the blank
        # password isn't sent before login is ready to read it.
        # Passwordless builds drop straight to a shell (no
        # prompt) — handle that too.
        pw_buf = b""
        pw_start = time.time()
        while time.time() - pw_start < T.LOGIN_STEP_TIMEOUT:
            chunk = self.ser.read(T.READ_PASSWORD)
            if chunk:
                pw_buf += chunk
                if b"assword" in pw_buf:
                    break
                if b"root@" in pw_buf and b"#" in pw_buf:
                    break
        if b"root@" in pw_buf and b"#" in pw_buf:
            verbose("✓ Recovery Linux booted and logged in")
            return True
        self.ser.write(b"\r")  # blank password — bare CR

        # Poll for the shell prompt instead of one fixed-sleep
        # read_all(). CONFIRMED BUG THIS GUARDS AGAINST: right
        # after boot, login spawning a shell and printing its
        # prompt can take longer than a sub-second window under
        # real-world jitter — a one-shot read after 0.5s raced
        # this and captured the login sequence still in flight
        # (e.g. just "root\r\nPassword: \r\n", before the shell
        # prompt had printed), misreading a slow-but-successful
        # blank-password login as a real password requirement.
        response = b""
        login_start = time.time()
        while time.time() - login_start < T.LOGIN_STEP_TIMEOUT:
            chunk = self.ser.read(T.READ_BOOT)
            if chunk:
                response += chunk
                if b"root@" in response and b"#" in response:
                    break
        response = response.decode("utf-8", errors="replace")
        if "root@" in response and "#" in response:
            verbose("✓ Recovery Linux booted and logged in")
            return True
        verbose(f"Login response: {response!r}", "warning")
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
            poll_timeout = T.BOOT_POLL_TIMEOUT
            poll_buf = b""

            while time.time() - poll_start < poll_timeout:
                chunk = self.ser.read(T.READ_BOOT)
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
                        time.sleep(T.STAGING_LOGIN_SETTLE)
                        resp = self.ser.read(T.READ_LOGIN_BURST).decode("utf-8", errors="replace")

                        if "root@" in resp and "#" in resp:
                            verbose("✓ Logged in to staging Linux (empty password)")
                            return True

                        if "Password" in resp or "password" in resp.lower():
                            # Try empty password first
                            self.ser.write(b"\r\n")
                            time.sleep(T.STAGING_PW_SETTLE)
                            r2 = self.ser.read(T.READ_BOOT).decode("utf-8", errors="replace")
                            if "root@" in r2 and "#" in r2:
                                verbose("✓ Logged in to staging Linux (empty password)")
                                return True

                            # Fall back to Armbian default: 1234
                            self.ser.write(T.ARMBIAN_DEFAULT_PASSWORD + b"\r\n")
                            time.sleep(T.STAGING_PW_SETTLE)
                            r3 = self.ser.read(T.READ_BOOT).decode("utf-8", errors="replace")
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
                f"last {len(poll_buf)} bytes: {poll_buf[-T.LOG_RAW_TAIL_LONG:]!r}"
            )
            return False

        except Exception as e:
            verbose(f"Failed to boot staging Linux: {e}", "error")
            return False

    def _wait_for_root_shell(self, timeout: float, label: str) -> bool:
        """
        Shared retry loop behind login_recovery()/login_staging(): send
        a bare Enter, read back whatever comes, and check for a root
        shell prompt ("root@...#"), reconnecting the serial port first
        if it dropped since the last attempt. Backs off 0.5s -> 5s
        (doubling) between attempts, up to timeout seconds total.
        """
        verbose(f"Verifying {label} login...")

        start_time = time.time()
        attempt = 0
        backoff = T.BACKOFF_START

        while time.time() - start_time < timeout:
            attempt += 1
            try:
                # If serial dropped, try to reconnect
                if not self.ser or not self.ser.is_open:
                    verbose("Serial disconnected — waiting for device to reappear...", "warning")
                    if not self.wait_for_port(timeout=T.RETRY_PORT_TIMEOUT):
                        time.sleep(min(backoff, T.BACKOFF_MAX))
                        backoff = min(backoff * 2, T.BACKOFF_MAX)
                        continue
                    self.connect(self.baud_rate or C.SERIAL_BAUD)

                # Send Enter to get a prompt
                self.ser.write(b"\r\n")
                time.sleep(T.ENTER_SETTLE)
                response = self.ser.read_all().decode("utf-8", errors="replace")

                if "root@" in response and "#" in response:
                    verbose(f"✓ Logged into {label} (attempt {attempt})")
                    return True

                verbose(f"{label} login attempt {attempt} — retrying in {backoff:.1f}s", "debug")
                time.sleep(backoff)
                backoff = min(backoff * 2, T.BACKOFF_MAX)

            except Exception as e:
                verbose(f"{label} login attempt {attempt} failed: {e} — retrying in {backoff:.1f}s", "debug")
                time.sleep(backoff)
                backoff = min(backoff * 2, T.BACKOFF_MAX)

        verbose(f"Failed to verify {label} login after {attempt} attempts", "error")
        return False

    def login_staging(self, timeout: float = C.LOGIN_TIMEOUT) -> bool:
        """
        Confirm we are at a root shell in the staging Linux environment.
        Checks for any 'root@<hostname>#' prompt — not locked to 'root@recovery'.
        Called after boot_linux_staging() in the staging-boot path.

        RECOVERY_PROMPT was previously checked here as a separate,
        looser fallback for "device is at a usable recovery shell even
        if staging Linux itself never came up" — removed because it was
        provably dead: RECOVERY_PROMPT (b"root@recovery:~# ") already
        contains both "root@" and "#", so the check above always
        matches first.
        """
        return self._wait_for_root_shell(timeout, "staging Linux")

    def login_recovery(self, timeout: float = C.LOGIN_TIMEOUT) -> bool:
        """
        Verify we are logged into recovery Linux (root@recovery prompt).
        Handles the case where boot_recovery already auto-logged in.

        Returns:
            True if root@recovery prompt confirmed, False otherwise
        """
        return self._wait_for_root_shell(timeout, "recovery Linux")
