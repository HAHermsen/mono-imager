"""
mono-imager journey: OpenWRT via LAN

Follows docs.mono.si "Installing OpenWRT" (2026 revision) exactly:
the eMMC's factory GPT (partition 1 = boot @32MiB, partition 2 =
rootfs @96MiB) is never repartitioned — the whole-disk image is
written with two dd passes that recreate the image's own GPT/boot
region while leaving Mono's firmware region (4KiB-32MiB) untouched.

  Revision note (see git log): earlier versions of this journey used
  a different, doc-mismatched scheme — fdisk'ing a single MBR
  partition at 32MiB and flashing only /dev/mmcblk0p1. That has been
  replaced to match the current doc exactly. Support for flashing
  OpenWRT sysupgrade.bin.gz tarballs (kernel+rootfs only, no GPT) has
  been DROPPED — that format has no partition table and doesn't fit
  the whole-disk model. Only the raw `*-emmc.img` / `*-emmc.img.gz`
  release image is accepted now.

Steps:
  1. Device network ready
  2. Start HTTP server
  3. Verify firmware reachable
  4. Flash OpenWRT image — two dd passes, per doc step 3:
       a) bs=512 count=8   → first 4KiB (partition table) to /dev/mmcblk0
       b) bs=1M skip=32 seek=32 → rest of image, 32MiB onward
     Image is downloaded to a local file on-device first, then both
     dd passes read that plain file directly (no curl|dd or gunzip|dd
     pipes into dd) — matching Mono's own vendor-documented procedure.
     A real-hardware finding already recorded in flash_orchestrator.py
     (FLASH_SIZE_CAP comment) found piping curl straight into dd
     produces all-partial-record reads; for this journey's exact-4KiB
     partition-table pass that would be a correctness bug on BusyBox
     dd (count= counts read() calls, not bytes), not just cosmetic
     log noise. OpenWRT images are ~100MB against recovery Linux's
     confirmed ~3.8GB tmpfs rootfs, so staging fits comfortably.
  5. Firmware update (eMMC bootloader) — refreshes eMMC's own firmware
     region (QSPI/boot area) while still NOR-booted, per doc step 7.

No automated final DIP-flip/boot-verify step: real-hardware testing
showed it could time out even after a fully clean flash, and — worse
— that failure wasn't being recorded in the step report at all, so the
run still looked like a full success. tui.py's own end-of-flash screen
already tells the user to flip the DIP switch to eMMC and power-cycle
once flash_success is True, so this journey now simply ends after the
firmware update and lets that existing, always-shown instruction do
the job — a static instruction beats an automated check that can
produce a false failure.

U-Boot pre-step:
  Ensures the 'recovery' U-Boot env variable is defined before
  boot_recovery() calls 'run recovery', and sets the official 'openwrt'
  + 'bootcmd' pair per doc step 4 (booti with kernel_addr_r/fdt_addr_r
  via the 'emmc_load' env var, falling back to 'run recovery' if that
  ever fails).

  On factory-fresh devices, 'recovery' is already in NOR env — nothing to do.

  If 'recovery' is missing (e.g., 'env default -a' was run in a previous
  failed attempt), tries to restore it from the redundant/backup env copy
  that U-Boot stores at a second NOR offset.  The recovery KERNEL is always
  safe in NOR flash — only the pointer variable in NOR env can go missing.

  ASSUMPTION NOT YET VERIFIED ON HARDWARE: this relies on 'emmc_load'
  being present in the factory U-Boot env, same as 'recovery'. Unlike
  'recovery', there is no restore-from-NOR-backup handling for
  'emmc_load' here yet — if a device has had 'env default -a' run and
  it's missing 'emmc_load' too, boot will fail after a clean flash.
  Add the same backup-restore treatment here if that turns out to
  matter in the field.

  NOR flash (4KiB-32MiB) is never touched by the flash step itself —
  only by the firmware-update step, which is the official procedure's
  whole point.

Author:  H.A. Hermsen
License: GPLv3
"""

import logging
import re

from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner, Spinner
from mono_imager.flash_orchestrator import (
    step, verbose, console_logger, wait_for_report,
)
from mono_imager.journeys import _common  # also provides shared _step_http_server_start / _step_firmware_reachable
from mono_imager import constants as C

