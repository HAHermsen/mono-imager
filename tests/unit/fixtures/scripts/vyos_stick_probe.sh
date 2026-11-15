if [ -b /dev/sda ]; then
  echo "STICK=YES"
  echo "SECTORS=$(cat /sys/block/sda/size 2>/dev/null)"
  echo "MODEL=$(echo $(cat /sys/block/sda/device/vendor /sys/block/sda/device/model 2>/dev/null))"
  if grep -q "^/dev/sda" /proc/mounts; then echo "MOUNTED=YES"; else echo "MOUNTED=NO"; fi
else
  echo "STICK=NO"
fi
