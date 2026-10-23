"""
mono-imager: step implementations shared by the two VyOS journeys (#28).

Not auto-discovered (leading underscore). vyos_net.py and
vyos_netemmc.py register these functions with their own transfer name
and requires=/produces= chain. All device/serial work goes through
_vyos.py; this file is the journey-level glue (step reporting, user
questions, ordering).

Author:  H.A. Hermsen
License: GPLv3
"""

from pathlib import Path

from mono_imager.flash_orchestrator import step, verbose, console_logger, wait_for_report
from mono_imager.spinner import with_spinner
from mono_imager.step_registry import StepContext
from mono_imager.journeys import _vyos as V
from mono_imager.journeys import _common


def _ask(prompt: str) -> str:
    return input(prompt).strip().lower()


# --- Host-only steps -----------------------------------------------------------

def step_password(ctx: StepContext) -> bool:
    pw = V.ask_vyos_password()
    if not pw:
        return step(0, "VyOS password chosen", False, "no matching password entered")
    ctx.set("vyos_password", pw)
    return step(0, "VyOS password chosen (typed in during install image)", True)


def step_check_iso(ctx: StepContext) -> bool:
    """Size limits + SHA-256 of the user's file (used only to prove the
    copy on the Mono is byte-identical — not a judgement of the ISO)."""
    path = Path(ctx.firmware_path)
    try:
        size = path.stat().st_size
    except OSError as e:
        return step(0, "ISO file readable", False, str(e))
    if size > V.STAGING_CAP:
        return step(0, "ISO fits in recovery Linux's RAM", False,
                    f"{size / 1024**3:.2f} GB is above the {V.STAGING_CAP / 1024**3:.2f} GB staging limit")
    if size % V.SECTOR:
        return step(0, "ISO size is whole 512-byte sectors", False, f"{size} bytes")
    sha, err = with_spinner(V.sha256_file, path, message="Fingerprinting the ISO (SHA-256)...")
    if err:
        return step(0, "ISO file readable", False, str(err))
    ctx.set("iso_bytes", size)
    ctx.set("iso_sha256", sha)
    verbose(f"  ISO: {path.name}, {size / 1024**2:.0f} MiB, SHA-256 {sha}")
    return step(0, f"ISO ready to send ({size / 1024**2:.0f} MiB)", True)


def step_confirm_experimental(ctx: StepContext) -> bool:
    print()
    print("  +-----------------------------------------------------+")
    print("  | EXPERIMENTAL — NOT TESTED ON HARDWARE YET           |")
    print("  |                                                     |")
    print("  | This path stages the ISO on the eMMC instead of a   |")
    print("  | USB stick, boots it with 'toram' (copy to RAM), and |")
    print("  | only then lets 'install image' repartition the eMMC.|")
    print("  | If the live system does not run from RAM, it STOPS  |")
    print("  | before installing — but the eMMC's current OS is    |")
    print("  | already gone by then. The USB-stick path is the     |")
    print("  | proven one.                                         |")
    print("  +-----------------------------------------------------+")
    ok = _ask("  Use the EXPERIMENTAL eMMC path anyway? [y/N]: ") == "y"
    return step(0, "EXPERIMENTAL path confirmed", ok, "declined by user" if not ok else "")


def step_prepare_emmc_boot(ctx: StepContext) -> bool:
    """v2: read the ISO's own partition table and boot script on the PC,
    BEFORE anything is written, and derive the eMMC staging layout and
    the U-Boot commands from them."""
    path = Path(ctx.firmware_path)
    layout, why = V.hybrid_layout(path, path.stat().st_size)
    if layout is None:
        return step(0, "ISO has a FAT32 boot partition", False, why)
    try:
        scr = V.read_fat32_file(path, layout["p2"]["start"] * V.SECTOR, "BOOT.SCR")
        lines = V.adapt_boot_script_for_emmc(V.parse_uboot_script(scr))
    except ValueError as e:
        return step(0, "ISO boot script usable from eMMC", False, str(e))
    ctx.set("vyos_layout", layout)
    ctx.set("vyos_boot_lines", lines)
    verbose("  U-Boot commands for the eMMC live boot:")
    for l in lines:
        verbose(f"    {l}")
    return step(0, "ISO boot script adapted for eMMC (toram)", True)


# --- Device steps (recovery Linux) ---------------------------------------------

