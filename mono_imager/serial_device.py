#!/usr/bin/env python3
"""
mono-imager: Serial I/O and boot-control layer
Provides UART autodetect, USB presence polling, U‑Boot automation, 
recovery boot handling, and firmware flashing utilities

Author:  H.A. Hermsen
Version: 0.2.0
License: MIT
"""

__version__ = "0.2.0"
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

    def safe_read(self, size=1):
        try:
            return self.ser._ser.read(size)
        except Exception as e:
            logger.warning(f"Read failed: {e} — reconnecting...")
            if self._attempt_reconnect():
                return self.ser._ser.read(size)
            return b""
        
    def safe_write(self, data):
        try:
            return self.ser._ser.write(data)
        except Exception as e:
            logger.warning(f"Write failed: {e} — reconnecting...")
            if self._attempt_reconnect():
                return self.ser._ser.write(data)
            return False
      
    def safe_read_all(self):
        try:
            return self.ser._ser.read_all()
        except Exception:
            if self._attempt_reconnect():
                return self.ser._ser.read_all()
            return b""        
    
    def disconnect(self):
        """Close serial connection"""
        if self.ser and self.ser.is_open:
            self.ser.close()
            logger.info("Serial connection closed")
            
    def _attempt_reconnect(self):
        logger.info("Attempting auto‑reconnect...")

        if not self.wait_for_port(timeout=20):
            return False

        return self.connect(self.baud_rate or 115200)
    
    def _has_prompt(self, response: bytes) -> bool:
        """Check if response contains a known prompt"""
        for prompt in self.UBOOT_PROMPTS + [self.RECOVERY_PROMPT]:
            if prompt in response:
                return True
        return False
    
    def send_command(self, command: str, wait_for_prompt: bool = True, 
                    timeout: Optional[float] = None) -> str:
        """
        Send command and wait for response
        
        Args:
            command: Command to send (without newline)
            wait_for_prompt: Wait for command prompt before returning
            timeout: Override default timeout
        
        Returns:
            Response text (stripped)
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial connection not open")
        
        timeout = timeout or 5.0  # Reduced from 15s default
        
        # Clear input buffer
        self.ser.reset_input_buffer()
        
        # Send command with newline
        logger.debug(f">> {command}")
        self.ser.write((command + "\r\n").encode())
        
        # Read response
        time.sleep(0.2)  # Wait for device to respond
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
            
            time.sleep(0.05)
        
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
                        logger.info("✓ Detected autoboot, spamming interrupt immediately...")

                        # Spam immediately and hard for 2 seconds
                        spam_start = time.time()
                        while time.time() - spam_start < 2.0:
                            self.ser.write(b" ")
                            time.sleep(0.02)

                        # Read what came back
                        time.sleep(0.5)
                        waiting = self.ser.in_waiting
                        response = self.ser.read(waiting) if waiting else b""
                        logger.info(f"Post-interrupt tail: {repr(response[-60:])}")

                        if b"=>" in response:
                            logger.info("✓ U-Boot prompt confirmed")
                            return True

                        logger.error("Could not confirm U-Boot prompt — interrupt may have been too late")
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
        Boot into recovery Linux from U-Boot
        
        Returns:
            True if recovery boot initiated, False otherwise
        """
        logger.info("Booting into recovery Linux...")
        
        try:
            response = self.send_command("run recovery", wait_for_prompt=False, timeout=15)
            
            # Wait for login prompt
            time.sleep(2)
            response = self.ser.read_all().decode('utf-8', errors='replace')
            
            if "login:" in response or "root@recovery" in response:
                logger.info("✓ Recovery Linux booted")
                return True
            
            logger.warning("Recovery boot may have failed")
            return False
            
        except Exception as e:
            logger.error(f"Failed to boot recovery: {e}")
            return False
    
    def login_recovery(self, timeout: float = 30) -> bool:
        """
        Login to recovery Linux (no password), with auto‑reconnect.
        """
        logger.info("Logging into recovery Linux...")

        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                # If serial dropped, try to reconnect
                if not self.ser or not self.ser.is_open:
                    logger.warning("Serial disconnected — waiting for device to reappear...")
                    if not self.wait_for_port(timeout=10):
                        continue
                    self.connect(self.baud_rate or 115200)
                    time.sleep(0.5)

                # Try sending a blank command
                response = self.send_command("", wait_for_prompt=True, timeout=5)

                if "root@recovery" in response:
                    logger.info("✓ Logged into recovery Linux")
                    return True

                # Try pressing Enter again
                self.ser.write(b"\r\n")
                time.sleep(0.5)

            except Exception as e:
                logger.debug(f"Login attempt failed: {e}")
                time.sleep(0.5)

        logger.error("Failed to login to recovery Linux")
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
           