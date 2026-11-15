"""
mono-imager: device-side shell script builders and parsers for snapshots (BusyBox ash)

Split out of snapshot.py; import everything through mono_imager.snapshot,
which re-exports these names.

Author:  H.A. Hermsen
License: GPLv3
"""

import re
from typing import Optional

from mono_imager import snapshot as S
from mono_imager import uboot_env

# --- Device-side shell (recovery Linux: BusyBox ash) ----------------------------------

def parse_kv(text: str) -> dict:
    """KEY=VALUE lines -> dict (first occurrence wins)."""
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    return out


def _q(s: str) -> str:
    """Single-quote for sh."""
    return "'" + str(s).replace("'", "'\\''") + "'"


# rd START LEN — print exactly LEN bytes of $DEV starting at START.
# Big aligned reads in 1 MiB blocks, the (sector-sized) tail in 512 B,
# so a range never reads past the image end on a bigger target eMMC.
_RD_FN = r"""rd() {
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
"""


def build_probe_script(dev: Optional[str] = None, usb_dev: Optional[str] = None) -> str:
    """Identity + capabilities of the device, as KEY=VALUE lines."""
    dev = dev or S.EMMC_DEV
    usb_dev = usb_dev or S.USB_DEV
    b = dev.split("/")[-1]
    tools = ["sha256sum", "zstd", "gzip", "curl", "sgdisk", "head", "tail",
             "mount", "df", "flashcp", "base64", "od", "e2fsck", "fsck.vfat", "dosfsck",
             "openssl", "tee", "mkfifo"]
    s = [
        f'if [ -b {dev} ]; then echo "EMMC_BYTES=$(( $(cat /sys/block/{b}/size) * 512 ))"; '
        'else echo "EMMC_BYTES=0"; fi',
        f'if grep -q "^{dev}" /proc/mounts; then echo "EMMC_MOUNTED=1"; else echo "EMMC_MOUNTED=0"; fi',
        'echo "MEM_KB=$(grep MemTotal /proc/meminfo | tr -s " " | cut -d" " -f2)"',
        'echo "BOOT_MEDIUM=$(grep -o "boot_medium=[a-z]*" /proc/cmdline | cut -d= -f2)"',
        'for i in eth0 eth1 eth2 eth3 eth4; do',
        '  if [ -r /sys/class/net/$i/address ]; then echo "MAC=$(cat /sys/class/net/$i/address)"; break; fi',
        'done',
        'e=/sys/bus/i2c/devices/3-0050/eeprom',
        '[ -r $e ] || { echo 24c32 0x50 > /sys/bus/i2c/devices/i2c-3/new_device; sleep 1; } 2>/dev/null',
        'if [ -r $e ]; then',
        '  echo "MODEL=$(dd if=$e bs=1 skip=8 count=32 2>/dev/null | tr -d "\\000")"',
        '  echo "SERIAL=$(dd if=$e bs=1 skip=40 count=24 2>/dev/null | tr -d "\\000")"',
        'fi',
        f'if [ -b {usb_dev} ]; then echo "USB_PRESENT=1"; else echo "USB_PRESENT=0"; fi',
        '[ -c /dev/mtd0 ] && echo "NOR_PRESENT=1"',
    ]
    for t in tools:
        s.append(f'command -v {t} >/dev/null 2>&1 && echo "HAVE_{t.upper().replace(".", "_")}=1"')
    s.append('echo x | gzip -1 >/dev/null 2>&1 && echo "HAVE_GZIP1=1"')
    s.append('echo "PROBE=done"')
    return "\n".join(s) + "\n"


def parse_probe(text: str) -> dict:
    kv = parse_kv(text)
    have = {k[len(S.HAVE_PREFIX):].lower() for k, v in kv.items() if k.startswith(S.HAVE_PREFIX) and v == "1"}
    try:
        emmc = int(kv.get("EMMC_BYTES", "0"))
    except ValueError:
        emmc = 0
    try:
        mem_kb = int(kv.get("MEM_KB", "0"))
    except ValueError:
        mem_kb = 0
    return {
        "ok": kv.get("PROBE") == "done",
        "emmc_bytes": emmc,
        "emmc_mounted": kv.get("EMMC_MOUNTED") == "1",
        "mem_kb": mem_kb,
        "boot_medium": kv.get("BOOT_MEDIUM", ""),
        "mac": S.normalize_mac(kv.get("MAC")),
        "model": kv.get("MODEL", ""),
        "serial": kv.get("SERIAL", ""),
        "usb_present": kv.get("USB_PRESENT") == "1",
        "nor_present": kv.get("NOR_PRESENT") == "1",
        "have": have,
    }


