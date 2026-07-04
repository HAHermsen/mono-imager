#!/usr/bin/env python3
"""
mono-imager: Recovery orchestration logic.

Implements the documented Mono Gateway recovery/firmware-update
procedure (https://docs.mono.si/gateway-development-kit/flashing-firmware):

  Modern path (firmware has the `firmware` command):
    1. Boot recovery from NOR, run `firmware update` (flashes eMMC)
    2. User flips DIP to eMMC, reboots, tool verifies eMMC boot
    3. Boot recovery from eMMC, run `firmware update` (flashes NOR)
    4. User flips DIP back to NOR, reboots

  Legacy path (no `firmware` command — older devices in the wild):
    curl + dd (eMMC, with the documented skip=1 seek=1 4KB offset)
    curl + flashcp (NOR)
    This tool's policy: legacy devices are always brought up to the
    CURRENT firmware via the legacy download, never re-flashed with
    old firmware.

  Which path applies is DETECTED LIVE per device (`which firmware`)
  — there is no published version cutoff to gate on; devices in the
  wild may have either.

DIP-switch flips and the reboots that follow them are physical user
actions this tool cannot perform — those steps explicitly pause and
prompt, matching the "POWER CYCLE NOW" pattern used elsewhere.

This is a SEPARATE module from flash_orchestrator.py (and gets its
own isolated `results` list) rather than reusing its reporting state,
since mixing two different orchestrators' results in one shared list
is exactly the stale-state bug class fixed earlier this session.

Author:  H.A. Hermsen
Version: v1.1.0
License: GPLv3
"""

__author__  = "H.A. Hermsen"

import re
import time
import logging
from typing import Optional, Callable

from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner

logger = logging.getLogger(__name__)
# Must match logging_setup.py's "mono_imager.console" exactly — that's the
# only logger name with a stdout handler attached (see configure_logging()).
# This used to be __name__ + ".console" ("mono_imager.recovery_orchestrator
# .console"), a different, unconfigured logger with no stdout handler — every
# console_logger.info() call in this module was silently going to the
# file-only root logger and never reaching the terminal. flash_orchestrator.py
# already uses the correct fixed name; this brings recovery_orchestrator.py
# in line with it.
console_logger = logging.getLogger("mono_imager.console")

# --- Result tracker (ISOLATED from flash_orchestrator.results — see
#     module docstring for why) -----------------------------------------

results: list[tuple[int, str, bool, str]] = []

def reset_results():
    """Clear accumulated step results before a new recovery attempt."""
    results.clear()

def step(num: int, description: str, passed: bool, reason: str = ""):
    mark = "✓" if passed else "✗"
    file_msg = f"Step {num:02d}: {'✓ PASS' if passed else '✗ FAIL'} — {description}"
    if reason:
        file_msg += f" ({reason})"
    logger.info(file_msg) if passed else logger.error(file_msg)
    console_logger.info(f"  {mark} {description}")
    results.append((num, description, passed, reason))
    return passed


# --- Firmware URLs (per documented "Manual flashing (legacy)" section) ------

LEGACY_EMMC_URL = "https://firmware.mono.si/firmware-emmc-gateway-dk.bin"
LEGACY_NOR_URL  = "https://firmware.mono.si/firmware-qspi-gateway-dk.bin"


# --- Detection ---------------------------------------------------------

def detect_modern_firmware_tool(d: SerialDevice) -> Optional[bool]:
    """
    Live-detect whether the device's CURRENT recovery Linux has the
    modern `firmware` command AND the kernel cmdline contains
    boot_medium= (set by U-Boot at boot, required by the tool to know
    which flash target to update). Both must be true for the modern
    path to work — old U-Boot versions omit boot_medium= and the
    command exits immediately with ERROR, making the modern path useless.

    Returns True if both conditions are met, False if either is absent,
    None if the detection itself failed (treat as "couldn't determine").
    """
    try:
        output = d.run_script("which firmware; echo RC=$?", marker="detect_fw_tool")
    except RuntimeError as e:
        logger.warning(f"detect_modern_firmware_tool: run_script failed: {e}")
        return None

    if "RC=" not in output:
        return None
    if "RC=0" not in output or "firmware" not in output:
        return False

    # Command exists — also verify boot_medium= is in /proc/cmdline.
    # U-Boot must pass this for `firmware update` to detect the target;
    # without it the command prints "ERROR: Cannot detect boot medium"
    # and exits immediately (confirmed on real hardware with old U-Boot).
    try:
        cmdline = d.run_script("cat /proc/cmdline", marker="check_cmdline", exec_timeout=5)
    except RuntimeError:
        cmdline = ""

    if "boot_medium=" not in cmdline:
        reason = (
            "'firmware' command present but boot_medium= absent from kernel cmdline "
            "(old U-Boot) — falling back to legacy path"
        )
        logger.info(reason)
        # Also on-screen, not just the log file: without this, "legacy
        # firmware tool detected" alone reads like a mis-detection —
        # the modern binary genuinely is there, so seeing why it's being
        # skipped (an old U-Boot never sets boot_medium=, a device-side
        # gap this tool can't fix) matters more here than for the plain
        # "no firmware command at all" case just above, which is
        # self-explanatory without extra detail.
        console_logger.info(f"  ({reason})")
        return False

    return True


