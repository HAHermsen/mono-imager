# mono-imager v1.1.0

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

Place the firmware image at the root of a FAT32 formatted USB stick. The tool auto-detects it by filename pattern — no renaming needed:

| OS | Accepted filenames |
|----|--------------------|
| Armbian | `Armbian_*.img.xz`, `Armbian_*.img` |
| OpenWRT | `openwrt-*.bin.gz`, `openwrt-*.bin`, `openwrt-*.img` |
| OPNsense | `OPNsense-*.img.bz2`, `OPNsense-*.img` |

Matching is case-insensitive. A **16 GB minimum** stick is recommended to cache all three OS images simultaneously.

---

## Flash journeys

| OS | Transfer | Steps |
|----|----------|-------|
| OPNsense | LAN (HTTP server) | 8 |
| OPNsense | USB stick | 9 |
| OpenWRT | LAN (HTTP server) | 8 |
| OpenWRT | USB stick | 9 |
| Armbian | LAN (HTTP server) | 6 |
| Armbian | USB stick | 5 |

See [mono_imager/journeys/JOURNEYS.md](mono_imager/journeys/JOURNEYS.md) for how to add new OS support or transfer methods.

---

## Project structure

```
mono_imager/
├── cli.py                    # Entry point
├── tui.py                    # Menu-driven CLI
├── flash_orchestrator.py     # Bootstrap, HTTP server, logging, result tracking
├── recovery_orchestrator.py  # Firmware update flow (separate from flash journeys)
├── step_registry.py          # @register_step decorator, FlowRunner, StepContext
├── serial_device.py          # Serial comms layer
├── config.py                 # Port detection, config persistence
├── spinner.py                # Terminal progress spinner
├── logging_setup.py          # Logging initialisation
└── journeys/                 # Flash journey files — one file per OS/transfer pair
    ├── __init__.py           # Auto-discovery, get_journey(), flash targets
    ├── armbian_lan.py
    ├── armbian_usb.py
    ├── openwrt_lan.py
    ├── openwrt_usb.py
    ├── opnsense_lan.py
    ├── opnsense_usb.py
    └── usb_utils.py          # Shared USB file detection helpers
```

---

## Troubleshooting

**No serial ports found** — Check USB cable and driver installation. On Windows, check Device Manager for CP210x / CH340 / FTDI.

**Bootstrap timeout** — Power cycle the device manually when prompted. If U-Boot autoboot is too fast, the tool may miss the interrupt window.

**Network step fails** — Any RJ-45 port works; the active one is auto-detected and the device's IP is resolved via DHCP automatically. If no DHCP response comes back, you'll be prompted to enter the device IP, subnet mask, gateway, and DNS server manually.

**OPNsense auth error (401)** — The MAC address provided does not match the firmware server's records. Check the label on the device or the U-Boot output for the correct MAC.

**Flash takes a long time** — Normal. OPNsense LAN flash typically completes in ~5–6 minutes over a local gigabit connection.

**Need full serial trace output** — Pass `--debug` (or its alias `--verbose`) to restore verbose console output including all serial commands sent and received:

```bash
mono-imager --debug
mono-imager --verbose   # same thing, either spelling
```

Equivalent to setting the `MONO_DEBUG=1` environment variable before launching, which still works too:

```bash
# Windows
set MONO_DEBUG=1 && mono-imager

# macOS / Linux
MONO_DEBUG=1 mono-imager
```

Quiet console output is the default either way — all serial I/O is always written to the log file regardless of this setting.

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

See [mono_imager/journeys/JOURNEYS.md](mono_imager/journeys/JOURNEYS.md) for the full guide to adding journeys.

---

## License

GPLv3 — see [LICENSE](LICENSE) for details.

**Built for the Mono community.**  
Questions? Open an issue or join the [Mono Discord](https://discord.gg/mono).