def device_compression(have: set) -> str:
    """Best compression the DEVICE can do for a USB snapshot."""
    if "zstd" in have:
        return "zst"
    if "gzip" in have:
        return "gz"
    return "raw"


def _comp_cmd(compression: str, have: set) -> str:
    if compression == "zst":
        return "zstd -q -c -3"
    if compression == "gz":
        return "gzip -1 -c" if "gzip1" in have else "gzip -c"
    return "cat"


def _decomp_cmd(compression: str) -> str:
    return {"zst": "zstd -q -d -c", "gz": "gzip -d -c", "raw": "cat"}[compression]


def can_decompress(compression: str, have: set) -> bool:
    return compression == "raw" or (compression == "zst" and "zstd" in have) \
        or (compression == "gz" and "gzip" in have)


def _report(url: str, body: str) -> str:
    """POST a small KEY=VALUE body to the PC's /report endpoint."""
    return (f"printf '%s\\n' {body} | curl -s -H 'Expect:' -X POST --data-binary @- "
            f"{_q(url)} >/dev/null 2>&1")


# -- LAN ------------------------------------------------------------------------------

PLAN = "/tmp/mono_imager_plan.txt"   # one line per chunk / range, read by a shell loop

# Single-pass snapshot: each chunk is tee'd into HASH_FIFO while it uploads;
# fsha reads the FIFO and writes the chunk's SHA-256 to HASH_OUT. The paths are
# short on purpose: every device-script line must stay short (see below).
HASH_FIFO = "/tmp/mi_f"
HASH_OUT = "/tmp/mi_v"

# Short names ON PURPOSE (deliberate exception to CLAUDE.md section 2,
# "Self-Documenting Naming"): device-script variables ($i, $off, $n, $urc,
# $vs, ...) and helpers (rd, wr, put, rep, fsha, dropc) are kept short so
# every line stays short on the serial console. Long lines wrap there and
# the wrapped echo garbles (see SerialDevice.run_script()).

# The scripts below are LOOPS over a short plan file, never one block per
# chunk: CONFIRMED ON HARDWARE, a 13 KB script (30 chunks unrolled) did not
# survive the serial heredoc write (run_script/launch_script's byte check
# failed). Plan + loop keeps every script around 2 KB, whatever the eMMC size.


def _plan(rows: list) -> list:
    """Lines writing `rows` (lists of fields) to PLAN with a quoted heredoc."""
    body = "\n".join(" ".join(str(x) for x in r) for r in rows)
    return [f"cat > {PLAN} <<'MI_PLAN_EOF'", body, "MI_PLAN_EOF"]


# wr START: dd parameters (bs, seek) for a write starting at START (aligned 1 MiB or 512 B)
_WR_FN = r"""wr() {
  if [ $(($1 % 1048576)) -eq 0 ]; then echo "bs=1048576 seek=$(($1 / 1048576))"
  else echo "bs=512 seek=$(($1 / 512))"; fi
}
"""

# LAN helpers — short lines: long lines wrap on the serial console and the
# wrapped echo garbles (see SerialDevice.run_script()).
#   put PATH       stdin -> PC (PUT $U/PATH)        rep STEP K=V...  report to the PC
_NET_FN = f"""put() {{ curl -s -S -H 'Expect:' -T - "$U/$1" -o /dev/null 2>>{S.LOG}; }}
rep() {{
  _st=$1; shift
  printf '%s\\n' "$@" | curl -s -H 'Expect:' -X POST --data-binary @- \\
    "$U/report?step=$_st" >/dev/null 2>&1
}}
"""


