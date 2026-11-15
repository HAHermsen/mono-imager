e=""
for s in /sys/class/mtd/mtd[0-9]*; do
  [ "$(cat "$s/name" 2>/dev/null)" = "uboot-env" ] || continue
  e="/dev/$(basename "$s")"; break
done
[ -n "$e" ] || { echo "NOR_ENV_WRITE=nodev"; exit 0; }
[ "$(sha256sum < /tmp/mono_imager_env_new.bin | cut -d" " -f1)" = "abababababababababababababababababababababababababababababababab" ] || { echo "NOR_ENV_WRITE=badsource"; exit 0; }
flashcp /tmp/mono_imager_env_new.bin "$e" >/dev/null 2>&1 || { echo "NOR_ENV_WRITE=flashcp"; exit 0; }
got=$(dd if="$e" bs=8192 count=1 2>/dev/null | sha256sum | cut -d" " -f1)
if [ "$got" = "abababababababababababababababababababababababababababababababab" ]; then echo "NOR_ENV_WRITE=ok"; else echo "NOR_ENV_WRITE=readback"; fi
