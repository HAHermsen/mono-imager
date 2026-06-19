import time
from mono_imager.serial_device import SerialDevice

def main():
    print("=== Hotplug Stress Test (3 cycles) ===")
    print("Unplug and replug the Mono Gateway when instructed.")
    print("This test will verify reconnect logic 3 times.\n")

    d = SerialDevice("COM5", timeout=1)

    print("Connecting initially...")
    if not d.connect(115200):
        print("Initial connect failed — plug in the device and try again.")
        return

    print("✓ Initial connection OK\n")

    cycles = 3
    successes = 0

    for cycle in range(1, cycles + 1):
        print(f"=== Cycle {cycle} of {cycles} ===")
        print("→ Unplug the device NOW...")

        # Wait for disconnect using raw serial port (no reconnect triggered)
        print("Waiting for disconnect...")
        while True:
            try:
                _ = d.ser._ser.in_waiting
                time.sleep(0.05)
            except Exception:
                print("✓ Disconnect detected")
                break

        print("→ Replug the device NOW...")

        # Now allow reconnect logic to run
        print("Waiting for reconnect...")

        reconnected = False
        start = time.time()

        while time.time() - start < 30:
            # This triggers safe_read → reconnect logic
            b = d.ser.read(1)
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