# --- U-Boot recovery repair (NOR) -------------------------------------------------
FIT_HEADER_PROBE_LEN = "0x100"           # bytes read to identify an image header
_FIT_SIZE_FIELD_ADDR = "0x82000004"      # UBOOT_LOAD_ADDR + 4: FIT total-size field
NOR_SCAN_START = 0x400000                # scan NOR for the recovery kernel,
NOR_SCAN_END   = 0x3C00000               #   1 MiB steps, past the env/DTB area
NOR_SCAN_STEP  = 0x100000
FIRMWARE_UPDATE_TIMEOUT = 300

# --- Mono's documented OpenWRT dd passes (whole-disk image, firmware region kept) --
PT_DD_ARGS   = f"bs={C.SECTOR_BYTES} count={C.EMMC_FW_START // C.SECTOR_BYTES}"    # first 4 KiB: partition table
BULK_DD_ARGS = f"bs=1M skip={C.EMMC_FW_END_MIB} seek={C.EMMC_FW_END_MIB}"        # 32 MiB onward

logger = logging.getLogger(__name__)

OS       = "OpenWRT"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the OpenWRT -emmc.img or -emmc.img.gz file:"
TRANSFER = "lan"

# Official U-Boot boot mechanism, per docs.mono.si "Installing OpenWRT" step 4:
# a self-contained 'openwrt' command (run emmc_load && booti) with bootcmd
# falling back to 'run recovery' if the eMMC boot ever fails. 'emmc_load' is
# assumed to be a pre-existing factory env var (loads kernel+fdt from the
# boot partition into kernel_addr_r/fdt_addr_r) — see the ASSUMPTION note
# in the module docstring above.
_UBOOT_SET_EMMC_CMD = (
    "setenv openwrt 'setenv bootargs \"${bootargs_console} boot_medium=emmc "
    "root=/dev/mmcblk0p2 rootwait\"; run emmc_load && booti ${kernel_addr_r} - ${fdt_addr_r}'"
)
_UBOOT_SET_BOOTCMD_CMD = "setenv bootcmd 'run openwrt || run recovery'"
_UBOOT_BOOTARGS_NOR_CMD = f'setenv bootargs "${{bootargs}} boot_medium={C.BOOT_MEDIUM_NOR}"'



def _md_be32(md_output: str) -> int:
    """
    Big-endian u32 from U-Boot `md.b <addr> 4` output, e.g.
    "82000004: 00 5a 1c 3e    .Z.>" -> 0x005a1c3e. Raises ValueError /
    IndexError on unparseable output (callers handle both).
    """
    hex_b = md_output.split(":")[-1].strip().split()[:4]
    return (int(hex_b[0], 16) << 24 | int(hex_b[1], 16) << 16 |
            int(hex_b[2], 16) << 8  | int(hex_b[3], 16))