def build_lan_snapshot_script(*, base_url: str, token: str, chunks: list,
                              have_sha: bool, nor: bool, dev: Optional[str] = None,
                              single_pass: bool = False) -> str:
    """
    Stream every chunk RAW to the PC (the PC compresses + hashes), then
    re-read it from the eMMC with the page cache dropped (a real media
    read) and hash it: on the device when it has sha256sum, otherwise
    by streaming it to the PC's hash-only endpoint. Optional read-only
    NOR dump at the end. One report per chunk (step=snap_<i>).

    single_pass: read each chunk once and hash it on the device while it
    streams (tee into a FIFO) instead of a second media read. Proves the
    PC got exactly what was read; ~2x faster. Needs tee, mkfifo and
    openssl or sha256sum on the device (the caller checks).
    """
    dev = dev or S.EMMC_DEV
    s = [f"DEV={dev}", f"U={_q(base_url)}", f"T={_q('token=' + token)}", _RD_FN, _NET_FN]
    s += _plan([[i, off, n] for i, off, n in chunks])
    s += [f'rd {S.FW_START} {S.FW_END - S.FW_START} | put "verify?$T&tag=fw"',
          f'rd 0 {S.FW_START} | put "verify?$T&tag=boot"',
          "while read -r i off n; do"]
    if single_pass:
        s += [f"  dropc; rm -f {HASH_FIFO} {HASH_OUT}; mkfifo {HASH_FIFO}",
              f"  fsha < {HASH_FIFO} > {HASH_OUT} &",
              f'  rd $off $n | tee {HASH_FIFO} | put "upload?$T&chunk=$i"; urc=$?',
              f"  wait; vs=$(cat {HASH_OUT})"]
    else:
        s += ['  rd $off $n | put "upload?$T&chunk=$i"; urc=$?',
              "  dropc"]
        if have_sha:
            s.append("  vs=$(rd $off $n | sha)")
        else:
            s.append('  rd $off $n | put "verify?$T&tag=c$i"; vs=host')
    s += ['  rep snap_$i "CHUNK=$i" "UP_RC=$urc" "VSHA=$vs"',
          f"done < {PLAN}"]
    if nor:
        s += [f'cat {S.NOR_DEV} | put "nor?$T"; nrc=$?',
              'rep snap_nor "NOR_RC=$nrc"']
    return "\n".join(s) + "\n"


def build_lan_hash_script(*, base_url: str, token: str, start: int, length: int,
                          tag: str, dev: Optional[str] = None) -> str:
    """Hash one eMMC range on the PC (e.g. the firmware region)."""
    dev = dev or S.EMMC_DEV
    return "\n".join([
        f"DEV={dev}", _RD_FN, "dropc",
        f"rd {start} {length} | curl -s -H 'Expect:' -T - "
        f"{_q(base_url + '/verify?token=' + token + '&tag=' + tag)} -o /dev/null",
        "rc=$?",
        _report(base_url + f"/report?step=hash_{tag}", '"RC=$rc"'),
    ]) + "\n"


def build_lan_restore_script(*, base_url: str, token: str, ranges: list,
                             have_sha: bool, dev: Optional[str] = None) -> str:
    """
    Write each range from the PC (served decompressed), then drop the page
    cache and re-read it from the eMMC to hash it (on the device, or on the
    PC when there is no sha256sum). Stops at the first transfer/dd error.
    One report per range (step=rst_<k>).
    """
    dev = dev or S.EMMC_DEV
    s = [f"DEV={dev}", f"U={_q(base_url)}", f"T={_q('token=' + token)}",
         _RD_FN, _WR_FN, _NET_FN]
    s += _plan([[k, r["chunk"], r["off"], r["len"], r["abs"]] for k, r in enumerate(ranges)])
    s += ["while read -r k c o n a; do",
          '  q="image?$T&k=$k&chunk=$c&off=$o&len=$n"',
          f'  {{ curl -s -S "$U/$q" 2>>{S.LOG}; echo $? > /tmp/mi_crc; }} |',
          f'    dd of="$DEV" $(wr $a) conv=notrunc 2>>{S.LOG}',
          "  drc=$?; crc=$(cat /tmp/mi_crc)",
          "  dropc"]
    if have_sha:
        s.append("  vs=$(rd $a $n | sha)")
    else:
        s.append('  rd $a $n | put "verify?$T&tag=r$k"; vs=host')
    s += ['  rep rst_$k "RANGE=$k" "CURL_RC=$crc" "DD_RC=$drc" "VSHA=$vs"',
          '  [ "$crc" = 0 ] && [ "$drc" = 0 ] || exit 1',
          f"done < {PLAN}"]
    return "\n".join(s) + "\n"


