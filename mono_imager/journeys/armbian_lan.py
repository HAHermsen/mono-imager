"""
mono-imager journey: Armbian via LAN

Follows the official documented procedure (we-are-mono/docs
"Installing Armbian") instead of the previous NOR-stays-boot-source
shortcut: no U-Boot env editing at all. DIP actually ends up flipped
to and verified on eMMC, with eMMC's own firmware region genuinely
refreshed (the whole-disk dd wipes it, same as it does for OpenWRT).

Steps:
  1. Device network ready
  2. Start HTTP server
  3. Verify firmware reachable
  4. Flash Armbian image (dd bs=1M, or xz -dc | dd for .img.xz)
  5. Flip DIP to eMMC and verify boot — manual confirm (press Enter
     once it's booted), not an automated check: real-hardware testing
     showed the automated boot-source poll could time out even after
     a clean flash, and its failure wasn't recorded in the step
     report either. A pause is still needed here regardless, since
     the NOR round-trip below can't start until eMMC is actually up.
     The pause relays live serial output while waiting (see
     _pause_with_live_serial) instead of leaving the console blank —
     a plain input() gave no way to actually see the device boot,
     since mono-imager holds the port exclusively (no second terminal
     can attach to watch it either).
  6. Refresh eMMC firmware (NOR round-trip) — flip back to NOR, power
     cycle, re-resolve network, run `firmware update` to restore
     eMMC's own firmware region, then flip to eMMC one last time
     (static instruction only — nothing further depends on it).

Author:  H.A. Hermsen
License: GPLv3
"""

import logging
import sys
import time
import threading
from mono_imager.step_registry import register_step, StepContext
from mono_imager.spinner import with_spinner
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


# ---------------------------------------------------------------------
# Official procedure (we-are-mono/docs "Installing Armbian") from here
# on, instead of the previous NOR-stays-boot-source shortcut: no U-Boot
# env editing at all — flip DIP to eMMC and let eMMC's own factory
# firmware boot Armbian directly, then refresh that firmware region
# (the whole-disk dd wipes it, same as it wipes OpenWRT's), then leave
# DIP parked on eMMC, verified.
# ---------------------------------------------------------------------

def _pause_with_live_serial(ctx: StepContext, prompt: str) -> None:
    """
    input(), but with the raw serial stream relayed to the console while
    waiting, instead of going silent. Real-hardware use showed the plain
    input() pause gave zero feedback during a manual DIP-flip/power-cycle
    — the console just sat blank, and a second terminal can't be opened
    on the same port since SerialDevice.connect() holds it exclusively
    for the whole session. This relays what's actually on the wire so
    "once it's booted" is something you can actually see, not guess at.

    Polls in_waiting rather than blocking on read() with the full 10s
    port timeout, so the relay thread notices the stop signal quickly
    once Enter is pressed. Any read/decode error (e.g. the port
    dropping mid power-cycle) is swallowed and retried — this is purely
    a visual aid and must never be able to abort the journey itself.
    """
    d = ctx.device
    stop = threading.Event()

    def relay():
        while not stop.is_set():
            try:
                waiting = d.ser.in_waiting
                if waiting:
                    chunk = d.ser.read(waiting)
                    if chunk:
                        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                        sys.stdout.flush()
                else:
                    time.sleep(0.05)
            except Exception:
                time.sleep(0.05)

    t = threading.Thread(target=relay, daemon=True)
    t.start()
    try:
        input(prompt)
    finally:
        stop.set()
        t.join(timeout=1)


def _flip_to_emmc_and_verify(ctx: StepContext) -> bool:
    """
    Official procedure steps 4-5: flip the DIP switch to eMMC and let
    the user confirm by eye that it booted — no automated
    verify_boot_source() poll anymore. That automated check proved
    unreliable on real hardware (a confirmed-good flash still timed
    out waiting for the boot-source marker line), and — worse — its
    failure wasn't even being recorded in the step report, so a
    genuinely failed check could still show up as a full success. A
    manual "did it come up?" confirmation can't produce that kind of
    false result.

    A pause is still needed here regardless of automation: the NOR
    round-trip that follows can't proceed until eMMC is actually up.
    Unlike the old automated check (which sent the reboot itself),
    the user must flip the DIP switch AND power-cycle the device
    themselves before pressing Enter. The pause relays live serial
    output while waiting (see _pause_with_live_serial) so there's
    actually something to look at instead of a blank console.
    """
    console_logger.info("")
    console_logger.info("=" * 60)
    console_logger.info("  ⚡ FLIP THE DIP SWITCH TO eMMC, THEN POWER-CYCLE ⚡")
    console_logger.info("=" * 60)
    _pause_with_live_serial(ctx, "  Once Armbian has booted, press Enter to continue...")
    return step(0, "DIP flipped to eMMC — boot confirmed by user", True)


