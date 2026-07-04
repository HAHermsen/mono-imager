# Firmware vs. OS Image — the eMMC Offset Conflict

This documents a physical constraint on the Mono Gateway's eMMC and how each OS journey (`journeys/<os>_<transfer>.py`) satisfies it. It's a companion to `JOURNEYS.md` (which documents the step-registry architecture) — this file is about *why* certain steps exist, not how to add new ones.

---

## The constraint

eMMC has two reserved regions no OS image is free to lay out however it likes:

- **Offset 0 – 4KiB**: the partition table (GPT/MBR).
- **Offset 4KiB – 32MiB**: Mono's own firmware/bootloader region (RCW, PBI, U-Boot).

Every OS image written to eMMC has to deal with this region somehow. In principle there are two clean options:

1. **Skip it** — partition the eMMC so the OS's own data starts at or after the 32MiB mark, leaving the firmware region untouched.
2. **Bundle it** — ship the firmware baked into the OS image itself, at the correct offset.

Option 2 isn't available to us: Mono's firmware requires proper signing/provisioning that generic third-party OS images (stock Armbian, stock OPNsense) have no way to include. So in practice, an OS image either respects the 32MiB boundary on its own, or it doesn't — and if it doesn't, the firmware region has to be restored afterward via `firmware update` from NOR recovery.

---

## Per-OS handling

### Armbian — flash whole, then restore

`step_flash_armbian` (`armbian_lan.py`) writes the raw Armbian image to `/dev/mmcblk0` — the whole disk, starting at offset 0 — via plain `dd`. This overwrites the firmware region along with everything else.

`_refresh_firmware_and_finish()` restores it afterward: flip DIP to eMMC and confirm Armbian's own bundled bootloader boots (per the official doc — Armbian images ship a working U-Boot for this board at their own offset), then flip back to NOR, power-cycle into recovery, and run `firmware update` to rewrite Mono's official firmware into the 0–32MiB region. One extra doc-mandated step (confirming the raw image's own bootloader works) sits in between, but the underlying mechanism is flash-whole-then-restore.

### OPNsense — same mechanism, and it's the documented procedure

`_uboot_steps_opnsense_lan()` (`opnsense_lan.py`) runs `mmc erase 0 3b48000` before flashing. `step_flash_opnsense` then writes the whole OPNsense image to `/dev/mmcblk0` via `bzip2 | dd` — again offset 0, again overwriting the firmware region. `step_reimage_emmc_firmware` runs `firmware update` afterward to restore it.

This isn't a workaround for OPNsense — it's the literal documented sequence (erase → flash whole disk → firmware update), per the official recipe at https://opnsense.mono.si/releases/26.1/ steps 2.1 → 3 → 4.

### OpenWRT — avoids the conflict instead of fixing it

`step_partition_emmc` (`openwrt_lan.py`) partitions eMMC with the first partition starting at sector 65536 — exactly 32MiB (65536 × 512 bytes). `step_flash_openwrt` then writes only to that partition (`/dev/mmcblk0p1`), never touching offset 0–32MiB at all.

OpenWRT still runs `step_firmware_update` (`firmware update`) afterward, but for a different reason than Armbian/OPNsense: there's nothing to *repair* since the OS write never touched the firmware region, but a fresh/erased eMMC still needs that region *populated* the first time.

---

## Summary

| OS | Flash target | Touches 0–32MiB during flash? | `firmware update` purpose |
|----|--------------|-------------------------------|---------------------------|
| Armbian | `/dev/mmcblk0` (whole disk) | Yes — overwritten | Restore after overwrite |
| OPNsense | `/dev/mmcblk0` (whole disk) | Yes — erased, then overwritten | Restore after overwrite (documented step) |
| OpenWRT | `/dev/mmcblk0p1` (partition @ 32MiB) | No — never touched | Populate reserved region |

All three end up with a correctly-populated firmware region by the time the journey completes — Armbian and OPNsense get there by fixing it up afterward, OpenWRT by never breaking it in the first place.