def step_detect_stick(ctx: StepContext) -> bool:
    d = ctx.device
    out, err = with_spinner(d.run_script, V.build_stick_probe_script(V.STICK_DEV),
                            marker="vyos_stick", exec_timeout=20,
                            message="Looking for the USB stick in the Mono...")
    if err:
        return step(0, "USB stick detected in the Mono", False, str(err))
    kv = V.parse_kv(out)
    if kv.get("STICK") != "YES":
        return step(0, "USB stick detected in the Mono", False,
                    f"no {V.STICK_DEV} — plug the stick into the Mono's USB port "
                    "(recovery Linux needs USB storage support: firmware 2026-04-15 or newer)")
    try:
        size = int(kv.get("SECTORS", "0")) * V.SECTOR
    except ValueError:
        size = 0
    if size and size < ctx.get("iso_bytes", 0):
        return step(0, "USB stick large enough", False,
                    f"{size / 1024**3:.1f} GB stick, ISO needs {ctx.get('iso_bytes') / 1024**3:.1f} GB")
    if kv.get("MOUNTED") == "YES":
        return step(0, "USB stick not in use", False, f"{V.STICK_DEV} is mounted in recovery Linux")
    model = kv.get("MODEL", "").strip() or "unknown model"
    print()
    print(f"  USB stick in the Mono: {V.STICK_DEV}, {size / 1024**3:.1f} GB, {model}")
    print("  EVERYTHING on this stick will be ERASED.")
    ok = _ask("  Erase it and write the VyOS ISO? [y/N]: ") == "y"
    return step(0, f"USB stick {V.STICK_DEV} ({size / 1024**3:.1f} GB) — erase confirmed", ok,
                "declined by user" if not ok else "")