def _uboot_steps_openwrt_lan(device) -> bool:
    """
    Ensure the 'recovery' U-Boot variable is defined so that boot_recovery()
    can call 'run recovery' to boot recovery Linux from NOR flash.

    On a factory-fresh device (or any device that hasn't had 'env default -a'
    run against it), 'recovery' is already in NOR env — nothing to do.

    If 'recovery' is missing (wiped by a previous 'env default -a'), attempt
    to restore it from U-Boot's redundant/backup env copy stored at a second
    NOR offset. U-Boot writes its primary env at one offset and keeps a backup
    at primary+size. The backup was not overwritten by 'saveenv' after
    'env default -a' — 'saveenv' only updates the primary slot.

    Tries several candidate backup offsets common on NXP LS1046A SPI NOR.
    If any succeeds, the full factory env (including 'recovery') is restored,
    then a correct bootcmd for OpenWRT/extlinux is set, and the result is
    saved back to NOR.

    If all candidates fail, the step fails with instructions for manual repair.
    """
    print("  Checking U-Boot 'recovery' variable...")

    # Fast path: recovery already present (normal case on any clean device).
    # If 'recovery' uses 'sf read', also validate the kernel size at that
    # offset so we don't keep a leftover pointer to a firmware FIT blob.
    try:
        out = device.send_command("printenv recovery", timeout=C.UBOOT_QUICK_TIMEOUT)
        verbose(f"  {out.strip()}")
        if "recovery=" in out:
            recovery_confirmed = False
            if "sf read" in out:
                # Extract NOR offset (2nd arg of "sf read <ram> <noroff> <size>")
                parts = out.split("sf read")[-1].strip().split()
                koffset = parts[1] if len(parts) >= 2 else ""
                if koffset:
                    try:
                        device.send_command("sf probe 0", timeout=C.UBOOT_CMD_TIMEOUT)
                        device.send_command(f"sf read {C.UBOOT_LOAD_ADDR} {koffset} {FIT_HEADER_PROBE_LEN}", timeout=C.UBOOT_WRITE_TIMEOUT)
                        magic_out = device.send_command(f"md.b {C.UBOOT_LOAD_ADDR} 4", timeout=C.UBOOT_QUICK_TIMEOUT)
                        if C.FIT_MAGIC_HEX in magic_out:
                            size_out = device.send_command(f"md.b {_FIT_SIZE_FIELD_ADDR} 4", timeout=C.UBOOT_QUICK_TIMEOUT)
                            try:
                                fit_size = _md_be32(size_out)
                                if fit_size < C.MIN_KERNEL_FIT_BYTES:
                                    verbose(f"  ✗ 'recovery' points to a non-kernel FIT "
                                            f"({fit_size / C.MIB:.1f} MB at {koffset}) — clearing...")
                                    device.send_command("setenv recovery", timeout=C.UBOOT_QUICK_TIMEOUT)
                                    # fall through to NOR scan
                                else:
                                    verbose(f"  ✓ 'recovery' confirmed — kernel FIT at {koffset} "
                                            f"({fit_size / C.MIB:.1f} MB)")
                                    recovery_confirmed = True
                            except (ValueError, IndexError):
                                verbose("  ✓ 'recovery' is defined (validation skipped)")
                                recovery_confirmed = True
                        elif C.GZIP_MAGIC_HEX in magic_out:
                            # Gzip kernel (Image.gz / AArch64 booti path).
                            # Also verify kernel_comp_addr_r is in NOR env —
                            # it gets wiped by 'env default -a' and booti
                            # silently fails without it.
                            kc = device.send_command("printenv kernel_comp_addr_r", timeout=C.UBOOT_QUICK_TIMEOUT)
                            if "kernel_comp_addr_r=" in kc:
                                verbose(f"  ✓ 'recovery' confirmed — gzip kernel at {koffset}")
                                recovery_confirmed = True
                            else:
                                verbose("  ✗ kernel_comp_addr_r missing — rebuilding recovery env...")
                                device.send_command("setenv recovery", timeout=C.UBOOT_QUICK_TIMEOUT)
                                # fall through to NOR scan
                        else:
                            verbose("  ✓ 'recovery' is defined — no changes needed")
                            recovery_confirmed = True
                    except Exception:
                        verbose("  ✓ 'recovery' is defined (validation skipped)")
                        recovery_confirmed = True
                else:
                    verbose("  ✓ 'recovery' is defined — no changes needed")
                    recovery_confirmed = True
            else:
                verbose("  ✓ 'recovery' is defined — no changes needed")
                recovery_confirmed = True

            if recovery_confirmed:
                device.send_command(_UBOOT_SET_EMMC_CMD, timeout=C.UBOOT_CMD_TIMEOUT)
                device.send_command(_UBOOT_SET_BOOTCMD_CMD, timeout=C.UBOOT_CMD_TIMEOUT)
                # Re-persist decompression vars — something (firmware update?) can wipe them
                # between runs, causing the next run to fall through to the slow NOR scan.
                device.send_command(f"setenv kernel_comp_addr_r {C.UBOOT_KERNEL_COMP_ADDR}", timeout=C.UBOOT_QUICK_TIMEOUT)
                device.send_command(f"setenv kernel_comp_size {C.UBOOT_KERNEL_COMP_SIZE}", timeout=C.UBOOT_QUICK_TIMEOUT)
                device.send_command("saveenv", timeout=C.UBOOT_WRITE_TIMEOUT)
                device.send_command(_UBOOT_BOOTARGS_NOR_CMD, timeout=C.UBOOT_QUICK_TIMEOUT)
                return step(0, "U-Boot 'recovery' variable confirmed present", True)
    except Exception as e:
        verbose(f"  printenv recovery: {e}", "warning")

    # 'recovery' is missing — attempt NOR backup env restoration.
    # Primary env is typically at 0x300000; redundant slot is at primary+size.
    # Try both 128KB (0x20000) and 64KB (0x10000) size variants.
    print("  'recovery' not found — attempting restore from NOR backup env...")
    CANDIDATES = [
        ("0x320000", "0x20000"),   # primary=0x300000 size=128KB
        ("0x310000", "0x10000"),   # primary=0x300000 size=64KB
        ("0x3F0000", "0x10000"),   # alternative near-end-of-flash layout
        ("0x3E0000", "0x20000"),   # alternative layout
    ]

    for offset, size in CANDIDATES:
        verbose(f"  Trying backup env at NOR offset {offset} (size {size})...")
        try:
            device.send_command("sf probe 0", timeout=C.UBOOT_CMD_TIMEOUT)
            device.send_command(f"sf read {C.UBOOT_LOAD_ADDR} {offset} {size}", timeout=C.UBOOT_WRITE_TIMEOUT)
            # -c checks CRC32; fails cleanly if data is not a valid env block
            device.send_command(f"env import -c {C.UBOOT_LOAD_ADDR} {size}", timeout=C.UBOOT_CMD_TIMEOUT)
            check = device.send_command("printenv recovery", timeout=C.UBOOT_QUICK_TIMEOUT)
            if "recovery=" in check:
                verbose(f"  ✓ Restored 'recovery' from NOR backup at {offset}")
                verbose(f"  {check.strip()}")
                # Override bootcmd so OpenWRT boots after the flash.
                # The restored factory bootcmd (e.g. 'run opnsense') won't work.
                device.send_command(_UBOOT_SET_EMMC_CMD, timeout=C.UBOOT_CMD_TIMEOUT)
                device.send_command(_UBOOT_SET_BOOTCMD_CMD, timeout=C.UBOOT_CMD_TIMEOUT)
                device.send_command("saveenv", timeout=C.UBOOT_WRITE_TIMEOUT)
                device.send_command(_UBOOT_BOOTARGS_NOR_CMD, timeout=C.UBOOT_QUICK_TIMEOUT)
                return step(0, f"U-Boot 'recovery' restored from NOR backup ({offset})", True)
        except Exception as e:
            verbose(f"  Candidate {offset} failed: {e}", "debug")
            continue

    # ── Phase 2: scan NOR for the recovery kernel ───────────────────────
    # Scan 64 MB NOR in 1 MB steps looking for any bootable image magic.
    # Detection map (confirmed on this Mono Gateway dk / LS1046A):
    #   0x500000 = DTB or small FIT header (d0 0d fe ed, ~38 KB) — remember it
    #   0xa00000 = gzip compressed ARM64 Image.gz  (1f 8b)       — kernel here
    # When gzip is found, pair with any earlier DTB offset to build a
    # booti command.  Also try external-FIT (bootm) as fallback in the
    # same 'recovery' variable so U-Boot tries both automatically.
    # Only print lines when something non-trivial is found.
    print("  NOR backup env unavailable — scanning NOR for recovery kernel (~60-90s)...")

    FIT_MAGIC  = C.FIT_MAGIC_HEX
    UIMG_MAGIC = C.UIMAGE_MAGIC_HEX
    GZIP_MAGIC = C.GZIP_MAGIC_HEX
    LOAD_ADDR  = C.UBOOT_LOAD_ADDR
    DTB_ADDR   = C.UBOOT_DTB_ADDR    # separate RAM area for DTB when using booti
    LOAD_SZ    = C.NOR_KERNEL_LEN    # 32 MB — comfortably covers any recovery image

    KERNEL_OFFSETS = [f"0x{off:x}" for off in range(NOR_SCAN_START, NOR_SCAN_END, NOR_SCAN_STEP)]

    try:
        device.send_command("sf probe 0", timeout=C.UBOOT_CMD_TIMEOUT)
    except Exception as e:
        verbose(f"  sf probe failed: {e}", "error")
        verbose("  Manual fix: setenv recovery \"<sf load cmd>\" && saveenv", "error")
        return step(0, "U-Boot 'recovery' variable missing — sf probe failed", False)

    dtb_offset = None   # first small FDT found (potential DTB or ext-FIT header)

    with Spinner(f"Scanning NOR ({len(KERNEL_OFFSETS)} offsets)..."):
        for koffset in KERNEL_OFFSETS:
            try:
                device.send_command(f"sf read {LOAD_ADDR} {koffset} 0x100", timeout=C.UBOOT_WRITE_TIMEOUT)
                magic_out = device.send_command(f"md.b {LOAD_ADDR} 4", timeout=C.UBOOT_QUICK_TIMEOUT)

                # ── Large standalone FIT (kernel+initrd inline) ──────────────
                if FIT_MAGIC in magic_out:
                    size_out = device.send_command(f"md.b {_FIT_SIZE_FIELD_ADDR} 4", timeout=C.UBOOT_QUICK_TIMEOUT)
                    try:
                        fit_size = _md_be32(size_out)
                    except (ValueError, IndexError):
                        fit_size = 0

                    if fit_size >= C.MIN_KERNEL_FIT_BYTES:
                        verbose(f"  ✓ Kernel FIT at {koffset} ({fit_size / C.MIB:.1f} MB)")
                        if dtb_offset:
                            # FIT has no embedded FDT — pair with the DTB found earlier.
                            # fdt_high prevents U-Boot from relocating the FDT below the kernel.
                            device.send_command(f"setenv fdt_high {C.UBOOT_FDT_HIGH_NO_RELOC}", timeout=C.UBOOT_QUICK_TIMEOUT)
                            recovery_cmd = (
                                f"sf probe 0;"
                                f"sf read {DTB_ADDR} {dtb_offset} {C.NOR_DTB_LEN};"
                                f"sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                                f"bootm {LOAD_ADDR} - {DTB_ADDR}"
                            )
                        else:
                            recovery_cmd = (
                                f"sf probe 0;sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                                f"bootm {LOAD_ADDR}"
                            )
                        # fall through to save & return below
                    else:
                        # Small FDT: either raw DTB or external-FIT header —
                        # remember it in case we find the kernel (gzip) later.
                        verbose(f"  DTB/ext-FIT at {koffset} ({fit_size / C.KIB:.1f} KB) — noted")
                        if dtb_offset is None:
                            dtb_offset = koffset
                        continue

                # ── Legacy uImage ─────────────────────────────────────────────
                elif UIMG_MAGIC in magic_out:
                    verbose(f"  ✓ uImage at {koffset}")
                    recovery_cmd = (
                        f"sf probe 0;sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                        f"bootm {LOAD_ADDR}"
                    )

                # ── Gzip compressed ARM64 Image.gz ───────────────────────────
                elif GZIP_MAGIC in magic_out:
                    verbose(f"  ✓ Gzip kernel at {koffset}")
                    if dtb_offset:
                        recovery_cmd = (
                            f"sf probe 0;"
                            f"sf read {DTB_ADDR} {dtb_offset} {C.NOR_DTB_LEN};"
                            f"sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                            f"booti {LOAD_ADDR} - {DTB_ADDR}"
                        )
                        device.send_command(f"setenv kernel_comp_addr_r {C.UBOOT_KERNEL_COMP_ADDR}", timeout=C.UBOOT_QUICK_TIMEOUT)
                        device.send_command(f"setenv kernel_comp_size {C.UBOOT_KERNEL_COMP_SIZE}", timeout=C.UBOOT_QUICK_TIMEOUT)
                    else:
                        recovery_cmd = (
                            f"sf probe 0;sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                            f"booti {LOAD_ADDR}"
                        )
                        device.send_command(f"setenv kernel_comp_addr_r {C.UBOOT_KERNEL_COMP_ADDR}", timeout=C.UBOOT_QUICK_TIMEOUT)
                        device.send_command(f"setenv kernel_comp_size {C.UBOOT_KERNEL_COMP_SIZE}", timeout=C.UBOOT_QUICK_TIMEOUT)

                else:
                    continue  # no interesting magic at this offset

                device.send_command(f'setenv recovery "{recovery_cmd}"', timeout=C.UBOOT_CMD_TIMEOUT)
                device.send_command(_UBOOT_SET_EMMC_CMD, timeout=C.UBOOT_CMD_TIMEOUT)
                device.send_command(_UBOOT_SET_BOOTCMD_CMD, timeout=C.UBOOT_CMD_TIMEOUT)
                device.send_command("saveenv", timeout=C.UBOOT_WRITE_TIMEOUT)
                device.send_command(_UBOOT_BOOTARGS_NOR_CMD, timeout=C.UBOOT_QUICK_TIMEOUT)
                return step(
                    0,
                    f"U-Boot 'recovery' reconstructed — NOR {koffset}",
                    True
                )

            except Exception as e:
                verbose(f"  Probe at {koffset} failed: {e}", "debug")
                continue

    verbose("  ✗ No recovery kernel found anywhere in NOR flash", "error")
    verbose("  Manual fix: on a working device run 'printenv recovery' then", "error")
    verbose("  on this device: setenv recovery \"<value>\" && saveenv", "error")
    return step(0, "U-Boot 'recovery' variable missing — recovery image not found", False)


