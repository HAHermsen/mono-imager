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
License: GPLv3
"""

import gzip
import io
import logging
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

from mono_imager.step_registry import register_step, register_uboot_steps, StepContext
from mono_imager.spinner import with_spinner, Spinner
from mono_imager.flash_orchestrator import step, verbose, console_logger, start_http_server, wait_for_report

logger = logging.getLogger(__name__)

OS       = "OpenWRT"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the OpenWRT .img or .bin.gz file:"
TRANSFER = "lan"



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
                device.send_command(
                    'setenv bootcmd "sysboot mmc 0:1 any 0x80000000 /boot/extlinux/extlinux.conf"',
                    timeout=10
                )
                # Re-persist decompression vars — something (firmware update?) can wipe them
                # between runs, causing the next run to fall through to the slow NOR scan.
                device.send_command("setenv kernel_comp_addr_r 0xa0000000", timeout=5)
                device.send_command("setenv kernel_comp_size 0x10000000", timeout=5)
                device.send_command("saveenv", timeout=15)
                device.send_command('setenv bootargs "${bootargs} boot_medium=qspi"', timeout=5)
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
                device.send_command("saveenv", timeout=15)
                device.send_command('setenv bootargs "${bootargs} boot_medium=qspi"', timeout=5)
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
    print(f"  NOR backup env unavailable — scanning NOR for recovery kernel (~60-90s)...")

    FIT_MAGIC  = "d0 0d fe ed"
    UIMG_MAGIC = "27 05 19 56"
    GZIP_MAGIC = "1f 8b"
    LOAD_ADDR  = "0x82000000"
    DTB_ADDR   = "0x90000000"   # separate RAM area for DTB when using booti
    LOAD_SZ    = "0x2000000"    # 32 MB — comfortably covers any recovery image

    KERNEL_OFFSETS = [f"0x{off:x}" for off in range(0x400000, 0x3C00000, 0x100000)]

    try:
        device.send_command("sf probe 0", timeout=10)
    except Exception as e:
        verbose(f"  sf probe failed: {e}", "error")
        verbose("  Manual fix: setenv recovery \"<sf load cmd>\" && saveenv", "error")
        return step(0, "U-Boot 'recovery' variable missing — sf probe failed", False)

    dtb_offset = None   # first small FDT found (potential DTB or ext-FIT header)

    with Spinner(f"Scanning NOR ({len(KERNEL_OFFSETS)} offsets)..."):
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
                        if dtb_offset:
                            # FIT has no embedded FDT — pair with the DTB found earlier.
                            # fdt_high prevents U-Boot from relocating the FDT below the kernel.
                            device.send_command("setenv fdt_high 0xffffffffffffffff", timeout=5)
                            recovery_cmd = (
                                f"sf probe 0;"
                                f"sf read {DTB_ADDR} {dtb_offset} 0x20000;"
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
                        recovery_cmd = (
                            f"sf probe 0;"
                            f"sf read {DTB_ADDR} {dtb_offset} 0x20000;"
                            f"sf read {LOAD_ADDR} {koffset} {LOAD_SZ};"
                            f"booti {LOAD_ADDR} - {DTB_ADDR}"
                        )
                        device.send_command("setenv kernel_comp_addr_r 0xa0000000", timeout=5)
                        device.send_command("setenv kernel_comp_size 0x10000000", timeout=5)
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
                device.send_command("saveenv", timeout=15)
                device.send_command('setenv bootargs "${bootargs} boot_medium=qspi"', timeout=5)
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

    def _find_root_in_tar(tf: tarfile.TarFile) -> Path | None:
        for member in tf.getmembers():
            if member.name == "root" or member.name.endswith("/root"):
                f = tf.extractfile(member)
                if f is None:
                    continue
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".ext4", delete=False,
                    dir=None,  # system temp dir, not alongside firmware file
                )
                tmp_path = Path(tmp.name)
                try:
                    try:
                        # Try gzip decompression first (LS1046A sysupgrade format:
                        # gzip(tar(kernel, root_gz)) — ext4 is gzip-compressed inside tar)
                        with gzip.open(f) as gz:
                            shutil.copyfileobj(gz, tmp)
                    except (gzip.BadGzipFile, OSError):
                        f.seek(0)
                        shutil.copyfileobj(f, tmp)
                    # Validate ext4 superblock magic (offset 0x438, little-endian 0xEF53)
                    tmp.seek(0x438)
                    magic = tmp.read(2)
                    if len(magic) < 2 or magic != b'\x53\xef':
                        logger.warning(f"Extracted 'root' member failed ext4 magic check (got {magic!r})")
                        tmp.close()
                        tmp_path.unlink(missing_ok=True)
                        return None
                    tmp.close()
                    return tmp_path
                except Exception:
                    tmp.close()
                    tmp_path.unlink(missing_ok=True)
                    raise
        return None

    # Stream outer.gz -> inner.gz -> tar without loading full image into RAM
    try:
        with gzip.open(firmware_path, "rb") as outer:
            try:
                with gzip.open(outer) as inner_gz:
                    with tarfile.open(fileobj=inner_gz) as tf:
                        result = _find_root_in_tar(tf)
                        if result:
                            return result, True
            except (OSError, gzip.BadGzipFile, tarfile.TarError):
                pass
    except (OSError, gzip.BadGzipFile):
        return firmware_path, False

    # Fallback: outer.gz -> plain tar (unusual but possible)
    try:
        with gzip.open(firmware_path, "rb") as outer:
            with tarfile.open(fileobj=outer) as tf:
                result = _find_root_in_tar(tf)
                if result:
                    return result, True
    except (OSError, gzip.BadGzipFile, tarfile.TarError):
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
    check, _rep_err = with_spinner(wait_for_report, "06", timeout=20.0, message="Verifying firmware reachable...")
    ok = check is not None and "200" in check
    return step(0, f"Firmware reachable ({url})", ok, f"HTTP {check}" if not ok else "")


@register_step(
    os=[OS], transfer=["lan", "usb"],
    requires=[], produces=["emmc_partitioned"],
    label="Partition eMMC (fdisk)"
)
def step_partition_emmc(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Partition eMMC"); verbose("=" * 60)
    d = ctx.device
    base = re.sub(r'p\d+$', '', ctx.flash_target)  # /dev/mmcblk0p1 → /dev/mmcblk0
    try:
        response, _fdisk_err = with_spinner(
            d.send_command,
            f"printf 'o\\nn\\np\\n\\n65536\\n\\nw\\n' | fdisk {base} 2>&1; echo RC=$?",
            timeout=30,
            message="Partitioning eMMC..."
        )
        if _fdisk_err:
            raise _fdisk_err
        ok = "RC=0" in response
        if ok:
            d.send_command(
                f"partprobe {base} 2>/dev/null || blockdev --rereadpt {base} 2>/dev/null || true",
                timeout=10
            )
        return step(0, f"eMMC partitioned ({ctx.flash_target}, first sector 65536)", ok,
                   response[-200:] if not ok else "")
    except Exception as e:
        return step(0, "eMMC partition table", False, str(e))


@register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready", "emmc_partitioned"], produces=["os_flashed"], label="Flash OpenWRT image (dd)")
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
    finally:
        if ctx.get("serve_raw_ext4"):
            extracted = ctx.get("extracted_rootfs")
            if extracted:
                Path(extracted).unlink(missing_ok=True)


@register_step(
    os=[OS], transfer=["lan", "usb"],
    requires=["os_flashed"], produces=["firmware_updated"],
    label="Firmware update (eMMC bootloader)"
)
def step_firmware_update(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Firmware update"); verbose("=" * 60)
    d = ctx.device
    try:
        if ctx.transfer == "lan" and ctx.host_ip:
            d.send_command(
                f"ip route add default via {ctx.host_ip} 2>/dev/null || true",
                timeout=10
            )
        else:
            d.send_command(
                "ip link set eth0 up 2>/dev/null; udhcpc -i eth0 -n -q 2>/dev/null || true",
                timeout=25
            )
        response, _fw_err = with_spinner(
            d.send_command,
            "printf 'yes\\n' | firmware update 2>&1; echo RC=$?",
            timeout=120,
            message="Updating eMMC bootloader (firmware update)..."
        )
        if _fw_err:
            raise _fw_err
        ok = "RC=0" in response
        return step(0, "Firmware update (eMMC bootloader)", ok,
                   response[-200:] if not ok else "")
    except Exception as e:
        return step(0, "Firmware update", False, str(e))


@register_step(
    os=[OS], transfer=["lan", "usb"],
    requires=["firmware_updated"], produces=["boot_configured"],
    label="Prepare eMMC boot config"
)
def step_prepare_emmc_boot(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Prepare eMMC boot config"); verbose("=" * 60)
    d = ctx.device

    # The Armbian U-Boot on eMMC uses sysboot to load /boot/extlinux/extlinux.conf
    # from partition 1.  OpenWRT does not ship this file, so we create it after
    # flashing.  The FIT image (kernel.itb) is booted via the kernel directive;
    # sysboot detects the FIT format and calls bootm internally.
    extlinux_content = (
        "timeout 1\\n"
        "default openwrt\\n"
        "\\n"
        "label openwrt\\n"
        "  kernel /boot/kernel.itb\\n"
        "  append root=/dev/mmcblk0p1 rw rootwait earlycon\\n"
    )
    MNT = "/mnt/mono_owrt"
    try:
        d.send_command(f"mkdir -p {MNT}", timeout=5)
        mount_resp, _mnt_err = with_spinner(
            d.send_command,
            f"mount -t ext4 /dev/mmcblk0p1 {MNT} 2>&1; echo RC=$?",
            timeout=15,
            message="Mounting eMMC partition..."
        )
        if _mnt_err:
            raise _mnt_err
        if "RC=0" not in mount_resp:
            verbose("  ⚠ Could not mount /dev/mmcblk0p1 — extlinux.conf not written", "warning")
            verbose("  If DIP=LEFT boot fails, at the U-Boot prompt run:", "warning")
            verbose('    setenv bootcmd "ext4load mmc 0:1 0x82000000 /boot/kernel.itb; bootm 0x82000000#config-1"', "warning")
            verbose('    setenv bootargs "root=/dev/mmcblk0p1 rw rootwait earlycon"', "warning")
            verbose("    saveenv", "warning")
            return step(0, "eMMC boot config (mount failed — see warning)", False)

        check = d.send_command(
            f"test -f {MNT}/boot/extlinux/extlinux.conf && echo EXISTS || echo MISSING",
            timeout=5
        )
        if "EXISTS" in check:
            d.send_command(f"umount {MNT}", timeout=10)
            return step(0, "eMMC extlinux.conf already present", True)

        d.send_command(f"mkdir -p {MNT}/boot/extlinux", timeout=5)
        d.send_command(
            f"printf '{extlinux_content}' > {MNT}/boot/extlinux/extlinux.conf",
            timeout=10
        )
        d.send_command("sync", timeout=10)
        d.send_command(f"umount {MNT}", timeout=15)
        d.send_command(f"rmdir {MNT} 2>/dev/null", timeout=5)
        return step(0, "eMMC extlinux.conf created", True)
    except Exception as e:
        try:
            d.send_command(f"umount {MNT} 2>/dev/null", timeout=10)
        except Exception as _umount_err:
            verbose(f"  ⚠ umount {MNT} failed: {_umount_err}", "debug")
        verbose(f"  ⚠ eMMC boot config step: {e}", "warning")
        return step(0, "eMMC boot config (warning — may need manual fix)", False)


@register_step(os=[OS], transfer=[TRANSFER], requires=["boot_configured"], produces=["rebooted"], label="Reboot device")
def step_reboot(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Reboot device"); verbose("=" * 60)
    try:
        ctx.device.send_command("reboot", wait_for_prompt=False, timeout=5)
        console_logger.info("Rebooting device...")
    except Exception as e:
        verbose(f"⚠ Reboot warning: {e}", "warning")
    return step(0, "Reboot sent", True)