def _refresh_firmware_and_finish(ctx: StepContext) -> bool:
    """
    Official procedure step 6 ("Write fresh firmware again"): the
    whole-disk Armbian flash also overwrote eMMC's own firmware/
    bootloader region (0-32MiB), so it needs to be restored via
    `firmware update`. Unlike the DIP flip above, getting back to
    NOR-booted recovery Linux needs an actual power cycle here — the
    device is now running Armbian itself (booted from eMMC), and
    mono-imager has no known login to send it a soft reboot with.

    Once back in recovery, the device network needs a fresh DHCP
    resolution (a brand new Linux boot, no state carried over from
    before) — done inline here with a throwaway RecoveryNetwork
    instance rather than the session-cached one on MonoImager, since
    journeys stay decoupled from tui.py (see JOURNEYS.md).
    """
    d = ctx.device
    console_logger.info("")
    console_logger.info("=" * 60)
    console_logger.info("  ⚡ FLIP THE DIP SWITCH TO NOR, THEN POWER-CYCLE ⚡")
    console_logger.info("=" * 60)
    input("  Press Enter once you've done that...")

    interrupted, _err = with_spinner(
        d.wait_for_autoboot, timeout=60,
        message="Waiting for U-Boot autoboot interrupt..."
    )
    if _err or not interrupted:
        return step(0, "U-Boot autoboot interrupted (NOR)", False,
                    str(_err) if _err else "autoboot message not seen within 60s")

    booted, _err = with_spinner(
        d.boot_recovery, boot_medium="qspi",
        message="Booting recovery Linux..."
    )
    if _err or not booted:
        return step(0, "Recovery Linux booted (NOR)", False, str(_err) if _err else "")

    logged_in, _err = with_spinner(
        d.login_recovery, timeout=30,
        message="Logging into recovery shell..."
    )
    if _err or not logged_in:
        return step(0, "Logged into recovery shell (NOR)", False, str(_err) if _err else "")
    step(0, "Back in NOR recovery shell", True)

    from mono_imager.device_net import RecoveryNetwork
    net = RecoveryNetwork()
    net_ok, _err = with_spinner(net.resolve, d, message="Resolving device network...")
    if _err or not net_ok:
        return step(0, "Device network ready (post-reboot)", False, str(_err) if _err else "")

    from mono_imager.recovery_orchestrator import run_firmware_update
    fw_ok, _err = with_spinner(
        run_firmware_update, d,
        message="Refreshing eMMC firmware region..."
    )
    if _err:
        fw_ok = False
    if not step(0, "eMMC firmware region refreshed", fw_ok):
        return False

    # Final DIP flip back to eMMC: no automated boot-source check here
    # either — see _flip_to_emmc_and_verify() above for why. This is
    # the true last step of the journey, so unlike that one there's
    # nothing further to gate on; hand off the instruction (tui.py's
    # own end-of-flash screen repeats a version of it too) and finish.
    console_logger.info("")
    console_logger.info("=" * 60)
    console_logger.info("  ⚡ FLIP THE DIP SWITCH BACK TO eMMC, THEN POWER-CYCLE ⚡")
    console_logger.info("  It will boot Armbian from eMMC with the refreshed firmware.")
    console_logger.info("=" * 60)
    return step(0, "Firmware refresh complete — flip DIP to eMMC and power-cycle", True)


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"], produces=["emmc_boot_verified"], label="Flip DIP to eMMC and verify boot")
def step_flip_to_emmc_and_verify(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Flip DIP to eMMC and verify boot"); verbose("=" * 60)
    return _flip_to_emmc_and_verify(ctx)


@register_step(os=[OS], transfer=[TRANSFER], requires=["emmc_boot_verified"], produces=["rebooted"], label="Refresh eMMC firmware (NOR round-trip)")
def step_refresh_firmware(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Refresh eMMC firmware (NOR round-trip)"); verbose("=" * 60)
    return _refresh_firmware_and_finish(ctx)
