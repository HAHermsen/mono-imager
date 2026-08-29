"""
mono-imager: Shared step implementations reused across journey files.

Leading underscore means journeys/__init__.py's auto-discovery scan
skips this file (see JOURNEYS.md) — its @register_step calls only
take effect once some auto-discovered journey file imports it.

Author:  H.A. Hermsen
License: GPLv3
"""

from mono_imager.step_registry import register_step, StepContext, ALL_OS
from mono_imager.flash_orchestrator import step, verbose, start_http_server, wait_for_report
from mono_imager.spinner import with_spinner


def _step_network_ready(ctx: StepContext) -> bool:
    """
    Confirm the device's own recovery-shell network is ready.

    Resolution — DHCP-first, verified, manual fallback — already
    happened exactly once, before this journey started running, via
    MonoImager._setup_recovery_network() (same mechanism used by the
    eMMC/NOR firmware-update menus and Test LAN). The result is cached
    on ctx.device_net. This step is only the requires=["network_up"]
    checkpoint the rest of the journey depends on; it does not scan
    interfaces or assign an IP itself — that would just repeat work
    already done, on a hardcoded/guessed interface instead of the one
    actually verified to work.
    """
    net = ctx.device_net
    if not net or not net.get("ip"):
        return step(0, "Device network ready", False,
                     "device network was not resolved before this journey started")
    if not ctx.device_ip:
        ctx.device_ip = net["ip"]
    dns_note = f", DNS {net['dns']}" if net.get("dns") else ""
    verbose(f"  Using {net['source']} network: {net['ip']}/{net['prefix']} "
            f"via {net['gateway']}{dns_note}")
    return step(0, f"Device network ready ({net['ip']}, {net['source']})", True)


# LAN journeys always need it — it's how the device reaches the host's
# HTTP firmware server. USB journeys only need it for OpenWRT/OPNsense,
# whose post-flash steps call the real internet-backed `firmware
# update` command; Armbian-via-USB never touches the network at all,
# so it's deliberately not registered here.
register_step(
    os=[ALL_OS], transfer=["lan"],
    requires=[], produces=["network_up"],
    label="Device network ready",
)(_step_network_ready)

register_step(
    os=["OpenWRT", "OPNsense"], transfer=["usb"],
    requires=[], produces=["network_up"],
    label="Device network ready",
)(_step_network_ready)


# --- Shared LAN steps ---------------------------------------------------
# Not auto-registered here (unlike _step_network_ready above) because
# requires= varies per journey — e.g. OPNsense's HTTP-server step also
# gates on "dip_confirmed_nor", which the others don't have. Each LAN
# journey file registers these explicitly:
#
#   register_step(os=[OS], transfer=[TRANSFER], requires=[...],
#                 produces=["http_server_up"], label="Start HTTP server"
#   )(_common._step_http_server_start)

def _step_http_server_start(ctx: StepContext) -> bool:
    """Serve ctx.firmware_path over HTTP for the device to curl from."""
    try:
        server = start_http_server(ctx.host_ip, ctx.http_port, ctx.firmware_path)
        if server:
            ctx.set("http_server", server)
            return step(0, f"HTTP server up ({ctx.host_ip}:{ctx.http_port})", True)
        return step(0, "HTTP server start", False)
    except Exception as e:
        return step(0, "HTTP server start", False, str(e))


def _step_firmware_reachable(ctx: StepContext) -> bool:
    """
    Confirm the device can reach the host's firmware HTTP server before
    committing to a flash. One HEAD request run on the device (curl -I),
    reported back over TCP/IP rather than read from serial — see
    flash_orchestrator.phase3_flash()'s Step 09 comment for why: a HEAD
    avoids downloading the whole image just to check reachability, and
    TCP/IP report-back is the reliable channel vs. serial-echo readback.
    """
    url = f"http://{ctx.host_ip}:{ctx.http_port}/firmware.img"
    ctx.set("firmware_source", url)
    check_script = (
        f"curl -sk -I -o /dev/null -w '%{{http_code}}' {url} "
        f"> /tmp/mono_imager_step06_code.txt; "
        f"curl -sk -X POST --data-binary @/tmp/mono_imager_step06_code.txt "
        f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=06\" >/dev/null 2>&1"
    )
    try:
        ctx.device.launch_script(check_script, marker="step06_reachable")
    except Exception as e:
        return step(0, f"Firmware reachable ({url})", False, str(e))
    check, _rep_err = with_spinner(wait_for_report, "06", timeout=20.0, message="Verifying firmware reachable...")
    ok = check is not None and "200" in check
    return step(0, f"Firmware reachable ({url})", ok, f"HTTP {check}" if not ok else "")
