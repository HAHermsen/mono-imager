"""
mono-imager journey: OpenWRT via LAN

Steps:
  1. Network setup (eth0)
  2. Start HTTP server
  3. Verify firmware reachable
  4. Flash OpenWRT image (dd bs=4096)
  5. Reboot device

U-Boot pre-step:
  Ensures the 'recovery' U-Boot env variable is defined before
  boot_recovery() calls 'run recovery'.

  On factory-fresh devices, 'recovery' is already in NOR env — nothing to do.

  If 'recovery' is missing (e.g., 'env default -a' was run in a previous
  failed attempt), tries to restore it from the redundant/backup env copy
  that U-Boot stores at a second NOR offset.  The recovery KERNEL is always
  safe in NOR flash — only the pointer variable in NOR env can go missing.

  OpenWRT is written to /dev/mmcblk0p1 (partition only).  NOR flash is
  never touched by the flash step.

Author:  H.A. Hermsen
License: MIT
"""

__version__ = "0.9.5"
__author__  = "H.A. Hermsen"

import gzip
import io
import logging
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger, start_http_server, wait_for_report

logger = logging.getLogger(__name__)

OS       = "OpenWRT"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the OpenWRT .img or .bin.gz file:"
TRANSFER = "lan"


def _update_emmc_env(device) -> bool:
    """
    Scan the eMMC raw area for the Armbian U-Boot environment block and patch
    bootcmd/bootargs so OpenWRT boots automatically when DIP switch is LEFT.

    Tries several (sector_offset, sector_count, env_size_bytes) candidates that
    cover the most common LS1046A Armbian U-Boot env layouts.  For each:
      1. mmc read → scratch RAM
      2. env import -c  (CRC check — fails cleanly on wrong offset/size)
      3. setenv bootcmd / bootargs
      4. env export -c -s → scratch RAM
      5. mmc write → eMMC

    Caller's in-memory NOR env vars are saved before and restored after so a
    subsequent saveenv still writes the correct NOR environment.
    Returns True if the eMMC env was successfully patched.
    """
    EMMC_BOOTCMD  = (
        "ext4load mmc 0:1 0x82000000 /boot/kernel.itb; bootm 0x82000000#config-1"
    )
    EMMC_BOOTARGS = "root=/dev/mmcblk0p1 rw rootwait earlycon"
    SCRATCH       = "0x84000000"

    verbose("  Patching eMMC U-Boot env for OpenWRT boot (DIP=LEFT)...")

    # Save key NOR env vars that env import -c will overwrite in memory.
    saved = {}
    for var in ("recovery", "kernel_comp_addr_r", "kernel_comp_size",
                "fdt_high", "bootcmd"):
        try:
            out = device.send_command(f"printenv {var}", timeout=5)
            if f"{var}=" in out:
                val = (out.split(f"{var}=", 1)[1]
                       .split("\r")[0].split("\n")[0].strip())
                saved[var] = val
        except Exception:
            pass

    def _restore():
        for k, v in saved.items():
            try:
                device.send_command(f'setenv {k} "{v}"', timeout=5)
            except Exception:
                pass
        # Remove eMMC-specific bootargs so it does not pollute NOR env.
        try:
            device.send_command("setenv bootargs", timeout=5)
        except Exception:
            pass

    # (mmc_sector_offset, sector_count, env_size_bytes)
    CANDIDATES = [
        (0x2000, 0x200, 0x40000),  # 4 MB, 256 KB  — most common LS1046A layout
        (0x800,  0x100, 0x20000),  # 1 MB, 128 KB
        (0x800,  0x200, 0x40000),  # 1 MB, 256 KB
        (0x1800, 0x200, 0x40000),  # 3 MB, 256 KB
        (0x2000, 0x100, 0x20000),  # 4 MB, 128 KB
        (0x600,  0x100, 0x20000),  # 768 KB, 128 KB
    ]

    updated = False
    for sector, count, env_size in CANDIDATES:
        imported = False
        try:
            device.send_command("mmc dev 0", timeout=5)
            rd = device.send_command(
                f"mmc read {SCRATCH} {hex(sector)} {hex(count)}", timeout=15
            )
            if "error" in rd.lower():
                continue

            # env import -c checks CRC32 and does NOT modify env on failure.
            imp = device.send_command(
                f"env import -c {SCRATCH} {hex(env_size)}", timeout=10
            )
            if "bad crc" in imp.lower() or "## error" in imp.lower():
                continue

            imported = True

            # Sanity-check: a valid env must expose bootcmd after import.
            chk = device.send_command("printenv bootcmd", timeout=5)
            if "bootcmd=" not in chk:
                continue

            verbose(f"    Found eMMC env at sector {hex(sector)} "
                    f"({hex(env_size)} B)")

            device.send_command(f'setenv bootcmd "{EMMC_BOOTCMD}"', timeout=5)
            device.send_command(f'setenv bootargs "{EMMC_BOOTARGS}"', timeout=5)

            exp = device.send_command(
                f"env export -c -s {hex(env_size)} {SCRATCH}", timeout=10
            )
            if "error" in exp.lower():
                verbose("    env export failed — skipping", "warning")
                continue

            wr = device.send_command(
                f"mmc write {SCRATCH} {hex(sector)} {hex(count)}", timeout=15
            )
            if "error" not in wr.lower() and "failed" not in wr.lower():
                verbose("    ✓ eMMC env patched")
                updated = True
            else:
                verbose(f"    mmc write failed: {wr.strip()}", "warning")

            break

        except Exception as e:
            verbose(f"    sector={hex(sector)}/{hex(env_size)}: {e}", "debug")
            continue
        finally:
            if imported:
                _restore()

    if not updated:
        verbose("  ⚠ eMMC env auto-patch failed — manual fix after DIP→LEFT:",
                "warning")
        verbose(f'    setenv bootcmd "{EMMC_BOOTCMD}"', "warning")
        verbose(f'    setenv bootargs "{EMMC_BOOTARGS}"', "warning")
        verbose("    saveenv", "warning")

    return updated


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
    verbose("Checking U-Boot 'recovery' variable...")

    # Fast path: recovery already present (normal case on any clean device).
    # If 'recovery' uses 'sf read', also validate the kernel size at that
    # offset so we don't keep a leftover pointer to a firmware FIT blob.
    try:
        out = device.send_command("printenv recovery", timeout=5)
        verbose(f"  {out.strip()}")
        if "recovery=" in out:
            recovery_confirmed = False
            if "sf read" in out:
                # Extract NOR offset (2nd arg of "sf read <ram> <noroff> <size>")
                parts = out.split("sf read")[-1].strip().split()
                koffset = parts[1] if len(parts) >= 2 else ""
                if koffset:
                    try:
                        device.send_command("sf probe 0", timeout=10)
                        device.send_command(f"sf read 0x82000000 {koffset} 0x100", timeout=15)
                        magic_out = device.send_command("md.b 0x82000000 4", timeout=5)
                        if "d0 0d fe ed" in magic_out:
                            size_out = device.send_command("md.b 0x82000004 4", timeout=5)
                            try:
                                hex_b = size_out.split(":")[-1].strip().split()[:4]
                                fit_size = (int(hex_b[0], 16) << 24 | int(hex_b[1], 16) << 16 |
                                            int(hex_b[2], 16) << 8  | int(hex_b[3], 16))
                                if fit_size < 5 * 1024 * 1024:
                                    verbose(f"  ✗ 'recovery' points to a non-kernel FIT "
                                            f"({fit_size/1024/1024:.1f} MB at {koffset}) — clearing...")
                                    device.send_command("setenv recovery", timeout=5)
                                    # fall through to NOR scan
                                else:
                                    verbose(f"  ✓ 'recovery' confirmed — kernel FIT at {koffset} "
                                            f"({fit_size/1024/1024:.1f} MB)")
                                    recovery_confirmed = True
                            except (ValueError, IndexError):
                                verbose("  ✓ 'recovery' is defined (validation skipped)")
                                recovery_confirmed = True
                        elif "1f 8b" in magic_out:
                            # Gzip kernel (Image.gz / AArch64 booti path).
                            # Also verify kernel_comp_addr_r is in NOR env —
                            # it gets wiped by 'env default -a' and booti
                            # silently fails without it.
                            kc = device.send_command("printenv kernel_comp_addr_r", timeout=5)
                            if "kernel_comp_addr_r=" in kc:
                                verbose(f"  ✓ 'recovery' confirmed — gzip kernel at {koffset}")
                                recovery_confirmed = True
                            else:
                                verbose("  ✗ kernel_comp_addr_r missing — rebuilding recovery env...")
                                device.send_command("setenv recovery", timeout=5)
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
                _update_emmc_env(device)
                return step(0, "U-Boot 'recovery' variable confirmed present", True)
    except Exception as e:
        verbose(f"  printenv recovery: {e}", "warning")

    # 'recovery' is missing — attempt NOR backup env restoration.
    # Primary env is typically at 0x300000; redundant slot is at primary+size.
    # Try both 128KB (0x20000) and 64KB (0x10000) size variants.
    verbose("  'recovery' not found — attempting restore from NOR backup env...")
    CANDIDATES = [
        ("0x320000", "0x20000"),   # primary=0x300000 size=128KB
        ("0x310000", "0x10000"),   # primary=0x300000 size=64KB
        ("0x3F0000", "0x10000"),   # alternative near-end-of-flash layout
        ("0x3E0000", "0x20000"),   # alternative layout
    ]

    for offset, size in CANDIDATES:
        verbose(f"  Trying backup env at NOR offset {offset} (size {size})...")
        try:
            device.send_command("sf probe 0", timeout=10)
            device.send_command(f"sf read 0x82000000 {offset} {size}", timeout=15)
            # -c checks CRC32; fails cleanly if data is not a valid env block
            device.send_command(f"env import -c 0x82000000 {size}", timeout=10)
            check = device.send_command("printenv recovery", timeout=5)
            if "recovery=" in check:
                verbose(f"  ✓ Restored 'recovery' from NOR backup at {offset}")
                verbose(f"  {check.strip()}")
                # Override bootcmd to extlinux so OpenWRT boots after the flash.
                # The restored factory bootcmd (e.g. 'run opnsense') won't work.
                device.send_command(
                    'setenv bootcmd "sysboot mmc 0:1 any 0x80000000 /boot/extlinux/extlinux.conf"',
                    timeout=10
                )
                _update_emmc_env(device)
                device.send_command("saveenv", timeout=15)
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
    verbose("  NOR backup env unavailable — scanning NOR for recovery kernel...")

    FIT_MAGIC  = "d0 0d fe ed"
    UIMG_MAGIC = "27 05 19 56"
    GZIP_MAGIC = "1f 8b"
    LOAD_ADDR  = "0x82000000"
    DTB_ADDR   = "0x90000000"   # separate RAM area for DTB when using booti
    LOAD_SZ    = "0x2000000"    # 32 MB — comfortably covers any recovery image

    KERNEL_OFFSETS = [f"0x{off:x}" for off in range(0x400000, 0x3C00000, 0x100000)]
    verbose(f"  Scanning {len(KERNEL_OFFSETS)} 1MB offsets across 4–60 MB of NOR...")

    try:
        device.send_command("sf probe 0", timeout=10)
    except Exception as e:
        verbose(f"  sf probe failed: {e}", "error")
        verbose("  Manual fix: setenv recovery \"<sf load cmd>\" && saveenv", "error")
        return step(0, "U-Boot 'recovery' variable missing — sf probe failed", False)

    dtb_offset = None   # first small FDT found (potential DTB or ext-FIT header)

    for koffset in KERNEL_OFFSETS:
        try:
            device.send_command(f"sf read {LOAD_ADDR} {koffset} 0x100", timeout=15)
            magic_out = device.send_command(f"md.b {LOAD_ADDR} 4", timeout=5)

            # ── Large standalone FIT (kernel+initrd inline) ──────────────
            if FIT_MAGIC in magic_out:
                size_out = device.send_command("md.b 0x82000004 4", timeout=5)
                try:
                    hex_b = size_out.split(":")[-1].strip().split()[:4]
                    fit_size = (int(hex_b[0], 16) << 24 | int(hex_b[1], 16) << 16 |
                                int(hex_b[2], 16) << 8  | int(hex_b[3], 16))
                except (ValueError, IndexError):
                    fit_size = 0

                if fit_size >= 5 * 1024 * 1024:
                    verbose(f"  ✓ Kernel FIT at {koffset} ({fit_size/1024/1024:.1f} MB)")
                    recovery_cmd = (
                        f"sf probe 0;sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                        f"bootm {LOAD_ADDR}"
                    )
                    # fall through to save & return below
                else:
                    # Small FDT: either raw DTB or external-FIT header —
                    # remember it in case we find the kernel (gzip) later.
                    verbose(f"  DTB/ext-FIT at {koffset} ({fit_size/1024:.1f} KB) — noted")
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
                    # The LS1046A uses booti (AArch64 Image.gz) + separate DTB.
                    # Also embed an external-FIT attempt first so U-Boot tries
                    # AArch64 booti path: load DTB + gzip kernel separately.
                    # kernel_comp_addr_r / kernel_comp_size tell U-Boot where
                    # to decompress Image.gz; wiped by env default -a so we
                    # set them permanently alongside the recovery command.
                    recovery_cmd = (
                        f"sf probe 0;"
                        f"sf read {DTB_ADDR} {dtb_offset} 0x20000;"
                        f"sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                        f"booti {LOAD_ADDR} - {DTB_ADDR}"
                    )
                    # Save decompression workspace vars to NOR so they
                    # survive future reboots.
                    device.send_command("setenv kernel_comp_addr_r 0xa0000000", timeout=5)
                    device.send_command("setenv kernel_comp_size 0x10000000", timeout=5)
                    device.send_command("setenv fdt_high 0xffffffffffffffff", timeout=5)
                else:
                    recovery_cmd = (
                        f"sf probe 0;sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                        f"booti {LOAD_ADDR}"
                    )
                    device.send_command("setenv kernel_comp_addr_r 0xa0000000", timeout=5)
                    device.send_command("setenv kernel_comp_size 0x10000000", timeout=5)

            else:
                continue  # no interesting magic at this offset

            device.send_command(f'setenv recovery "{recovery_cmd}"', timeout=10)
            device.send_command(
                'setenv bootcmd "sysboot mmc 0:1 any 0x80000000 /boot/extlinux/extlinux.conf"',
                timeout=10
            )
            _update_emmc_env(device)
            device.send_command("saveenv", timeout=15)
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