# -- USB ------------------------------------------------------------------------------

def build_usb_mount_script(*, mode: str, usb_dev: Optional[str] = None,
                           mount: Optional[str] = None) -> str:
    """
    Mount a partition of the stick at `mount`. mode='restore': the first
    one holding mono-snapshots/; mode='snapshot': that one too if present,
    else the first writable one. Lists complete snapshots (SNAP_<n>=label).
    """
    mount = mount or S.USB_MOUNT
    usb_dev = usb_dev or S.USB_DEV
    want_dir = f'[ -d {mount}/{S.USB_ROOT} ]'
    s = [
        f"mkdir -p {mount}",
        f"umount {mount} 2>/dev/null",
        f'n=$(basename {usb_dev})',
        'C=$(grep -o "$n[0-9][0-9]*$" /proc/partitions | sort -u; echo $n)',
        "found=",
        "for p in $C; do",
        f"  mount /dev/$p {mount} 2>/dev/null || continue",
        f"  if {want_dir}; then found=/dev/$p; break; fi",
        f"  umount {mount} 2>/dev/null",
        "done",
    ]
    if mode == "snapshot":
        s += [
            'if [ -z "$found" ]; then',
            "  for p in $C; do",
            f"    mount /dev/$p {mount} 2>/dev/null || continue",
            f"    if touch {mount}/.mi_w 2>/dev/null; then rm -f {mount}/.mi_w; found=/dev/$p; break; fi",
            f"    umount {mount} 2>/dev/null",
            "  done",
            "fi",
        ]
    s += [
        'if [ -n "$found" ]; then',
        '  echo "USB_PART=$found"',
        f'  echo "USB_FS=$(grep " {mount} " /proc/mounts | cut -d" " -f3)"',
        f'  echo "USB_FREE_KB=$(df -k {mount} | tail -n 1 | tr -s " " | cut -d" " -f4)"',
        "  i=0",
        f"  for m in {mount}/{S.USB_ROOT}/*/manifest.json; do",
        '    [ -f "$m" ] || continue',
        '    d=$(dirname "$m"); d=$(basename "$d")',
        '    case "$d" in *.partial) continue;; esac',
        '    echo "SNAP_$i=$d"; i=$((i + 1))',
        "  done",
        "else",
        '  echo "USB_PART="',
        "fi",
        'echo "MOUNT=done"',
    ]
    return "\n".join(s) + "\n"


def parse_usb_mount(text: str) -> dict:
    kv = parse_kv(text)
    snaps = [v for _, v in sorted((int(k[len(S.SNAP_PREFIX):]), v) for k, v in kv.items()
                                  if re.fullmatch(r"SNAP_\d+", k))]
    try:
        free_kb = int(kv.get("USB_FREE_KB", "0") or 0)
    except ValueError:
        free_kb = 0
    return {"part": kv.get("USB_PART", ""), "fs": kv.get("USB_FS", ""),
            "free_kb": free_kb, "snapshots": snaps, "ok": kv.get("MOUNT") == "done"}


def _bg(body: list, done_key: str) -> str:
    """Run body in the background (so the serial shell stays free for
    polling), always finishing with <done_key>=1 in the log."""
    inner = "\n".join(body)
    return (f": > {S.LOG}\n( ( set +e\n{inner}\n)\necho \"{done_key}=1\" >> {S.LOG}\n) "
            f">/dev/null 2>&1 &\necho STARTED=1\n")