def get_device_mac(d: SerialDevice, interface: str = "eth0") -> Optional[str]:
    """
    Get the device's real MAC address from `ip a`, parsed — never
    assumed or asked of the user (avoids transcription errors). Tries
    the given interface first, falls back to the first link/ether seen
    if that specific interface isn't found.
    """
    try:
        output = d.run_script(f"ip addr show {interface} 2>/dev/null || ip addr", marker="get_mac")
    except RuntimeError as e:
        logger.warning(f"get_device_mac: run_script failed: {e}")
        return None

    match = re.search(r'link/ether\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})', output)
    if match:
        return match.group(1).lower()
    return None


def check_internet_reachable(d: SerialDevice, gateway: Optional[str] = None,
                              host: str = "firmware.mono.si", timeout: int = 15) -> bool:
    """
    Verify the device can actually reach the internet — not just that
    local 'ip link'/'ip addr'/'ip route' commands reported success,
    which only confirms local interface configuration, not real
    reachability.

    CONFIRMED BUG THIS GUARDS AGAINST: a real run reported RC=0 from
    network setup (interface up, IP assigned, route added) yet
    'firmware update' still aborted within seconds of its own
    confirmation prompt, because the interface had no actual working
    path to the real internet on that physical port. Local config
    succeeding and real reachability are not the same thing, and
    nothing was checking the latter before this.

    Pings the gateway first if given (confirms basic LAN/L3
    connectivity — catches "wrong cable/port" early), then pings
    `host` (confirms DNS + a real path out to the internet — the
    actual prerequisite 'firmware update' and the legacy curl path
    both need). Returns False with a specific reason logged for
    whichever layer failed, rather than a generic failure the user
    has to guess at.
    """
    if gateway:
        try:
            gw_output = d.run_script(
                f"ping -c 2 {gateway} >/dev/null 2>&1; echo RC=$?",
                marker="check_gateway", exec_timeout=timeout,
            )
        except RuntimeError as e:
            msg = f"Could not run the gateway ping check at all: {e}"
            logger.warning(f"check_internet_reachable: {msg}")
            console_logger.info(f"  ⚠ {msg}")
            return False
        if "RC=0" not in gw_output:
            msg = (
                f"Gateway {gateway} is not reachable from the device — "
                "check the cable and which physical port is actually in use."
            )
            logger.error(msg)
            console_logger.info(f"  ⚠ {msg}")
            return False

    try:
        host_output = d.run_script(
            f"ping -c 2 {host} >/dev/null 2>&1; echo RC=$?",
            marker="check_internet_host", exec_timeout=timeout,
        )
    except RuntimeError as e:
        msg = f"Could not run the internet-host ping check at all: {e}"
        logger.warning(f"check_internet_reachable: {msg}")
        console_logger.info(f"  ⚠ {msg}")
        return False

    if "RC=0" not in host_output:
        msg = (
            f"{host} is not reachable from the device — gateway responds but "
            "there's no real path to the internet (DNS, routing, or upstream issue)."
        )
        logger.error(msg)
        console_logger.info(f"  ⚠ {msg}")
        return False

    return True


