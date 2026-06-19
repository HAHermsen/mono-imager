import serial, time
ser = serial.Serial('COM5', 115200, timeout=2)
print('Power cycle NOW...')
buf = b''
while True:
    chunk = ser.read(512)
    if chunk:
        buf += chunk
        if b'Hit any key' in buf:
            print('Spamming...')
            t = time.time()
            while time.time()-t < 1.0:
                ser.write(b' ')
                time.sleep(0.05)
            time.sleep(1.0)
            waiting = ser.in_waiting
            print(f'in_waiting: {waiting}')
            r = ser.read(waiting) if waiting else b''
            print(f'response: {repr(r)}')
            break
ser.close()