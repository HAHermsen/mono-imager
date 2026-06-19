#!/usr/bin/env python3
"""
mono-imager: Serial I/O and boot-control layer
Provides UART autodetect, USB presence polling, U‑Boot automation, 
recovery boot handling, and firmware flashing utilities

Author:  H.A. Hermsen
Version: 0.3.0
License: MIT
"""

__version__ = "0.3.0"
__author__ = "H.A. Hermsen"

import serial
import time
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)


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
            logger.error(f"Device on {self.port} did not appear — cannot connect")
            return False

        rates_to_try = [baud_rate] if baud_rate else self.BAUD_RATES

        for rate in rates_to_try:
            try:
                logger.info(f"Attempting connection at {rate} baud...")

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
                    logger.debug(f"Response at {rate} baud: {response[:100]}")

                if self._has_prompt(response) or len(response) > 0:
                    self.baud_rate = rate
                    logger.info(f"✓ Connected at {rate} baud")
                    return True

                # Close real port if no response
                real.close()

            except serial.SerialException as e:
                logger.debug(f"Failed to connect at {rate} baud: {e}")
                continue

        logger.error(f"Failed to connect to {self.port} at any baud rate")
        return False

    def disconnect(self):
        """Close serial connection"""
        if self.ser and self.ser.is_open:
            self.ser.close()
            logger.info("Serial connection closed")
            
    def _attempt_reconnect(self) -> bool:
        """
        Wait for port to reappear and reconnect at the last known baud rate.
        Warns explicitly if baud rate was never detected and falls back to 115200.
        """
        logger.info("Attempting auto‑reconnect...")

        if not self.wait_for_port(timeout=20):
            return False

        baud = self.baud_rate
        if baud is None:
            logger.warning("Baud rate was never successfully detected — falling back to 115200")
            baud = 115200

        return self.connect(baud)
    
    def _has_prompt(self, response: bytes) -> bool:
        """Check if response contains a known prompt"""
        for prompt in self.UBOOT_PROMPTS + [self.RECOVERY_PROMPT]:
            if prompt in response:
                return True
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
        logger.debug(f">> {command}")
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
        
        # Strip command echo (device echoes back what we sent)
        if command and response_str.startswith(command):
            response_str = response_str[len(command):].strip()
        
        # Remove trailing prompt lines (=> or #)
        lines = [l for l in response_str.splitlines() if l.strip() not in ("=>", "#", "")]
        
        # Remove duplicate lines (echo artifact)
        seen = []
        for line in lines:
            if line not in seen:
                seen.append(line)
        response_str = "\n".join(seen).strip()
        
        logger.debug(f"<< {response_str[:200]}")
        return response_str
    
    def wait_for_autoboot(self, timeout: float = 30) -> bool:
        """
        Wait for U-Boot autoboot countdown and auto-interrupt

        Args:
            timeout: Max time to wait for autoboot message

        Returns:
            True if interrupted and U-Boot prompt reached, False otherwise
        """
        logger.info("Waiting for U-Boot autoboot countdown...")

        start_time = time.time()
        buffer = b""

        while time.time() - start_time < timeout:
            try:
                # Read one byte at a time so we trigger ASAP
                byte = self.ser.read(1)
                if byte:
                    buffer += byte

                    if b"Hit any key to stop autoboot" in buffer:
                        logger.info("✓ Detected autoboot — interrupting and polling for prompt...")

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
                                    logger.info("✓ U-Boot prompt confirmed")
                                    return True

                        logger.error(
                            f"U-Boot prompt not seen within {interrupt_timeout}s after interrupt — "
                            f"last bytes: {repr(interrupt_buf[-60:])}"
                        )
                        return False

            except serial.SerialException:
                break

        logger.warning("Autoboot countdown not detected within timeout")
        return False
    
    def interrupt_autoboot(self) -> bool:
        """
        Interrupt U-Boot autoboot countdown (manual)
        
        Returns:
            True if successfully interrupted, False otherwise
        """
        logger.info("Interrupting U-Boot autoboot...")
        
        # Send multiple spaces/enters to interrupt
        for _ in range(5):
            self.ser.write(b" ")
            time.sleep(0.1)
        
        time.sleep(0.5)
        response = self.ser.read_all().decode('utf-8', errors='replace')
        
        if self._has_prompt(response.encode()):
            logger.info("✓ U-Boot interrupted")
            return True
        
        logger.warning("Failed to interrupt autoboot")
        return False
    
    def boot_recovery(self) -> bool:
        """
        Boot into recovery Linux from U-Boot.

        Returns:
            True if recovery boot confirmed, False otherwise
        """
        logger.info("Booting into recovery Linux...")

        try:
            self.send_command("run recovery", wait_for_prompt=False, timeout=15)

            # Poll for login or recovery prompt — no blind sleep
            poll_start = time.time()
            poll_timeout = 60.0
            poll_buf = b""

            while time.time() - poll_start < poll_timeout:
                chunk = self.ser.read(256)
                if chunk:
                    poll_buf += chunk
                    tail = poll_buf.decode("utf-8", errors="replace")
                    if "login:" in tail or "root@recovery" in tail:
                        logger.info("✓ Recovery Linux booted")
                        return True

            logger.warning(
                f"Recovery boot prompt not seen within {poll_timeout}s — "
                f"last output: {repr(poll_buf[-120:])}"
            )
            return False

        except Exception as e:
            logger.error(f"Failed to boot recovery: {e}")
            return False
    
    def login_recovery(self, timeout: float = 30) -> bool:
        """
        Login to recovery Linux (no password), with auto‑reconnect and exponential backoff.
        """
        logger.info("Logging into recovery Linux...")

        start_time = time.time()
        attempt = 0
        backoff = 0.5  # initial backoff in seconds

        while time.time() - start_time < timeout:
            attempt += 1
            try:
                # If serial dropped, try to reconnect
                if not self.ser or not self.ser.is_open:
                    logger.warning("Serial disconnected — waiting for device to reappear...")
                    if not self.wait_for_port(timeout=10):
                        time.sleep(min(backoff, 5.0))
                        backoff = min(backoff * 2, 5.0)
                        continue
                    self.connect(self.baud_rate or 115200)

                # Try sending a blank command
                response = self.send_command("", wait_for_prompt=True, timeout=5)

                if "root@recovery" in response:
                    logger.info(f"✓ Logged into recovery Linux (attempt {attempt})")
                    return True

                # Send Enter and back off before retrying
                self.ser.write(b"\r\n")
                logger.debug(f"Login attempt {attempt} — retrying in {backoff:.1f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

            except Exception as e:
                logger.debug(f"Login attempt {attempt} failed: {e} — retrying in {backoff:.1f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

        logger.error(f"Failed to login to recovery Linux after {attempt} attempts")
        return False

               
    def wait_for_port(self, timeout: float = 30.0) -> bool:
        """
        Wait until the serial port appears (device plugged in or rebooted).
        """
        logger.info(f"Waiting for device on {self.port}...")

        start = time.time()
        while time.time() - start < timeout:
            try:
                # Try opening the port non‑blocking
                test = serial.Serial(self.port)
                test.close()
                logger.info("✓ Device detected")
                return True
            except serial.SerialException:
                time.sleep(0.5)

        logger.error(f"Device on {self.port} did not appear within {timeout}s")
        return False
               
    def safe_write(self, data: bytes) -> bool:
        """
        Write data to the serial port with auto‑reconnect on failure.
        """
        try:
            return self.ser._ser.write(data)   # <-- REAL serial port
        except Exception as e:
            logger.warning(f"Write failed ({e}) — attempting reconnect...")

            if not self._attempt_reconnect():
                return False

            try:
                return self.ser._ser.write(data)
            except Exception as e2:
                logger.error(f"Retry write failed: {e2}")
                return False
            
    def safe_read(self, size: int = 1) -> bytes:
        """
        Read from serial port with auto‑reconnect on failure.
        """
        try:
            return self.ser._ser.read(size)   # <-- REAL serial port
        except Exception as e:
            logger.warning(f"Read failed ({e}) — attempting reconnect...")

            if not self._attempt_reconnect():
                return b""

            try:
                return self.ser._ser.read(size)
            except Exception as e2:
                logger.error(f"Retry read failed: {e2}")
                return b""

    def safe_read_all(self) -> bytes:
        """
        Read all available bytes with auto‑reconnect on failure.
        """
        try:
            return self.ser._ser.read_all()
        except Exception as e:
            logger.warning(f"Read-all failed ({e}) — attempting reconnect...")

            if not self._attempt_reconnect():
                return b""

            try:
                return self.ser._ser.read_all()
            except Exception as e2:
                logger.error(f"Retry read-all failed: {e2}")
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

    # Fallback: forward any unknown attribute to real serial object
    def __getattr__(self, name):
        return getattr(self._ser, name)
           