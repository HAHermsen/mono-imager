e=""
for s in /sys/class/mtd/mtd[0-9]*; do
  [ "$(cat "$s/name" 2>/dev/null)" = "uboot-env" ] || continue
  e="/dev/$(basename "$s")"; break
done
[ -n "$e" ] && dd if="$e" of='/mnt/usb/mono-snapshots/armbian-20261114-2010.partial/nor_env.bin' bs=8192 count=1 2>/dev/null && sync && echo "NOR_ENV_SHA=$(sha256sum < '/mnt/usb/mono-snapshots/armbian-20261114-2010.partial/nor_env.bin' | cut -d" " -f1)"
echo NOR_ENV_SAVED=1
