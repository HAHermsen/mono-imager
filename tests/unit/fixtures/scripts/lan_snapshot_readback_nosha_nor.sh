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

put() { curl -s -S -H 'Expect:' -T - "$U/$1" -o /dev/null 2>>/tmp/mono_imager_snap.log; }
rep() {
  _st=$1; shift
  printf '%s\n' "$@" | curl -s -H 'Expect:' -X POST --data-binary @- \
    "$U/report?step=$_st" >/dev/null 2>&1
}

cat > /tmp/mono_imager_plan.txt <<'MI_PLAN_EOF'
0 0 1073741824
1 1073741824 1073741824
2 2147483648 1073741824
3 3221225472 1073741824
4 4294967296 1073741824
5 5368709120 1073741824
6 6442450944 1073741824
7 7516192768 1073741824
8 8589934592 1073741824
9 9663676416 1073741824
10 10737418240 1073741824
11 11811160064 1073741824
12 12884901888 1073741824
13 13958643712 1073741824
14 15032385536 1073741824
15 16106127360 1073741824
16 17179869184 1073741824
17 18253611008 1073741824
18 19327352832 1073741824
19 20401094656 1073741824
20 21474836480 1073741824
21 22548578304 1073741824
22 23622320128 1073741824
23 24696061952 1073741824
24 25769803776 1073741824
25 26843545600 1073741824
26 27917287424 1073741824
27 28991029248 1073741824
28 30064771072 1073741824
29 31138512896 130023424
MI_PLAN_EOF
rd 4096 33550336 | put "verify?$T&tag=fw"
rd 0 4096 | put "verify?$T&tag=boot"
while read -r i off n; do
  rd $off $n | put "upload?$T&chunk=$i"; urc=$?
  dropc
  rd $off $n | put "verify?$T&tag=c$i"; vs=host
  rep snap_$i "CHUNK=$i" "UP_RC=$urc" "VSHA=$vs"
done < /tmp/mono_imager_plan.txt
cat /dev/mtd0 | put "nor?$T"; nrc=$?
rep snap_nor "NOR_RC=$nrc"
