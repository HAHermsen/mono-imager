"""
mono-imager journey: OPNsense via LAN

Steps:
  1. Confirm DIP switch is RIGHT (NOR)
  2. Network setup (eth0)
  3. Start HTTP server
  4. Verify firmware reachable
  5. Flash OPNsense image (curl | bzip2 -dck | dd bs=1M)
  6. Detect device MAC address
  7. Re-image eMMC firmware (firmware update)
  8. DIP flip to eMMC + power cycle

Author:  H.A. Hermsen
License: MIT
"""

__version__ = "0.9.5"
__author__  = "H.A. Hermsen"

import logging
from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger, start_http_server, wait_for_report

logger = logging.getLogger(__name__)

OS       = "OPNsense"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the OPNsense .img.bz2 file:"
TRANSFER = "lan"


def _uboot_steps_opnsense_lan(device) -> bool:
    """
    U-Boot commands for OPNsense — runs between phase1_uboot() and phase1_recovery().
    Set env vars and saveenv BEFORE erasing eMMC — MMC env backend is primary,
    so we must write while MMC is intact, then erase after.
    """
    verbose("Setting U-Boot env for OPNsense...")
    try:
        device.send_command("setenv bootcmd_bak ${bootcmd}", timeout=10)
        device.send_command('setenv bootcmd "run opnsense || run recovery"', timeout=10)
        device.send_command(
            'setenv opnsense "mmc dev 0; load mmc 0:1 0x82000000 kernel.img; '
            'load mmc 0:1 0x88000000 dtb/mono-gateway-dk.dtb; booti 0x82000000 - 0x88000000"',
            timeout=10
        )
        device.send_command("saveenv", timeout=15)
        step(0, "U-Boot env set for OPNsense", True)
    except Exception as e:
        verbose(f"✗ U-Boot env set failed: {e}", "error")
        return False

    verbose("Erasing eMMC (OPNsense requirement)...")
    try:
        response = device.send_command("mmc erase 0 3b48000", timeout=120)
        if "error" in response.lower():
            verbose(f"✗ eMMC erase failed: {response[-100:]}", "error")
            return False
        step(0, "eMMC erase (mmc erase 0 3b48000)", True)
    except Exception as e:
        verbose(f"✗ eMMC erase exception: {e}", "error")
        return False

    return True

register_uboot_steps(OS, TRANSFER, _uboot_steps_opnsense_lan)


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["dip_confirmed_nor"], label="Confirm DIP switch is RIGHT (NOR)")
def step_confirm_dip_nor(ctx: StepContext) -> bool:
    console_logger.info("")
    console_logger.info("  ┌─────────────────────────────────────────────────┐")
    console_logger.info("  │  START HERE: DIP switch → RIGHT (NOR)            │")
    console_logger.info("  │                                                   │")
    console_logger.info("  │  OPNsense flashes and boots entirely from NOR.   │")
    console_logger.info("  │  Keep DIP RIGHT throughout — do NOT flip to LEFT. │")
    console_logger.info("  └─────────────────────────────────────────────────┘")
    console_logger.info("")
    try:
        input("  Confirm DIP is RIGHT and press ENTER to continue... ")
    except KeyboardInterrupt:
        return step(0, "DIP confirmed NOR", False, "interrupted")
    return step(0, "DIP confirmed NOR (RIGHT)", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["dip_confirmed_nor"], produces=["network_up"], label="Network setup (eth0)")
