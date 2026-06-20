# mono-imager

Automated firmware flashing tool for Mono Gateway Routers and the Mono Development Kit.  
Provides a guided, reliable flashing experience with serial and network support, U‑Boot recovery handling, dual‑flash orchestration (NOR ↔ eMMC), and safe rollback.

# Features Overview

## Serial Features
- **[Serial USB Port Detection](ca://s?q=Explain_serial_usb_port_detection)** — Autodetects the MONO Gateway and selects the correct COM port and baudrate automatically.  
- **[Auto Serial USB Reconnect Engine](ca://s?q=Explain_auto_serial_usb_reconnect_engine)** — Survives USB disconnects, FTDI resets, device reboots, and COM‑port re‑enumeration without user intervention.  
- **[Fault Tolerant](ca://s?q=Explain_fault_tolerant_serial_io)** — All serial I/O is wrapped into fault‑tolerant logic that prevents crashes and auto‑triggers reconnects.  
- **[Hotplug Support](ca://s?q=Explain_hotplug_support)** — Fully supports unplug/replug cycles during flashing, bootloader transitions, and recovery mode.  
- **[Reliability](ca://s?q=Explain_serial_reliability)** — Designed for reliable firmware flashing and embedded development workflows.
- **[Interactive & Simulated Hotplug Tests](ca://s?q=Explain_interactive_and_simulated_hotplug_tests)** — Validated with both real hardware unplugging and automated software‑simulated disconnects.  

## Network Features
- **[DHCP Auto‑Discovery](ca://s?q=Explain_DHCP_auto_discovery)** — Automatically detects the MONO Gateway’s IP address on the local network.  
- **[ARP & Link Detection](ca://s?q=Explain_ARP_link_detection)** — Identifies devices even when DHCP is slow or unavailable.  
- **[Network Reachability Checks](ca://s?q=Explain_network_reachability_checks)** — Verifies connectivity before flashing to avoid half‑written images.  
- **[Retry‑Safe Networking](ca://s?q=Explain_retry_safe_networking)** — Handles NO‑CARRIER, flaky Wi‑Fi, and transient Ethernet drops gracefully.  
- **[Secure Transfer Support](ca://s?q=Explain_secure_transfer_support)** — Uses safe, checksum‑verified transfer methods for firmware delivery.  
- **[Boot‑Phase Network Detection](ca://s?q=Explain_boot_phase_network_detection)** — Detects when the device switches between U‑Boot, initramfs, and Linux networking.  
- **[Gateway Identity Verification](ca://s?q=Explain_gateway_identity_verification)** — Confirms the device is the correct MONO Gateway before flashing.

## Flashing Features
- **[Menu Driven](ca://s?q=Explain_menu_driven_interface)** — Simple, intuitive menu‑driven CLI.  
- **[One‑Command Flashing](ca://s?q=Explain_one_command_flashing)** — No TFTP, firewall rules, or manual U‑Boot commands.  
- **[Dual‑Flash Support](ca://s?q=Explain_dual_flash_support)** — NOR → eMMC → NOR safe flashing sequence.  
- **[Retry Logic](ca://s?q=Explain_retry_logic)** — Handles network flakes and NO‑CARRIER issues.  
- **[Pre‑Built Firmware Download](ca://s?q=Explain_prebuilt_firmware_download)** — Fetches images directly from Mono/Armbian official sources.  
- **[Custom Image Support](ca://s?q=Explain_custom_image_support)** — Flash any local `.img` or `.bin` file.  
- **[Boot Verification](ca://s?q=Explain_boot_verification)** — Confirms successful flashing before proceeding.


## Requirements

- **Python 3.8+**  
- **USB‑C cable** (for initial device onboarding)  
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