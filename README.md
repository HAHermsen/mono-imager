# mono-imager

Flash, back up and restore the **Mono Gateway** (NXP LS1046A) from your PC, over a USB serial cable. mono-imager drives U-Boot and the recovery Linux for you: no manual `dd`, `tftp` or DIP-switch juggling.

**Version 1.9.0** · H.A. Hermsen · GPLv3

## Features

- **Flash an OS**: OpenWRT, Armbian or OPNsense over LAN or from a USB stick in the Mono, and VyOS ([LS1046A port](https://github.com/mihakralj/vyos-ls1046a-build)).
- **Update firmware**: the official Mono NOR / eMMC U-Boot firmware.
- **Snapshot / restore the eMMC**: a bit-for-bit copy to your PC or a USB stick, restored later on the same board or another one.
- **Checks every step** and ends with a pass/fail report plus a full log file.

mono-imager does **not** download OS images. Get and verify your image yourself.

## Install

Requires Python 3.10+ and a USB-to-UART adapter on the Mono's console port.

```bash
python -m venv venv
venv\Scripts\activate          # Linux/macOS: source venv/bin/activate
pip install -e .               # from a release: pip install mono-imager-1.9.0.zip
```

On Python below 3.14, `pip install zstandard` makes LAN snapshots smaller (gzip is used otherwise). More detail: [Installing-mono-imager.md](Installing-mono-imager.md).

## Use

```bash
mono-imager            # --debug for every serial command, --version
```

On launch it finds the serial port, catches U-Boot and sets up the Mono's network (DHCP, manual fallback) once per session. Everything else is a menu:

```
1) Flash OS              5) Test Serial connection    9)  Snapshot eMMC
2) Update eMMC firmware  6) Test LAN connection       10) Restore eMMC
3) Update NOR firmware   7) Test USB stick            11) Exit
4) CLI only (serial)     8) Show Device Stats
```

## Snapshot and restore

- **Snapshot** copies the whole eMMC in 1 GiB chunks, each SHA-256-checked, with a `manifest.json` and `SHA256SUMS`. *Thorough verify* also re-reads every chunk from the eMMC.
- **Restore** checks the snapshot first, offers a snapshot of the current eMMC as undo point (opt-out), and asks for `yes` before writing.
- **Names**: `dump-YYYYMMDD-HHMM`; the OS name is only stored if you type it.
- **Duration** (32 GB eMMC): LAN about 17 min (Thorough verify about 21 min), USB about 2–3x longer.
- **NOR** can be dumped read-only for forensics; it is never restored.

> **A dump contains everything on the device**: users, passwords, keys and personal data. Keep it safe. Restoring a dump on another Mono is at your own risk; neither mono-imager nor its author is responsible for security or privacy issues.

## Development

```bash
pip install -e .[dev]
python tests/run_tests.py --unit-only     # no hardware needed
ruff check mono_imager && mypy mono_imager
```

Code lives in `mono_imager/` (one journey per OS in `journeys/`, see [JOURNEYS.md](mono_imager/journeys/JOURNEYS.md)); tests and hardware tests in `tests/` (see [tests/README.md](tests/README.md)).

## License

GPLv3, see [LICENSE](LICENSE).
