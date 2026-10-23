"""
mono-imager journey: VyOS via LAN -> USB stick in the Mono (v1, #28)

Follows the VyOS LS1046A port's INSTALL.md (github.com/mihakralj/
vyos-ls1046a-build), with mono-imager doing the typing:

  1. Device network ready
  2. VyOS password chosen        (asked once, typed in by step 9)
  3. ISO ready to send           (size limits + SHA-256 of YOUR file)
  4. USB stick in the Mono       (found, big enough, erase confirmed)
  5. Start HTTP server
  6. Verify ISO reachable
  7. Write ISO to the stick      (INSTALL.md step 1: raw dd of the hybrid
                                  ISO; written only after the copy in RAM
                                  matched the PC's file, then read back)
  8. Live-boot VyOS              (INSTALL.md step 2: the documented U-Boot
                                  line, then log in as vyos/vyos)
  9. install image -> eMMC       (INSTALL.md step 3; RAID no, eMMC as the
                                  target, serial console, defaults else)
 10. Boot settings on NOR        (the build's vyos-postinstall writes
                                  bootcmd='run vyos || run recovery' with
                                  fw_setenv — read back to confirm)

The ISO is a LOCAL file only, by design: mono-imager never downloads or
verifies OS images (README). The user checks the source; the tool only
proves the bytes arrived and landed unchanged.

Final DIP: NOR — VyOS's boot settings live in NOR's U-Boot env.

Author:  H.A. Hermsen
License: GPLv3
"""

from mono_imager.step_registry import register_step
from mono_imager.journeys import _common, _vyos, _vyos_steps as S

OS       = _vyos.OS
TRANSFER = "net"
TRANSFER_LABEL  = "LAN -> USB stick in the Mono"
FIRMWARE_PROMPT = "Type the full path (or drag-n-drop) of the VyOS LS1046A .iso file on this PC:"
NOTICE = _vyos.SOURCE_NOTICE

register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["network_up"],
              label="Device network ready")(_common._step_network_ready)
register_step(os=[OS], transfer=[TRANSFER], requires=[], produces=["vyos_password"],
              label="Choose the VyOS password")(S.step_password)
register_step(os=[OS], transfer=[TRANSFER], requires=["vyos_password"], produces=["iso_checked"],
              label="Prepare the ISO")(S.step_check_iso)
register_step(os=[OS], transfer=[TRANSFER], requires=["network_up", "iso_checked"],
              produces=["target_ready"], label="USB stick in the Mono")(S.step_detect_stick)
register_step(os=[OS], transfer=[TRANSFER], requires=["network_up", "target_ready"],
              produces=["http_server_up"], label="Start HTTP server")(S.step_http_server)
register_step(os=[OS], transfer=[TRANSFER], requires=["http_server_up"],
              produces=["firmware_ready"], label="Verify ISO reachable")(S.step_reachable)
register_step(os=[OS], transfer=[TRANSFER], requires=["firmware_ready"],
              produces=["iso_written"], label="Write the ISO to the USB stick")(S.step_write_stick)
register_step(os=[OS], transfer=[TRANSFER], requires=["iso_written"],
              produces=["live_up"], label="Live-boot VyOS from the USB stick")(S.step_boot_live_stick)
register_step(os=[OS], transfer=[TRANSFER], requires=["live_up"],
              produces=["os_flashed"], label="Install VyOS to the eMMC")(S.step_install_image)
register_step(os=[OS], transfer=[TRANSFER], requires=["os_flashed"],
              produces=["boot_configured"], label="Check the boot settings on NOR")(S.step_check_boot_env)
