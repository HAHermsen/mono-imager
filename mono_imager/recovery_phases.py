"""
mono-imager: Flash phases: env stash/restore, boot-source verify, legacy flashing

Split out of recovery_orchestrator.py; import it through mono_imager.recovery_orchestrator,
which re-exports these names. Calls go through `R.` so that the
names stay patchable on the parent module, exactly as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""


import re
from collections.abc import Callable

from mono_imager import recovery_orchestrator as R


def stash_uboot_env(d: R.SerialDevice, target: str) -> str | None:
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
            f'if [ -n "$e" ] && dd if="$e" of={R.UBOOT_ENV_STASH} bs={R.UBOOT_ENV_SIZE} count=1 2>/dev/null '
            f'&& [ "$(wc -c < {R.UBOOT_ENV_STASH})" -eq {R.UBOOT_ENV_SIZE} ]; then echo "STASH_OK:$e"; '
            'else echo STASH_FAIL; fi\n'
        )
    else:
        script = (
            f"if dd if=/dev/mmcblk0 of={R.UBOOT_ENV_STASH} bs=1 skip={R.UBOOT_ENV_EMMC_OFFSET} "
            f"count={R.UBOOT_ENV_SIZE} 2>/dev/null && "
            f'[ "$(wc -c < {R.UBOOT_ENV_STASH})" -eq {R.UBOOT_ENV_SIZE} ]; '
            'then echo "STASH_OK:/dev/mmcblk0"; else echo STASH_FAIL; fi\n'
        )
    try:
        out = d.run_script(script, marker="stash_uboot_env", exec_timeout=R.ENV_STASH_TIMEOUT)
    except RuntimeError as e:
        R.logger.error(f"stash_uboot_env: run_script failed: {e}")
        return None
    m = re.search(r"STASH_OK:(/dev/(?:mtd[0-9]+|mmcblk0))", out)
    if not m:
        R.logger.error(f"stash_uboot_env: failed — output:\n{out}")
        return None
    return m.group(1)


def restore_uboot_env_region(d: R.SerialDevice, target: str, env_dev: str) -> bool:
    """Write UBOOT_ENV_STASH back to env_dev (see stash_uboot_env())."""
    if target == "qspi":
        if not re.fullmatch(r"/dev/mtd[0-9]+", env_dev):
            return False
        cmd = f"flashcp {R.UBOOT_ENV_STASH} {env_dev} && echo RESTORE_OK || echo RESTORE_FAIL"
    else:
        cmd = (
            f"dd if={R.UBOOT_ENV_STASH} of=/dev/mmcblk0 bs=1 seek={R.UBOOT_ENV_EMMC_OFFSET} 2>/dev/null "
            "&& sync && echo RESTORE_OK || echo RESTORE_FAIL"
        )
    try:
        out = d.run_script(cmd, marker="restore_uboot_env", exec_timeout=R.ENV_STASH_TIMEOUT)
    except RuntimeError as e:
        R.logger.error(f"restore_uboot_env_region: run_script failed: {e}")
        return False
    ok = "RESTORE_OK" in out
    if not ok:
        R.logger.error(f"restore_uboot_env_region: failed — output:\n{out}")
    return ok


def verify_boot_source(d: R.SerialDevice, expected: str, timeout: float = R.VERIFY_BOOT_TIMEOUT) -> bool:
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

    R.logger.info(f"Initiating reboot to verify boot source ({marker_text!r})...")

    # CRITICAL: Send reboot command NOW. Device is at recovery shell
    # prompt (silent). Without this command, the byte-reading loop below
    # listens to empty serial → timeout → false failure 100% of the time.
    try:
        d.send_command("reboot", wait_for_prompt=False)
    except Exception as e:
        R.logger.warning(f"reboot command exception (expected — device disconnects): {e}")

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
                        R.logger.debug("U-Boot output detected — now watching for boot marker")
                        buffer = b""  # reset to fresh buffer
                    continue

                # Second: look for boot source marker in U-Boot output
                if marker_text.encode() in buffer:
                    R.logger.info(f"✓ Boot source confirmed: {marker_text}")
                    return True
        except Exception as e:
            R.logger.debug(f"Serial read exception: {e}")
            break

    # Timeout without finding marker
    if not uboot_found:
        R.logger.warning(f"Did not detect U-Boot output within {timeout}s — device may not have rebooted")
    else:
        R.logger.warning(f"U-Boot detected but did not see {marker_text!r} within {timeout}s")
    return False


# --- Legacy path: curl + dd / flashcp -----------------------------------

def legacy_flash_emmc(d: R.SerialDevice, mac: str) -> bool:
    """
    Legacy eMMC flash exactly per the documented "Manual flashing
    (legacy)" procedure: curl with mono:{MAC} basic auth, then dd
    with the documented skip=1 seek=1 (skips the first 4KB / GPT
    region on both input and output, per the docs' own explanation).
    """
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        R.logger.error(f"legacy_flash_emmc: invalid MAC address: {mac!r}")
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
        f"curl -k -u mono:{mac} -z firmware-emmc-gateway-dk.bin -O {R.LEGACY_EMMC_URL} && "
        f"dd if=firmware-emmc-gateway-dk.bin of=/dev/mmcblk0 bs=4096 skip=1 seek=1; "
        f"echo RC=$?"
    )
    try:
        output = d.run_script(cmd, marker="legacy_emmc", exec_timeout=R.LEGACY_FLASH_TIMEOUT)
    except RuntimeError as e:
        R.logger.error(f"legacy_flash_emmc: run_script failed: {e}")
        return False

    success = "RC=0" in output and ("records out" in output or "records in" in output)
    if not success:
        R.logger.error(f"legacy eMMC flash did not confirm success — output:\n{output}")
    return success


def legacy_flash_nor(d: R.SerialDevice, mac: str) -> bool:
    """
    Legacy NOR flash exactly per the documented procedure: curl with
    mono:{MAC} basic auth, then flashcp to /dev/mtd0.
    """
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        R.logger.error(f"legacy_flash_nor: invalid MAC address: {mac!r}")
        return False
    # -z: see legacy_flash_emmc() above — skip re-download only when the
    # server's copy isn't newer than what's already sitting on the device.
    cmd = (
        f"curl -k -u mono:{mac} -z firmware-qspi-gateway-dk.bin -O {R.LEGACY_NOR_URL} && "
        f"flashcp -v firmware-qspi-gateway-dk.bin /dev/mtd0; "
        f"echo RC=$?"
    )
    try:
        output = d.run_script(cmd, marker="legacy_nor", exec_timeout=R.LEGACY_FLASH_TIMEOUT)
    except RuntimeError as e:
        R.logger.error(f"legacy_flash_nor: run_script failed: {e}")
        return False

    success = "RC=0" in output
    if not success:
        R.logger.error(f"legacy NOR flash did not confirm success — output:\n{output}")
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

def phase_modern_flash_emmc(d: R.SerialDevice, on_output: Callable[[str], None] | None = None,
                            preserve_env: bool | None = None) -> bool:
    """
    Modern path, step 1: from NOR-booted recovery, run `firmware
    update` to flash eMMC. Returns True on confirmed success.
    """
    R.console_logger.info("Running 'firmware update' to flash eMMC...")
    ok = R.step(1, "Flash eMMC via 'firmware update'",
              R.run_firmware_update(d, on_output=on_output, preserve_env=preserve_env))
    return ok


def phase_modern_verify_emmc_boot(d: R.SerialDevice, timeout: float = R.VERIFY_BOOT_MODERN_TIMEOUT) -> bool:
    """
    Modern path, step 2: after the user flips the DIP switch to eMMC
    and reboots, confirm the device actually booted from eMMC by
    watching U-Boot's own confirmation line. Does NOT send the reboot
    itself or block on input — caller handles prompting the user to
    flip the switch and reboot; this just waits for and verifies the
    result once that happens.
    """
    ok = R.step(2, "Verify eMMC boot", R.verify_boot_source(d, "EMMC", timeout=timeout))
    return ok


def phase_modern_flash_nor(d: R.SerialDevice, on_output: Callable[[str], None] | None = None,
                           preserve_env: bool | None = None) -> bool:
    """
    Modern path, step 3: from eMMC-booted recovery, run `firmware
    update` again — it auto-targets NOR this time since eMMC is now
    the active boot source. Returns True on confirmed success.
    """
    R.console_logger.info("Running 'firmware update' to flash NOR...")
    ok = R.step(3, "Flash NOR via 'firmware update'",
              R.run_firmware_update(d, on_output=on_output, preserve_env=preserve_env))
    return ok


def phase_modern_verify_nor_boot(d: R.SerialDevice, timeout: float = R.VERIFY_BOOT_MODERN_TIMEOUT) -> bool:
    """
    Modern path, step 4: after the user flips the DIP switch back to
    NOR and reboots, confirm the device actually booted from NOR.
    """
    ok = R.step(4, "Verify NOR boot (back to factory default)", R.verify_boot_source(d, "NOR", timeout=timeout))
    return ok


def _legacy_flash_with_env(d: R.SerialDevice, target: str, flash_fn, env_dev: str | None) -> bool:
    """
    Run a legacy flash (curl+dd / curl+flashcp — both rewrite the env
    region) and, if env_dev is set (stash_uboot_env() succeeded
    beforehand), write the stashed env back afterwards (#24).
    """
    ok = flash_fn()
    if ok and env_dev:
        if R.restore_uboot_env_region(d, target, env_dev):
            R.console_logger.info(f"  ✓ U-Boot env restored ({env_dev})")
        else:
            R.console_logger.info(f"  ⚠ Flash OK, but restoring the U-Boot env to {env_dev} FAILED — "
                                "env is at factory defaults.")
    return ok


def phase_legacy_flash_emmc(d: R.SerialDevice, env_dev: str | None = None) -> bool:
    """
    Legacy path, step 1: get the device's real MAC, then flash eMMC
    via curl+dd per the documented legacy procedure. env_dev: see
    _legacy_flash_with_env().
    """
    mac = R.get_device_mac(d)
    if mac is None:
        return R.step(1, "Flash eMMC (legacy curl+dd)", False, "could not determine device MAC address")
    R.console_logger.info(f"Device MAC: {mac}")
    R.console_logger.info("Downloading and flashing eMMC (legacy path)...")
    ok = R.step(1, "Flash eMMC (legacy curl+dd)",
              R._legacy_flash_with_env(d, "emmc", lambda: R.legacy_flash_emmc(d, mac), env_dev))
    return ok


def phase_legacy_flash_nor(d: R.SerialDevice, env_dev: str | None = None) -> bool:
    """
    Legacy path, step 2: same MAC, flash NOR via curl+flashcp. env_dev:
    see _legacy_flash_with_env().
    """
    mac = R.get_device_mac(d)
    if mac is None:
        return R.step(2, "Flash NOR (legacy curl+flashcp)", False, "could not determine device MAC address")
    R.console_logger.info(f"Device MAC: {mac}")
    R.console_logger.info("Downloading and flashing NOR (legacy path)...")
    ok = R.step(2, "Flash NOR (legacy curl+flashcp)",
              R._legacy_flash_with_env(d, "qspi", lambda: R.legacy_flash_nor(d, mac), env_dev))
    return ok


__all__ = [
    "_legacy_flash_with_env",
    "legacy_flash_emmc",
    "legacy_flash_nor",
    "phase_legacy_flash_emmc",
    "phase_legacy_flash_nor",
    "phase_modern_flash_emmc",
    "phase_modern_flash_nor",
    "phase_modern_verify_emmc_boot",
    "phase_modern_verify_nor_boot",
    "restore_uboot_env_region",
    "stash_uboot_env",
    "verify_boot_source",
]