register_uboot_steps(OS, TRANSFER, _uboot_steps_openwrt_lan)


# NOTE: sysupgrade.bin.gz support (extracting the raw ext4 'root' member
# from an OpenWRT sysupgrade tarball) was dropped here — that format has
# no partition table and doesn't fit the whole-disk-with-GPT model this
# journey now uses. Only the raw `*-emmc.img[.gz]` release image is
# accepted. See module docstring "Revision note".


@register_step(os=[OS], transfer=[TRANSFER], requires=["network_up"], produces=["http_server_up"], label="Start HTTP server")
def step_http_server_start(ctx: StepContext) -> bool:
    verbose("=" * C.DIVIDER_WIDTH); verbose("Start HTTP server"); verbose("=" * C.DIVIDER_WIDTH)
    return _common._step_http_server_start(ctx)


@register_step(os=[OS], transfer=[TRANSFER], requires=["network_up", "http_server_up"], produces=["firmware_ready"], label="Verify firmware reachable")
def step_firmware_reachable(ctx: StepContext) -> bool:
    verbose("=" * C.DIVIDER_WIDTH); verbose("Verify firmware reachable"); verbose("=" * C.DIVIDER_WIDTH)
    return _common._step_firmware_reachable(ctx)


# No fdisk/partition step: the doc's own two dd passes recreate the
# image's embedded GPT (first 4KiB) on every flash, so the eMMC never
# needs a separate partitioning step and is never left in a state that
# depends on whatever partition table an earlier OS (OPNsense/Armbian,
# which both flash-whole-disk-from-0) may have left behind.

