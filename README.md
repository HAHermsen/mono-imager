# mono-imager

Automated firmware flashing tool for Mono Gateway Routers and the Mono Gateway Development Kit (NXP LS1046A). Talks to the device over a USB-to-UART serial connection, drives U-Boot and its recovery Linux shell, and flashes OpenWRT, Armbian, or OPNsense over LAN or USB — no manual `dd`/`tftp` fiddling required.

Version: **1.4.0** &nbsp;·&nbsp; Author: H.A. Hermsen &nbsp;·&nbsp; License: GPLv3

---

## What it does

mono-imager automates the full flashing procedure for the Mono Gateway hardware:

- Detects the serial port, connects, and interrupts U-Boot autoboot automatically.
- Boots the device into its NOR or eMMC recovery Linux shell and logs in.
- Resolves the device's own network (DHCP first, verified for real internet reachability, falling back to manual IP/subnet/gateway/DNS entry) — done once per session and reused everywhere.
- Downloads the official MONO NOR /eMMC uboot firmware from MONO's official repo (user/password protected) and flashes it for you.
- Flashes a full OS image over LAN (via a local HTTP server + `curl`/`dd` on the device) or from a USB stick plugged into the device itself.
- Refreshes eMMC/NOR firmware regions where the flashing procedure requires it, following each OS's documented official procedure.
- Falls back to a legacy `curl`+`dd`/`flashcp` path automatically on older devices that don't have the modern `firmware update` tool.
- Prints a step-by-step pass/fail report for every operation, plus a full log file.

Supported operating systems: **OpenWRT**, **Armbian**, **OPNsense** — each available via **LAN** or **USB** transfer.

## What it does **NOT** do (and never will)

- Does NOT download any OS image files for you (official MONO uboot firmware only). You are responsible for obtaining and verifying the OS file before flashing it to the Mono Gateway.


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

See [`Installing-mono-imager.md`](Installing-mono-imager.md) for further instructons. 

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

**Ethernet port selection**: the Mono Gateway's copper RJ-45 jacks (eth0-eth2) are tried before its SFP+ cages (eth3-eth4) by default, since an unpopulated SFP cage otherwise wastes time on DHCP/reachability attempts that can never succeed. This is a soft preference, not a restriction — SFP is a legitimate WAN uplink on some deployments, so it's still tried automatically if no copper port works.

If manual IP entry is needed and a typed configuration doesn't reach the internet, the retry prompt offers:
- **y** (or Enter) — try again with the same port(s)
- **p** — pick one specific Ethernet port to target, instead of the tool cycling through every port with a live cable
- **s** — skip network setup for now (you can still use option 4, "CLI only (serial)", for a raw console, or flash an OS over USB, which needs no device-side network for Armbian)
- **n** — give up and return to the menu

### Screen behavior

Every menu appends to a continuous, scrolling transcript instead of clearing the screen and redrawing — the terminal never wipes prior output, so anything from earlier in the session (a previous step's result, an error, a warning) stays visible and scrollable, matching what the session's log file already preserves. Each screen prints a divider and a one-line status recap in place of the old boxed header:

```
------------------------------------------------------------
mono-imager 1.4.0 - 192.168.1.50/24 via 192.168.1.1 (DNS 1.1.1.1) - dhcp
```

before showing its own prompt or choices. Two screens were already scrolling before this branch existed — the live flash-progress view and the final result screen — specifically so their output stays visible for debugging; every other menu now follows the same pattern for consistency.

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
