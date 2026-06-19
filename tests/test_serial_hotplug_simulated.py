import sys
import time
import logging

from datetime import datetime
from pathlib import Path
from mono_imager.config import detect_serial_ports
from mono_imager.serial_device import SerialDevice

def main():
    print("=== Automated Hotplug Test (3 simulated cycles) ===")

    d = SerialDevice("COM5", timeout=1)

    print("Connecting initially...")
    if not d.connect(115200):
        print("Initial connect failed — is the device plugged in?")
        return

    print("✓ Initial connection OK\n")

    cycles = 3
    successes = 0

    for cycle in range(1, cycles + 1):
        print(f"=== Cycle {cycle} of {cycles} ===")

        # Simulate unplug
        print("→ Simulating UNPLUG (closing real serial port)")
        try:
            d.ser._ser.close()   # <-- software unplug
        except Exception:
            pass

        # Wait for reconnect logic to kick in
        print("Waiting for reconnect...")

        reconnected = False
        start = time.time()

        while time.time() - start < 30:
            b = d.ser.read(1)  # safe_read will auto-reconnect
            if b is not None:
                reconnected = True
                break
            time.sleep(0.05)

        if reconnected:
            print(f"✓ Reconnected successfully (cycle {cycle})\n")
            successes += 1
        else:
            print(f"✗ Reconnect FAILED (cycle {cycle})\n")
            break

    print("=== Test Summary ===")
    print(f"Successful reconnects: {successes}/{cycles}")

    if successes == cycles:
        print("✓ PASS — reconnect logic is stable")
    else:
        print("✗ FAIL — reconnect logic is unreliable")

    d.disconnect()


if __name__ == "__main__":
    main()