def try_dhcp(d: SerialDevice, iface: str = "eth0", timeout: int = 12) -> Optional[dict]:
    """
    Bring up `iface` and request a lease via udhcpc, then read back
    whatever the lease actually produced (IP/prefix, default gateway,
    DNS) instead of assuming success.

    Single run_script() round trip — same "one round trip beats many"
    reasoning as tui.py's _setup_recovery_network eth-up sequence:
    each round trip on this link costs real seconds, so the lease
    request and the three read-back commands are combined into one
    script body.

    -t 3 -T 2 caps udhcpc's own retry/backoff schedule (BusyBox's
    default is ~3 attempts with increasing per-attempt timeouts,
    ~20+ real seconds before giving up with no responder) — a real
    DHCP server answers the first discover in well under a second
    regardless of these flags, so this only speeds up the FAILURE
    path (no server on this network) and falls back to manual entry
    much sooner; it does not affect the success path at all.

    Returns {"ip", "prefix", "gateway", "dns"} on a lease that produced
    both an address and a default route. Returns None if udhcpc got no
    lease, or the output couldn't be parsed — callers must treat that
    as "DHCP failed" and fall back to manual entry, not guess.
    """
    try:
        output = d.run_script(
            f"ip link set {iface} up 2>/dev/null; "
            f"udhcpc -i {iface} -n -q -t 3 -T 2 2>/dev/null; "
            f"ip -4 addr show {iface}; "
            f"echo ---ROUTE---; ip route show default; "
            f"echo ---DNS---; cat /etc/resolv.conf 2>/dev/null",
            marker="try_dhcp", exec_timeout=timeout,
        )
    except RuntimeError as e:
        logger.warning(f"try_dhcp: run_script failed: {e}")
        return None

    ip_match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", output)
    gw_match = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", output)
    if not ip_match or not gw_match:
        logger.info("try_dhcp: no lease obtained (no address and/or no default route)")
        return None

    dns_match = re.search(r"nameserver\s+(\S+)", output)

    return {
        "ip": ip_match.group(1),
        "prefix": ip_match.group(2),
        "gateway": gw_match.group(1),
        "dns": dns_match.group(1) if dns_match else "",
        "iface": iface,
    }


# --- Modern path: `firmware update` -------------------------------------

def _stream_command(d: SerialDevice, command: str, idle_timeout: float = 30.0,
                     max_total: float = 900.0, auto_confirm_response: str = None,
                     on_output: Optional[Callable[[str], None]] = None) -> str:
    """
    Send a command and stream its raw output live from the serial
    port, rather than buffering it via run_script() (which blocks
    until the command returns to the shell prompt).

    This exists specifically because the real `firmware update`
    command shows its OWN interactive confirmation prompt ("Type
    'yes' to proceed") and can run for several real minutes
    (download + verify + flash) — run_script() would just sit
    waiting for a prompt that never arrives until it times out.

    If auto_confirm_response is provided, the command is automatically
    piped with the response (e.g. `echo yes | firmware update`) to
    avoid interactive prompt timing issues and input buffering bugs.

    Uses non-blocking serial reads (in_waiting check + short timeout)
    with a 10ms polling loop to monitor output and detect completion.

    Returns when either idle_timeout seconds pass with no new bytes
    (command likely finished, back at a prompt) or max_total seconds
    pass overall (hard ceiling).
    """
    # If auto_confirm_response is provided, pipe it to avoid interactive issues
    if auto_confirm_response:
        command = f"echo {auto_confirm_response} | {command}"

    d.ser.reset_input_buffer()
    d.ser.write((command + "\r\n").encode())

    buffer = b""
    last_byte_time = time.time()
    overall_start = time.time()
    poll_interval = 0.01  # 10ms polling loop

    while True:
        now = time.time()
        if now - overall_start > max_total:
            logger.warning(f"_stream_command: hit hard ceiling of {max_total}s")
            break
        if now - last_byte_time > idle_timeout:
            logger.debug(f"_stream_command: {idle_timeout}s with no new output — assuming done")
            break

        # Non-blocking: check if data is available without waiting
        if d.ser.in_waiting > 0:
            chunk = d.ser.read(256)
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                if on_output:
                    on_output(text)
                buffer += chunk
                last_byte_time = now
        else:
            # No data available; sleep briefly before polling again
            time.sleep(poll_interval)

    return buffer.decode("utf-8", errors="replace")


