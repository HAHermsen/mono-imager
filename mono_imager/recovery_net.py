"""
mono-imager: Device networking from recovery Linux: MAC, DHCP, reachability, clock

Split out of recovery_orchestrator.py; import it through mono_imager.recovery_orchestrator,
which re-exports these names. Calls go through `R.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


import re
import time
from collections.abc import Callable

from mono_imager import recovery_orchestrator as R


def detect_modern_firmware_tool(d: R.SerialDevice) -> bool | None:
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
        R.logger.warning(f"detect_modern_firmware_tool: run_script failed: {e}")
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
        cmdline = d.run_script("cat /proc/cmdline", marker="check_cmdline", exec_timeout=R.CMDLINE_TIMEOUT)
    except RuntimeError:
        cmdline = ""

    if "boot_medium=" not in cmdline:
        reason = (
            "'firmware' command present but boot_medium= absent from kernel cmdline "
            "(old U-Boot) — falling back to legacy path"
        )
        R.logger.info(reason)
        # Also on-screen, not just the log file: without this, "legacy
        # firmware tool detected" alone reads like a mis-detection —
        # the modern binary genuinely is there, so seeing why it's being
        # skipped (an old U-Boot never sets boot_medium=, a device-side
        # gap this tool can't fix) matters more here than for the plain
        # "no firmware command at all" case just above, which is
        # self-explanatory without extra detail.
        R.console_logger.info(f"  ({reason})")
        return False

    return True


def get_device_mac(d: R.SerialDevice, interface: str = R.C.DEFAULT_ETH_IFACE) -> str | None:
    """
    Get the device's real MAC address from `ip a`, parsed — never
    assumed or asked of the user (avoids transcription errors). Tries
    the given interface first, falls back to the first link/ether seen
    if that specific interface isn't found.
    """
    try:
        output = d.run_script(f"ip addr show {interface} 2>/dev/null || ip addr", marker="get_mac")
    except RuntimeError as e:
        R.logger.warning(f"get_device_mac: run_script failed: {e}")
        return None

    match = re.search(r'link/ether\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})', output)
    if match:
        return match.group(1).lower()
    return None


def check_internet_reachable(d: R.SerialDevice, gateway: str | None = None,
                              host: str = "firmware.mono.si",
                              ping_ip: str = R.C.INTERNET_PROBE_IP, timeout: int = R.INTERNET_CHECK_TIMEOUT) -> bool:
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
        R.logger.warning(f"check_internet_reachable: {msg}")
        R.console_logger.info(f"  ⚠ {msg}")
        return False
    if "RC=0" not in ping_out:
        msg = (
            f"No route to the internet — ping to {ping_ip} failed. Check the "
            "device IP, gateway, cable, and which physical port is in use."
        )
        R.logger.error(msg)
        R.console_logger.info(f"  ⚠ {msg}")
        return False
    R.console_logger.info(f"  ✓ Reachability OK (ping {ping_ip}).")

    # 2. DNS — resolve the real firmware host with nslookup.
    try:
        dns_out = d.run_script(
            f"nslookup {host} >/dev/null 2>&1; echo RC=$?",
            marker="check_dns", exec_timeout=timeout,
        )
    except RuntimeError as e:
        msg = f"Could not run the DNS check: {e}"
        R.logger.warning(f"check_internet_reachable: {msg}")
        R.console_logger.info(f"  ⚠ {msg}")
        return False
    if "RC=0" not in dns_out:
        msg = (
            f"DNS lookup failed ({host}) — routing works but name resolution "
            "does not. Check the DNS server you set is reachable and correct."
        )
        R.logger.error(msg)
        R.console_logger.info(f"  ⚠ {msg}")
        return False
    R.console_logger.info(f"  ✓ DNS lookup OK ({host}).")

    return True


def try_dhcp(d: R.SerialDevice, iface: str = R.C.DEFAULT_ETH_IFACE, timeout: int = R.DHCP_TIMEOUT) -> dict | None:
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
        R.logger.warning(f"try_dhcp: run_script failed: {e}")
        return None

    ip_match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", output)
    gw_match = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", output)
    if not ip_match or not gw_match:
        R.logger.info("try_dhcp: no lease obtained (no address and/or no default route)")
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

def _stream_command(d: R.SerialDevice, command: str, idle_timeout: float = R.STREAM_IDLE_TIMEOUT,
                     max_total: float = R.STREAM_MAX_TOTAL, auto_confirm_response: str = None,
                     on_output: Callable[[str], None] | None = None,
                     done_markers: list | None = None) -> str:
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
    poll_interval = R.STREAM_POLL_INTERVAL
    marker_seen_at = None  # set once a done_marker appears in the output
    marker_grace = R.STREAM_MARKER_GRACE

    while True:
        now = time.time()
        if now - overall_start > max_total:
            R.logger.warning(f"_stream_command: hit hard ceiling of {max_total}s")
            break
        if now - last_byte_time > idle_timeout:
            R.logger.debug(f"_stream_command: {idle_timeout}s with no new output - assuming done")
            break
        # A completion marker (e.g. "Firmware update complete") means the
        # command is done - stop after a short grace to catch the trailing
        # shell prompt, instead of waiting out the full idle_timeout (which
        # looked like a ~30s hang after the flash finished).
        if marker_seen_at is not None and now - marker_seen_at > marker_grace:
            R.logger.debug("_stream_command: done marker seen - finishing early")
            break

        # Non-blocking: check if data is available without waiting
        if d.ser.in_waiting > 0:
            chunk = d.ser.read(R.STREAM_READ_BYTES)
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


def sync_device_clock(d: R.SerialDevice, timeout: float = R.CLOCK_SYNC_TIMEOUT) -> bool:
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
    R.console_logger.info("  Syncing device clock via NTP (pool.ntp.org)...")
    try:
        output = d.run_script(
            "ntpd -q -n -p pool.ntp.org 2>&1; echo RC=$?; "
            "date -u '+DEVICE_TIME=%Y-%m-%d %H:%M:%S UTC'",
            marker="sync_device_clock", exec_timeout=timeout,
        )
    except RuntimeError as e:
        R.logger.warning(f"sync_device_clock: run_script failed: {e}")
        R.console_logger.info(f"  ⚠ NTP sync could not run ({e}) — continuing with the current clock.")
        return False
    ok = "RC=0" in output
    m = re.search(r"DEVICE_TIME=([0-9-]+ [0-9:]+ UTC)", output)
    now = m.group(1) if m else "unknown"
    if ok:
        R.console_logger.info(f"  ✓ Clock synced — device time {now}")
    else:
        R.logger.warning(f"sync_device_clock: ntpd did not report success — output:\n{output}")
        R.console_logger.info(
            f"  ⚠ NTP sync failed — device time {now}. If it is wrong, the "
            "HTTPS download may fail with 'certificate is not yet valid'."
        )
    return ok


__all__ = [
    "_stream_command",
    "check_internet_reachable",
    "detect_modern_firmware_tool",
    "get_device_mac",
    "sync_device_clock",
    "try_dhcp",
]
