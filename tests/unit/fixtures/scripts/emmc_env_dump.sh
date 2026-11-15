dd if=/dev/mmcblk0 bs=512 skip=6144 count=16 2>/dev/null | od -v -b; echo DUMP_DONE=1