def run_firmware_update(d: SerialDevice, on_output: Optional[Callable[[str], None]] = None,
                         idle_timeout: float = 30.0, max_total: float = 900.0) -> bool:
    """
    Run the modern `firmware update` command and confirm it reported
    success. This command downloads, verifies, and flashes the OTHER
    medium than the one currently booted (per docs: auto-detects boot
    source, never overwrites what you're currently running from).

    Requires real internet access on the device's network — this is
    a hard, documented prerequisite for both paths, not something
    this tool can route around.

    Uses the device's own default `firmware update` mode — no source
    flag — which downloads from https://firmware.mono.si itself,
    verifies, and flashes. Confirmed via `firmware update --help` on
    real hardware that this (not `--from`) is the documented primary
    path; `--from` is for offline/USB use with pre-staged files.

    We used to pre-download .bin/.sig ourselves via curl and call
    `firmware update --from /tmp/firmware`, but that hit a 401 from
    the server that our plain curl couldn't get past (confirmed by
    cat'ing the "downloaded" file on real hardware — it was the
    401 error body, not firmware). Letting the tool do its own
    download sidesteps whatever auth/headers it needs.

    NOTE: the interactive "Type 'yes' to proceed" confirmation prompt
    still appears in this mode too — confirmed on real hardware.
    auto_confirm_text/auto_confirm_response are passed to
    _stream_command() to answer it, rather than letting the command
    self-abort after its own timeout.

    Uses _stream_command() because the real command can run for several
    real minutes (download via curl + verify + flash) — see _stream_command()'s
    docstring. Once streaming settles, the exit code is confirmed with
    a short, separate run_script() call (run_script() is fine for that
    — it's a trivial, non-interactive command).

    NOTE: confirmed on real hardware that exit code alone isn't a
    reliable success signal — a self-aborted run (prompt timed out
    with nothing answering it) still reported RC=0 despite printing
    "Aborted." and flashing nothing. The streamed output is checked
    for "Aborted" explicitly, on top of the RC check, and is always
    logged in full so a human can review it (signature verified,
    flash complete, etc.) regardless of which check fires.

    Args:
        d: connected SerialDevice, at the recovery shell.
        on_output: optional callback(text_chunk) for live progress —
            e.g. tui.py can print chunks as they arrive instead of
            the caller seeing nothing for several minutes.
        idle_timeout, max_total: passed straight to _stream_command();
            defaults match the values proven on real hardware. Only
            overridden by tests, which use a fake serial source that
            never naturally goes idle for 30 real seconds.
    """
    # Step 1: Detect which medium we're booting from (so we know which to flash)
    try:
        boot_output = d.run_script(
            "cat /proc/cmdline | grep -o 'root=/dev/[^ ]*' || echo 'root=/dev/mmcblk0p1'",
            marker="detect_boot_source", exec_timeout=5
        )
    except RuntimeError as e:
        logger.warning(f"run_firmware_update: could not detect boot source: {e}")
        # Default: assume booted from NOR, so flash eMMC
        target = "emmc"
    else:
        # If booted from mmcblk0 (eMMC), target is qspi (NOR); otherwise target is emmc
        target = "qspi" if "mmcblk0" in boot_output else "emmc"

    logger.info(f"run_firmware_update: will flash {target} (auto-detected by device)")

    # Step 2: Run firmware update, auto-confirming the device's own
    # "Type 'yes' to proceed" prompt.
    #
    # BUG FIXED: we used to pre-download .bin/.sig ourselves via curl
    # and call `firmware update --from /tmp/firmware`. That hit a 401
    # from https://firmware.mono.si that our plain curl couldn't get
    # past — confirmed on real hardware (`cat`'d the "downloaded" file,
    # it was a 26-byte "401 Authorization Required" body, not firmware).
    #
    # Confirmed via `firmware update --help` on real hardware: the
    # tool's own default mode (no source flag) downloads from
    # https://firmware.mono.si itself — that's the documented primary
    # path, not --from (which is for offline/USB use with pre-staged
    # files). Letting the tool do its own download instead of routing
    # around it with our own curl sidesteps whatever auth/headers it
    # needs that we were never going to replicate correctly.
    #
    # BUG FIXED (separate issue): the --from path was also assumed to
    # make the confirmation prompt fully non-interactive, but real
    # hardware showed the "Type 'yes' to proceed" prompt still
    # appears regardless. auto_confirm_text/auto_confirm_response
    # were already built into _stream_command() for exactly this,
    # just never passed in at this call site. Wiring them up here.
    #
    # --preserve-env is always passed (not user-configurable): without
    # it, the device's own env restore is skipped and U-Boot vars this
    # tool doesn't separately back up can be lost. Always preserving is
    # strictly safer than the previous default of not preserving.
    output = _stream_command(
        d, "firmware update --preserve-env",
        idle_timeout=idle_timeout, max_total=max_total,
        auto_confirm_response="yes",
        on_output=on_output,
    )

    try:
        rc_output = d.run_script("echo RC=$?", marker="firmware_update_rc", exec_timeout=10)
    except RuntimeError as e:
        logger.warning(f"run_firmware_update: could not verify exit code: {e}")
        rc_output = ""

    # RC=0 alone isn't a reliable success signal:
    #   • "Aborted." — self-abort on confirmation prompt, exits 0.
    #   • "ERROR:"   — e.g. "Cannot detect boot medium" when old U-Boot
    #                  omits boot_medium= from the kernel cmdline; also exits 0.
    # Both are treated as hard failures regardless of exit code.
    aborted      = "Aborted" in output
    error_output = "ERROR:" in output
    success = ("RC=0" in rc_output) and not aborted and not error_output
    logger.info(f"firmware update — full streamed output:\n{output}")
    if aborted:
        logger.error(
            "firmware update printed 'Aborted.' — the confirmation prompt "
            "was not answered in time, nothing was flashed, regardless of "
            "the reported exit code."
        )
    elif error_output:
        logger.error(
            "firmware update printed 'ERROR:' — it failed before doing "
            "anything (likely old U-Boot without boot_medium= on kernel "
            "cmdline). Legacy curl+dd fallback will be used instead."
        )
    elif not success:
        logger.error(
            f"firmware update did not report RC=0 (got: {rc_output!r}) — "
            "review the streamed output above to confirm what actually happened."
        )
    return success



