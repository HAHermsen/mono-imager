#!/usr/bin/env python3
"""
mono-imager: standalone hardware diagnostics.

Extracted from tui.py (menu options "Test Serial", "Test LAN", "Test USB
stick") — same principle as recovery_orchestrator.py / flash_orchestrator.py:
this module owns the actual hardware interaction and prints its own live
progress and summary, but never touches MenuState and never decides where
the menu goes next. Session-state values that depend on MonoImager (the
cached device network, the soft-reboot helper) are passed in as callables
rather than imported, so this module has no dependency on tui.py at all.

Each test_*() function returns just enough for the caller to decide what
session state is safe to persist (matching the original behaviour: a
port/network is only remembered once the connection that produced it is
actually proven, not just attempted).

Author:  H.A. Hermsen
License: GPLv3
"""

import time
import tempfile
import pathlib
from pathlib import Path
from typing import Callable, Optional

from mono_imager import console
from mono_imager.spinner import with_spinner, Spinner


def test_serial(port: str) -> bool:
    """
    Serial connectivity test: connect, interrupt U-Boot, confirm a
    command response, boot recovery Linux, confirm login.

    Returns True as soon as the initial connect succeeds — the caller
    should persist this port in that case, regardless of how the later
    checks turn out (mirrors the original "remember the working port"
    rule).
    """
    from mono_imager.serial_device import SerialDevice

    print()
    print(f"  Port:  {port}")
    print("  Baud:  115200")
    print()

    results = []

    d = SerialDevice(port, timeout=5)
    connected = console.check(results, "Connect at 115200 baud", d.connect(115200))
    if not connected:
        input("\n  Press Enter to return to main menu...")
        return False

    try:
        print()
        print("  " + "─" * 56)
        print("  ⚡  POWER CYCLE YOUR DEVICE NOW  ⚡")
        print("  " + "─" * 56)
        print()
        autoboot_ok, autoboot_err = with_spinner(
            d.wait_for_autoboot, timeout=60,
            message="Waiting for U-Boot autoboot interrupt..."
        )
        if autoboot_err:
            autoboot_ok = False
        console.check(results, "U-Boot autoboot interrupted", bool(autoboot_ok))
        if not results[-1]:
            input("\n  Press Enter to return to main menu...")
            return connected

        response = d.send_command("printenv ethact", timeout=5)
        console.check(results, "U-Boot responds to commands",
                       bool(response.strip()),
                       response.strip() if response.strip() else "no response")

        booted = False
        buffer = b""
        with Spinner("Booting recovery Linux..."):
            d.send_command("run recovery", wait_for_prompt=False, timeout=3)
            start = time.time()
            while time.time() - start < 60:
                byte = d.ser.read(1)
                if byte:
                    buffer += byte
                    if b"root@recovery" in buffer or b"login:" in buffer:
                        if b"login:" in buffer and b"root@recovery" not in buffer:
                            d.ser.write(b"root\r\n")
                            time.sleep(1)
                        booted = True
                        break
        console.check(results, "Recovery Linux booted", booted)

        if booted:
            d.ser.write(b"\r\n")
            time.sleep(0.5)
            waiting = d.ser.in_waiting
            response = d.ser.read(waiting) if waiting else b""
            at_shell = b"root@recovery" in buffer or b"root@recovery" in response
            console.check(results, "Logged into recovery shell", at_shell)

    finally:
        d.disconnect()

    print()
    print("  " + "─" * 56)
    total  = len(results)
    passed = sum(results)
    if passed == total:
        print(f"  ✓  All {total} checks passed — serial connection is healthy.")
    else:
        print(f"  ✗  {total - passed}/{total} checks failed.")

    input("\n  Press Enter to return to main menu...")
    return connected