def build_usb_snapshot_script(*, label: str, chunks: list, compression: str,
                              have: set, nor: bool, dev: Optional[str] = None,
                              mount: Optional[str] = None) -> str:
    """
    Background job: per chunk, eMMC -> compressor -> file on the stick,
    then (page cache dropped) hash the decompressed file AND a fresh eMMC
    read; both must match. Results go to the log as C_<i>=<rc> <h_file> <h_emmc>.
    """
    dev = dev or S.EMMC_DEV
    mount = mount or S.USB_MOUNT
    d = f"{mount}/{S.USB_ROOT}/{label}.partial"
    comp, dec = _comp_cmd(compression, have), _decomp_cmd(compression)
    pre = _plan([[i, off, n, S.chunk_file(i, compression)] for i, off, n in chunks])
    b = [f"DEV={dev}", _RD_FN, f"D={_q(d)}", 'rm -rf "$D"; mkdir -p "$D" || exit 1',
         f'echo "FW_SHA=$(rd {S.FW_START} {S.FW_END - S.FW_START} | sha)" >> {S.LOG}',
         f'echo "BOOT_SHA=$(rd 0 {S.FW_START} | sha)" >> {S.LOG}',
         "while read -r i off n f; do",
         f'  rd $off $n | {comp} > "$D/$f"; c=$?',
         "  dropc",
         f'  h1=$({dec} < "$D/$f" | sha); h2=$(rd $off $n | sha)',
         f'  echo "C_$i=$c $h1 $h2" >> {S.LOG}',
         f'  [ "$c" = 0 ] && [ "$h1" = "$h2" ] || {{ echo "FAILED=$i" >> {S.LOG}; exit 1; }}',
         f"done < {PLAN}"]
    if nor:
        b += [f'cat {S.NOR_DEV} > "$D"/nor.bin; echo "NOR=$? $(sha < "$D"/nor.bin)" >> {S.LOG}']
    b.append('sync')
    return "\n".join(pre) + "\n" + _bg(b, "SNAP_DONE")


def build_usb_finalize_script(*, label: str, manifest_text: str, text_files: dict,
                              mount: Optional[str] = None) -> str:
    """Write manifest + env text files, build SHA256SUMS over every file on
    the device, publish the folder."""
    mount = mount or S.USB_MOUNT
    base = f"{mount}/{S.USB_ROOT}"
    d, final = f"{base}/{label}.partial", f"{base}/{label}"
    s = [f"cd {_q(d)} || exit 1",
         "cat > manifest.json <<'MI_MANIFEST_EOF'", manifest_text.rstrip("\n"), "MI_MANIFEST_EOF"]
    for i, (name, text) in enumerate(sorted(text_files.items())):
        if text:
            tag = f"MI_TEXT_{i}_EOF"
            s += [f"cat > {_q(name)} <<'{tag}'", text.rstrip("\n"), tag]
    s += ['F=; for f in *; do [ "$f" = SHA256SUMS ] || F="$F $f"; done',
          "sha256sum $F > SHA256SUMS",
          "rc=$?", "sync", "cd /",
          f"if [ -e {_q(final)} ]; then echo FINAL=exists; "
          f"elif [ $rc = 0 ] && mv {_q(d)} {_q(final)}; then sync; echo FINAL=ok; fi",
          "echo FINAL_DONE=1"]
    return "\n".join(s) + "\n"


def build_usb_nor_env_save_script(*, label: str, mount: Optional[str] = None) -> str:
    """Copy the NOR env region (read-only) into the snapshot folder on the stick."""
    mount = mount or S.USB_MOUNT
    dst = f"{mount}/{S.USB_ROOT}/{label}.partial/{S.NOR_ENV_FILE}"
    return "\n".join([
        'e=""',
        f"for s in {S.MTD_SYSFS}/mtd[0-9]*; do",
        f'  [ "$(cat "$s/name" 2>/dev/null)" = "{S.NOR_ENV_MTD_NAME}" ] || continue',
        f'  e="{S.MTD_DEV_DIR}/$(basename "$s")"; break',
        "done",
        f'[ -n "$e" ] && dd if="$e" of={_q(dst)} bs={S.NOR_ENV_SIZE} count=1 2>/dev/null && sync '
        f'&& echo "NOR_ENV_SHA=$(sha256sum < {_q(dst)} | cut -d" " -f1)"',
        "echo NOR_ENV_SAVED=1",
    ]) + "\n"