# Local staging path for the decompressed image on-device. OpenWRT
# images are ~100MB (usb_utils.py docstring) against recovery Linux's
# confirmed ~3.8GB tmpfs rootfs (see flash_orchestrator.py's
# FLASH_SIZE_CAP comment, ≈3.0GB safe cap) — comfortably below it, so
# no separate size check is done here the way the >3GB OPNsense/Armbian
# path does.
_STAGED_IMG = "/tmp/mono_imager_openwrt.img"


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash OpenWRT image (dd)")
def step_flash_openwrt(ctx: StepContext) -> bool:
    """
    Downloads the image to a local file on-device, then runs both dd
    passes against that plain local file — no curl|dd or gunzip|dd
    pipes into dd at all.

    This mirrors Mono's own vendor-documented procedure (wget once,
    dd twice from the local file) rather than streaming, per a
    real-hardware finding already recorded elsewhere in this codebase
    (flash_orchestrator.py, FLASH_SIZE_CAP comment): piping curl
    directly into dd produces all-partial-record reads ("0+N records",
    zero full records), because a pipe never delivers clean
    fixed-size blocks regardless of bs. For the bulk pass that was
    only confusing (verified working via mounted-filesystem check on
    real hardware), but for this journey's exact-4KiB partition-table
    pass it would be a correctness bug, not just cosmetic: BusyBox dd
    counts each read() as one record toward `count=`, so a
    short/partial read could let `count=8` finish with fewer than
    4096 bytes actually written — a truncated, invalid GPT.
    """
    verbose("=" * C.DIVIDER_WIDTH); verbose("Flash OpenWRT image"); verbose("=" * C.DIVIDER_WIDTH)
    d = ctx.device
    source = ctx.get("firmware_source")
    is_gz = str(ctx.firmware_path).lower().endswith(".gz")

    staged_gz = _STAGED_IMG + ".gz"
    download_step = (
        f"curl -sk -o {staged_gz if is_gz else _STAGED_IMG} {source} "
        f"> /tmp/mono_imager_dl.log 2>&1; "
    )
    decompress_step = f"gunzip -f {staged_gz}; " if is_gz else ""

    script = (
        f"rm -f {_STAGED_IMG} {staged_gz}; "
        f"{download_step}"
        f"{decompress_step}"
        f"{{ "
        f"dd if={_STAGED_IMG} of={ctx.flash_target} {PT_DD_ARGS}; "
        f"dd if={_STAGED_IMG} of={ctx.flash_target} {BULK_DD_ARGS}; "
        f"}} > /tmp/mono_imager_flash.log 2>&1; "
        f"sync; "
        f"rm -f {_STAGED_IMG}; "
        f"curl -sk -X POST --data-binary @/tmp/mono_imager_flash.log "
        f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=flash\" >/dev/null 2>&1"
    )

    try:
        d.launch_script(script, marker="flash")
    except Exception as e:
        return step(0, "OpenWRT flash launched", False, str(e))

    console_logger.info("Flashing OpenWRT — downloading, then writing both dd passes...")
    raw, err = with_spinner(wait_for_report, "flash", timeout=C.FLASH_TIMEOUT, message="Flashing OpenWRT")
    if err or raw is None:
        return step(0, "OpenWRT flash (dd)", False,
                    str(err) if err else f"no report-back from device in {C.FLASH_TIMEOUT}s")

    # Two dd calls -> two "records out" lines in the combined log.
    matches = re.findall(r"(\d+)\+(\d+)\s+records out", raw)
    has_error = "error" in raw.lower() or "failed" in raw.lower() or "not in" in raw.lower() \
        or "no space" in raw.lower()

    if len(matches) < 2:
        return step(0, "OpenWRT flash (dd, both passes)", False,
                    f"expected 2 'records out' lines, got {len(matches)}: {raw[-C.LOG_TAIL_LONG:]}")

    pt_full, pt_partial = int(matches[0][0]), int(matches[0][1])
    pt_bytes = pt_full * C.SECTOR_BYTES + pt_partial
    bulk_full, bulk_partial = int(matches[1][0]), int(matches[1][1])
    bulk_bytes = bulk_full * C.MIB + bulk_partial

    step(0, f"OpenWRT partition table (dd {PT_DD_ARGS})", pt_bytes > 0,
         raw[-C.LOG_TAIL:] if pt_bytes == 0 else "")
    ok = pt_bytes > 0 and bulk_bytes > 0 and not has_error
    step(0, f"OpenWRT image (dd {BULK_DD_ARGS}, {bulk_bytes // C.MIB} MB)",
         ok, raw[-C.LOG_TAIL:] if not ok else "")
    return ok


