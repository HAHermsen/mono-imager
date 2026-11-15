mkdir -p /mnt/usb
umount /mnt/usb 2>/dev/null
n=$(basename /dev/sda)
C=$(grep -o "$n[0-9][0-9]*$" /proc/partitions | sort -u; echo $n)
found=
for p in $C; do
  mount /dev/$p /mnt/usb 2>/dev/null || continue
  if [ -d /mnt/usb/mono-snapshots ]; then found=/dev/$p; break; fi
  umount /mnt/usb 2>/dev/null
done
if [ -n "$found" ]; then
  echo "USB_PART=$found"
  echo "USB_FS=$(grep " /mnt/usb " /proc/mounts | cut -d" " -f3)"
  echo "USB_FREE_KB=$(df -k /mnt/usb | tail -n 1 | tr -s " " | cut -d" " -f4)"
  i=0
  for m in /mnt/usb/mono-snapshots/*/manifest.json; do
    [ -f "$m" ] || continue
    d=$(dirname "$m"); d=$(basename "$d")
    case "$d" in *.partial) continue;; esac
    echo "SNAP_$i=$d"; i=$((i + 1))
  done
else
  echo "USB_PART="
fi
echo "MOUNT=done"
