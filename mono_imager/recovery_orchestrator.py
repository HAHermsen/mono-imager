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
Version: v1.2.9
License: GPLv3
"""

__author__  = "H.A. Hermsen"

import re
import time
import logging
from typing import Optional, Callable

from mono_imager.serial_device import SerialDevice
from mono_imager.spinner import with_spinner
from mono_imager.step_tracker import StepTracker

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
# Bookkeeping itself (format/log/accumulate) is shared via StepTracker —
# see step_tracker.py's module docstring — but this module keeps its own
# instance/list, same isolation as before.

_tracker = StepTracker(logger, console_logger, auto_number=False)
results  = _tracker.results

def reset_results():
    """Clear accumulated step results before a new recovery attempt."""
    _tracker.reset()

step = _tracker.step


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
                              host: str = "firmware.mono.si",
                              ping_ip: str = "8.8.8.8", timeout: int = 15) -> bool:
    """
    Validate real WAN connectivity in two INDEPENDENT checks, so a failure
    points at the layer that is actually broken (issue #16):

      1. Routing: ping a raw WAN IP (ping_ip, default 8.8.8.8). A numeric
         target needs no DNS, so this isolates reachability/routing. The
         default gateway is deliberately NOT pinged — a firewall gateway
         dropping ICMP to itself is expected behaviour, not a failure.
      2. DNS: resolve `host` with nslookup. 'firmware update' downloads
         from firmware.mono.si, so name resolution must work even when
         routing already does.

    Returns True only if both pass. `gateway` is accepted for backward
    compatibility but no longer pinged.
    """
    # 1. Routing / reachability — ping a raw WAN IP (no DNS dependency).
    try:
        ping_out = d.run_script(
            f"ping -c 2 {ping_ip} >/dev/null 2>&1; echo RC=$?",
            marker="check_wan_ping", exec_timeout=timeout,
        )
    except RuntimeError as e:
        msg = f"Could not run the WAN ping check: {e}"
        logger.warning(f"check_internet_reachable: {msg}")
        console_logger.info(f"  ⚠ {msg}")
        return False
    if "RC=0" not in ping_out:
        msg = (
            f"No route to the internet — ping to {ping_ip} failed. Check the "
            "device IP, gateway, cable, and which physical port is in use."
        )
        logger.error(msg)
        console_logger.info(f"  ⚠ {msg}")
        return False
    console_logger.info(f"  ✓ Reachability OK (ping {ping_ip}).")

    # 2. DNS — resolve the real firmware host with nslookup.
    try:
        dns_out = d.run_script(
            f"nslookup {host} >/dev/null 2>&1; echo RC=$?",
            marker="check_dns", exec_timeout=timeout,
        )
    except RuntimeError as e:
        msg = f"Could not run the DNS check: {e}"
        logger.warning(f"check_internet_reachable: {msg}")
        console_logger.info(f"  ⚠ {msg}")
        return False
    if "RC=0" not in dns_out:
        msg = (
            f"DNS lookup failed ({host}) — routing works but name resolution "
            "does not. Check the DNS server you set is reachable and correct."
        )
        logger.error(msg)
        console_logger.info(f"  ⚠ {msg}")
        return False
    console_logger.info(f"  ✓ DNS lookup OK ({host}).")

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
                     on_output: Optional[Callable[[str], None]] = None,
                     done_markers: Optional[list] = None) -> str:
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
    marker_seen_at = None  # set once a done_marker appears in the output
    marker_grace = 0.3     # after a done marker the result is known - stop almost immediately

    while True:
        now = time.time()
        if now - overall_start > max_total:
            logger.warning(f"_stream_command: hit hard ceiling of {max_total}s")
            break
        if now - last_byte_time > idle_timeout:
            logger.debug(f"_stream_command: {idle_timeout}s with no new output - assuming done")
            break
        # A completion marker (e.g. "Firmware update complete") means the
        # command is done - stop after a short grace to catch the trailing
        # shell prompt, instead of waiting out the full idle_timeout (which
        # looked like a ~30s hang after the flash finished).
        if marker_seen_at is not None and now - marker_seen_at > marker_grace:
            logger.debug("_stream_command: done marker seen - finishing early")
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
                if marker_seen_at is None and done_markers:
                    tail = buffer.decode("utf-8", errors="replace")
                    if any(m in tail for m in done_markers):
                        marker_seen_at = now
        else:
            # No data available; sleep briefly before polling again
            time.sleep(poll_interval)

    return buffer.decode("utf-8", errors="replace")


def sync_device_clock(d: SerialDevice, timeout: float = 15.0) -> bool:
    """
    Best-effort NTP time sync in the recovery shell, via busybox
    ntpd's one-shot query mode (-q: set the clock and exit, -n: stay
    in the foreground so run_script can wait on it).

    Recovery Linux can boot with a stale clock. `firmware update`
    downloads over HTTPS with TLS verification (meta-mono removed
    `curl -k` from the tool on 2026-04-11), which OpenSSL rejects
    outright — "certificate is not yet valid" — whenever the device's
    clock sits behind the cert's notBefore date. Confirmed on real
    hardware (#23).

    Best-effort by design: returns False on any failure rather than
    raising. The result is now shown on the console, together with the
    device's resulting UTC time (#24), instead of only in the log file —
    so a user can see whether the clock was actually set before the
    TLS-verified download runs.
    """
    console_logger.info("  Syncing device clock via NTP (pool.ntp.org)...")
    try:
        output = d.run_script(
            "ntpd -q -n -p pool.ntp.org 2>&1; echo RC=$?; "
            "date -u '+DEVICE_TIME=%Y-%m-%d %H:%M:%S UTC'",
            marker="sync_device_clock", exec_timeout=timeout,
        )
    except RuntimeError as e:
        logger.warning(f"sync_device_clock: run_script failed: {e}")
        console_logger.info(f"  ⚠ NTP sync could not run ({e}) — continuing with the current clock.")
        return False
    ok = "RC=0" in output
    m = re.search(r"DEVICE_TIME=([0-9-]+ [0-9:]+ UTC)", output)
    now = m.group(1) if m else "unknown"
    if ok:
        console_logger.info(f"  ✓ Clock synced — device time {now}")
    else:
        logger.warning(f"sync_device_clock: ntpd did not report success — output:\n{output}")
        console_logger.info(
            f"  ⚠ NTP sync failed — device time {now}. If it is wrong, the "
            "HTTPS download may fail with 'certificate is not yet valid'."
        )
    return ok


# --- firmware tool capabilities / helpers (#24) ---------------------------
#
# Facts below are taken from the official tool's source,
# we-are-mono/meta-mono recipes-support/firmware-tools/files/firmware:
#   - `update` options: --usb, --from PATH, --url URL, --preserve-env
#     (--from/--usb/--preserve-env all added 2026-04-18). Default mode is a
#     FULL REWRITE: the target's U-Boot env resets to firmware defaults.
#     Before 2026-04-18 the tool always backed up + restored the env and
#     rejected unknown options ("ERROR: Unknown option").
#   - Files: firmware-<emmc|qspi>-gateway-dk.bin + .bin.sig, fetched from
#     https://firmware.mono.si with basic auth mono:<MAC of the first
#     interface `ip -o link` lists with an ether address>.
#   - The .bin is ALWAYS verified against /etc/firmware/firmware-signing.pub
#     (openssl dgst -sha256 -verify) before flashing — which is why
#     downloading it ourselves with `curl -k` is safe: TLS only protects
#     the transport, the signature check protects what gets flashed.
#   - U-Boot env: QSPI = MTD partition labelled "uboot-env", 0x2000 bytes at
#     offset 0; eMMC = /dev/mmcblk0, 0x2000 bytes at offset 0x300000.

FIRMWARE_BASE_URL = "https://firmware.mono.si"
FIRMWARE_MACHINE  = "gateway-dk"
MANUAL_FW_DIR     = "/tmp/mono_imager_fw"

UBOOT_ENV_SIZE        = 0x2000
UBOOT_ENV_EMMC_OFFSET = 0x300000
UBOOT_ENV_STASH       = "/tmp/mono_imager_uboot_env.bin"

# curl/openssl wording for a TLS failure caused by a wrong device clock
# (or TLS in general) — the case NTP re-sync + retry can actually fix.
_TLS_ERROR_RE = re.compile(
    r"certificate is not yet valid|certificate has expired|"
    r"SSL certificate problem|curl: \((?:35|60)\)",
    re.IGNORECASE,
)


def default_preserve_env(target: str) -> bool:
    """
    Default for the "preserve U-Boot env?" choice, per target medium.

    eMMC: False. Preserving on an eMMC flash restores the device's OLD eMMC
    env (e.g. a prior Armbian env with no "recovery" command) over the new
    firmware's env — that wiped the "recovery" command option 3 (NOR
    update) needs to boot recovery from eMMC ("run recovery -> not
    defined"). NOR (qspi): True — NOR carries the boot settings
    (e.g. OPNsense's bootcmd) the user normally wants to keep.
    """
    return target != "emmc"


def is_tls_error(output: str) -> bool:
    """True if `firmware update` output shows a TLS/certificate failure."""
    return bool(_TLS_ERROR_RE.search(output or ""))


def firmware_tool_caps(d: SerialDevice) -> dict:
    """
    Which `firmware update` options this device's tool supports, from
    `firmware help`. Returns {"preserve_env": bool, "from": bool}.

    Only downgrades when the help text was actually read (contains
    "Usage: firmware") and the option is absent — an unreadable/odd
    response keeps the current assumption (modern tool, both supported)
    rather than guessing the device is old.
    """
    caps = {"preserve_env": True, "from": True}
    try:
        out = d.run_script("firmware help 2>&1", marker="fw_tool_caps", exec_timeout=10)
    except RuntimeError as e:
        logger.warning(f"firmware_tool_caps: run_script failed: {e}")
        return caps
    if "Usage: firmware" in out:
        caps["preserve_env"] = "--preserve-env" in out
        caps["from"] = "--from" in out
    logger.info(f"firmware_tool_caps: {caps}")
    return caps


def _run_firmware_cmd(d: SerialDevice, fw_cmd: str, on_output, idle_timeout: float,
                      max_total: float):
    """
    Stream one `firmware update ...` invocation, auto-answering its
    "Type 'yes' to proceed" prompt, and judge the result.
    Returns (success, output).

    NOTE: confirmed on real hardware that exit code alone isn't a
    reliable success signal — a self-aborted run (prompt timed out with
    nothing answering it) still reported RC=0 despite printing
    "Aborted." and flashing nothing; "ERROR:" (die()) is likewise a hard
    failure. The tool's own "Firmware update complete" line is a
    definitive success signal and skips the slow `echo RC=$?` round-trip
    (~20 s of run_script hops).
    """
    output = _stream_command(
        d, fw_cmd,
        idle_timeout=idle_timeout, max_total=max_total,
        auto_confirm_response="yes",
        on_output=on_output,
        done_markers=["Firmware update complete", "Aborted", "ERROR:"],
    )

    aborted      = "Aborted" in output
    error_output = "ERROR:" in output
    completed    = "Firmware update complete" in output

    if completed:
        rc_output = "RC=0 (inferred from 'Firmware update complete')"
    else:
        try:
            rc_output = d.run_script("echo RC=$?", marker="firmware_update_rc", exec_timeout=10)
        except RuntimeError as e:
            logger.warning(f"run_firmware_update: could not verify exit code: {e}")
            rc_output = ""

    success = ("RC=0" in rc_output) and not aborted and not error_output
    logger.info(f"{fw_cmd} — full streamed output:\n{output}")
    if aborted:
        logger.error(
            "firmware update printed 'Aborted.' — the confirmation prompt "
            "was not answered in time, nothing was flashed, regardless of "
            "the reported exit code."
        )
    elif error_output:
        logger.error(
            "firmware update printed 'ERROR:' — it stopped before or during "
            "the flash (see the streamed output above for the reason)."
        )
    elif not success:
        logger.error(
            f"firmware update did not report RC=0 (got: {rc_output!r}) — "
            "review the streamed output above to confirm what actually happened."
        )
    return success, output


def _manual_download(d: SerialDevice, target: str) -> bool:
    """
    Fetch firmware-<target>-gateway-dk.bin + .bin.sig into MANUAL_FW_DIR
    with `curl -k`, using the same mono:<MAC> auth and MAC detection as
    the official tool (a plain unauthenticated curl got a 401 before).
    -k only skips TLS verification of the transport; the tool still
    verifies the signature before flashing (see block comment above).
    """
    fw = f"firmware-{target}-{FIRMWARE_MACHINE}.bin"
    script = (
        f"rm -rf {MANUAL_FW_DIR} && mkdir -p {MANUAL_FW_DIR} && cd {MANUAL_FW_DIR} && "
        r"mac=$(ip -o link show | grep -m1 'ether' | sed 's|.*ether \([^ ]*\).*|\1|') && "
        f'curl -kfsS -u "mono:$mac" -o {fw} {FIRMWARE_BASE_URL}/{fw} && '
        f'curl -kfsS -u "mono:$mac" -o {fw}.sig {FIRMWARE_BASE_URL}/{fw}.sig; '
        "echo DL_RC=$?"
    )
    try:
        out = d.run_script(script, marker="manual_fw_download", exec_timeout=300)
    except RuntimeError as e:
        logger.error(f"_manual_download: run_script failed: {e}")
        return False
    ok = "DL_RC=0" in out
    if not ok:
        logger.error(f"_manual_download: download failed — output:\n{out}")
    return ok


def run_firmware_update(d: SerialDevice, on_output: Optional[Callable[[str], None]] = None,
                         idle_timeout: float = 30.0, max_total: float = 900.0,
                         preserve_env: Optional[bool] = None) -> bool:
    """
    Flash the OTHER medium than the one booted (the tool auto-detects
    boot_medium= and never overwrites what you're running from) via the
    modern `firmware` tool, degrading gracefully and deterministically
    (#24):

      0. NTP clock sync, result shown on the console.
      1. `firmware update [--preserve-env]` — the tool downloads itself
         (TLS-verified).
      2. If that failed with a TLS/certificate error: WARN, re-sync NTP,
         retry once.
      3. If it still failed and the tool supports --from: download .bin
         and .bin.sig ourselves with `curl -k` (signature still verified
         by the tool) and run `firmware update --from DIR [--preserve-env]`
         — keeps the tool, and therefore the env choice, in charge.

    The legacy curl+dd / curl+flashcp path is NOT part of this ladder: it
    rewrites the env region regardless, so it only runs from the menu
    flows after an explicit user confirmation (see _run_medium_update()).

    Args:
        preserve_env: keep the target's U-Boot env (--preserve-env).
            None = default_preserve_env(target) — the historical policy
            (eMMC: no, NOR: yes), used by the OS journeys.
        on_output: optional callback(text_chunk) for live progress.
        idle_timeout, max_total: passed to _stream_command(); only
            overridden by tests.

    Requires real internet access on the device's network — a hard,
    documented prerequisite.
    """
    # Step 0: best-effort NTP sync before anything TLS-verified runs (#23).
    sync_device_clock(d)

    # Step 1: which medium are we booted from -> which one gets flashed.
    # Keys off boot_medium= (set by U-Boot's "emmc"/"recovery" boot
    # commands) — the same signal the tool itself uses. The old root=
    # grep made a detection FAILURE indistinguishable from an eMMC boot.
    try:
        boot_output = d.run_script(
            "cat /proc/cmdline | grep -o 'boot_medium=[a-z]*' || echo 'boot_medium=unknown'",
            marker="detect_boot_source", exec_timeout=5
        )
    except RuntimeError as e:
        logger.warning(f"run_firmware_update: could not detect boot source: {e}")
        target = "emmc"  # default: assume booted from NOR, so flash eMMC
    else:
        if "boot_medium=emmc" in boot_output:
            target = "qspi"
        elif "boot_medium=qspi" in boot_output:
            target = "emmc"
        else:
            logger.warning(
                f"run_firmware_update: boot_medium not found in cmdline "
                f"(got: {boot_output!r}) — defaulting to emmc target"
            )
            target = "emmc"

    logger.info(f"run_firmware_update: will flash {target} (auto-detected by device)")

    if preserve_env is None:
        preserve_env = default_preserve_env(target)

    # Build flags from what this device's tool actually supports. A tool
    # older than 2026-04-18 has no --preserve-env and dies on it — but it
    # always preserves the env anyway, so plain `firmware update` gives
    # the "preserve" outcome there and "wipe" is simply unavailable.
    caps = firmware_tool_caps(d)
    flags = ""
    if preserve_env:
        if caps["preserve_env"]:
            flags = " --preserve-env"
        else:
            console_logger.info("  (Older firmware tool: it always preserves the U-Boot env — no flag needed.)")
    elif not caps["preserve_env"]:
        console_logger.info(
            "  ⚠ Older firmware tool: it cannot wipe the U-Boot env — "
            "the existing env will be kept."
        )
    console_logger.info(f"  U-Boot env on {target}: {'preserved' if preserve_env or not caps['preserve_env'] else 'reset to factory defaults'}")

    # Tier 1: the tool's own download.
    ok, output = _run_firmware_cmd(d, f"firmware update{flags}", on_output, idle_timeout, max_total)
    if ok:
        return True

    # Tier 2: TLS error -> re-sync the clock and retry once.
    if is_tls_error(output):
        console_logger.info("  ⚠ WARN: TLS/certificate error from firmware.mono.si — "
                            "re-syncing the clock and retrying once...")
        logger.warning("run_firmware_update: TLS error detected, NTP re-sync + retry")
        sync_device_clock(d)
        ok, output = _run_firmware_cmd(d, f"firmware update{flags}", on_output, idle_timeout, max_total)
        if ok:
            return True

    # Tier 3: our own curl -k download, flashed by the tool via --from.
    if not caps["from"]:
        console_logger.info("  ⚠ This firmware tool has no --from option — manual-download retry skipped.")
        return False
    console_logger.info("  ⚠ WARN: 'firmware update' failed — downloading firmware manually "
                        "(curl -k; the tool still verifies the signature)...")
    if not _manual_download(d, target):
        console_logger.info("  ❌ Manual firmware download failed.")
        return False
    ok, _output = _run_firmware_cmd(
        d, f"firmware update --from {MANUAL_FW_DIR}{flags}", on_output, idle_timeout, max_total,
    )
    if ok:
        console_logger.info("  ✓ Flashed via 'firmware update --from' (manual download).")
    return ok


# --- U-Boot env stash/restore for the legacy path (#24) -------------------

def stash_uboot_env(d: SerialDevice, target: str) -> Optional[str]:
    """
    Copy the target medium's raw U-Boot env region to UBOOT_ENV_STASH
    before a legacy curl+dd / curl+flashcp rewrite, using the same
    locations and size as the official tool's --preserve-env (see the
    block comment above). Returns the env device ("/dev/mtdN" or
    "/dev/mmcblk0") on success, None on failure (caller must warn).
    """
    if target == "qspi":
        script = (
            'e=""\n'
            'for s in /sys/class/mtd/mtd[0-9]*; do\n'
            '  [ "$(cat "$s/name" 2>/dev/null)" = "uboot-env" ] || continue\n'
            '  e="/dev/$(basename "$s")"; break\n'
            'done\n'
            f'if [ -n "$e" ] && dd if="$e" of={UBOOT_ENV_STASH} bs={UBOOT_ENV_SIZE} count=1 2>/dev/null '
            f'&& [ "$(wc -c < {UBOOT_ENV_STASH})" -eq {UBOOT_ENV_SIZE} ]; then echo "STASH_OK:$e"; '
            'else echo STASH_FAIL; fi\n'
        )
    else:
        script = (
            f"if dd if=/dev/mmcblk0 of={UBOOT_ENV_STASH} bs=1 skip={UBOOT_ENV_EMMC_OFFSET} "
            f"count={UBOOT_ENV_SIZE} 2>/dev/null && "
            f'[ "$(wc -c < {UBOOT_ENV_STASH})" -eq {UBOOT_ENV_SIZE} ]; '
            'then echo "STASH_OK:/dev/mmcblk0"; else echo STASH_FAIL; fi\n'
        )
    try:
        out = d.run_script(script, marker="stash_uboot_env", exec_timeout=60)
    except RuntimeError as e:
        logger.error(f"stash_uboot_env: run_script failed: {e}")
        return None
    m = re.search(r"STASH_OK:(/dev/(?:mtd[0-9]+|mmcblk0))", out)
    if not m:
        logger.error(f"stash_uboot_env: failed — output:\n{out}")
        return None
    return m.group(1)


def restore_uboot_env_region(d: SerialDevice, target: str, env_dev: str) -> bool:
    """Write UBOOT_ENV_STASH back to env_dev (see stash_uboot_env())."""
    if target == "qspi":
        if not re.fullmatch(r"/dev/mtd[0-9]+", env_dev):
            return False
        cmd = f"flashcp {UBOOT_ENV_STASH} {env_dev} && echo RESTORE_OK || echo RESTORE_FAIL"
    else:
        cmd = (
            f"dd if={UBOOT_ENV_STASH} of=/dev/mmcblk0 bs=1 seek={UBOOT_ENV_EMMC_OFFSET} 2>/dev/null "
            "&& sync && echo RESTORE_OK || echo RESTORE_FAIL"
        )
    try:
        out = d.run_script(cmd, marker="restore_uboot_env", exec_timeout=60)
    except RuntimeError as e:
        logger.error(f"restore_uboot_env_region: run_script failed: {e}")
        return False
    ok = "RESTORE_OK" in out
    if not ok:
        logger.error(f"restore_uboot_env_region: failed — output:\n{out}")
    return ok


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

def phase_modern_flash_emmc(d: SerialDevice, on_output: Optional[Callable[[str], None]] = None,
                            preserve_env: Optional[bool] = None) -> bool:
    """
    Modern path, step 1: from NOR-booted recovery, run `firmware
    update` to flash eMMC. Returns True on confirmed success.
    """
    console_logger.info("Running 'firmware update' to flash eMMC...")
    ok = step(1, "Flash eMMC via 'firmware update'",
              run_firmware_update(d, on_output=on_output, preserve_env=preserve_env))
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


def phase_modern_flash_nor(d: SerialDevice, on_output: Optional[Callable[[str], None]] = None,
                           preserve_env: Optional[bool] = None) -> bool:
    """
    Modern path, step 3: from eMMC-booted recovery, run `firmware
    update` again — it auto-targets NOR this time since eMMC is now
    the active boot source. Returns True on confirmed success.
    """
    console_logger.info("Running 'firmware update' to flash NOR...")
    ok = step(3, "Flash NOR via 'firmware update'",
              run_firmware_update(d, on_output=on_output, preserve_env=preserve_env))
    return ok


def phase_modern_verify_nor_boot(d: SerialDevice, timeout: float = 90) -> bool:
    """
    Modern path, step 4: after the user flips the DIP switch back to
    NOR and reboots, confirm the device actually booted from NOR.
    """
    ok = step(4, "Verify NOR boot (back to factory default)", verify_boot_source(d, "NOR", timeout=timeout))
    return ok


def _legacy_flash_with_env(d: SerialDevice, target: str, flash_fn, env_dev: Optional[str]) -> bool:
    """
    Run a legacy flash (curl+dd / curl+flashcp — both rewrite the env
    region) and, if env_dev is set (stash_uboot_env() succeeded
    beforehand), write the stashed env back afterwards (#24).
    """
    ok = flash_fn()
    if ok and env_dev:
        if restore_uboot_env_region(d, target, env_dev):
            console_logger.info(f"  ✓ U-Boot env restored ({env_dev})")
        else:
            console_logger.info(f"  ⚠ Flash OK, but restoring the U-Boot env to {env_dev} FAILED — "
                                "env is at factory defaults.")
    return ok


def phase_legacy_flash_emmc(d: SerialDevice, env_dev: Optional[str] = None) -> bool:
    """
    Legacy path, step 1: get the device's real MAC, then flash eMMC
    via curl+dd per the documented legacy procedure. env_dev: see
    _legacy_flash_with_env().
    """
    mac = get_device_mac(d)
    if mac is None:
        return step(1, "Flash eMMC (legacy curl+dd)", False, "could not determine device MAC address")
    console_logger.info(f"Device MAC: {mac}")
    console_logger.info("Downloading and flashing eMMC (legacy path)...")
    ok = step(1, "Flash eMMC (legacy curl+dd)",
              _legacy_flash_with_env(d, "emmc", lambda: legacy_flash_emmc(d, mac), env_dev))
    return ok


def phase_legacy_flash_nor(d: SerialDevice, env_dev: Optional[str] = None) -> bool:
    """
    Legacy path, step 2: same MAC, flash NOR via curl+flashcp. env_dev:
    see _legacy_flash_with_env().
    """
    mac = get_device_mac(d)
    if mac is None:
        return step(2, "Flash NOR (legacy curl+flashcp)", False, "could not determine device MAC address")
    console_logger.info(f"Device MAC: {mac}")
    console_logger.info("Downloading and flashing NOR (legacy path)...")
    ok = step(2, "Flash NOR (legacy curl+flashcp)",
              _legacy_flash_with_env(d, "qspi", lambda: legacy_flash_nor(d, mac), env_dev))
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

# _MEDIUM_CONFIG parametrizes the one real difference between the eMMC
# and NOR update flows: which medium to boot from/target, which modern-
# path and legacy-path functions to call, and the messages that name
# them. See _run_medium_update() below for the shared flow itself.
#
# modern_flash/legacy_flash are lambdas, not bare function references —
# a bare `phase_legacy_flash_emmc` here would bind that name's value at
# module-import time, so a test's patch.object(rec, "phase_legacy_flash_emmc", ...)
# (the pattern this file's own tests use elsewhere) would silently miss
# this dict's already-captured reference. The lambda defers the name
# lookup to call time, same as every other call site in this module.
_MEDIUM_CONFIG = {
    "emmc": {
        "boot_medium":   "qspi",   # boot from NOR recovery so 'firmware update' targets eMMC
        "label":         "eMMC",
        "target":        "emmc",   # firmware tool's name for the flashed medium
        "tool_name":     "curl+dd",
        "modern_flash":  lambda d, on_output, preserve_env: phase_modern_flash_emmc(d, on_output=on_output, preserve_env=preserve_env),
        "legacy_flash":  lambda d, env_dev: phase_legacy_flash_emmc(d, env_dev=env_dev),
        "legacy_message": "Flashing eMMC (legacy curl+dd)...",
        "legacy_only_extra_note": None,
    },
    "nor": {
        "boot_medium":   "emmc",   # boot from eMMC recovery so 'firmware update' targets NOR
        "label":         "NOR",
        "target":        "qspi",
        "tool_name":     "curl+flashcp",
        "modern_flash":  lambda d, on_output, preserve_env: phase_modern_flash_nor(d, on_output=on_output, preserve_env=preserve_env),
        "legacy_flash":  lambda d, env_dev: phase_legacy_flash_nor(d, env_dev=env_dev),
        "legacy_message": "Flashing NOR (legacy curl+flashcp)...",
        "legacy_only_extra_note": "  (No DIP-switch flip needed for this path.)",
    },
}



# --- Interactive choices for the menu update flows (#24) ------------------
# Only _run_medium_update() (menu options 2/3) prompts; the OS journeys call
# run_firmware_update() non-interactively with the default env policy.

def _ask_yes_no(question: str, default: bool) -> bool:
    """input()-based Y/n prompt; empty answer (or EOF) = default."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {question} {hint}: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "j", "ja")


def ask_preserve_env(target: str, label: str) -> bool:
    """Ask whether to keep the target's U-Boot env; default per target."""
    default = default_preserve_env(target)
    console_logger.info("")
    if target == "emmc":
        console_logger.info("  U-Boot env: default is NOT to preserve on eMMC — an old eMMC env")
        console_logger.info("  (e.g. from Armbian) can lack the 'recovery' command the NOR update")
        console_logger.info("  (option 3) needs. Preserve it if an OS on eMMC depends on it.")
    else:
        console_logger.info("  U-Boot env: default is to preserve on NOR — it holds your boot")
        console_logger.info("  settings (e.g. OPNsense's bootcmd). Not preserving resets it to")
        console_logger.info("  factory defaults.")
    choice = _ask_yes_no(f"Preserve the {label} U-Boot environment?", default)
    logger.info(f"ask_preserve_env: target={target} preserve={choice}")
    return choice


def _confirm_legacy_fallback(cfg: dict, preserve_env: bool) -> bool:
    """Explicit consent before the legacy last resort (default: No)."""
    console_logger.info("")
    console_logger.info(f"  ⚠ The firmware tool could not update {cfg['label']} (all retries failed).")
    console_logger.info(f"  Last resort: legacy {cfg['tool_name']}, which bypasses the tool and")
    console_logger.info("  rewrites the U-Boot env region.")
    if preserve_env:
        console_logger.info("  The env will be stashed first and written back afterwards.")
    else:
        console_logger.info("  The U-Boot env WILL be reset to factory defaults.")
    return _ask_yes_no(f"Run the legacy {cfg['tool_name']} fallback?", False)


def _run_legacy(d: SerialDevice, cfg: dict, preserve_env: bool) -> bool:
    """
    Legacy flash with the env choice applied: stash the env region
    first when preserving; if that stash fails, WARN and ask before
    flashing anyway (env would be lost).
    """
    env_dev = None
    if preserve_env:
        env_dev = stash_uboot_env(d, cfg["target"])
        if env_dev:
            console_logger.info(f"  ✓ U-Boot env stashed from {env_dev}")
        else:
            console_logger.info("  ⚠ WARN: could not stash the U-Boot env — flashing now will reset")
            console_logger.info("    it to factory defaults.")
            if not _ask_yes_no("Flash anyway and lose the U-Boot env?", False):
                return False
    ok, _leg_err = with_spinner(cfg["legacy_flash"], d, env_dev, message=cfg["legacy_message"])
    if _leg_err:
        logger.error(f"legacy flash raised: {_leg_err}")
        return False
    return bool(ok)


def _run_medium_update(
    medium: str,
    port: str,
    setup_network: Callable[[SerialDevice], bool],
    on_output: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Shared flow behind run_emmc_update()/run_nor_update(): bootstrap
    into the recovery shell for the OTHER medium, detect modern vs.
    legacy firmware tool, resolve the device network, ask whether to
    preserve the target's U-Boot env, then flash the target medium via
    the modern `firmware update` ladder (run_firmware_update(): NTP,
    TLS retry, curl -k + --from). The legacy curl-based path runs only
    when the device has no usable tool, or — after an explicit y/N —
    as the last resort; either way the env choice is applied via
    stash_uboot_env()/restore_uboot_env_region() (#24).
    Prints its own step-by-step report before returning.

    This ~90-line bootstrap/detect/flash/fallback sequence used to be
    duplicated almost verbatim between run_emmc_update() and
    run_nor_update() themselves (previously copy-pasted from
    tui.py's menu_update_emmc()/menu_update_nor() — see git history);
    _MEDIUM_CONFIG above now carries the only real differences.

    Returns True on overall success.
    """
    from mono_imager import flash_orchestrator as core

    cfg = _MEDIUM_CONFIG[medium]
    d = None
    try:
        # NOTE: no auto soft-reboot here. phase1_uboot()'s own
        # "POWER CYCLE NOW" prompt drives the reboot, so we don't send a
        # silent reset that would contradict that on-screen instruction.
        d = core.phase1_bootstrap(port, 115200, boot_medium=cfg["boot_medium"])
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

        # One env choice per run, honoured by every tier below (#24):
        # the modern tool (--preserve-env) and the legacy path (manual
        # stash + restore of the env region).
        preserve_env = ask_preserve_env(cfg["target"], cfg["label"])

        if is_modern:
            console_logger.info("")
            console_logger.info("  Modern firmware tool detected.")
            console_logger.info("")
            ok = cfg["modern_flash"](d, on_output, preserve_env)
            if not ok:
                # Last resort only, and only with consent: the legacy path
                # bypasses the tool and rewrites the env region itself.
                if not _confirm_legacy_fallback(cfg, preserve_env):
                    console_logger.info(f"  ❌ Update of {cfg['label']} aborted — nothing further flashed.")
                    return print_report()
                ok = _run_legacy(d, cfg, preserve_env)
                if not ok:
                    console_logger.info(f"  ❌ Legacy fallback also failed for {cfg['label']}.")
                    return print_report()
                console_logger.info("  ✓ Legacy fallback succeeded.")
        else:
            console_logger.info("")
            console_logger.info(f"  Legacy firmware tool detected — using {cfg['tool_name']} directly.")
            if cfg["legacy_only_extra_note"]:
                console_logger.info(cfg["legacy_only_extra_note"])
            console_logger.info("")
            ok = _run_legacy(d, cfg, preserve_env)
            if not ok:
                return print_report()

    finally:
        if d:
            d.disconnect()

    return print_report()


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
    returning. See _run_medium_update() for the shared implementation.

    Returns True on overall success.
    """
    return _run_medium_update("emmc", port, setup_network, on_output)


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
    before returning. See _run_medium_update() for the shared
    implementation.

    Returns True on overall success.
    """
    return _run_medium_update("nor", port, setup_network, on_output)


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