def build_usb_discard_script(*, label: str, mount: Optional[str] = None) -> str:
    mount = mount or S.USB_MOUNT
    return f"rm -rf {_q(f'{mount}/{S.USB_ROOT}/{label}.partial')}; sync; echo DISCARDED=1\n"


def build_usb_cat_script(*, label: str, name: str, mount: Optional[str] = None) -> str:
    """Print one small text file of a USB snapshot, one KEY=VALUE line per
    source line (L<n>=...) so run_script()'s duplicate-line filter can't
    merge identical lines."""
    mount = mount or S.USB_MOUNT
    p = f"{mount}/{S.USB_ROOT}/{label}/{name}"
    return (f"if [ -f {_q(p)} ]; then n=0; while IFS= read -r line || [ -n \"$line\" ]; do "
            f"echo \"L$n=$line\"; n=$((n + 1)); done < {_q(p)}; fi; echo CAT_DONE=1\n")


def parse_cat(text: str) -> str:
    lines = {}
    for line in (text or "").splitlines():
        m = re.match(r"^L(\d+)=(.*)$", line)
        if m:
            lines[int(m.group(1))] = m.group(2)
    return "".join(lines[i] + "\n" for i in sorted(lines))


def build_usb_restore_script(*, label: str, manifest: dict, ranges: list,
                             dev: Optional[str] = None, mount: Optional[str] = None) -> str:
    """
    Background job: sha256sum -c the snapshot on the stick first (nothing is
    written if any file is off), then per range decompress -> cut -> dd, drop
    the page cache, and compare a fresh eMMC read with the expected hash
    (manifest for whole chunks, recomputed from the file for partial ones).
    """
    dev = dev or S.EMMC_DEV
    mount = mount or S.USB_MOUNT
    d = f"{mount}/{S.USB_ROOT}/{label}"
    by_index = {c["index"]: c for c in manifest["chunks"]}
    rows = []
    for k, r in enumerate(ranges):
        c = by_index[r["chunk"]]
        full = r["off"] == 0 and r["len"] == c["bytes"]
        rows.append([k, c["file"], c["compression"], r["off"], r["len"], r["abs"],
                     c["sha256"] if full else "-"])
    pre = _plan(rows)
    b = [f"DEV={dev}", _RD_FN, _WR_FN,
         'dec() { case $1 in zst) zstd -q -d -c;; gz) gzip -d -c;; *) cat;; esac; }',
         f"cd {_q(d)} || {{ echo 'SUMS=missing' >> {S.LOG}; exit 1; }}",
         f"if sha256sum -c SHA256SUMS >/dev/null 2>&1; then echo SUMS=ok >> {S.LOG}; "
         f"else echo SUMS=bad >> {S.LOG}; exit 1; fi",
         "src() { if [ \"$w\" = - ]; then dec $z < \"$f\" | tail -c +$((o + 1)) | head -c $n; "
         "else dec $z < \"$f\"; fi; }",
         "while read -r k f z o n a w; do",
         '  src | dd of="$DEV" $(wr $a) conv=notrunc 2>/dev/null; drc=$?',
         "  dropc",
         '  if [ "$w" = - ]; then want=$(src | sha); else want=$w; fi',
         "  got=$(rd $a $n | sha)",
         f'  echo "R_$k=$drc $want $got" >> {S.LOG}',
         f'  [ "$drc" = 0 ] && [ "$want" = "$got" ] || {{ echo "FAILED=$k" >> {S.LOG}; exit 1; }}',
         f"done < {PLAN}",
         "sync"]
    return "\n".join(pre) + "\n" + _bg(b, "RESTORE_DONE")


def build_log_script() -> str:
    return f"cat {S.LOG} 2>/dev/null; echo LOG_READ=1\n"


def parse_usb_chunk_results(kv: dict, prefix: str) -> dict:
    """{index: (rc, h_a, h_b)} from C_<i>= / R_<k>= log lines."""
    out = {}
    for k, v in kv.items():
        m = re.fullmatch(prefix + r"_(\d+)", k)
        if m:
            parts = v.split()
            if len(parts) == 3:   # rc, hash A, hash B
                out[int(m.group(1))] = (parts[0], parts[1], parts[2])
    return out


