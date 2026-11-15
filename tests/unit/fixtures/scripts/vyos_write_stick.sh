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
  dd if="$f" of=/dev/sda bs=1M conv=notrunc >>"$l" 2>&1
  echo "DD_RC=$?" >>"$l"
  sync
  if dd if=/dev/sda bs=512 skip=0 count=1048576 2>/dev/null | cmp -s - "$f"; then
    echo "VERIFY=OK" >>"$l"
  else
    echo "VERIFY=BAD" >>"$l"
  fi
else
  echo "WRITE=SKIPPED" >>"$l"
fi
rm -f "$f"
curl -sk -X POST --data-binary @"$l" "http://192.168.168.89:8080/report?step=vyos_write" >/dev/null 2>&1
