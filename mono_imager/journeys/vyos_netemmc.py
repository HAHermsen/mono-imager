"""
mono-imager journey: VyOS via LAN -> eMMC staging (v2, EXPERIMENTAL, #28)

Same as vyos_net.py, but without a USB stick: the ISO is staged on the
eMMC itself. NOT TESTED ON HARDWARE — shown as EXPERIMENTAL and asks
for an explicit yes before doing anything.

Why it needs care: `install image` repartitions the target disk FIRST
and copies the system image from the live medium AFTERWARDS (vyos-1x
image_installer.py). With the medium on that same eMMC, it would erase
its own source. So the live system is booted with `toram` (live-boot
copies itself into RAM) and step 10 checks that this really happened
before `install image` may run.

  1. Device network ready
  2. Confirm the EXPERIMENTAL path
  3. VyOS password chosen
  4. ISO ready to send            (size limits + SHA-256 of YOUR file)
  5. eMMC boot prepared on the PC (from the ISO's own partition table and
                                   boot.scr: where the FAT32 boot partition
                                   is, and the U-Boot commands — usb 0:2 ->
                                   mmc 0:2, + toram. Anything unexpected in
                                   boot.scr stops here, before any write.)
  6. Start HTTP server
  7. Verify ISO reachable
  8. Stage the ISO on the eMMC    (partition table in the first 4 KiB, the
                                   ISO at 64 MiB — past Mono's 4 KiB-32 MiB
                                   eMMC firmware region; read back)
  9. Live-boot VyOS from the eMMC (the adapted boot.scr commands)
 10. Safety check                 (toram on the cmdline, eMMC not mounted,
                                   system image readable from RAM)
 11. install image -> eMMC
 12. Boot settings on NOR

Final DIP: NOR (same as v1).

Author:  H.A. Hermsen
License: GPLv3
"""

from mono_imager.step_registry import register_step
from mono_imager.journeys import _common, _vyos, _vyos_steps as S

OS       = _vyos.OS
TRANSFER = "netemmc"
TRANSFER_LABEL  = "LAN -> eMMC staging, no USB stick (EXPERIMENTAL)"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the VyOS LS1046A .iso file on this PC:"
NOTICE = _vyos.SOURCE_NOTICE

register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["network_up"],
              label="Device network ready")(_common._step_network_ready)
register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["experimental_ok"],
              label="Confirm the EXPERIMENTAL path")(S.step_confirm_experimental)
register_step(os=[OS], transfer=[TRANSFER], requires=["experimental_ok"], produces=["vyos_password"],
              label="Choose the VyOS password")(S.step_password)
register_step(os=[OS], transfer=[TRANSFER], requires=["vyos_password"], produces=["iso_checked"],
              label="Prepare the ISO")(S.step_check_iso)
register_step(os=[OS], transfer=[TRANSFER], requires=["iso_checked"], produces=["target_ready"],
              label="Prepare the eMMC boot (from the ISO)")(S.step_prepare_emmc_boot)
register_step(os=[OS], transfer=[TRANSFER], requires=["network_up", "target_ready"],
              produces=["http_server_up"], label="Start HTTP server")(S.step_http_server)
register_step(os=[OS], transfer=[TRANSFER], requires=["http_server_up"],
              produces=["firmware_ready"], label="Verify ISO reachable")(S.step_reachable)
register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"],
              produces=["iso_written"], label="Stage the ISO on the eMMC")(S.step_stage_emmc)
register_step(os=[OS], transfer=[TRANSFER], requires=["iso_written"],
              produces=["live_up"], label="Live-boot VyOS from the eMMC (toram)")(S.step_boot_live_emmc)
register_step(os=[OS], transfer=[TRANSFER], requires=["live_up"],
              produces=["ram_ok"], label="Safety check: live system runs from RAM")(S.step_ram_safety_check)
register_step(os=[OS], transfer=[TRANSFER], requires=["ram_ok"],
              produces=["os_flashed"], label="Install VyOS to the eMMC")(S.step_install_image)
register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"],
              produces=["boot_configured"], label="Check the boot settings on NOR")(S.step_check_boot_env)
