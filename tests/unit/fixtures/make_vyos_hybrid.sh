#!/bin/sh
# Rebuilds tests/unit/fixtures/vyos_hybrid.img.gz — a miniature VyOS
# LS1046A hybrid ISO made exactly the way the build makes the real one
# (mihakralj/vyos-ls1046a-build bin/ci-build-iso.sh @ 588f2b7):
#   1 MiB stand-in for the ISO9660 data, FAT32 partition (mkdosfs -F 32,
#   label VYOSBOOT) with boot.scr from the build's real boot.cmd
#   (mkimage -A arm64 -T script -C none), live/vmlinuz, live/initrd.img,
#   mono-gw.dtb, appended at the 1 MiB boundary, and the same MBR at 440.
# vyos_boot.cmd is the build's board/scripts/boot.cmd, verbatim
# (Apache-2.0, (c) the vyos-ls1046a-build authors).
# Needs: mkdosfs (dosfstools), mtools, mkimage (u-boot-tools), python3, gzip.
# Only needed to regenerate the fixture; the tests use the .gz.
set -e
here=$(cd "$(dirname "$0")" && pwd)
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
mkimage -A arm64 -T script -C none -n "VyOS LS1046A USB Boot" -d "$here/vyos_boot.cmd" "$t/boot.scr" >/dev/null
printf 'KERNEL' > "$t/vmlinuz"; printf 'INITRD' > "$t/initrd.img"; printf 'DTB' > "$t/mono-gw.dtb"
FAT_BYTES=$((36 * 1024 * 1024))
truncate -s "$FAT_BYTES" "$t/fat.img"
mkdosfs -F 32 -n VYOSBOOT "$t/fat.img" >/dev/null
mmd   -i "$t/fat.img" ::/live
mcopy -i "$t/fat.img" "$t/vmlinuz"    ::/live/vmlinuz
mcopy -i "$t/fat.img" "$t/initrd.img" ::/live/initrd.img
mcopy -i "$t/fat.img" "$t/mono-gw.dtb" ::mono-gw.dtb
mcopy -i "$t/fat.img" "$t/boot.scr"   ::boot.scr
python3 -c "
import sys; d = bytearray(b'CD001-fake-iso9660-area' * 45591)[:1024*1024]; open(sys.argv[1], 'wb').write(d)
" "$t/hybrid.img"
cat "$t/fat.img" >> "$t/hybrid.img"
python3 -c "
import struct, sys
iso_s, fat_s = (1024*1024)//512, $FAT_BYTES//512
with open(sys.argv[1], 'r+b') as f:
    f.seek(440)
    f.write(struct.pack('<IH', 0x56594F53, 0))
    f.write(struct.pack('<BBBBBBBBII', 0x00, 0xFE, 0xFF, 0xFF, 0x17, 0xFE, 0xFF, 0xFF, 0, iso_s))
    f.write(struct.pack('<BBBBBBBBII', 0x80, 0xFE, 0xFF, 0xFF, 0x0C, 0xFE, 0xFF, 0xFF, iso_s, fat_s))
    f.write(b'\x00' * 32)
    f.write(struct.pack('<H', 0xAA55))
" "$t/hybrid.img"
gzip -9n -c "$t/hybrid.img" > "$here/vyos_hybrid.img.gz"
cp "$t/boot.scr" "$here/vyos_boot.scr"
echo "wrote $here/vyos_hybrid.img.gz and vyos_boot.scr"
