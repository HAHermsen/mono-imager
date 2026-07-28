# Installing mono-imager (Beginner's Guide)

This guide walks you through installing **mono-imager** step by step.

## What you'll need first

1. **A computer** running Windows, macOS, or Linux.
2. **Python 3.10 or newer** installed.
   - Check if you already have it: open a terminal and type:
     ```
     python3 --version
     ```
     (On Windows, try `python --version` instead.)
   - If it prints a version number of 3.10 or higher, you're set.
   - If not, download Python from https://www.python.org/downloads/ and install it. On Windows, tick "Add Python to PATH" during setup.
3. **A USB cable**, connected between your computer and the Mono Gateway device on the **MIDDLE** USB C socket on the Mono.

## What's a "terminal"?

- **Windows**: search for "Command Prompt" or "PowerShell" in the Start menu.
- **macOS**: open "Terminal" from Applications → Utilities.
- **Linux**: usually `Ctrl+Alt+T`, or search "Terminal".

You'll type commands into this window and press Enter after each one.

## Step 1 — Get the mono-imager files

Download or copy the mono-imager project folder onto your computer. In your terminal, navigate into that folder, for example:

```
cd path/to/mono-imager
```

(Replace `path/to/mono-imager` with wherever you saved it.)

## Step 2 — Install the required packages

Type this and press Enter:

```
pip install -r requirements.txt
```

This installs two small helper packages the tool needs (`pyserial` and `icmplib`).

## Step 3 — Install mono-imager itself

Type:

```
pip install -e .
```

This makes the `mono-imager` command available to run from anywhere in your terminal.

> **If `pip` isn't recognized:** try `pip3` instead of `pip`, or `python3 -m pip` / `python -m pip` in front of the same command.

## Step 4 — Run it

Type:

```
mono-imager
```

If that doesn't work for any reason, you can run it this way instead, from inside the mono-imager folder:

```
python -m mono_imager.cli
```

Either way, the tool will connect to your device and show you a menu of options (Flash OS, update firmware, run tests, etc.). Everything from there is menu-driven — you don't need to type any more commands.

## Troubleshooting

- **"command not found"** — make sure Python was installed with "Add to PATH" checked (Windows), or restart your terminal after installing.
- **Permission errors on macOS/Linux** — try adding `--user` to the pip commands, e.g. `pip install -r requirements.txt --user`.
- **Nothing happens when connecting to the device** — double-check the USB-to-UART adapter is plugged in and recognized by your computer before launching mono-imager.
