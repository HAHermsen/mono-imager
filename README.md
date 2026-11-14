# mono-imager

Automated firmware flashing tool for Mono Gateway Routers and the Mono Gateway Development Kit (NXP LS1046A). Talks to the device over a USB-to-UART serial connection, drives U-Boot and its recovery Linux shell, and flashes OpenWRT, Armbian, or OPNsense over LAN or USB — or installs VyOS — no manual `dd`/`tftp` fiddling required. It can also snapshot the whole eMMC and restore it later, to swap between configured OSes.

Version: **1.7.5** &nbsp;·&nbsp; Author: H.A. Hermsen &nbsp;·&nbsp; License: GPLv3

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

Supported operating systems: **OpenWRT**, **Armbian**, **OPNsense** — each available via **LAN** or **USB** transfer — and **VyOS** (the [LS1046A port](https://github.com/mihakralj/vyos-ls1046a-build)):

- **VyOS via LAN → USB stick in the Mono**: you point it at the `.iso` on your PC; it is sent to the Mono, written to the USB stick in the Mono (read back to confirm), live-booted, and installed to eMMC with VyOS's own `install image` — mono-imager answers the installer's questions (eMMC as the target, serial console, your chosen password). DIP stays on NOR afterwards. New in 1.5.0: built and tested without hardware (simulated device); the first real run is still to come.
- **VyOS via LAN → eMMC staging (EXPERIMENTAL, not yet tested on hardware)**: same, without a USB stick — the ISO is staged on the eMMC and booted with `toram`; it stops before installing if the live system is not running from RAM.

**eMMC snapshot / restore (new in 1.7.0, #29 — first hardware runs in progress)**: configure an OS once, snapshot the whole eMMC, and swap between finished states later — config, SSH host keys and all.

- **Snapshot** copies the entire eMMC bit for bit (no partition parsing) to this PC over LAN, or to a USB stick in the Mono. It is stored as 1 GiB chunks (FAT32-safe) with a `manifest.json` and `SHA256SUMS`, and every chunk is read back from the eMMC (page cache dropped) and must match what was stored. An optional read-only NOR dump can be kept for forensics; it is never restored.
- **Restore** checks `SHA256SUMS` before a single byte is sent, takes an automatic snapshot of the current eMMC first (so a restore is itself undoable), shows a summary and waits for an explicit `yes`, writes, and re-hashes everything on the device. On a mismatch you can retry or roll back to the auto-snapshot. The firmware region (4 KiB..32 MiB) is skipped when it is already identical.
- **Own snapshot or someone else's?** Every port's MAC address recorded in the snapshot is compared with the ones this board reports. No match = clone mode, which must be confirmed and comes with a warning: hostname, SSH host keys, IP addresses and MAC-pinned interface settings come along unchanged (mono-imager never edits the OS config), with a hint where that OS keeps them.
- **Clone mode**: `firmware update` provisions this board first, then only 0..4 KiB and 32 MiB..end are written; a smaller target eMMC is refused, a larger one gets `sgdisk -e` (when the device has it).
- **Firmware check**: the U-Boot versions of this board (NOR and eMMC) are compared with the snapshot's. A restore never writes NOR firmware, so when this board's NOR is older you are advised to stop and run "Update NOR firmware" first.
- **Inventory with every snapshot** (`inventory.txt`, key facts in `manifest.json`): date (UTC, NTP-synced over LAN), U-Boot version strings found in NOR and in the eMMC firmware region, firmware tool / recovery kernel / recovery OS, MTD partitions, eMMC chip and wear level, all MAC addresses, RAM, the U-Boot banner and power-on self-test of the boot that was caught, and both U-Boot env sets (NOR and eMMC).
- **Boots whatever the DIP position (opt-in)**: after a restore, the OS's boot variables (`bootcmd` and everything it runs — never `recovery`) can be copied into the other U-Boot env, so it boots with the DIP switch in either position. One env region is rewritten, read back and compared.
- **Nothing changes while it copies**: the OS is shut down; recovery Linux runs from RAM and the eMMC is not mounted (the snapshot refuses if it is). A clean `reboot` before the snapshot keeps the filesystems clean; a power-cycle captures a dirty filesystem as-is.
- **Health check first (read-only, warnings only)**: the GPT is checked at both ends of the disk (headers, CRCs, partitions inside the usable area and not overlapping, backup header consistent and at the real end of the disk), and every ext2/3/4 and FAT partition is checked with `e2fsck -n` / `fsck.vfat -n` when recovery Linux has those tools. The result is stored in `manifest.json`; on errors you decide whether to snapshot anyway. An entry-CRC mismatch caused by Mono's firmware region overlapping the OS image's own GPT (Armbian, OPNsense) is reported as a warning — it boots fine.
- **No DIP switch in the happy path**: you reboot the running OS cleanly, mono-imager catches U-Boot and loads NOR recovery Linux into RAM (`sf read` + `booti`, nothing saved). Only when that is impossible (blank eMMC, `bootdelay=0`, a U-Boot without `sf`) are you asked to flip the DIP switch to NOR, and back afterwards.
- **NOR U-Boot env travels with the snapshot**: the 8 KiB `uboot-env` region is read from NOR (read-only, whatever the DIP position) and stored as `nor_env.bin` + readable `nor_env.txt`. On a same-board restore, if NOR's env differs, you are offered to write it back — the only time snapshot/restore writes NOR, and only that env region, never the firmware; it is read back and compared. Clone mode never offers it (the env holds the other board's settings, such as MAC addresses). Declined, the restore prints the `setenv` commands the OS needs instead.
- LAN snapshots are compressed on the PC (zstd when available: Python 3.14+, or `pip install zstandard`; gzip otherwise), so recovery Linux needs no zstd. USB snapshots are compressed on the device with the best tool it has (zstd, gzip, or none).

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
mono-imager 1.7.5 - 192.168.1.50/24 via 192.168.1.1 (DNS 1.1.1.1) - dhcp
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
9) Snapshot eMMC              — whole eMMC -> this PC (LAN) or a USB stick in the Mono
10) Restore eMMC              — snapshot -> whole eMMC (auto-snapshot first)
11) Exit
```

Flashing an OS (option 1) walks through: pick the serial port, pick the OS + transfer method (LAN or USB), point it at a firmware file (or let it auto-detect one from a plugged-in USB stick), confirm a pre-flash summary, then watch the flash run with live progress and a final pass/fail report.

## Project layout

```
mono_imager/
  cli.py                    entry point (argument parsing, logging setup)
  tui.py                    menu-driven application controller
  constants.py              shared hardware facts and defaults (serial, paths, eMMC/NOR layout, timeouts)
  flash_orchestrator.py     bootstrap phases + LAN/USB flash-journey execution
  recovery_orchestrator.py  eMMC/NOR-only firmware update flows, modern/legacy detection
  step_registry.py          declarative @register_step journey system
  serial_device.py          serial I/O, U-Boot automation, recovery boot/login
  device_net.py             device network resolution (DHCP/manual/verify)
  console.py                terminal rendering (pure presentation layer)
  uboot_parse.py            U-Boot boot-output parsing (identity + self-test)
  diagnostics.py            Test Serial / Test LAN / Test USB menu logic
  snapshot.py               eMMC snapshot/restore: format, planning, recovery-Linux scripts
  snapshot_server.py        streaming HTTP upload/serve for LAN snapshot/restore
  snapshot_orchestrator.py  Snapshot eMMC / Restore eMMC journeys
  inventory.py              platform inventory kept with every snapshot, U-Boot version compare
  uboot_env.py              raw U-Boot env regions: parse, rebuild, boot-variable closure
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
