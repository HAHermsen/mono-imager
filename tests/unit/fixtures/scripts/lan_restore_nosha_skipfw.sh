DEV=/dev/mmcblk0
U='http://192.168.168.89:8080'
T='token=0123456789abcdef0123456789abcdef'
rd() {
  _s=$1; _n=$2; _m=1048576
  if [ $((_s % _m)) -eq 0 ] && [ "$_n" -ge "$_m" ]; then
    _q=$((_n / _m)); _r=$((_n % _m))
    dd if="$DEV" bs=$_m skip=$((_s / _m)) count=$_q 2>/dev/null
    if [ "$_r" -gt 0 ]; then
      dd if="$DEV" bs=512 skip=$(((_s + _q * _m) / 512)) count=$((_r / 512)) 2>/dev/null
    fi
  else
    dd if="$DEV" bs=512 skip=$((_s / 512)) count=$((_n / 512)) 2>/dev/null
  fi
  return 0
}
dropc() { sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; return 0; }
sha() { sha256sum | cut -d' ' -f1; }
# fast SHA-256: openssl uses the CPU's SHA instructions (~10x sha256sum, LS1046A)
fsha() {
  if command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 | cut -d= -f2 | tr -d ' '
  else sha256sum | cut -d' ' -f1; fi
}

wr() {
  if [ $(($1 % 1048576)) -eq 0 ]; then echo "bs=1048576 seek=$(($1 / 1048576))"
  else echo "bs=512 seek=$(($1 / 512))"; fi
}

put() { curl -s -S -H 'Expect:' -T - "$U/$1" -o /dev/null 2>>/tmp/mono_imager_snap.log; }
rep() {
  _st=$1; shift
  printf '%s\n' "$@" | curl -s -H 'Expect:' -X POST --data-binary @- \
    "$U/report?step=$_st" >/dev/null 2>&1
}

cat > /tmp/mono_imager_plan.txt <<'MI_PLAN_EOF'
0 0 0 4096 0
1 0 33554432 1040187392 33554432
2 1 0 1073741824 1073741824
3 2 0 1073741824 2147483648
4 3 0 1073741824 3221225472
5 4 0 1073741824 4294967296
6 5 0 1073741824 5368709120
7 6 0 1073741824 6442450944
8 7 0 1073741824 7516192768
9 8 0 1073741824 8589934592
10 9 0 1073741824 9663676416
11 10 0 1073741824 10737418240
12 11 0 1073741824 11811160064
13 12 0 1073741824 12884901888
14 13 0 1073741824 13958643712
15 14 0 1073741824 15032385536
16 15 0 1073741824 16106127360
17 16 0 1073741824 17179869184
18 17 0 1073741824 18253611008
19 18 0 1073741824 19327352832
20 19 0 1073741824 20401094656
21 20 0 1073741824 21474836480
22 21 0 1073741824 22548578304
23 22 0 1073741824 23622320128
24 23 0 1073741824 24696061952
25 24 0 1073741824 25769803776
26 25 0 1073741824 26843545600
27 26 0 1073741824 27917287424
28 27 0 1073741824 28991029248
29 28 0 1073741824 30064771072
30 29 0 130023424 31138512896
MI_PLAN_EOF
while read -r k c o n a; do
  q="image?$T&k=$k&chunk=$c&off=$o&len=$n"
  { curl -s -S "$U/$q" 2>>/tmp/mono_imager_snap.log; echo $? > /tmp/mi_crc; } |
    dd of="$DEV" $(wr $a) conv=notrunc 2>>/tmp/mono_imager_snap.log
  drc=$?; crc=$(cat /tmp/mi_crc)
  dropc
  rd $a $n | put "verify?$T&tag=r$k"; vs=host
  rep rst_$k "RANGE=$k" "CURL_RC=$crc" "DD_RC=$drc" "VSHA=$vs"
  [ "$crc" = 0 ] && [ "$drc" = 0 ] || exit 1
done < /tmp/mono_imager_plan.txt