def _extract_sysupgrade_rootfs(firmware_path: Path) -> tuple[Path, bool]:
    """
    Detect and extract the raw ext4 rootfs from an OpenWRT sysupgrade.bin.gz.

    OpenWRT sysupgrade images are NOT raw partition images — they are a
    gzip-compressed tar archive (tar.gz) whose 'root' member is the actual
    ext4 image.  Writing the .bin.gz directly through gunzip|dd puts a
    tar.gz stream on the partition instead of an ext4 filesystem, which is
    why U-Boot ext4load says "Can't set block device" afterwards.

    Returns (extracted_temp_path, True) when the input is a sysupgrade tar.
    Returns (original_path, False) for raw images (.img, .img.gz, etc.) so
    the caller can still gunzip+dd those in the normal way.
    """
    name = firmware_path.name.lower()
    if not name.endswith(".bin.gz") and not name.endswith(".bin"):
        return firmware_path, False

    try:
        with gzip.open(firmware_path, "rb") as outer:
            inner_bytes = outer.read()
    except (OSError, gzip.BadGzipFile):
        return firmware_path, False

    def _find_root_in_tar(tf: tarfile.TarFile) -> Path | None:
        for member in tf.getmembers():
            if member.name == "root" or member.name.endswith("/root"):
                f = tf.extractfile(member)
                if f is None:
                    continue
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".ext4", delete=False,
                    dir=firmware_path.parent,
                )
                shutil.copyfileobj(f, tmp)
                tmp.close()
                return Path(tmp.name)
        return None

    # The inner content is a tar.gz (sysupgrade-tar uses tar -czf)
    try:
        with gzip.open(io.BytesIO(inner_bytes)) as inner_gz:
            with tarfile.open(fileobj=inner_gz) as tf:
                result = _find_root_in_tar(tf)
                if result:
                    return result, True
    except (OSError, gzip.BadGzipFile, tarfile.TarError):
        pass

    # Fallback: inner is a plain tar (unusual but possible)
    try:
        with tarfile.open(fileobj=io.BytesIO(inner_bytes)) as tf:
            result = _find_root_in_tar(tf)
            if result:
                return result, True
    except tarfile.TarError:
        pass

    return firmware_path, False