def step_network_setup(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Network setup"); verbose("=" * 60)
    d = ctx.device
    try:
        d.send_command("ip link set eth0 up", timeout=10)
        d.send_command(f"ip addr add {ctx.device_ip}/24 dev eth0", timeout=10)
        return step(0, f"Network up ({ctx.device_ip})", True)
    except Exception as e:
        return step(0, "Network up", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["network_up"], produces=["http_server_up"], label="Start HTTP server")
def step_http_server_start(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Start HTTP server"); verbose("=" * 60)
    try:
        server = start_http_server(ctx.host_ip, ctx.http_port, ctx.firmware_path)
        if server:
            ctx.set("http_server", server)
            return step(0, f"HTTP server up ({ctx.host_ip}:{ctx.http_port})", True)
        return step(0, "HTTP server start", False)
    except Exception as e:
        return step(0, "HTTP server start", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["network_up", "http_server_up"], produces=["firmware_ready"], label="Verify firmware reachable")
def step_firmware_reachable(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Verify firmware reachable"); verbose("=" * 60)
    url = f"http://{ctx.host_ip}:{ctx.http_port}/firmware.img"
    ctx.set("firmware_source", url)
    check_script = (
        f"curl -s -I -o /dev/null -w '%{{http_code}}' {url} "
        f"> /tmp/mono_imager_step06_code.txt; "
        f"curl -s -X POST --data-binary @/tmp/mono_imager_step06_code.txt "
        f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=06\" >/dev/null 2>&1"
    )
    try:
        ctx.device.launch_script(check_script, marker="step06_reachable")
    except Exception as e:
        return step(0, f"Firmware reachable ({url})", False, str(e))
    check = wait_for_report("06", timeout=20.0)
    ok = check is not None and "200" in check
    return step(0, f"Firmware reachable ({url})", ok, f"HTTP {check}" if not ok else "")


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash OPNsense image (bzip2 | dd)")
def step_flash_opnsense(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Flash OPNsense image"); verbose("=" * 60)
    d = ctx.device
    source = ctx.get("firmware_source")
    # { } group captures stderr from curl, bzip2, and dd into one log file.
    flash_script = (
        f"{{ curl -s {source} | bzip2 -dck | "
        f"dd of={ctx.flash_target} bs=1M; }} "
        f"> /tmp/mono_imager_step07_flash.log 2>&1; "
        f"sync; "
        f"curl -s -X POST --data-binary @/tmp/mono_imager_step07_flash.log "
        f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=07\" "
        f">/dev/null 2>&1"
    )
    console_logger.info("Flashing OPNsense — streaming + decompressing, this takes several minutes...")
    try:
        d.launch_script(flash_script, marker="step07_flash")
    except Exception as e:
        return step(0, "OPNsense flash launched", False, str(e))
    raw, err = with_spinner(wait_for_report, "07", timeout=1200.0, message="Flashing OPNsense (bzip2 | dd)")
    if err or raw is None:
        return step(0, "OPNsense flash (bzip2 | dd)", False, str(err) if err else "no report-back from device in 1200s")
    has_records = "records in" in raw and "records out" in raw
    has_error   = "error" in raw.lower() or "failed" in raw.lower()
    step(0, "OPNsense flash executed", True)
    step(0, "dd records in/out confirmed", has_records, raw[-200:] if not has_records else "")
    step(0, "No errors", not has_error, raw[-200:] if has_error else "")
    return has_records and not has_error


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["device_mac_known"], label="Detect device MAC address")
def step_detect_mac(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Detect device MAC address"); verbose("=" * 60)
    d = ctx.device
    try:
        result = d.run_script("cat /sys/class/net/eth0/address 2>/dev/null || echo unknown", marker="get_mac", exec_timeout=5)
        mac = result.strip()
        if mac and mac != "unknown" and ":" in mac:
            ctx.device_mac = mac
            ctx.set("device_mac", mac)
            ctx.set("device_mac_known", mac)
            verbose(f"✓ MAC: {mac}")
            return step(0, f"Device MAC detected ({mac})", True)
    except Exception as e:
        verbose(f"⚠ MAC detection failed: {e}", "warning")
    ctx.set("device_mac_known", None)
    step(0, "Device MAC detected", False, "will prompt at re-image step")
    return True  # non-fatal


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed", "device_mac_known"], produces=["emmc_firmware_reimaged"], label="Re-image eMMC firmware (firmware update)")
def step_reimage_emmc_firmware(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Re-image eMMC firmware via 'firmware update'"); verbose("=" * 60)
    from mono_imager.recovery_orchestrator import run_firmware_update
    d = ctx.device
    try:
        ok = run_firmware_update(d)
        return step(0, "eMMC firmware re-image (firmware update)", ok)
    except Exception as e:
        return step(0, "eMMC firmware re-image (firmware update)", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["emmc_firmware_reimaged"], produces=["rebooted"], label="Reboot into OPNsense")
def step_reboot(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Reboot into OPNsense"); verbose("=" * 60)
    console_logger.info("")
    console_logger.info("✓ Rebooting — U-Boot will boot OPNsense from eMMC automatically.")
    console_logger.info("  DIP stays RIGHT (NOR). No action needed.")
    console_logger.info("")
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent — OPNsense booting from eMMC", True)
