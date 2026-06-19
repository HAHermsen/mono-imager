#!/usr/bin/env python3
"""
Manual test for SerialDevice - autoboot interrupt and U-Boot interaction
Run this with device powered off, then power it on during execution
"""

import sys
from mono_imager.serial_device import SerialDevice


def main():
    print("=" * 60)
    print("mono-imager Serial Device Test")
    print("=" * 60)
    print()
    
    # Connect to device
    print("Step 1: Connecting to COM5 at 115200 baud...")
    d = SerialDevice('COM5', timeout=15)
    
    if not d.connect(115200):
        print("❌ Failed to connect")
        return False
    
    print("✓ Connected")
    print()
    
    # Wait for autoboot and interrupt
    print("Step 2: Waiting for U-Boot autoboot countdown...")
    print("(Power cycle your device NOW if it's off)")
    print()
    
    if not d.wait_for_autoboot(timeout=15):
        print("❌ Failed to detect and interrupt autoboot")
        return False
    
    print("✓ U-Boot autoboot interrupted")
    print()
    
    # Test U-Boot commands
    print("Step 3: Testing U-Boot commands...")
    print()
    
    try:
        # Send immediate no-op to confirm we're at U-Boot prompt
        d.send_command("", wait_for_prompt=True, timeout=3)
        
        response = d.send_command("printenv ethact")
        print(f"ethact: {response}")
        print()
        
        response = d.send_command("printenv load_addr")
        print(f"load_addr: {response}")
        print()
        
        print("✓ All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Command failed: {e}")
        return False
    finally:
        d.disconnect()


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)