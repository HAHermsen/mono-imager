# mono-imager

Automated firmware flashing tool for Mono Gateway Routers and Development Kit. Supports serial and networked connections. Handles U-Boot recovery boot, network setup retry logic, dual-flash orchestration (NOR ↔ eMMC), and safe rollback.

## Features

- 🔌 **Serial Detection** — Auto-detect USB-to-UART connections
- ⚡ **Automatic Baud Rate Detection** — No manual configuration needed
- 🚀 **One-Command Flashing** — No TFTP, firewall rules, or manual U-Boot commands
- 🔄 **Dual-Flash Support** — NOR → eMMC → NOR safe orchestration
- 🛡️ **Retry Logic** — Handles network flakes and NO-CARRIER ghosts
- 📥 **Firmware Download** — Direct from Armbian/Mono official sources
- ✅ **Verify Boot** — Confirms successful flashing

## Requirements

- **Python 3.8+**
- **USB-to-UART serial cable** (for serial connection)
- **Ethernet connection** (for device network setup)

## Installation

### Windows

1. Install Python 3.8+ from [python.org](https://www.python.org/downloads/) (check "Add Python to PATH")

2. Clone the repository:
   ```bash
   git clone https://github.com/HAHermsen/mono-imager.git
   cd mono-imager
   ```

3. Create virtual environment:
   ```bash
   python -m venv venv
   venv\Scripts\activate
   ```

4. Install mono-imager:
   ```bash
   pip install -e .
   ```

### macOS / Linux

1. Install Python 3.8+ (via Homebrew on macOS, package manager on Linux)

2. Clone the repository:
   ```bash
   git clone https://github.com/HAHermsen/mono-imager.git
   cd mono-imager
   ```

3. Create virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

4. Install mono-imager:
   ```bash
   pip install -e .
   ```

## Usage

1. **Plug in your Mono device via USB-to-UART serial cable**

2. **Run mono-imager:**
   ```bash
   mono-imager
   ```

3. **Follow the menu:**
   - Select connection type (Serial/Network)
   - Choose device
   - Select flash mode (eMMC/NOR/Dual)
   - Confirm and flash

4. **Device reboots with new firmware**

## Troubleshooting

- **No serial devices found?** Ensure USB cable is plugged in and drivers are installed
- **NO-CARRIER error?** Tool will retry automatically; ensure Ethernet is connected
- **Flashing takes time** — Large images can take 5-10 minutes

For more help, see [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) or open an issue on GitHub.

## Development

To contribute:

```bash
git clone https://github.com/HAHermsen/mono-imager.git
cd mono-imager
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

See [CONTRIBUTING.md](docs/CONTRIBUTING.md) for guidelines.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Credits

Built for the Mono community with support from Mono team.

---

**Have questions?** Open an issue or join the [Mono Discord](https://discord.gg/mono).