@register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["network_up"], label="Network setup (eth0)")
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
    serve_path, extracted = _extract_sysupgrade_rootfs(Path(ctx.firmware_path))
    if extracted:
        verbose(f"  Sysupgrade format detected — extracted rootfs to {serve_path.name}")
        ctx.set("serve_raw_ext4", True)   # flash step must NOT gunzip
        ctx.set("extracted_rootfs", str(serve_path))
    else:
        serve_path = Path(ctx.firmware_path)
        ctx.set("serve_raw_ext4", False)
    try:
        server = start_http_server(ctx.host_ip, ctx.http_port, serve_path)
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


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"], produces=["os_flashed"], label="Flash OpenWRT image (dd)")
def step_flash_openwrt(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Flash OpenWRT image"); verbose("=" * 60)
    d = ctx.device
    source = ctx.get("firmware_source")

    # serve_raw_ext4=True means the HTTP server is already serving the raw
    # ext4 partition image (extracted from the sysupgrade tar on the host).
    # In that case we must NOT gunzip on the device — the file is already
    # uncompressed.  For plain .img or .img.gz the normal logic applies.
    serve_raw = ctx.get("serve_raw_ext4", False)

    if serve_raw:
        # Raw ext4 served directly — stream into dd, no gunzip
        flash_script = (
            f"{{ curl -s {source} | "
            f"dd of={ctx.flash_target} bs=4096; }} "
            f"> /tmp/mono_imager_step07_flash.log 2>&1; "
            f"sync; "
            f"curl -s -X POST --data-binary @/tmp/mono_imager_step07_flash.log "
            f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=07\" "
            f">/dev/null 2>&1"
        )
        console_logger.info("Flashing OpenWRT (raw ext4) — this takes several minutes...")
    elif str(ctx.firmware_path).lower().endswith(".gz"):
        flash_script = (
            f"{{ curl -s {source} | gunzip -c | "
            f"dd of={ctx.flash_target} bs=4096; }} "
            f"> /tmp/mono_imager_step07_flash.log 2>&1; "
            f"sync; "
            f"curl -s -X POST --data-binary @/tmp/mono_imager_step07_flash.log "
            f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=07\" "
            f">/dev/null 2>&1"
        )
        console_logger.info("Flashing OpenWRT (gz streaming) — this takes several minutes...")
    else:
        flash_script = (
            f"curl -s -o /tmp/mono_imager_firmware.img {source} "
            f"> /tmp/mono_imager_step07_flash.log 2>&1; "
            f"dd if=/tmp/mono_imager_firmware.img of={ctx.flash_target} bs=4096 "
            f">> /tmp/mono_imager_step07_flash.log 2>&1; "
            f"sync; "
            f"rm -f /tmp/mono_imager_firmware.img; "
            f"curl -s -X POST --data-binary @/tmp/mono_imager_step07_flash.log "
            f"\"http://{ctx.host_ip}:{ctx.http_port}/report?step=07\" "
            f">/dev/null 2>&1"
        )
        console_logger.info("Flashing OpenWRT — this takes several minutes...")

    try:
        d.launch_script(flash_script, marker="step07_flash")
    except Exception as e:
        return step(0, "OpenWRT flash launched", False, str(e))

    raw, err = with_spinner(wait_for_report, "07", timeout=600.0, message="Flashing OpenWRT")
    if err or raw is None:
        return step(0, "OpenWRT flash (dd)", False,
                    str(err) if err else "no report-back from device in 600s")

    # Count actual records written — "0+0 records out" still contains
    # "records out" and used to pass the old check despite writing nothing.
    m = re.search(r"(\d+)\+(\d+)\s+records out", raw)
    if m:
        full_records, partial_records = int(m.group(1)), int(m.group(2))
        bytes_written = full_records * 4096 + partial_records  # approx
    else:
        full_records = partial_records = bytes_written = 0

    has_real_data = bytes_written > 0
    has_error = "error" in raw.lower() or "failed" in raw.lower() or "not in" in raw.lower()

    step(0, "OpenWRT flash executed", True)
    step(0, f"dd wrote data ({bytes_written // 1024} KB)", has_real_data,
         raw[-200:] if not has_real_data else "")
    step(0, "No errors", not has_error, raw[-200:] if has_error else "")
    return has_real_data and not has_error


@register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"], produces=["rebooted"], label="Reboot device")
def step_reboot(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Reboot device"); verbose("=" * 60)
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
        console_logger.info("Rebooting device...")
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent", True)
