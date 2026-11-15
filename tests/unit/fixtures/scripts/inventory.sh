echo "DATE=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "KERNEL=$(uname -r)"
echo "KERNEL_BUILD=$(uname -v)"
[ -r /etc/os-release ] && echo "RECOVERY_OS=$(. /etc/os-release; echo "$PRETTY_NAME")"
command -v firmware >/dev/null 2>&1 && echo "FWTOOL=$(firmware help 2>&1 | head -n 1)"
echo "MEM_KB=$(grep MemTotal /proc/meminfo | tr -s " " | cut -d" " -f2)"
i=0
[ -r /proc/mtd ] && while read -r m size erase name; do case $m in mtd*)
  echo "MTD_$i=${m%:} $size $erase $name"; i=$((i + 1));; esac
done < /proc/mtd
for m in $(grep -o '^mtd[0-9]*' /proc/mtd 2>/dev/null); do
  v=$(strings /dev/$m 2>/dev/null | grep -m 1 "^U-Boot 20")
  [ -n "$v" ] && echo "UBOOT_NOR_$(echo $m | tr a-z A-Z)=$v"
done
echo "UBOOT_EMMC=$(dd if=/dev/mmcblk0 bs=1M count=32 2>/dev/null | strings | grep -m 1 "^U-Boot 20")"
for f in name manfid oemid serial date fwrev hwrev life_time pre_eol_info; do
  [ -r /sys/block/mmcblk0/device/$f ] && echo "EMMC_$(echo $f | tr a-z A-Z)=$(cat /sys/block/mmcblk0/device/$f)"
done
for n in /sys/class/net/eth*; do
  [ -r "$n/address" ] && echo "MAC_$(basename "$n" | tr a-z A-Z)=$(cat "$n/address")"
done
echo INVENTORY=done
