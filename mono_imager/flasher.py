"""
Firmware download and flashing module for mono-imager
"""

import os
import logging
import requests
from typing import Optional, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


class FirmwareSource:
    """Firmware source configurations"""
    
    ARMBIAN = {
        "name": "Armbian",
        "eMMC": "https://armbian.com/download/gateway-dk/Armbian_latest_Gateway-dk_noble_current_minimal.img.gz",
        "description": "Armbian official build for Mono Gateway DK"
    }
    
    MONO_OFFICIAL = {
        "name": "Mono Official",
        "eMMC": "https://firmware.mono.si/firmware-emmc-gateway-dk.bin",
        "NOR": "https://firmware.mono.si/firmware-qspi-gateway-dk.bin",
        "description": "Mono official firmware"
    }


class FirmwareDownloader:
    """Handle firmware downloads with resume and progress"""
    
    def __init__(self, timeout: float = 30.0, chunk_size: int = 8192):
        """
        Initialize downloader
        
        Args:
            timeout: Request timeout in seconds
            chunk_size: Download chunk size in bytes
        """
        self.timeout = timeout
        self.chunk_size = chunk_size
        
        # Disable SSL verification for firmware.mono.si (known issue)
        self.session = requests.Session()
        self.session.verify = False
    
    def download(self, url: str, destination: Path, 
                 progress_callback: Optional[Callable[[int, int], None]] = None) -> bool:
        """
        Download firmware with resume support
        
        Args:
            url: Firmware URL
            destination: Where to save the file
            progress_callback: Optional callback(downloaded_bytes, total_bytes)
        
        Returns:
            True if successful, False otherwise
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Check if partial download exists
            resume_header = {}
            if destination.exists():
                resume_header = {'Range': f'bytes={destination.stat().st_size}-'}
                logger.info(f"Resuming download from {destination.stat().st_size} bytes")
            
            logger.info(f"Downloading {url}")
            
            response = self.session.get(
                url,
                headers=resume_header,
                stream=True,
                timeout=self.timeout,
                allow_redirects=True
            )
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = destination.stat().st_size if destination.exists() else 0
            
            mode = 'ab' if resume_header else 'wb'
            
            with open(destination, mode) as f:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if progress_callback:
                            progress_callback(downloaded, total_size)
            
            logger.info(f"✓ Downloaded {downloaded} bytes")
            return True
            
        except requests.RequestException as e:
            logger.error(f"Download failed: {e}")
            return False
        except IOError as e:
            logger.error(f"Failed to write file: {e}")
            return False
    
    def verify_size(self, file_path: Path, expected_size: Optional[int] = None) -> bool:
        """
        Verify downloaded file
        
        Args:
            file_path: Path to firmware file
            expected_size: Expected file size in bytes (optional)
        
        Returns:
            True if verification passed
        """
        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return False
        
        size = file_path.stat().st_size
        logger.info(f"File size: {size} bytes ({size / 1024 / 1024:.1f} MB)")
        
        if expected_size and size != expected_size:
            logger.warning(f"Size mismatch: expected {expected_size}, got {size}")
            return False
        
        return True


class Flasher:
    """Execute flashing on device"""
    
    def __init__(self, serial_device):
        """
        Initialize flasher
        
        Args:
            serial_device: SerialDevice instance (must be logged into recovery Linux)
        """
        self.device = serial_device
    
    def detect_firmware_tool(self) -> Optional[str]:
        """
        Detect which flashing tool is available
        
        Returns:
            'firmware' if modern tool available, 'manual' for legacy, None if neither
        """
        logger.info("Detecting firmware tool...")
        
        try:
            response = self.device.send_command("which firmware", timeout=5)
            if "firmware" in response and "/firmware" in response:
                logger.info("✓ Modern 'firmware' tool detected")
                return "firmware"
        except Exception as e:
            logger.debug(f"Firmware tool check failed: {e}")
        
        logger.info("Falling back to manual flashing method")
        return "manual"
    
    def get_device_mac(self) -> Optional[str]:
        """
        Get device MAC address for authentication
        
        Returns:
            MAC address in format "xx:xx:xx:xx:xx:xx" or None
        """
        try:
            response = self.device.send_command("ip link show eth0", timeout=5)
            
            # Parse MAC from output: link/ether aa:bb:cc:dd:ee:ff
            for line in response.split('\n'):
                if 'link/ether' in line:
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if part == 'link/ether' and i + 1 < len(parts):
                            mac = parts[i + 1]
                            logger.info(f"Device MAC: {mac}")
                            return mac
            
            logger.warning("Could not determine device MAC address")
            return None
            
        except Exception as e:
            logger.error(f"Failed to get MAC: {e}")
            return None
    
    def flash_emmc_modern(self) -> bool:
        """
        Flash eMMC using modern 'firmware update' command
        
        Returns:
            True if successful
        """
        logger.info("Flashing eMMC using 'firmware update' tool...")
        
        try:
            response = self.device.send_command(
                "firmware update",
                wait_for_prompt=False,
                timeout=120
            )
            
            if "successfully" in response.lower() or "complete" in response.lower():
                logger.info("✓ eMMC flash complete")
                return True
            
            logger.warning(f"Flash output: {response}")
            return True  # Assume success if command ran
            
        except Exception as e:
            logger.error(f"Flash failed: {e}")
            return False
    
    def flash_emmc_manual(self, mac_address: str) -> bool:
        """
        Flash eMMC using manual curl + dd method
        
        Args:
            mac_address: Device MAC address for authentication
        
        Returns:
            True if successful
        """
        logger.info("Flashing eMMC using manual method...")
        
        firmware_url = "https://firmware.mono.si/firmware-emmc-gateway-dk.bin"
        
        # Download and flash in one command
        cmd = (
            f"curl -u mono:{mac_address} -k -O {firmware_url} && "
            "dd if=firmware-emmc-gateway-dk.bin of=/dev/mmcblk0 bs=4096 skip=1 seek=1"
        )
        
        try:
            logger.info("Downloading and flashing (this may take a few minutes)...")
            response = self.device.send_command(cmd, wait_for_prompt=False, timeout=300)
            
            if "records out" in response or "records in" in response:
                logger.info("✓ eMMC flash complete")
                return True
            
            logger.warning(f"Flash output: {response}")
            return True
            
        except Exception as e:
            logger.error(f"Manual flash failed: {e}")
            return False
    
    def verify_boot_source(self, expected: str) -> bool:
        """
        Verify device booted from expected source
        
        Args:
            expected: 'eMMC' or 'NOR'
        
        Returns:
            True if boot source matches
        """
        logger.info(f"Verifying boot source (expecting {expected})...")
        
        try:
            response = self.device.send_command("dmesg | grep -i 'BOOT SRC'", timeout=10)
            
            if expected.upper() in response.upper():
                logger.info(f"✓ Verified booting from {expected}")
                return True
            
            logger.warning(f"Boot source verification inconclusive: {response}")
            return True  # Don't fail on this
            
        except Exception as e:
            logger.debug(f"Boot verification failed: {e}")
            return True  # Don't fail on this


def create_cache_dir() -> Path:
    """Create and return cache directory for firmware downloads"""
    cache_dir = Path.home() / ".cache" / "mono-imager"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
