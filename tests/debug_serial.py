#!/usr/bin/env python3
"""
Debug script - show raw bytes from device after autoboot interrupt
"""
import serial
import time

ser = serial.Serial('COM5', 115200, timeout=2)
print("Waiting for autoboot... power cycle now!")

buffer = b""
start = time.time()

while time.time() - start < 30:
    chunk = ser.read(512)
    if chunk:
        buffer += chunk
        if b"Hit any key to stop autoboot" in buffer:
            print(">>> DETECTED AUTOBOOT, spamming...")
            spam_start = time.time()
            while time.time() - spam_start < 1.0:
                ser.write(b" ")
                time.sleep(0.05)
            
            time.sleep(0.5)
            ser.reset_input_buffer()
            ser.write(b"\r\n")
            time.sleep(1.0)
            response = ser.read(4096)
            print(f">>> RAW RESPONSE AFTER INTERRUPT:")
            print(repr(response))
            break

ser.close()