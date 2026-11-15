for p in $(grep -o "mmcblk0p[0-9][0-9]*$" /proc/partitions); do
  n=${p##*p}; d=/dev/$p; t=other
  set -- $(dd if=$d bs=1 skip=1080 count=2 2>/dev/null | od -b | head -n 1)
  [ "$2 $3" = "123 357" ] && t=ext
  for o in 54 82; do
    [ "$(dd if=$d bs=1 skip=$o count=3 2>/dev/null)" = "FAT" ] && t=vfat
  done
  r=nocheck
  if [ $t = ext ]; then
    if command -v e2fsck >/dev/null 2>&1; then e2fsck -n -f $d >/dev/null 2>&1; r=$?; else r=notool; fi
  elif [ $t = vfat ]; then
    if command -v fsck.vfat >/dev/null 2>&1; then fsck.vfat -n $d >/dev/null 2>&1; r=$?
    elif command -v dosfsck >/dev/null 2>&1; then dosfsck -n $d >/dev/null 2>&1; r=$?
    else r=notool; fi
  fi
  echo "FS_$n=$t $r"
done
echo FSCHECK=done
