if [ -b /dev/mmcblk0 ]; then echo "EMMC_BYTES=$(( $(cat /sys/block/mmcblk0/size) * 512 ))"; else echo "EMMC_BYTES=0"; fi
if grep -q "^/dev/mmcblk0" /proc/mounts; then echo "EMMC_MOUNTED=1"; else echo "EMMC_MOUNTED=0"; fi
echo "MEM_KB=$(grep MemTotal /proc/meminfo | tr -s " " | cut -d" " -f2)"
echo "BOOT_MEDIUM=$(grep -o "boot_medium=[a-z]*" /proc/cmdline | cut -d= -f2)"
for i in eth0 eth1 eth2 eth3 eth4; do
  if [ -r /sys/class/net/$i/address ]; then echo "MAC=$(cat /sys/class/net/$i/address)"; break; fi
done
e=/sys/bus/i2c/devices/3-0050/eeprom
[ -r $e ] || { echo 24c32 0x50 > /sys/bus/i2c/devices/i2c-3/new_device; sleep 1; } 2>/dev/null
if [ -r $e ]; then
  echo "MODEL=$(dd if=$e bs=1 skip=8 count=32 2>/dev/null | tr -d "\000")"
  echo "SERIAL=$(dd if=$e bs=1 skip=40 count=24 2>/dev/null | tr -d "\000")"
fi
if [ -b /dev/sda ]; then echo "USB_PRESENT=1"; else echo "USB_PRESENT=0"; fi
[ -c /dev/mtd0 ] && echo "NOR_PRESENT=1"
command -v sha256sum >/dev/null 2>&1 && echo "HAVE_SHA256SUM=1"
command -v zstd >/dev/null 2>&1 && echo "HAVE_ZSTD=1"
command -v gzip >/dev/null 2>&1 && echo "HAVE_GZIP=1"
command -v curl >/dev/null 2>&1 && echo "HAVE_CURL=1"
command -v sgdisk >/dev/null 2>&1 && echo "HAVE_SGDISK=1"
command -v head >/dev/null 2>&1 && echo "HAVE_HEAD=1"
command -v tail >/dev/null 2>&1 && echo "HAVE_TAIL=1"
command -v mount >/dev/null 2>&1 && echo "HAVE_MOUNT=1"
command -v df >/dev/null 2>&1 && echo "HAVE_DF=1"
command -v flashcp >/dev/null 2>&1 && echo "HAVE_FLASHCP=1"
command -v base64 >/dev/null 2>&1 && echo "HAVE_BASE64=1"
command -v od >/dev/null 2>&1 && echo "HAVE_OD=1"
command -v e2fsck >/dev/null 2>&1 && echo "HAVE_E2FSCK=1"
command -v fsck.vfat >/dev/null 2>&1 && echo "HAVE_FSCK_VFAT=1"
command -v dosfsck >/dev/null 2>&1 && echo "HAVE_DOSFSCK=1"
command -v openssl >/dev/null 2>&1 && echo "HAVE_OPENSSL=1"
command -v tee >/dev/null 2>&1 && echo "HAVE_TEE=1"
command -v mkfifo >/dev/null 2>&1 && echo "HAVE_MKFIFO=1"
echo x | gzip -1 >/dev/null 2>&1 && echo "HAVE_GZIP1=1"
echo "PROBE=done"