def verify_boot_source(d: SerialDevice, expected: str, timeout: float = 60) -> bool:
    """
    Initiate a reboot, then confirm the device booted from the expected
    medium by watching for U-Boot's own confirmation line, exactly as
    the docs say to check manually (Step 5) and as confirmed in a real
    boot capture earlier this session:

        "RCW BOOT SRC is SD/EMMC"   (eMMC boot)
        "RCW BOOT SRC is QSPI"      (NOR boot — QSPI is the real
                                      flash interface name U-Boot
                                      uses, not "NOR")

    CRITICAL FIX (v0.9.5): The device is at recovery shell prompt when
    this is called — silent, no pending output. It won't emit boot
    diagnostics until reboot is issued. Previous code listened passively
    to the silent serial port and timed out 100% of the time.
    Now: send 'reboot' command first (line 399), then listen.

    Args:
        expected: "EMMC" or "NOR" (caller-facing naming) — mapped
            internally to the real U-Boot marker text above.
    """
    marker_text = {
        "EMMC": "RCW BOOT SRC is SD/EMMC",
        "NOR":  "RCW BOOT SRC is QSPI",
    }.get(expected.upper())

    if marker_text is None:
        raise ValueError(f"verify_boot_source: expected must be 'EMMC' or 'NOR', got {expected!r}")

    logger.info(f"Initiating reboot to verify boot source ({marker_text!r})...")

    # CRITICAL: Send reboot command NOW. Device is at recovery shell
    # prompt (silent). Without this command, the byte-reading loop below
    # listens to empty serial → timeout → false failure 100% of the time.
    try:
        d.send_command("reboot", wait_for_prompt=False)
    except Exception as e:
        logger.warning(f"reboot command exception (expected — device disconnects): {e}")

    # HARDENING (v0.9.5): After reboot is issued, the device emits
    # shutdown noise (/etc/init.d/rcK, umount messages, etc.) before
    # U-Boot starts. We need to skip this garbage and listen only for
    # U-Boot's actual boot diagnostics.
    #
    # Strategy: Watch for "U-Boot" string (appears early in U-Boot output),
    # then switch to looking for the boot source marker. This skips the
    # shutdown chatter and syncs us to the real boot output.

    import time
    start = time.time()
    buffer = b""
    uboot_found = False

    while time.time() - start < timeout:
        try:
            byte = d.ser.read(1)
            if byte:
                buffer += byte

                # First: sync to U-Boot output (skip shutdown noise)
                if not uboot_found:
                    if b"U-Boot" in buffer:
                        uboot_found = True
                        logger.debug("U-Boot output detected — now watching for boot marker")
                        buffer = b""  # reset to fresh buffer
                    continue

                # Second: look for boot source marker in U-Boot output
                if marker_text.encode() in buffer:
                    logger.info(f"✓ Boot source confirmed: {marker_text}")
                    return True
        except Exception as e:
            logger.debug(f"Serial read exception: {e}")
            break

    # Timeout without finding marker
    if not uboot_found:
        logger.warning(f"Did not detect U-Boot output within {timeout}s — device may not have rebooted")
    else:
        logger.warning(f"U-Boot detected but did not see {marker_text!r} within {timeout}s")
    return False


# --- Legacy path: curl + dd / flashcp -----------------------------------