def step_http_server(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Start HTTP server"); verbose("=" * 60)
    return _common._step_http_server_start(ctx)


def step_reachable(ctx: StepContext) -> bool:
    verbose("=" * 60); verbose("Verify ISO reachable"); verbose("=" * 60)
    return _common._step_firmware_reachable(ctx)


def _write(ctx: StepContext, dev: str, *, seek_lba=0, partition_table=b"", expect_p2="",
           label: str) -> bool:
    size, sha = ctx.get("iso_bytes"), ctx.get("iso_sha256")
    report_url = f"http://{ctx.host_ip}:{ctx.http_port}/report?step=vyos_write"
    script = V.build_write_script(
        url=ctx.get("firmware_source"), dev=dev, iso_bytes=size, iso_sha256=sha,
        report_url=report_url, seek_lba=seek_lba,
        partition_table=partition_table, expect_p2=expect_p2,
    )
    try:
        ctx.device.launch_script(script, marker="vyos_write")
    except Exception as e:
        return step(0, f"{label} launched", False, str(e))
    console_logger.info(f"{label} — sending, writing and reading back ~{size / 1024**2:.0f} MiB...")
    raw, err = with_spinner(wait_for_report, "vyos_write", timeout=1800.0, message=label)
    if err or raw is None:
        return step(0, label, False, str(err) if err else "no report from the device within 30 min")
    verbose(f"  write report:\n{raw.strip()}", "debug")
    ok, why = V.check_write_report(V.parse_kv(raw), size, sha,
                                   partition_table=bool(partition_table),
                                   expect_p2=bool(expect_p2))
    return step(0, label, ok, why)


def step_write_stick(ctx: StepContext) -> bool:
    return _write(ctx, V.STICK_DEV, label=f"ISO written to the USB stick ({V.STICK_DEV}) and verified")


def step_stage_emmc(ctx: StepContext) -> bool:
    table = V.stage_partition_table(ctx.get("vyos_layout"))
    return _write(ctx, V.EMMC_DEV, seek_lba=V.STAGE_BASE_LBA, partition_table=table,
                  expect_p2=f"{V.EMMC_DEV}p2",
                  label="ISO staged on the eMMC (at 64 MiB) and verified")


# --- Live boot -------------------------------------------------------------------

def _boot_live(ctx: StepContext, setup_lines, boot_line: str, label: str) -> bool:
    d = ctx.device
    console_logger.info("Rebooting into U-Boot...")
    at_uboot, err = with_spinner(V.reboot_to_uboot, d, message="Rebooting into U-Boot...")
    if err or not at_uboot:
        return step(0, "Stopped at the U-Boot prompt", False, str(err) if err else "autoboot not interrupted")
    con = V.Console(d)
    V.uboot_prime(con)
    for line in setup_lines:
        ok, out = V.uboot_run(con, line)
        if not ok:
            return step(0, f"U-Boot: {line[:60]}", False, out)
    con.drain(idle=0.3, max_wait=2.0)
    con.write(boot_line + "\r\n")
    console_logger.info("Booting VyOS live — takes a few minutes (the build reboots itself once)...")
    res, err = with_spinner(V.wait_live_login, con, message="Waiting for the VyOS login")
    ok, why = (False, str(err)) if err else res
    return step(0, label, ok, why)


# The INSTALL.md step-2 line, verbatim.
USB_BOOT_LINE = "usb start; fatload usb 0:2 ${load_addr} boot.scr; source ${load_addr}"
# ...which needs ${load_addr}. The build's own vyos-postinstall notes the
# address variables are not all in the factory env, so give it the value
# the build itself uses — only when it is missing, and without saveenv.
LOAD_ADDR_GUARD = 'if test -z "${load_addr}"; then setenv load_addr 0xa0000000; fi'


def step_boot_live_stick(ctx: StepContext) -> bool:
    return _boot_live(ctx, [LOAD_ADDR_GUARD], USB_BOOT_LINE,
                      "VyOS live booted from the USB stick and logged in")


def step_boot_live_emmc(ctx: StepContext) -> bool:
    lines = ctx.get("vyos_boot_lines")
    return _boot_live(ctx, lines[:-1], lines[-1], "VyOS live booted from the eMMC copy (toram) and logged in")


# --- In the VyOS live shell --------------------------------------------------------

# Where `install image` copies the system image FROM (vyos-1x
# FILE_ROOTFS_SRC). Commands typed into the live shell stay short: the
# console is 80 columns and a wrapped echo would garble the read-back.
_MEDIUM_DIR = "/usr/lib/live/mount/medium/live"


def step_ram_safety_check(ctx: StepContext) -> bool:
    """
    v2's guard: `install image` repartitions the eMMC FIRST and copies
    the system image from the live medium AFTERWARDS. That only works if
    the medium is no longer the eMMC — i.e. live-boot honoured `toram`.
    Every check must pass, or the journey stops here (nothing installed).
    """
    con = V.Console(ctx.device)
    try:
        cmdline = V.live_cmd(con, "cat /proc/cmdline")
        mounts = V.live_cmd(con, "grep mmcblk0 /proc/mounts")
        V.live_cmd(con, f"cd {_MEDIUM_DIR}")
        readable = V.live_cmd(con, "test -r filesystem.squashfs && echo READABLE")
        src = V.live_cmd(con, "df -P . | tail -n 1")
        V.live_cmd(con, "cd")
    except TimeoutError as e:
        return step(0, "Live system runs from RAM", False, str(e))
    problems = []
    if "toram" not in cmdline.split():
        problems.append("'toram' is not on the kernel command line")
    if "mmcblk0" in mounts:
        problems.append(f"{V.EMMC_DEV} is still mounted")
    if readable.strip() != "READABLE":
        problems.append("the system image (filesystem.squashfs) is not readable")
    elif not src or src.startswith("Filesystem") or "mmcblk" in src:
        problems.append(f"the system image does not come from RAM ({src or 'no df output'})")
    if problems:
        return step(0, "Live system runs from RAM (safe to repartition the eMMC)", False,
                    "; ".join(problems) + " — stopped BEFORE install image")
    return step(0, "Live system runs from RAM (safe to repartition the eMMC)", True)


def step_install_image(ctx: StepContext) -> bool:
    con = V.Console(ctx.device)
    console_logger.info("Running 'install image' to the eMMC — this takes several minutes...")
    res, err = with_spinner(V.run_install_image, con, ctx.get("vyos_password"),
                            message="Installing VyOS to the eMMC")
    ok, transcript = (False, str(err)) if err else res
    verbose(f"  install image transcript (tail):\n{transcript[-1500:]}", "debug")
    return step(0, "VyOS installed to the eMMC (install image)", ok,
                transcript[-300:] if not ok else "")


def step_check_boot_env(ctx: StepContext) -> bool:
    """`install image` runs the build's vyos-postinstall, which writes the
    U-Boot boot settings to NOR (fw_setenv). Read them back."""
    con = V.Console(ctx.device)
    try:
        bootcmd = V.live_cmd(con, "sudo fw_printenv -n bootcmd")
    except TimeoutError as e:
        return step(0, "Boot settings written to NOR", False, str(e))
    ok = "run vyos" in bootcmd
    if ok:
        print()
        print("  Note from the VyOS build: the very first boot from eMMC can fail")
        print("  once ('Failed to load /boot/vyos.env') — just power-cycle again.")
        print("  A USB stick still in the Mono is not needed any more (VyOS boots")
        print("  from eMMC) — you can take it out.")
    return step(0, "Boot settings written to NOR (bootcmd runs vyos)", ok,
                f"bootcmd is {bootcmd!r} — see the build's INSTALL.md 'Manual U-Boot "
                "environment setup'" if not ok else "")