@register_step(
    os=[OS], transfer=["lan", "usb"],
    requires=["os_flashed", "network_up"], produces=["firmware_updated"],
    label="Firmware update (eMMC bootloader)"
)
def step_firmware_update(ctx: StepContext) -> bool:
    verbose("=" * C.DIVIDER_WIDTH); verbose("Firmware update"); verbose("=" * C.DIVIDER_WIDTH)
    d = ctx.device
    try:
        response, _fw_err = with_spinner(
            d.send_command,
            "printf 'yes\\n' | firmware update 2>&1; echo RC=$?",
            timeout=FIRMWARE_UPDATE_TIMEOUT,
            message="Updating eMMC bootloader (firmware update)..."
        )
        if _fw_err:
            raise _fw_err
        ok = "RC=0" in response
        return step(0, "Firmware update (eMMC bootloader)", ok,
                   response[-C.LOG_TAIL:] if not ok else "")
    except Exception as e:
        return step(0, "Firmware update", False, str(e))


# No "prepare eMMC boot config" step anymore — the official procedure's
# 'emmc' bootcmd (see _UBOOT_SET_EMMC_CMD above) ext4loads /boot/kernel.itb
# directly, no /boot/extlinux/extlinux.conf file required on the eMMC
# partition. That file-writing step (mount + printf + umount) is gone.
#
# No automated "flip DIP + verify boot" step either anymore — see the
# module docstring. tui.py's own end-of-flash screen (menu_done())
# already prints the "flip DIP to eMMC, power-cycle" instruction
# unconditionally whenever flash_success is True, so this journey's
# last step is simply the firmware update above.