def legacy_flash_emmc(d: SerialDevice, mac: str) -> bool:
    """
    Legacy eMMC flash exactly per the documented "Manual flashing
    (legacy)" procedure: curl with mono:{MAC} basic auth, then dd
    with the documented skip=1 seek=1 (skips the first 4KB / GPT
    region on both input and output, per the docs' own explanation).
    """
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        logger.error(f"legacy_flash_emmc: invalid MAC address: {mac!r}")
        return False
    # -z <file>: curl only re-downloads if the server's copy is newer
    # than the local file's mtime (compares Last-Modified), instead of
    # unconditionally re-fetching tens of MB every single run even when
    # a previous attempt already left the exact same file sitting there
    # (confirmed on real hardware: firmware-emmc-gateway-dk.bin survives
    # between recovery-shell boots on the same eMMC/NOR image). Safer
    # than skipping on bare filename presence, which would silently
    # flash a stale image if firmware.mono.si ever published an update.
    cmd = (
        f"curl -k -u mono:{mac} -z firmware-emmc-gateway-dk.bin -O {LEGACY_EMMC_URL} && "
        f"dd if=firmware-emmc-gateway-dk.bin of=/dev/mmcblk0 bs=4096 skip=1 seek=1; "
        f"echo RC=$?"
    )
    try:
        output = d.run_script(cmd, marker="legacy_emmc", exec_timeout=300)
    except RuntimeError as e:
        logger.error(f"legacy_flash_emmc: run_script failed: {e}")
        return False

    success = "RC=0" in output and ("records out" in output or "records in" in output)
    if not success:
        logger.error(f"legacy eMMC flash did not confirm success — output:\n{output}")
    return success


def legacy_flash_nor(d: SerialDevice, mac: str) -> bool:
    """
    Legacy NOR flash exactly per the documented procedure: curl with
    mono:{MAC} basic auth, then flashcp to /dev/mtd0.
    """
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        logger.error(f"legacy_flash_nor: invalid MAC address: {mac!r}")
        return False
    # -z: see legacy_flash_emmc() above — skip re-download only when the
    # server's copy isn't newer than what's already sitting on the device.
    cmd = (
        f"curl -k -u mono:{mac} -z firmware-qspi-gateway-dk.bin -O {LEGACY_NOR_URL} && "
        f"flashcp -v firmware-qspi-gateway-dk.bin /dev/mtd0; "
        f"echo RC=$?"
    )
    try:
        output = d.run_script(cmd, marker="legacy_nor", exec_timeout=300)
    except RuntimeError as e:
        logger.error(f"legacy_flash_nor: run_script failed: {e}")
        return False

    success = "RC=0" in output
    if not success:
        logger.error(f"legacy NOR flash did not confirm success — output:\n{output}")
    return success


# --- Top-level recovery phases -------------------------------------------
#
# These functions are UI-AGNOSTIC, same separation of concerns as
# flash_orchestrator.py's phaseN_* functions: they do not call input()
# or block waiting for a keypress. Where a PHYSICAL user action is
# required (flipping the DIP switch), the function prints the
# instruction and then actively polls the device for the RESULT of
# that action (boot source confirmation) — same pattern as
# phase1_bootstrap's "POWER CYCLE NOW" + wait_for_autoboot(). The
# caller (tui.py) is responsible for any additional pacing/messaging
# around these calls, not for driving the wait itself.

def phase_modern_flash_emmc(d: SerialDevice, on_output: Optional[Callable[[str], None]] = None) -> bool:
    """
    Modern path, step 1: from NOR-booted recovery, run `firmware
    update` to flash eMMC. Returns True on confirmed success.
    """
    console_logger.info("Running 'firmware update' to flash eMMC...")
    ok = step(1, "Flash eMMC via 'firmware update'", run_firmware_update(d, on_output=on_output))
    return ok


def phase_modern_verify_emmc_boot(d: SerialDevice, timeout: float = 90) -> bool:
    """
    Modern path, step 2: after the user flips the DIP switch to eMMC
    and reboots, confirm the device actually booted from eMMC by
    watching U-Boot's own confirmation line. Does NOT send the reboot
    itself or block on input — caller handles prompting the user to
    flip the switch and reboot; this just waits for and verifies the
    result once that happens.
    """
    ok = step(2, "Verify eMMC boot", verify_boot_source(d, "EMMC", timeout=timeout))
    return ok


def phase_modern_flash_nor(d: SerialDevice, on_output: Optional[Callable[[str], None]] = None) -> bool:
    """
    Modern path, step 3: from eMMC-booted recovery, run `firmware
    update` again — it auto-targets NOR this time since eMMC is now
    the active boot source. Returns True on confirmed success.
    """
    console_logger.info("Running 'firmware update' to flash NOR...")
    ok = step(3, "Flash NOR via 'firmware update'", run_firmware_update(d, on_output=on_output))
    return ok