def build_gpt_fix_script(dev: Optional[str] = None) -> str:
    """After a clone to a bigger eMMC: move the backup GPT to the real end."""
    dev = dev or S.EMMC_DEV
    return "\n".join([
        f'if [ "$(dd if={dev} bs=512 skip=1 count=1 2>/dev/null | head -c 8)" = "EFI PART" ]; then',
        '  if command -v sgdisk >/dev/null 2>&1; then',
        f'    sgdisk -e {dev} >/dev/null 2>&1; echo "GPT_FIX=$?"',
        '  else echo "GPT_FIX=nosgdisk"; fi',
        'else echo "GPT_FIX=nogpt"; fi',
    ]) + "\n"


# --- Reading raw bytes over serial (od) ----------------------------------------------
# Small regions (GPT, NOR env) are read as `od -v -b` text: octal offset +
# octal bytes. CONFIRMED ON HARDWARE: recovery Linux's BusyBox od has no -A
# / -t ("od: invalid option -- 'A'"); -b and -v are the classic options every
# od has. Every line starts with its own offset, so run_script()'s
# duplicate-line filter can never merge two lines.

OD_ARGS = "-v -b"


def build_dump_script(src: str, skip: int, count: int, bs: int = 512) -> str:  # 512 = SECTOR
    """Octal dump of `count` blocks of `bs` bytes of `src`, starting at block `skip`."""
    return (f"dd if={src} bs={bs} skip={skip} count={count} 2>/dev/null | od {OD_ARGS}; "
            "echo DUMP_DONE=1\n")


def parse_od(text: str) -> Optional[bytes]:
    """Bytes from `od -v -b` output (octal), or None if it is not contiguous."""
    rows = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([0-7]{7,})((?: [0-7]{3})+)\s*$", line.strip())
        if m:
            rows[int(m.group(1), 8)] = bytes(int(b, 8) for b in m.group(2).split())
    out, pos = bytearray(), 0
    for off in sorted(rows):
        if off != pos:
            return None
        out += rows[off]
        pos += len(rows[off])
    return bytes(out)


# --- NOR U-Boot env -------------------------------------------------------------------

def build_nor_env_dump_script() -> str:
    """Find the NOR env MTD partition and hex-dump its env region (read-only)."""
    return "\n".join([
        'e=""',
        f"for s in {S.MTD_SYSFS}/mtd[0-9]*; do",
        f'  [ "$(cat "$s/name" 2>/dev/null)" = "{S.NOR_ENV_MTD_NAME}" ] || continue',
        f'  e="{S.MTD_DEV_DIR}/$(basename "$s")"; break',
        "done",
        'echo "NOR_ENV_DEV=$e"',
        f'[ -n "$e" ] && dd if="$e" bs={S.NOR_ENV_SIZE} count=1 2>/dev/null | od {OD_ARGS}',
        "echo DUMP_DONE=1",
    ]) + "\n"


def parse_uboot_env_blob(blob: Optional[bytes]) -> Optional[dict]:
    """Variables of a raw U-Boot env region, or None when its CRC does not match."""
    region = uboot_env.parse(blob)
    return region.vars if region else None


def build_emmc_env_dump_script(dev: Optional[str] = None) -> str:
    """Hex dump of the eMMC U-Boot env region (inside the firmware region)."""
    return build_dump_script(dev or S.EMMC_DEV, S.EMMC_ENV_SECTOR, S.EMMC_ENV_SECTORS)


UPLOAD_PIECE = 512                   # bytes of a blob per serial script (~2 KB of text)


def build_upload_blob_scripts(data: bytes, dst: str) -> list:
    """
    Scripts that recreate `data` as file `dst` on the device over serial
    (printf, octal escapes), each small enough for one serial heredoc.
    The last one prints UPLOAD_SHA=<sha256 of dst>.
    """
    scripts = []
    for start in range(0, len(data), UPLOAD_PIECE):
        piece = data[start:start + UPLOAD_PIECE]
        s = [f": > {dst}"] if start == 0 else []
        for i in range(0, len(piece), S.PRINTF_CHUNK):
            s.append(f"printf '{printf_octal(piece[i:i + S.PRINTF_CHUNK])}' >> {dst}")
        scripts.append("\n".join(s) + "\n")
    scripts.append(f'echo "UPLOAD_SHA=$(sha256sum < {dst} | cut -d" " -f1)"\n')
    return scripts


