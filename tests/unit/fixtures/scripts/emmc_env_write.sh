[ "$(sha256sum < /tmp/mono_imager_env_new.bin | cut -d" " -f1)" = "abababababababababababababababababababababababababababababababab" ] || { echo "EMMC_ENV_WRITE=badsource"; exit 0; }
dd if=/tmp/mono_imager_env_new.bin of=/dev/mmcblk0 bs=512 seek=6144 count=16 2>/dev/null && sync || { echo "EMMC_ENV_WRITE=dd"; exit 0; }
got=$(dd if=/dev/mmcblk0 bs=512 skip=6144 count=16 2>/dev/null | sha256sum | cut -d" " -f1)
if [ "$got" = "abababababababababababababababababababababababababababababababab" ]; then echo "EMMC_ENV_WRITE=ok"; else echo "EMMC_ENV_WRITE=readback"; fi