def phase_modern_verify_nor_boot(d: SerialDevice, timeout: float = 90) -> bool:
    """
    Modern path, step 4: after the user flips the DIP switch back to
    NOR and reboots, confirm the device actually booted from NOR.
    """
    ok = step(4, "Verify NOR boot (back to factory default)", verify_boot_source(d, "NOR", timeout=timeout))
    return ok


def phase_legacy_flash_emmc(d: SerialDevice) -> bool:
    """
    Legacy path, step 1: get the device's real MAC, then flash eMMC
    via curl+dd per the documented legacy procedure.
    """
    mac = get_device_mac(d)
    if mac is None:
        return step(1, "Flash eMMC (legacy curl+dd)", False, "could not determine device MAC address")
    console_logger.info(f"Device MAC: {mac}")
    console_logger.info("Downloading and flashing eMMC (legacy path)...")
    ok = step(1, "Flash eMMC (legacy curl+dd)", legacy_flash_emmc(d, mac))
    return ok


def phase_legacy_flash_nor(d: SerialDevice) -> bool:
    """
    Legacy path, step 2: same MAC, flash NOR via curl+flashcp.
    """
    mac = get_device_mac(d)
    if mac is None:
        return step(2, "Flash NOR (legacy curl+flashcp)", False, "could not determine device MAC address")
    console_logger.info(f"Device MAC: {mac}")
    console_logger.info("Downloading and flashing NOR (legacy path)...")
    ok = step(2, "Flash NOR (legacy curl+flashcp)", legacy_flash_nor(d, mac))
    return ok


# --- Top-level update flows ----------------------------------------------
#
# run_emmc_update() / run_nor_update() are the single entry points
# tui.py calls for menu options 2/3 — same "one call, own your report"
# shape as a flash journey's get_journey()+.run(), and the same
# "domain module owns its full flow, including any physical-action
# pause + prompt" convention flash_orchestrator.phase1_uboot() and
# device_net.RecoveryNetwork.resolve() already use elsewhere in this
# codebase. Previously this ~120-line bootstrap/detect/flash/fallback
# sequence was duplicated almost verbatim between tui.py's
# menu_update_emmc() and menu_update_nor(); it now lives here once.
#
# soft_reboot / setup_network are passed in rather than imported —
# both are session-scoped on MonoImager (soft-reboot is a serial-only
# best-effort nudge; setup_network shares the single cached
# device-network resolution used by every other caller: journeys,
# Test LAN, startup). Passing them in keeps this module with no
# dependency on tui.py at all, same pattern as diagnostics.py.

