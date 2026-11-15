e=""
for s in /sys/class/mtd/mtd[0-9]*; do
  [ "$(cat "$s/name" 2>/dev/null)" = "uboot-env" ] || continue
  e="/dev/$(basename "$s")"; break
done
echo "NOR_ENV_DEV=$e"
[ -n "$e" ] && dd if="$e" bs=8192 count=1 2>/dev/null | od -v -b
echo DUMP_DONE=1
