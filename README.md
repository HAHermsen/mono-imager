# mono-imager

Automated firmware flashing tool for Mono Gateway Routers and the Mono Gateway Development Kit (NXP LS1046A). Talks to the device over a USB-to-UART serial connection, drives U-Boot and its recovery Linux shell, and flashes OpenWRT, Armbian, or OPNsense over LAN or USB — no manual `dd`/`tftp` fiddling required.

Version: **1.2.3** &nbsp;·&nbsp; Author: H.A. Hermsen &nbsp;·&nbsp; License: GPLv3

---

## What it does

mono-imager automates the full flashing procedure for the Mono Gateway hardware:

- Detects the serial port, connects, and interrupts U-Boot autoboot automatically.
- Boots the device into its NOR or eMMC recovery Linux shell and logs in.
- Resolves the device's own network (DHCP first, verified for real internet reachability, falling back to manual IP/subnet/gateway/DNS entry) — done once per session and reused everywhere.
- Flashes a full OS image over LAN (via a local HTTP server + `curl`/`dd` on the device) or from a USB stick plugged into the device itself.
- Refreshes eMMC/NOR firmware regions where the flashing procedure requires it, following each OS's documented official procedure.
- Falls back to a legacy `curl`+`dd`/`flashcp` path automatically on older devices that don't have the modern `firmware update` tool.
- Prints a step-by-step pass/fail report for every operation, plus a full log file.

Supported operating systems: **OpenWRT**, **Armbian**, **OPNsense** — each available via **LAN** or **USB** transfer.

## Requirements

- Python 3.10+
- A USB-to-UART serial adapter connected to the Mono Gateway device
- Packages: `pyserial>=3.5`, `icmplib>=3.0` (see `requirements.txt`)

## Install

```bash
pip install -r requirements.txt
pip install -e .
```

This registers the `mono-imager` command via the `[project.scripts]` entry point in `pyproject.toml`. Alternatively, run it straight from the repo without installing:

```bash
python -m mono_imager.cli
```

## Usage

```bash
mono-imager [--debug | --verbose]
```

| Argument | Description |
|---|---|
| `--debug`, `--verbose` | Print verbose console output — every serial command sent and received. Quiet by default; the log file always captures full detail regardless of this flag. Equivalent to setting the environment variable `MONO_DEBUG=1` before launch. |
| `--version`| Prints version, author and license |

There are no other CLI arguments — everything else (which OS, which port, which firmware file, network settings) is driven interactively through the menu once the tool starts.

### On launch

Before showing any menu, mono-imager connects to the device and resolves its network once (DHCP first, manual fallback if needed). This can take a minute or two and only happens once per session — every menu afterward reuses the result.

### Main menu

```
1) Flash OS                  — flash OpenWRT / Armbian / OPNsense via LAN or USB
2) Update eMMC firmware       — re-flash eMMC firmware only (DIP switch → NOR)
3) Update NOR firmware        — re-flash NOR firmware only (DIP switch → eMMC)
4) CLI only (serial)          — raw interactive serial console pass-through
5) Test Serial connection     — connect, interrupt U-Boot, confirm recovery login
6) Test LAN connection        — full network resolve + reach the host HTTP server
7) Test USB stick             — confirm a USB stick mounts and has usable images
8) Show Device Stats          — read and display U-Boot boot diagnostics
9) Exit
```

Flashing an OS (option 1) walks through: pick the serial port, pick the OS + transfer method (LAN or USB), point it at a firmware file (or let it auto-detect one from a plugged-in USB stick), confirm a pre-flash summary, then watch the flash run with live progress and a final pass/fail report.

## Project layout

```
mono_imager/
  cli.py                    entry point (argument parsing, logging setup)
  tui.py                    menu-driven application controller
  flash_orchestrator.py     bootstrap phases + LAN/USB flash-journey execution
  recovery_orchestrator.py  eMMC/NOR-only firmware update flows, modern/legacy detection
  step_registry.py          declarative @register_step journey system
  serial_device.py          serial I/O, U-Boot automation, recovery boot/login
  device_net.py             device network resolution (DHCP/manual/verify)
  console.py                terminal rendering (pure presentation layer)
  uboot_parse.py            U-Boot boot-output parsing (identity + self-test)
  diagnostics.py            Test Serial / Test LAN / Test USB menu logic
  journeys/                 one file per OS+transfer flashing journey
    JOURNEYS.md              journey system docs + how to add a new journey
    FIRMWARE_VS_OS_IMAGE.md  eMMC firmware-region-vs-OS-image offset explainer
tests/
  README.md                 full test suite breakdown (unit/hardware/destructive/archive)
```

## Testing

See [`tests/README.md`](tests/README.md) for the full breakdown. Quick start:

```bash
# No hardware required
for f in tests/unit/test_*.py; do python "$f"; done

# Requires a connected Mono Gateway (non-destructive)
python tests/hardware/test_serial_connect.py --port COM5
```

## License

GPLv3. See the `LICENSE` file for the full text.