def run_emmc_update(
    port: str,
    soft_reboot: Callable[[str], None],
    setup_network: Callable[[SerialDevice], bool],
    on_output: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Flash eMMC firmware only. Device must be in NOR recovery (DIP RIGHT)
    — bootstraps into it, detects modern vs. legacy firmware tool,
    resolves the device network, then flashes eMMC via the modern
    `firmware update` (falling back to legacy curl+dd if that fails or
    isn't available). Prints its own step-by-step report before
    returning.

    Returns True on overall success.
    """
    from mono_imager import flash_orchestrator as core

    d = None
    try:
        soft_reboot(port)
        d = core.phase1_bootstrap(port, 115200, boot_medium="qspi")
        if d is None:
            console_logger.info("")
            console_logger.info("  ❌ Could not bootstrap into the recovery shell.")
            return core.print_report()

        reset_results()
        is_modern, _fw_err = with_spinner(
            detect_modern_firmware_tool, d,
            message="Detecting firmware tool type..."
        )
        if _fw_err:
            is_modern = None

        if is_modern is None:
            console_logger.info("")
            console_logger.info("  ❌ Could not determine the device's firmware tool type.")
            return print_report()

        if not setup_network(d):
            return print_report()

        if is_modern:
            console_logger.info("")
            console_logger.info("  Modern firmware tool detected.")
            console_logger.info("")
            emmc_ok = phase_modern_flash_emmc(d, on_output=on_output)
            if not emmc_ok:
                console_logger.info("")
                console_logger.info("  ⚠ Modern 'firmware update' failed — falling back to legacy curl+dd...")
                emmc_ok, _leg_err = with_spinner(
                    phase_legacy_flash_emmc, d,
                    message="Flashing eMMC (legacy curl+dd)..."
                )
                if _leg_err:
                    emmc_ok = False
                if not emmc_ok:
                    console_logger.info("  ❌ Legacy fallback also failed for eMMC.")
                    return print_report()
                console_logger.info("  ✓ Legacy fallback succeeded.")
        else:
            console_logger.info("")
            console_logger.info("  Legacy firmware tool detected — using curl+dd directly.")
            console_logger.info("")
            emmc_ok, _leg_err = with_spinner(
                phase_legacy_flash_emmc, d,
                message="Flashing eMMC (legacy curl+dd)..."
            )
            if _leg_err:
                emmc_ok = False
            if not emmc_ok:
                return print_report()

    finally:
        if d:
            d.disconnect()

    return print_report()


def run_nor_update(
    port: str,
    soft_reboot: Callable[[str], None],
    setup_network: Callable[[SerialDevice], bool],
    on_output: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Flash NOR firmware only. Device must be in eMMC recovery (DIP LEFT)
    — bootstraps into it, detects modern vs. legacy firmware tool,
    resolves the device network, then flashes NOR via the modern
    `firmware update` (falling back to legacy curl+flashcp if that
    fails or isn't available). Does not prompt for a DIP-switch flip
    back to NOR or verify the resulting boot — the caller is
    responsible for that if/when they want it (see
    phase_modern_verify_nor_boot() below, still available but no
    longer called from here). Prints its own step-by-step report
    before returning.

    Returns True on overall success.
    """
    from mono_imager import flash_orchestrator as core

    d = None
    try:
        soft_reboot(port)
        d = core.phase1_bootstrap(port, 115200, boot_medium="emmc")
        if d is None:
            console_logger.info("")
            console_logger.info("  ❌ Could not bootstrap into the recovery shell.")
            return core.print_report()

        reset_results()
        is_modern, _fw_err = with_spinner(
            detect_modern_firmware_tool, d,
            message="Detecting firmware tool type..."
        )
        if _fw_err:
            is_modern = None

        if is_modern is None:
            console_logger.info("")
            console_logger.info("  ❌ Could not determine the device's firmware tool type.")
            return print_report()

        if not setup_network(d):
            return print_report()

        if is_modern:
            console_logger.info("")
            console_logger.info("  Modern firmware tool detected.")
            console_logger.info("")
            nor_ok = phase_modern_flash_nor(d, on_output=on_output)
            if not nor_ok:
                console_logger.info("")
                console_logger.info("  ⚠ Modern 'firmware update' failed — falling back to legacy curl+flashcp...")
                nor_ok, _leg_err = with_spinner(
                    phase_legacy_flash_nor, d,
                    message="Flashing NOR (legacy curl+flashcp)..."
                )
                if _leg_err:
                    nor_ok = False
                if not nor_ok:
                    console_logger.info("  ❌ Legacy fallback also failed for NOR.")
                    return print_report()
                console_logger.info("  ✓ Legacy fallback succeeded.")

        else:
            console_logger.info("")
            console_logger.info("  Legacy firmware tool detected — using curl+flashcp directly.")
            console_logger.info("  (No DIP-switch flip needed for this path.)")
            console_logger.info("")
            nor_ok, _leg_err = with_spinner(
                phase_legacy_flash_nor, d,
                message="Flashing NOR (legacy curl+flashcp)..."
            )
            if _leg_err:
                nor_ok = False

    finally:
        if d:
            d.disconnect()

    return print_report()


def print_report() -> bool:
    """
    Summarize the recovery attempt's results — same OK/NOK verdict
    pattern as flash_orchestrator.py's print_report(), but recovery
    doesn't have its own dedicated log file, so this only logs via
    the standard logger/console_logger rather than referencing a
    log_file path.
    """
    logger.info("=" * 60)
    logger.info("Recovery Report")
    logger.info("=" * 60)
    passed = sum(1 for _, _, p, _ in results if p)
    total = len(results)
    for num, desc, p, reason in results:
        mark = "✓ PASS" if p else "✗ FAIL"
        line = f"  Step {num:02d}: {mark} — {desc}"
        if reason:
            line += f"\n           {reason}"
        logger.info(line)
    logger.info("-" * 60)
    verdict = "OK" if total > 0 and passed == total else "NOK"
    logger.info(f"Result: {verdict} ({passed}/{total} steps passed)")

    console_logger.info("")
    if verdict == "OK":
        console_logger.info("✓ Recovery completed successfully.")
    else:
        console_logger.info("✗ Recovery did not complete successfully.")
        failed = [desc for _, desc, p, _ in results if not p]
        for desc in failed:
            console_logger.info(f"  - {desc}")

    return verdict == "OK"
