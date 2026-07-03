"""
mono-imager journey: OPNsense via LAN

Steps:
  1. Device network ready
  2. Confirm DIP switch is RIGHT (NOR)
  3. Start HTTP server
  4. Verify firmware reachable
  5. Flash OPNsense image (curl | bzip2 -dck | dd bs=1M)
  6. Detect device MAC address
  7. Re-image eMMC firmware (firmware update)
  8. Reboot into OPNsense (DIP stays RIGHT / NOR)

Author:  H.A. Hermsen
License: GPLv3
"""

import logging
from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner, Spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger, start_http_server, wait_for_report
from mono_imager.journeys import _common  # noqa: F401 — registers "Device network ready" step

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

    print("  Erasing eMMC (OPNsense requirement)...")
    try:
        response, _erase_err = with_spinner(
            device.send_command, "mmc erase 0 3b48000", timeout=120,
            message="Erasing eMMC (this takes ~60s)..."
        )
        if _erase_err:
            raise _erase_err
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


@register_step(os=[OS], transfer=[TRANSFER], requires=["dip_confirmed_nor", "network_up"], produces=["http_server_up"], label="Start HTTP server")
def step_http_server_start(ctx: StepContext) -> bool:
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


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash OPNsense image (bzip2 | dd)")
def step_flash_opnsense(ctx: StepContext) -> bool:
    d = ctx.device
    source = ctx.get("firmware_source")
    # { } group captures stderr from curl, bzip2, and dd into one log file.
    flash_script = (
        f"{{ curl -sk {source} | bzip2 -dck | "
        f"dd of={ctx.flash_target} bs=1M; }} "
        f"> /tmp/mono_imager_step07_flash.log 2>&1; "
        f"sync; "
        f"curl -sk -X POST --data-binary @/tmp/mono_imager_step07_flash.log "
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


@register_step(os=[OS], transfer=[TRANSFER], requires=["network_up"], produces=["device_mac_known"], label="Detect device MAC address")
def step_detect_mac(ctx: StepContext) -> bool:
    d = ctx.device
    iface = (ctx.device_net or {}).get("iface", "eth0")
    try:
        result = d.run_script(f"cat /sys/class/net/{iface}/address 2>/dev/null || echo unknown", marker="get_mac", exec_timeout=5)
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
    step(0, "Device MAC detection", True, "warning: detection failed — will prompt at re-image step")
    return True


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed", "device_mac_known", "network_up"], produces=["emmc_firmware_reimaged"], label="Re-image eMMC firmware (firmware update)")
def step_reimage_emmc_firmware(ctx: StepContext) -> bool:
    from mono_imager.recovery_orchestrator import run_firmware_update
    d = ctx.device
    try:
        with Spinner("Re-imaging eMMC firmware (firmware update)..."):
            ok = run_firmware_update(d)
        return step(0, "eMMC firmware re-image (firmware update)", ok)
    except Exception as e:
        return step(0, "eMMC firmware re-image (firmware update)", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["emmc_firmware_reimaged"], produces=["rebooted"], label="Reboot into OPNsense")
def step_reboot(ctx: StepContext) -> bool:
    console_logger.info("")
    console_logger.info("✓ Rebooting — U-Boot will boot OPNsense from eMMC automatically.")
    console_logger.info("  DIP stays RIGHT (NOR). No action needed.")
    console_logger.info("")
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent — OPNsense booting from eMMC", True)
