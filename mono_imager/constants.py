"""
mono-imager: shared constants — hardware facts and cross-module defaults.

One place for every value that more than one module relies on: serial
settings, device paths, the eMMC/NOR layout of the Mono Gateway
(docs.mono.si "Flashing firmware"), U-Boot RAM addresses, image magic
bytes as U-Boot's `md.b` prints them, and the timeouts that were tuned
on real hardware. Module-specific tuning stays in its own module, next
to the code that uses it, as named constants at the top of that module.

Changing a value here changes it everywhere — that is the point. Several
of these were confirmed on real hardware (see the comments); change them
only with a hardware test.

Author:  H.A. Hermsen
License: GPLv3
"""

# --- Units ----------------------------------------------------------------------

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB
SECTOR_BYTES = 512                    # eMMC / USB logical block

# Recovery Linux keeps /tmp in RAM; a whole image fits there up to ~3.8 GB
# (confirmed on hardware). Buffered (download-then-dd) paths stay at 80 % of it.
RECOVERY_ROOT_CAP_GB = 3.8
RAM_SAFETY_MARGIN = 0.8
RAM_STAGING_CAP = int(RECOVERY_ROOT_CAP_GB * GIB * RAM_SAFETY_MARGIN)

# --- Serial ---------------------------------------------------------------------

SERIAL_BAUD = 115200                  # Mono Gateway console
SERIAL_BAUD_CANDIDATES = [115200, 9600, 57600, 38400]   # tried in this order

# --- Network --------------------------------------------------------------------

HTTP_PORT = 8080                      # PC-side HTTP server for LAN journeys
LANTEST_HTTP_PORT = 18080             # Test LAN uses its own port (never clashes)
INTERNET_PROBE_IP = "8.8.8.8"         # reachability probe / host-IP route lookup
INTERNET_PROBE_PORT = 80
DEFAULT_ETH_IFACE = "eth0"
ETH_IFACE_COUNT = 5                   # recovery Linux exposes eth0..eth4

# --- Device paths (recovery Linux) ------------------------------------------------

EMMC_DEV = "/dev/mmcblk0"
USB_DEV = "/dev/sda"                  # USB stick in the Mono
USB_MOUNT = "/mnt/usb"
NOR_MTD_DEV = "/dev/mtd0"

# --- eMMC layout ------------------------------------------------------------------
# The first 32 MiB of the eMMC hold Mono's firmware (bootloader, U-Boot,
# recovery Linux). The first 4 KiB are not used by the CPU (GPT lives there).

EMMC_FW_START = 4 * KIB
EMMC_FW_END = 32 * MIB
EMMC_FW_END_MIB = EMMC_FW_END // MIB  # for `dd bs=1M skip=32 seek=32`
EMMC_ERASE_SECTORS_HEX = "3b48000"    # whole 32 GB eMMC in 512 B sectors (`mmc erase 0 <n>`)

# --- NOR layout (64 MiB QSPI NOR) -------------------------------------------------

NOR_SIZE = 64 * MIB
NOR_ENV_OFFSET = 0x300000             # U-Boot environment
NOR_DTB_OFFSET = "0x500000"           # device tree (confirmed on hardware)
NOR_DTB_LEN = "0x20000"
NOR_KERNEL_OFFSET = "0xa00000"        # gzip Image + initramfs (confirmed on hardware)
NOR_KERNEL_LEN = "0x2000000"          # 32 MiB read window, covers any recovery image

# --- U-Boot RAM addresses / env ---------------------------------------------------

UBOOT_LOAD_ADDR = "0x82000000"        # kernel / scratch load address
UBOOT_DTB_ADDR = "0x90000000"         # separate DTB area for booti
UBOOT_KERNEL_COMP_ADDR = "0xa0000000" # decompression buffer for gzip Image (booti)
UBOOT_KERNEL_COMP_SIZE = "0x10000000"
UBOOT_FDT_HIGH_NO_RELOC = "0xffffffffffffffff"
UBOOT_ENV_SIZE = 0x2000
UBOOT_ENV_EMMC_OFFSET = 0x300000

# Recovery kernel command line (confirmed on hardware, U-Boot 2025.04);
# boot_medium=<nor|qspi|emmc> is appended so `firmware update` knows the medium.
RECOVERY_BOOTARGS = "console=ttyS0,115200 root=/dev/ram0 earlycon=uart8250,mmio,0x21c0500"
BOOT_MEDIUM_NOR = "qspi"
BOOT_MEDIUM_EMMC = "emmc"

# --- Image magic, as U-Boot `md.b` prints it ----------------------------------------

FIT_MAGIC_HEX = "d0 0d fe ed"         # FDT / FIT
GZIP_MAGIC_HEX = "1f 8b"
UIMAGE_MAGIC_HEX = "27 05 19 56"
MIN_KERNEL_FIT_BYTES = 5 * MIB        # smaller FITs are DTBs / firmware blobs, not kernels

# --- Timeouts (seconds) — tuned on real hardware ------------------------------------

UBOOT_QUICK_TIMEOUT = 5               # printenv / setenv / md.b
UBOOT_CMD_TIMEOUT = 10                # sf probe / env import / long setenv / printenv (all)
UBOOT_WRITE_TIMEOUT = 15              # saveenv / small sf read
SHELL_QUICK_TIMEOUT = 5               # one-line recovery-shell command
SHELL_CMD_TIMEOUT = 10
REBOOT_CMD_TIMEOUT = 5
UMOUNT_TIMEOUT = 15
AUTOBOOT_TIMEOUT = 60                 # wait for the U-Boot "Hit any key" line
LOGIN_TIMEOUT = 30
REPORT_TIMEOUT = 15.0                 # wait for a device /report
REACHABLE_REPORT_TIMEOUT = 20.0       # firmware-reachable check (HEAD + report)
FLASH_TIMEOUT = 600                   # curl|dd of a typical image
FLASH_TIMEOUT_SLOW = 1200             # bzip2 | dd (OPNsense)
SCRIPT_EXEC_TIMEOUT = 60.0            # SerialDevice.run_script default

# --- Logging / console ----------------------------------------------------------------

DIVIDER_WIDTH = 60                    # "=" * 60 section dividers
BOX_RULE_WIDTH = 56                   # "─" * 56 inside indented boxes
LOG_TAIL_SHORT = 100                  # characters of device output kept in a message
LOG_TAIL = 200
LOG_TAIL_LONG = 300