def build_emmc_env_write_script(src: str, want_sha: str, dev: Optional[str] = None) -> str:
    """Write a rebuilt env region to the eMMC env location, read back, compare."""
    dev = dev or S.EMMC_DEV
    return "\n".join([
        f'[ "$(sha256sum < {src} | cut -d" " -f1)" = "{want_sha}" ] || '
        '{ echo "EMMC_ENV_WRITE=badsource"; exit 0; }',
        f"dd if={src} of={dev} bs={S.SECTOR} seek={S.EMMC_ENV_SECTOR} count={S.EMMC_ENV_SECTORS} "
        "conv=notrunc 2>/dev/null && sync || { echo \"EMMC_ENV_WRITE=dd\"; exit 0; }",
        f'got=$(dd if={dev} bs={S.SECTOR} skip={S.EMMC_ENV_SECTOR} count={S.EMMC_ENV_SECTORS} 2>/dev/null '
        '| sha256sum | cut -d" " -f1)',
        f'if [ "$got" = "{want_sha}" ]; then echo "EMMC_ENV_WRITE=ok"; else echo "EMMC_ENV_WRITE=readback"; fi',
    ]) + "\n"


def printf_octal(data: bytes) -> str:
    """`data` as a printf format string of octal escapes (POSIX / BusyBox printf)."""
    return "".join(f"\\{b:03o}" for b in data)


def build_nor_env_write_script(src: str, want_sha: str) -> str:
    """
    Write a saved NOR env region back (opt-in only): check the source file,
    flashcp it onto the env MTD partition, read it back and compare.
    """
    return "\n".join([
        'e=""',
        f"for s in {S.MTD_SYSFS}/mtd[0-9]*; do",
        f'  [ "$(cat "$s/name" 2>/dev/null)" = "{S.NOR_ENV_MTD_NAME}" ] || continue',
        f'  e="{S.MTD_DEV_DIR}/$(basename "$s")"; break',
        "done",
        '[ -n "$e" ] || { echo "NOR_ENV_WRITE=nodev"; exit 0; }',
        f'[ "$(sha256sum < {src} | cut -d" " -f1)" = "{want_sha}" ] || '
        '{ echo "NOR_ENV_WRITE=badsource"; exit 0; }',
        f'flashcp {src} "$e" >/dev/null 2>&1 || {{ echo "NOR_ENV_WRITE=flashcp"; exit 0; }}',
        f'got=$(dd if="$e" bs={S.NOR_ENV_SIZE} count=1 2>/dev/null | sha256sum | cut -d" " -f1)',
        f'if [ "$got" = "{want_sha}" ]; then echo "NOR_ENV_WRITE=ok"; else echo "NOR_ENV_WRITE=readback"; fi',
    ]) + "\n"


__all__ = [
    "HASH_FIFO",
    "HASH_OUT",
    "OD_ARGS",
    "PLAN",
    "UPLOAD_PIECE",
    "_NET_FN",
    "_RD_FN",
    "_WR_FN",
    "_bg",
    "_comp_cmd",
    "_decomp_cmd",
    "_plan",
    "_q",
    "_report",
    "build_dump_script",
    "build_emmc_env_dump_script",
    "build_emmc_env_write_script",
    "build_gpt_fix_script",
    "build_lan_hash_script",
    "build_lan_restore_script",
    "build_lan_snapshot_script",
    "build_log_script",
    "build_nor_env_dump_script",
    "build_nor_env_write_script",
    "build_probe_script",
    "build_upload_blob_scripts",
    "build_usb_cat_script",
    "build_usb_discard_script",
    "build_usb_finalize_script",
    "build_usb_mount_script",
    "build_usb_nor_env_save_script",
    "build_usb_restore_script",
    "build_usb_snapshot_script",
    "can_decompress",
    "device_compression",
    "parse_cat",
    "parse_kv",
    "parse_od",
    "parse_probe",
    "parse_uboot_env_blob",
    "parse_usb_chunk_results",
    "parse_usb_mount",
    "printf_octal",
]
