cat > /tmp/mono_imager_plan.txt <<'MI_PLAN_EOF'
0 0 1073741824 emmc.000.img
1 1073741824 1073741824 emmc.001.img
2 2147483648 1073741824 emmc.002.img
3 3221225472 1073741824 emmc.003.img
4 4294967296 1073741824 emmc.004.img
5 5368709120 1073741824 emmc.005.img
6 6442450944 1073741824 emmc.006.img
7 7516192768 1073741824 emmc.007.img
8 8589934592 1073741824 emmc.008.img
9 9663676416 1073741824 emmc.009.img
10 10737418240 1073741824 emmc.010.img
11 11811160064 1073741824 emmc.011.img
12 12884901888 1073741824 emmc.012.img
13 13958643712 1073741824 emmc.013.img
14 15032385536 1073741824 emmc.014.img
15 16106127360 1073741824 emmc.015.img
16 17179869184 1073741824 emmc.016.img
17 18253611008 1073741824 emmc.017.img
18 19327352832 1073741824 emmc.018.img
19 20401094656 1073741824 emmc.019.img
20 21474836480 1073741824 emmc.020.img
21 22548578304 1073741824 emmc.021.img
22 23622320128 1073741824 emmc.022.img
23 24696061952 1073741824 emmc.023.img
24 25769803776 1073741824 emmc.024.img
25 26843545600 1073741824 emmc.025.img
26 27917287424 1073741824 emmc.026.img
27 28991029248 1073741824 emmc.027.img
28 30064771072 1073741824 emmc.028.img
29 31138512896 130023424 emmc.029.img
MI_PLAN_EOF
: > /tmp/mono_imager_snap.log
( ( set +e
DEV=/dev/mmcblk0
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

D='/mnt/usb/mono-snapshots/armbian-20261114-2010.partial'
rm -rf "$D"; mkdir -p "$D" || exit 1
echo "FW_SHA=$(rd 4096 33550336 | sha)" >> /tmp/mono_imager_snap.log
echo "BOOT_SHA=$(rd 0 4096 | sha)" >> /tmp/mono_imager_snap.log
while read -r i off n f; do
  rd $off $n | cat > "$D/$f"; c=$?
  dropc
  h1=$(cat < "$D/$f" | sha); h2=$(rd $off $n | sha)
  echo "C_$i=$c $h1 $h2" >> /tmp/mono_imager_snap.log
  [ "$c" = 0 ] && [ "$h1" = "$h2" ] || { echo "FAILED=$i" >> /tmp/mono_imager_snap.log; exit 1; }
done < /tmp/mono_imager_plan.txt
sync
)
echo "SNAP_DONE=1" >> /tmp/mono_imager_snap.log
) >/dev/null 2>&1 &
echo STARTED=1
