f=/tmp/mono_imager_vyos.iso
l=/tmp/mono_imager_vyos_write.log
rm -f "$f"
: > "$l"
curl -sk -o "$f" "http://192.168.168.89:8080/vyos.iso" >>"$l" 2>&1
echo "DL_RC=$?" >>"$l"
sz=$(wc -c < "$f" 2>/dev/null | tr -d " ")
echo "SIZE=$sz" >>"$l"
ok=1
[ "$sz" = "536870912" ] || ok=0
if command -v sha256sum >/dev/null 2>&1; then
  sum=$(sha256sum "$f" | cut -d" " -f1)
  echo "SHA=$sum" >>"$l"
  [ "$sum" = "abababababababababababababababababababababababababababababababab" ] || ok=0
else
  echo "SHA=unavailable" >>"$l"
fi
if [ "$ok" = 1 ]; then
  dd if=/dev/zero of=/dev/mmcblk0 bs=512 count=8 >>"$l" 2>&1
  echo "PT_ZERO_RC=$?" >>"$l"
  printf '\000\001\002\003\004\005\006\007\010\011\012\013\014\015\016\017\020\021\022\023\024\025\026\027\030\031\032\033\034\035\036\037\040\041\042\043\044\045\046\047\050\051\052\053\054\055\056\057\060\061\062\063\064\065\066\067\070\071\072\073\074\075\076\077' | dd of=/dev/mmcblk0 bs=1 seek=446 >>"$l" 2>&1
  echo "PT_RC=$?" >>"$l"
  dd if="$f" of=/dev/mmcblk0 bs=1M seek=64 >>"$l" 2>&1
  echo "DD_RC=$?" >>"$l"
  sync
  if dd if=/dev/mmcblk0 bs=512 skip=131072 count=1048576 2>/dev/null | cmp -s - "$f"; then
    echo "VERIFY=OK" >>"$l"
  else
    echo "VERIFY=BAD" >>"$l"
  fi
  blockdev --rereadpt /dev/mmcblk0 >>"$l" 2>&1 || partprobe /dev/mmcblk0 >>"$l" 2>&1
  sleep 2
  if [ -b /dev/mmcblk0p2 ]; then echo "P2=OK" >>"$l"; else echo "P2=MISSING" >>"$l"; fi
else
  echo "WRITE=SKIPPED" >>"$l"
fi
rm -f "$f"
curl -sk -X POST --data-binary @"$l" "http://192.168.168.89:8080/report?step=vyos_write" >/dev/null 2>&1
