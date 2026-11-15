"""
mono-imager: The flash journey's phases: U-Boot bootstrap, recovery boot, flashing, post-flash

Split out of flash_orchestrator.py; import it through mono_imager.flash_orchestrator,
which re-exports these names. Calls go through `F.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


from typing import Optional

from mono_imager import flash_orchestrator as F


def phase1_uboot(port: str, baud: int = F.C.SERIAL_BAUD, wait_message: Optional[str] = None,
                 autoboot_timeout: float = F.C.AUTOBOOT_TIMEOUT) -> Optional[F.SerialDevice]:
    """
    Phase 1a: Serial bootstrap up to the U-Boot prompt.

    Detects the port, connects, waits for the user to power-cycle,
    and interrupts U-Boot autoboot. Returns the device at the U-Boot
    prompt — ready for the caller to run any U-Boot commands before
    handing off to phase1_recovery().

    If the device is already sitting at the U-Boot prompt (e.g. a
    retry within the same session), the power-cycle wait is skipped —
    see SerialDevice.probe_uboot_prompt(). This never skips anything
    else; journey-specific U-Boot commands and phase1_recovery() still
    run identically either way.

    Also snapshots the current U-Boot environment (printenv) before
    returning, so a later whole-disk flash to eMMC — which can reset
    this hardware's env storage area to factory defaults, since its
    U-Boot env backend is MMC-primary — has something to restore from.
    See capture_uboot_env()/restore_uboot_env() below.

    No OS awareness. No eMMC erase. No bootcmd changes.
    Those belong in journey files.

    wait_message / autoboot_timeout: optional, for callers that want the
    user to reboot the running OS cleanly instead of power-cycling (eMMC
    snapshot/restore, #29) and so need longer than 60 s for the OS to
    shut down. Defaults leave every existing caller unchanged.

    Returns SerialDevice at U-Boot prompt on success, None on failure.
    """
    F.reset_results()
    F.verbose("=" * F.C.DIVIDER_WIDTH)
    F.verbose("Phase 1 — Bootstrap (serial)")
    F.verbose("=" * F.C.DIVIDER_WIDTH)
    F.console_logger.info("Connecting to device...")

    # Step 1: Port present (only checks the OS lists it; Step 2 proves a device answers)
    try:
        known, other = F.detect_serial_ports()
        all_ports    = known + other
        found        = any(p.device == port for p in all_ports)
        if not F.step(1, f"Port {port} present", found, "" if found else "port not in list"):
            return None
    except RuntimeError as e:
        F.step(1, f"Port {port} present", False, str(e))
        return None

    # Step 2: Connect
    d = F.SerialDevice(port, timeout=F.CONNECT_READ_TIMEOUT)
    connected = d.connect(baud)
    if not F.step(2, f"Connect at {baud} baud", connected):
        return None

    # Step 3: Interrupt U-Boot — skip the wait if we're already there.
    already_at_prompt = d.probe_uboot_prompt(timeout=F.PROMPT_PROBE_TIMEOUT)
    if already_at_prompt:
        F.verbose("✓ Device already at U-Boot prompt — skipping power-cycle wait")
        interrupted = True
    else:
        print()
        print("=" * F.C.DIVIDER_WIDTH)
        print(wait_message or "  ⚡ POWER CYCLE YOUR DEVICE NOW ⚡")
        print("=" * F.C.DIVIDER_WIDTH)
        print()
        interrupted, _autoboot_err = F.with_spinner(
            d.wait_for_autoboot, timeout=autoboot_timeout,
            message="Waiting for U-Boot autoboot interrupt..."
        )
        if _autoboot_err:
            interrupted = False
    if not F.step(3, "U-Boot autoboot interrupted", interrupted,
                "already at prompt — power cycle skipped" if already_at_prompt else ""):
        d.disconnect()
        return None

    # Snapshot the env now, before any journey-specific U-Boot commands
    # run — see capture_uboot_env() below. Failure is non-fatal here;
    # it just leaves nothing to restore later.
    d.captured_uboot_env = F.capture_uboot_env(d)

    return d


def phase1_recovery(d: F.SerialDevice,
                     boot_method: str = "boot_recovery",
                     login_method: str = "login_recovery",
                     boot_medium: Optional[str] = None) -> Optional[F.SerialDevice]:
    """
    Phase 1b: Boot staging Linux and log in.

    Called after phase1_uboot() — and after any journey-specific
    U-Boot commands (eMMC erase, bootcmd changes, etc.) have been
    run by the journey file.

    boot_method / login_method select which SerialDevice methods to call.
    Default "boot_recovery" + "login_recovery" is the original behaviour.
    Journeys that need a different staging OS (e.g., Armbian when the
    'recovery' U-Boot variable has been lost) register alternative methods
    via step_registry.register_staging_boot() and tui.py passes them here.

    boot_medium: forwarded to boot_recovery() (only when that's the boot
    method) so the modern firmware tool can detect the medium. Default
    None leaves every existing caller unchanged.

    Returns the same SerialDevice now at the staging shell, or None on failure.
    """
    boot_fn  = getattr(d, boot_method)
    login_fn = getattr(d, login_method)

    boot_label  = "Recovery Linux booted" if boot_method == "boot_recovery" else "Staging Linux booted"
    login_label = ("Logged into recovery shell (root@recovery)"
                   if login_method == "login_recovery"
                   else "Logged into staging shell")

    # Step 4: Boot
    boot_msg = "Booting recovery Linux..." if boot_method == "boot_recovery" else "Booting staging Linux..."
    boot_args = {"boot_medium": boot_medium} if (boot_method == "boot_recovery" and boot_medium) else {}
    booted, _boot_err = F.with_spinner(boot_fn, message=boot_msg, **boot_args)
    if _boot_err:
        booted = False
    if not F.step(4, boot_label, booted):
        d.disconnect()
        return None

    # Step 5: Login
    login_msg = "Logging into recovery shell..." if login_method == "login_recovery" else "Logging into staging shell..."
    logged_in, _login_err = F.with_spinner(login_fn, timeout=F.RECOVERY_LOGIN_TIMEOUT, message=login_msg)
    if _login_err:
        logged_in = False
    if not F.step(5, login_label, logged_in):
        d.disconnect()
        return None

    return d


def phase1_bootstrap(port: str, baud: int = F.C.SERIAL_BAUD,
                     boot_medium: Optional[str] = None) -> Optional[F.SerialDevice]:
    """
    Phase 1: Full bootstrap — U-Boot interrupt + recovery boot + login.

    Convenience wrapper around phase1_uboot() + phase1_recovery() for
    callers that have no U-Boot commands to run in between (option 2
    firmware update, raw serial console, etc.).

    For OS flash journeys, call phase1_uboot() and phase1_recovery()
    separately so the journey can run its U-Boot steps in between.

    boot_medium: forwarded to phase1_recovery() so the modern firmware
    tool can detect the medium. Default None leaves existing callers
    unchanged.

    Returns SerialDevice at recovery shell on success, None on failure.
    """
    d = F.phase1_uboot(port, baud)
    if d is None:
        return None
    return F.phase1_recovery(d, boot_medium=boot_medium)


def _step09_source_reachable(d: F.SerialDevice, host_ip: str, port: int, url: str) -> bool:
    """Step 9: the device can reach the host's firmware URL (HEAD, reported over TCP/IP)."""
    # Step 9: Firmware source reachable.
    #
    # PROTOTYPE: TCP/IP-based result reporting, replacing serial-echo
    # readback. Reading a script's result back over serial was found
    # to be intermittently unreliable on real hardware in EARLIER
    # testing (~50% failure rate) — but that was measured against
    # multi-command scripts using $() substitution and curl chaining,
    # NOT against simple single-curl commands. A later baseline test
    # (test_run_script_reliability.py --mode step09) proved the exact
    # curl-against-real-URL pattern below is 100% reliable across 5
    # real hardware runs via run_script() (which DOES wait for and
    # read the serial response). The earlier theory — that ALL serial
    # readback is unreliable — was wrong; only the longer, chained,
    # $()-substitution script was failing. This version is
    # deliberately kept as close to that proven-reliable pattern as
    # possible: ONE curl call writing its status code to a file, then
    # ONE follow-up curl call uploading that file's content via POST.
    # No command substitution, no semicolon-chained multi-command
    # script. Still launched via launch_script() (fire-and-forget) and
    # reported back via TCP/IP (not parsed from serial), per the
    # earlier architectural decision that the TCP/IP link has been
    # 100% reliable all session — this keeps that part.
    #
    # ACTUAL ROOT CAUSE (found after the above still failed on real
    # hardware with the real ~400MB firmware file, despite passing
    # 100% in every isolated test that used a tiny dummy payload):
    # the first curl call used a full GET, which downloads the ENTIRE
    # firmware file before "completing" (-o /dev/null discards the
    # body, but curl still waits for the full transfer). For a real
    # multi-hundred-MB file, that download alone could exceed the 20s
    # wait_for_report() timeout, so the second chained command (the
    # report-back curl) genuinely never got a chance to run in time.
    # Switched to "curl -I" (HEAD request) below — confirms reachability
    # via headers only, no body transferred — which is also a more
    # correct check for Step 09's actual goal ("is the source
    # reachable", not "can we download the whole thing"; the real
    # download happens later in Steps 10-12 via curl | dd anyway).
    # Requires a do_HEAD handler on the host's HTTP server (added
    # alongside do_GET above), since BaseHTTPRequestHandler returns 501
    # for HEAD by default.
    check_script = (
        f"curl -sk -I -o /dev/null -w '%{{http_code}}' {url} "
        f"> /tmp/mono_imager_step09_code.txt; "
        f"curl -sk -X POST --data-binary @/tmp/mono_imager_step09_code.txt "
        f"\"http://{host_ip}:{port}/report?step=09\" >/dev/null 2>&1"
    )
    # Every isolated test of this exact script (test_run_script_reliability.py
    # --mode launchtest, and test_real_boot_then_launch.py) explicitly
    # checked launch_script()'s return value / raised exceptions and
    # passed 100% of the time. This real call site previously discarded
    # the return value entirely — if launch_script() raised here (e.g.
    # the byte-count verification mismatch seen elsewhere in this
    # session), that exception would propagate silently into whatever
    # wraps phase3_flash(), with no specific log line pointing at this
    # being the cause. Capturing and logging it explicitly closes that
    # gap, matching what every passing isolated test already did.
    try:
        remote_path = d.launch_script(check_script, marker="step09_reachable")
        F.verbose(f"✓ Step 09: launch_script() returned without raising: {remote_path}", "debug")
    except Exception as e:
        F.verbose(f"✗ Step 09: launch_script() RAISED: {e}", "error")
        remote_path = None

    check = F.wait_for_report("09", timeout=F.C.REACHABLE_REPORT_TIMEOUT)
    reachable = check is not None and "200" in check

    debug_detail = f"HTTP status: {check}" if not reachable else ""
    if not reachable:
        F.logger.error(
            f"Step 09: report never arrived or was unexpected (got: {check!r}). "
            "This script is the simplified, baseline-proven-reliable version — "
            "if this still fails, the issue is likely NOT script complexity."
        )

    return F.step(9, f"Firmware source reachable ({url})", reachable, debug_detail)


def _flash_script(url: str, flash_target: str, firmware_size: int) -> str:
    """Steps 10-12 script: download-then-dd, or curl | dd when the image is too big for RAM."""
    # Step 10+11+12: download firmware, then dd from local file —
    # UNLESS the image is too large to fit in recovery Linux's root
    # filesystem (confirmed on real hardware to be a 3.8GB tmpfs-
    # backed root, NOT the full 8GB physical RAM — `df -h /tmp` shows
    # `rootfs 3.8G`, and /tmp is not a separate, larger mount). A
    # ~5GB OPNsense image was confirmed via manual on-device curl
    # testing to fail at 74% (3.74GB written) with curl error 23
    # ("Failure writing output to destination, passed 16384 returned
    # 257") — a disk-full write failure, not a network or server
    # issue (ruled out: Python's stock http.server delivered the same
    # file at full transfer speed with no server-side error; the
    # device's own curl 8.19.0 is a full build with Largefile support,
    # not a limited busybox curl).
    #
    # FLASH_SIZE_CAP below is set at 80% of that confirmed 3.8GB cap
    # (≈3.0GB) to leave headroom for the flash script itself plus
    # curl's in-flight buffers — not a guess, a deliberate margin
    # under the hard, confirmed ceiling.
    #
    # Below the cap: download-then-dd, matching Mono's own documented
    # procedures (both the OpenWRT recovery guide and the OPNsense
    # install guide at docs.mono.si / opnsense.mono.si never pipe
    # curl directly into dd — both download the full image to disk
    # first, then dd from that local file, bs=1M). This was changed
    # from an earlier `curl | dd bs=4M` streaming-pipe version after
    # real-hardware testing showed dd reporting 100% partial records
    # (e.g. "0+95365 records in / 0+95365 records out", zero full
    # records) — a known consequence of piping curl directly into dd,
    # since a pipe never delivers clean fixed-size blocks regardless
    # of bs. The flash still worked when piped (later verified by
    # mounting /dev/mmcblk0p1 and confirming a complete, valid
    # filesystem), but the all-partial-records log output is
    # confusing and is an unverified deviation from the
    # vendor-documented method on hardware where a bad flash means
    # physical re-recovery.
    #
    # Above the cap: there is no other option — streaming curl | dd
    # directly to the eMMC target is the only way an oversized image
    # can be flashed at all, since it never touches the
    # capacity-limited root filesystem. This re-enables that
    # previously-tested, previously-reverted path, but ONLY for
    # images that need it; small images keep using the safer,
    # quieter, vendor-documented buffered method.
    #
    # dd's "records in/out" summary is written to stderr, which 2>&1
    # merges into stdout — redirect that combined stream to a file,
    # then cat it back, same reasoning as Step 9 above.
    #
    # NOTE: a live-progress version (dd status=progress + periodic
    # TCP/IP reporting) was attempted and reverted. Root cause: this
    # device runs BUSYBOX dd, which does NOT support status=progress
    # at all (confirmed directly: BusyBox dd only accepts
    # status=noxfer or status=none). A SIGUSR1-based alternative
    # (BusyBox dd does print intermediate progress on SIGUSR1) was
    # explored but the PID-tracking needed to signal the right
    # process reliably wasn't solid by testing time. Live progress
    # remains a real opportunity for a future, more careful pass —
    # not worth the risk of breaking a working flash step in the
    # meantime.
    FLASH_SIZE_CAP = F.C.RAM_STAGING_CAP  # ≈3.0GB, 80% of confirmed 3.8GB root cap

    use_streaming = firmware_size > 0 and firmware_size > FLASH_SIZE_CAP

    if use_streaming:
        F.verbose(
            f"Firmware size ({firmware_size / F.C.GIB:.2f} GB) exceeds the "
            f"{FLASH_SIZE_CAP / F.C.GIB:.2f} GB buffered-flash cap — using "
            "streaming mode (curl | dd direct to target).",
            "warning"
        )
        flash_script = (
            f"curl -sk {url} 2>/tmp/mono_imager_step10_flash.log | "
            f"dd of={flash_target} bs=1M "
            f">> /tmp/mono_imager_step10_flash.log 2>&1; "
            f"cat /tmp/mono_imager_step10_flash.log"
        )
    else:
        local_fw_path = "/tmp/mono_imager_firmware.img"
        flash_script = (
            f"curl -sk -o {local_fw_path} {url} "
            f"> /tmp/mono_imager_step10_flash.log 2>&1; "
            f"dd if={local_fw_path} of={flash_target} bs=4096 "
            f">> /tmp/mono_imager_step10_flash.log 2>&1; "
            f"rm -f {local_fw_path}; "
            f"cat /tmp/mono_imager_step10_flash.log"
        )
    return flash_script


def phase3_flash(d: F.SerialDevice, host_ip: str, port: int, flash_target: str,
                  firmware_size: int = 0) -> bool:
    """
    Phase 3: Flash — curl | dd on device, verify dd output.

    Uses run_script() with output redirected to files on-device, then
    read back via cat/wc — NOT direct stdout capture. This matters:
    direct-stdout capture (curl's real output landing straight in the
    exec response, e.g. "curl -s -o /dev/null -w '%{http_code}' <url>"
    read directly) was found to be FLAKY on real hardware — passing
    roughly 1 in 3 runs with identical code, identical command, same
    device. Across many real runs, the file-redirect pattern (write
    output to a file, then separately cat/wc that file — exactly what
    a parallel diagnostic script's "Stage 3" does) passed 100% of the
    time, while the direct-stdout form failed intermittently. Rather
    than chase the exact cause of that intermittent loss (suspected:
    curl's stdout buffering mode varies depending on pty/line-
    discipline state at process start, though this was not confirmed
    — a competing theory, shell line-editing interference, was tested
    and found inconclusive on a single run), this switches to the
    pattern with a 100% observed pass rate.
    """
    F.verbose("=" * F.C.DIVIDER_WIDTH)
    F.verbose("Phase 3 — Flash")
    F.verbose("=" * F.C.DIVIDER_WIDTH)
    F.console_logger.info("Preparing to flash...")

    url = f"http://{host_ip}:{port}/firmware.img"

    if not F._step09_source_reachable(d, host_ip, port, url):
        return False

    F.verbose(f"Flashing {flash_target} — this may take several minutes...")
    F.console_logger.info("Flashing firmware — this may take several minutes...")
    flash_script = F._flash_script(url, flash_target, firmware_size)
    response, flash_error = F.with_spinner(
        d.run_script, flash_script,
        marker="step10_flash", exec_timeout=F.C.FLASH_TIMEOUT,
        message="Flashing — this may take several minutes"
    )
    if flash_error is not None:
        F.verbose(f"✗ Step 10: run_script() RAISED: {flash_error}", "error")
        F.step(10, "curl | dd executed on device", False, str(flash_error))
        F.step(11, "dd confirmed records in/out", False)
        F.step(12, "No curl errors", False)
        return False

    has_records = "records in" in response and "records out" in response
    has_error   = "error" in response.lower() or "failed" in response.lower()

    F.step(10, "curl | dd executed on device", True)
    F.step(11, "dd confirmed records in/out", has_records,
         f"output: {response[-F.C.LOG_TAIL:]}" if not has_records else "")
    F.step(12, "No curl errors", not has_error,
         f"output: {response[-F.C.LOG_TAIL:]}" if has_error else "")

    return has_records and not has_error


def phase4_postflash(d: F.SerialDevice) -> bool:
    """
    Phase 4: Post-flash reboot.

    Sends reboot to the device. Journey files handle any post-flash
    steps before calling this (firmware re-image, DIP flip prompts, etc.).
    """
    F.verbose("=" * F.C.DIVIDER_WIDTH)
    F.verbose("Phase 4 — Post-flash")
    F.verbose("=" * F.C.DIVIDER_WIDTH)
    F.verbose("Sending reboot command...")
    d.send_command("reboot", wait_for_prompt=False, timeout=F.C.REBOOT_CMD_TIMEOUT)
    F.verbose("Flash complete. Reboot sent.")
    F.console_logger.info("Rebooting device...")
    return True


__all__ = [
    "_flash_script",
    "_step09_source_reachable",
    "phase1_bootstrap",
    "phase1_recovery",
    "phase1_uboot",
    "phase3_flash",
    "phase4_postflash",
]
