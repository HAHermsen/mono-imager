"""
mono-imager journey: Armbian via LAN

DIP switch: RIGHT (NOR) throughout — NOR U-Boot loads Armbian from eMMC via extlinux.

Steps:
  1. Device network ready
  2. Start HTTP server
  3. Verify firmware reachable
  4. Flash Armbian image (dd bs=1M, or xz -dc | dd for .img.xz)
  5. Reboot to U-Boot
  6. Configure U-Boot to boot Armbian (setenv bootcmd + saveenv)

Author:  H.A. Hermsen
License: GPLv3
"""

import logging
from mono_imager.step_registry import register_step, StepContext
from mono_imager.spinner import with_spinner, Spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger, start_http_server, wait_for_report
from mono_imager.journeys import _common  # noqa: F401 — registers "Device network ready" step

logger = logging.getLogger(__name__)

OS           = "Armbian"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the Armbian .img or .img.xz file:"
TRANSFER     = "lan"


@register_step(os=[OS], transfer=[TRANSFER], requires=["network_up"], produces=["http_server_up"], label="Start HTTP server")
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


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash Armbian image (dd bs=1M)")
def step_flash_armbian(ctx: StepContext) -> bool:
    d = ctx.device
    source = ctx.get("firmware_source")
    # launch_script + wait_for_report (not run_script): serial readback after a long
    # exec is ~50% unreliable on real hardware; device POSTs results back via TCP/IP.
    flash_script = (
        f"curl -sk -o /tmp/mono_imager_firmware.img {source} "
        f"> /tmp/mono_imager_step07_flash.log 2>&1; "
        f"dd if=/tmp/mono_imager_firmware.img of={ctx.flash_target} bs=1M "
        f">> /tmp/mono_imager_step07_flash.log 2>&1; "
        f"sync; "
        f"rm -f /tmp/mono_imager_firmware.img; "
        f"curl -sk -X POST --data-binary @/tmp/mono_imager_step07_flash.log "
        f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=07\" "
        f">/dev/null 2>&1"
    )
    is_xz = str(ctx.firmware_path).lower().endswith(".xz")
    if is_xz:
        # Stream curl → xz -dc → dd: no temp file, decompresses on the fly.
        # { } group captures stderr from all three commands into one log file.
        flash_script = (
            f"{{ curl -sk {source} | xz -dc | "
            f"dd of={ctx.flash_target} bs=1M; }} "
            f"> /tmp/mono_imager_step07_flash.log 2>&1; "
            f"sync; "
            f"curl -sk -X POST --data-binary @/tmp/mono_imager_step07_flash.log "
            f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=07\" "
            f">/dev/null 2>&1"
        )
        console_logger.info("Flashing Armbian (XZ streaming) — this takes several minutes...")
    else:
        console_logger.info("Flashing Armbian — this takes several minutes...")
    try:
        d.launch_script(flash_script, marker="step07_flash")
    except Exception as e:
        return step(0, "Armbian flash launched", False, str(e))
    raw, err = with_spinner(wait_for_report, "07", timeout=600.0, message="Flashing Armbian")
    if err or raw is None:
        return step(0, "Armbian flash (dd)", False, str(err) if err else "no report-back from device in 600s")
    has_records = "records in" in raw and "records out" in raw
    has_error   = "error" in raw.lower() or "failed" in raw.lower()
    step(0, "Armbian flash executed", True)
    step(0, "dd records in/out confirmed", has_records, raw[-200:] if not has_records else "")
    step(0, "No errors", not has_error, raw[-200:] if has_error else "")
    return has_records and not has_error


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"], produces=["reboot_triggered"], label="Reboot to U-Boot")
def step_reboot_to_uboot(ctx: StepContext) -> bool:
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=1)
        console_logger.info("  Rebooting — waiting for U-Boot autoboot...")
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot triggered", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["reboot_triggered"], produces=["rebooted"], label="Configure U-Boot to boot Armbian")
def step_configure_uboot(ctx: StepContext) -> bool:
    """
    Interrupt U-Boot after the Armbian flash and set the bootcmd to load
    Armbian via extlinux from eMMC partition 1.

    Must happen AFTER the flash because dd overwrites the eMMC env area,
    resetting bootcmd to the factory default. Official procedure:
      setenv bootcmd "sysboot mmc 0:1 any 0x80000000 /boot/extlinux/extlinux.conf"
      saveenv
    Source: https://opnsense.mono.si/experimental/
    """
    d = ctx.device
    interrupted, _ab_err = with_spinner(d.wait_for_autoboot, timeout=90, message="Waiting for U-Boot autoboot (up to 90s)...")
    if _ab_err or not interrupted:
        return step(0, "U-Boot interrupt", False, "autoboot message not seen within 90s")
    try:
        # Restore whatever env vars the whole-disk flash reset to
        # factory defaults, BEFORE setting our own bootcmd below — see
        # flash_orchestrator.restore_uboot_env(): it's safe to restore
        # first and override after, since U-Boot env is last-write-wins.
        from mono_imager.flash_orchestrator import restore_uboot_env
        restore_uboot_env(d, getattr(d, "captured_uboot_env", None))

        d.send_command(
            'setenv bootcmd "sysboot mmc 0:1 any 0x80000000 /boot/extlinux/extlinux.conf"',
            timeout=10
        )
        d.send_command("saveenv", timeout=15)
        step(0, "U-Boot bootcmd set (sysboot mmc 0:1 extlinux)", True)
        d.send_command("boot", wait_for_prompt=False, timeout=5)
        return step(0, "Armbian booting from eMMC", True)
    except Exception as e:
        return step(0, "U-Boot bootcmd configure", False, str(e))
