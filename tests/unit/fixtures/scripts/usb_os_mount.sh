m="/mnt/usb"
mkdir -p "$m"
umount "$m" 2>/dev/null
first=""
for p in $(awk '$4 ~ /^sda[0-9]+$/ {print $4}' /proc/partitions) sda; do
  mount "/dev/$p" "$m" 2>/dev/null || continue
  hit=0
  for f in "$m"/*; do
    [ -f "$f" ] || continue
    b=$(basename "$f" | tr 'A-Z' 'a-z')
    case "$b" in armbian*.img.xz|armbian*.img) hit=1; break ;; esac
  done
  if [ $hit -eq 1 ]; then echo "USB_MOUNTED:/dev/$p:IMAGE"; exit 0; fi
  [ -z "$first" ] && first="$p"
  umount "$m" 2>/dev/null
done
if [ -n "$first" ] && mount "/dev/$first" "$m" 2>/dev/null; then
  echo "USB_MOUNTED:/dev/$first:NOIMAGE"; exit 0
fi
echo "USB_MOUNT_FAILED"
