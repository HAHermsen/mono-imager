# mono-imager

Automated firmware flashing tool for Mono Gateway Routers and the Mono Development Kit.  
Provides a guided, reliable flashing experience with serial console control, HTTP and USB firmware transfer, U‑Boot recovery handling, and full OPNsense eMMC re-imaging support.

## Features

### Serial
- **Auto port detection** — Identifies the correct COM port and baud rate automatically
- **Auto reconnect** — Survives USB disconnects, FTDI resets, device reboots, and COM port re-enumeration
- **Fault tolerant I/O** — All serial commands are wrapped in retry and error-handling logic
- **Hotplug support** — Handles unplug/replug during flashing and bootloader transitions
- **Interactive serial console** — Raw serial access mode for manual device interaction

### Network
- **HTTP firmware server** — Serves firmware directly from the host PC over a local HTTP server
- **Network reachability checks** — Verifies connectivity before flashing to prevent partial writes
- **Auto interface detection** — Detects which Ethernet port has carrier in recovery Linux
- **TCP/IP result reporting** — Device reports step results back over HTTP, not serial (more reliable)

### Flashing
- **Menu driven** — Simple CLI with guided prompts for every decision
- **6 flash journeys** — OPNsense, OpenWRT, and Armbian via HTTP server or USB stick
- **Dual-flash support** — NOR ↔ eMMC safe sequencing with DIP switch guidance
- **OPNsense eMMC re-imaging** — Automatically re-images the first 32MB bootloader region after OS flash, allowing eMMC boot
- **Custom image support** — Flash any local `.img` file
- **Streaming flash** — Large images (>3GB) stream directly via `curl | dd`, smaller ones buffer first
- **Extensible journey system** — New OS or transfer method = one file, no orchestrator rewrites

### Recovery
- **Firmware update** — Updates device firmware via the built-in `firmware update` command (modern devices) or legacy `curl | dd` (older devices)
- **Auto path detection** — Detects modern vs legacy firmware path per device at runtime

---

## Requirements

- **Python 3.10+**
- **USB-to-UART serial cable** (for device console access)
- **Ethernet cable** (for network flash journeys)
- **USB stick** (for USB flash journeys, formatted FAT32, firmware named `firmware.img` at root)

### Python dependencies

```
pyserial>=3.5
icmplib>=3.0
```

---

## Installation

### Windows

```bash
git clone https://github.com/HAHermsen/mono-imager.git
cd mono-imager
python -m venv venv
venv\Scripts\activate
pip install -e .
```

### macOS / Linux

```bash
git clone https://github.com/HAHermsen/mono-imager.git
cd mono-imager
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

---

## Usage

1. **Connect your device** via USB-to-UART serial cable (COM5 / `/dev/ttyUSB0`)
2. **Run the tool:**
   ```bash
   mono-imager
   ```
3. **Follow the menu:**
   - Select serial port
   - Choose OS (OPNsense, OpenWRT, Armbian)
   - Choose transfer method (HTTP server or USB stick)
   - Provide firmware path
   - Tool handles the rest — U-Boot interrupt, eMMC erase, flash, reboot

### OPNsense note

OPNsense requires two DIP switch flips during installation. The tool pauses and guides you through each one. You will need the device's MAC address for firmware server authentication (printed on the device label or visible in U-Boot output).

### USB stick preparation

Name the firmware file `firmware.img` and place it at the root of a FAT32 formatted USB stick. The tool mounts and verifies it automatically.

---

## Flash journeys

| OS | Transfer | Steps |
|----|----------|-------|
| OPNsense | HTTP server | 10 |
| OPNsense | USB stick | 10 |
| OpenWRT | HTTP server | 5 |
| OpenWRT | USB stick | 5 |
| Armbian | HTTP server | 5 |
| Armbian | USB stick | 5 |

See [JOURNEYS.md](JOURNEYS.md) for how to add new OS support or transfer methods.

---

## Project structure

```
mono_imager/
├── tui.py                    # Menu-driven CLI
├── step_registry.py          # @register_step decorator, FlowRunner, StepContext
├── journey_steps.py          # All flash journey steps — edit this to add journeys
├── flash_orchestrator.py     # Bootstrap, HTTP server, logging, result tracking
├── recovery_orchestrator.py  # Firmware update flow (separate from flash journeys)
├── serial_device.py          # Serial comms layer
├── config.py                 # Port detection, config persistence
├── spinner.py                # Terminal progress spinner
└── cli.py                    # Entry point
```

---

## Troubleshooting

**No serial ports found** — Check USB cable and driver installation. On Windows, check Device Manager for CP210x / CH340 / FTDI.

**Bootstrap timeout** — Power cycle the device manually when prompted. If U-Boot autoboot is too fast, the tool may miss the interrupt window.

**Network step fails** — Ensure the Ethernet cable is in the rightmost RJ-45 port (eth0 / `fm1-mac2`). Other ports may not be active in recovery.

**OPNsense auth error (401)** — The MAC address provided does not match the firmware server's records. Check the label on the device or the U-Boot output for the correct MAC.

**Flash takes a long time** — Normal. OPNsense images are ~5GB and transfer at ~2–3 MB/s over a local HTTP connection.

For more help, open an issue on GitHub or join the [Mono Discord](https://discord.gg/mono).

---

## Development

```bash
pip install -e ".[dev]"
```

To preview what steps a journey will run without executing it:

```python
from mono_imager.step_registry import list_journey
for i, label in enumerate(list_journey("OPNsense", "usb"), 1):
    print(f"  {i}. {label}")
```

See [JOURNEYS.md](JOURNEYS.md) for the full guide to adding journeys.

---

## License

MIT — see [LICENSE](LICENSE) for details.

**Built for the Mono community.**  
Questions? Open an issue or join the [Mono Discord](https://discord.gg/mono).