def test_lan(
    port: str,
    host_ip: Optional[str],
    soft_reboot: Callable[[str], None],
    setup_network: Callable[[object], bool],
    get_device_net: Callable[[], Optional[dict]],
) -> Optional[dict]:
    """
    Full end-to-end LAN test — boots device into recovery, sets up
    networking, and confirms the device can reach the host HTTP server.

    soft_reboot / setup_network / get_device_net are passed in rather
    than imported because they're session-scoped on MonoImager (the
    cached device network in particular must stay the single source of
    truth used everywhere else — journeys, eMMC/NOR updates, startup).

    Returns {"serial_port", "host_ip", "device_ip"} to persist once the
    connection + network are proven, or None if the test didn't get far
    enough to trust those values (mirrors the original behaviour: the
    final reachability check can still fail without invalidating a
    working port/network).
    """
    from mono_imager.flash_orchestrator import (
        phase1_bootstrap, detect_host_ip,
        start_http_server, wait_for_report,
    )

    print("  Test LAN Connection")
    print("  " + "─" * 56)
    print()

    results = []

    print()
    print("  Rebooting device into recovery Linux...")
    soft_reboot(port)

    d = phase1_bootstrap(port, 115200)
    if not console.check(results, "Device in recovery shell", d is not None):
        input("\n  Press Enter to return to main menu...")
        return None

    try:
        host_ip = host_ip or detect_host_ip()
        if not console.check(results, "Host IP detected", bool(host_ip), host_ip or "could not detect"):
            input("\n  Press Enter to return to main menu...")
            return None

        if not console.check(results, "Device network ready", setup_network(d)):
            input("\n  Press Enter to return to main menu...")
            return None
        device_net = get_device_net() or {}
        device_ip = device_net.get("ip")

        http_port = 18080
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as _tf:
            _tf.write(b"LAN_TEST")
            tmp = pathlib.Path(_tf.name)
        server = start_http_server(host_ip, http_port, tmp)
        if not console.check(results, f"HTTP server up on {host_ip}:{http_port}", server is not None):
            tmp.unlink(missing_ok=True)
            input("\n  Press Enter to return to main menu...")
            return None

        url = f"http://{host_ip}:{http_port}/firmware.img"
        check_script = (
            f"curl -sk -I -o /dev/null -w '%{{http_code}}' {url} "
            f"> /tmp/lantest_code.txt; "
            f"curl -sk -X POST --data-binary @/tmp/lantest_code.txt "
            f"\"http://{host_ip}:{http_port}/report?step=lantest\" >/dev/null 2>&1"
        )
        try:
            d.launch_script(check_script, marker="lantest")
            report, _rep_err = with_spinner(
                wait_for_report, "lantest", timeout=15.0,
                message="Waiting for device HTTP report..."
            )
            device_sees_host = report is not None and "200" in report
        except Exception:
            device_sees_host = False
        finally:
            server.shutdown()
            tmp.unlink(missing_ok=True)

        _curl_detail = (
            "" if device_sees_host else
            "device's network is up but can't reach the host — either port 18080 is "
            "blocked (allow Python.exe through Windows Defender Firewall) or the cable "
            "doesn't connect to the SAME router/switch as the host"
        )
        console.check(results, "Device can reach host HTTP server", device_sees_host, _curl_detail)

    finally:
        if d:
            d.disconnect()

    print()
    print("  " + "─" * 56)
    passed_count = sum(results)
    total        = len(results)
    if passed_count == total:
        print("  ✓  LAN path confirmed end-to-end.")
    else:
        print(f"  ✗  {total - passed_count}/{total} checks failed.")

    input("\n  Press Enter to return to main menu...")
    return {"serial_port": port, "host_ip": host_ip, "device_ip": device_ip}


def test_usb(port: str) -> bool:
    """
    Verify a USB stick is connected, mountable, and (optionally) already
    staged with recognizable OS images.

    Returns True once the mount succeeds — the caller should persist
    this port in that case, regardless of whether an OS image was
    actually found on the stick (mirrors the original behaviour).
    """
    import logging
    from mono_imager.flash_orchestrator import phase1_bootstrap
    from mono_imager.journeys.usb_utils import check_usb_size, find_image_on_usb

    logger = logging.getLogger(__name__)

    print("  Test USB Stick")
    print("  " + "─" * 56)
    print()

    results = []

    d = phase1_bootstrap(port, 115200)
    if not console.check(results, "Device in recovery shell", d is not None):
        input("\n  Press Enter to return to main menu...")
        return False

    usb_device = "/dev/sda"
    usb_mount  = "/mnt/usb"
    mounted    = False

    try:
        try:
            d.send_command(f"mkdir -p {usb_mount}", timeout=5)
            response, _mnt_err = with_spinner(
                d.send_command, f"mount {usb_device}1 {usb_mount} 2>&1; echo RC=$?",
                timeout=15, message="Mounting USB stick..."
            )
            if _mnt_err:
                raise _mnt_err
            mounted = "RC=0" in response
            if not mounted:
                response = d.send_command(f"mount {usb_device} {usb_mount} 2>&1; echo RC=$?", timeout=15)
                mounted = "RC=0" in response
        except Exception as e:
            mounted, response = False, str(e)

        if not console.check(results, f"USB mounted ({usb_device} -> {usb_mount})", mounted,
                              "" if mounted else "no USB stick detected, or it's not FAT32/exFAT formatted"):
            input("\n  Press Enter to return to main menu...")
            return False

        check_usb_size(d, usb_mount)

        print()
        found = {}
        for os_name in ["OPNsense", "OpenWRT", "Armbian"]:
            path, _fmt = find_image_on_usb(d, usb_mount, os_name)
            found[os_name] = path
            mark = "✓" if path else "·"
            detail = f" — {Path(path).name}" if path else " (not found)"
            print(f"  {mark}  {os_name} image{detail}")

        any_found = any(found.values())
        console.check(results, "At least one recognizable OS image on stick", any_found,
                      "" if any_found else "stick mounts fine but no armbian*/openwrt*/opnsense* "
                                            "image found — see README for expected filenames")

    finally:
        try:
            d.send_command(f"umount {usb_mount} 2>&1; sync", timeout=15)
        except Exception as e:
            print(f"⚠ USB unmount warning: {e}", flush=True)
            logger.warning(f"USB unmount warning: {e}")
        d.disconnect()

    print()
    print("  " + "─" * 56)
    passed_count = sum(results)
    total        = len(results)
    if passed_count == total:
        print("  ✓  USB stick mounted and verified.")
    else:
        print(f"  ✗  {total - passed_count}/{total} checks failed.")

    input("\n  Press Enter to return to main menu...")
    return mounted
