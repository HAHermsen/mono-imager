"""
mono-imager: Shared step implementations reused across journey files.

Leading underscore means journeys/__init__.py's auto-discovery scan
skips this file (see JOURNEYS.md) — its @register_step calls only
take effect once some auto-discovered journey file imports it.

Author:  H.A. Hermsen
License: GPLv3
"""

from mono_imager.step_registry import register_step, StepContext, ALL_OS
from mono_imager.flash_orchestrator import step, verbose


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
