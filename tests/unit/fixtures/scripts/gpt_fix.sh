if [ "$(dd if=/dev/mmcblk0 bs=512 skip=1 count=1 2>/dev/null | head -c 8)" = "EFI PART" ]; then
  if command -v sgdisk >/dev/null 2>&1; then
    sgdisk -e /dev/mmcblk0 >/dev/null 2>&1; echo "GPT_FIX=$?"
  else echo "GPT_FIX=nosgdisk"; fi
else echo "GPT_FIX=nogpt"; fi
