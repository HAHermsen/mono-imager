"""
mono-imager: golden files for every device shell script.

Every `build_*_script(s)` function that produces a script for the device is
called with fixed inputs, and its output must match, byte for byte, the
reference file in tests/unit/fixtures/scripts/. A refactor that changes even
one byte of a script sent to the board fails here. Scripts may only change
on purpose: regenerate the references and review them in `git diff`.

Also checks that every builder has at least one golden case (a new builder
without one fails), that no stale reference file is left behind, and that no
script contains a carriage return (the device's sh would choke on it).

Pure Python: runs the same on Windows, Linux and macOS (no sh needed).

Run:     python tests/unit/test_script_golden.py
Update:  python tests/unit/test_script_golden.py --update
"""

import json
import re
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import inventory as I
from mono_imager import snapshot as S
from mono_imager.journeys import _vyos as V
from mono_imager.journeys import usb_utils as U

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "scripts"
GOLDEN_SUFFIX = ".sh"
UPDATE_FLAG = "--update"
CR = b"\r"
CRLF = b"\r\n"
LF = b"\n"

# Fixed, realistic inputs (CLAUDE.md section 2: no magic numbers).
BASE_URL = "http://192.168.168.89:8080"
TOKEN = "0123456789abcdef0123456789abcdef"     # 32 hex digits, like a real session token
EMMC_32GB_BYTES = 31_268_536_320                # a representative 32 GB eMMC
LABEL = "armbian-20261114-2010"
SHA256_HEX_LEN = 64
FAKE_SHA = "ab" * (SHA256_HEX_LEN // 2)         # stands in for an expected digest
BLOB_BYTES = bytes(range(256)) * 3              # spans more than one printf upload script
USB_DEV = "/dev/sda"
USB_MOUNT = "/mnt/usb"
USB_OS_NAMES = ["Armbian"]
ISO_URL = BASE_URL + "/vyos.iso"
ISO_BYTES = 512 * S.MIB                         # whole number of sectors, as the builder requires
VYOS_REPORT_URL = BASE_URL + "/report?step=vyos_write"
PARTITION_TABLE = bytes(range(S.SECTOR // 8))   # any bytes: the builder printf-encodes them
EXPECT_P2 = "/dev/mmcblk0p2"
CAT_NAME = "manifest.json"
TEXT_FILES = {"uboot_env.txt": "bootcmd=run recovery\n", "inventory.txt": "SOC=LS1046A\n"}
SCRIPT_SEPARATOR = "-"                          # golden file name: <case>-<n>.sh for script lists

CHUNKS = S.plan_chunks(EMMC_32GB_BYTES)


def manifest(compression: str) -> dict:
    """A minimal manifest for CHUNKS: what the restore builders read from it."""
    return {"label": LABEL, "chunks": [
        {"index": i, "offset": off, "bytes": n, "compression": compression,
         "file": S.chunk_file(i, compression), "sha256": f"{i:0{SHA256_HEX_LEN}x}"}
        for i, off, n in CHUNKS]}


def lan_snapshot(single: bool, have_sha: bool, nor: bool) -> str:
    return S.build_lan_snapshot_script(base_url=BASE_URL, token=TOKEN, chunks=CHUNKS,
                                       have_sha=have_sha, nor=nor, single_pass=single)


def lan_restore(have_sha: bool, skip_fw: bool) -> str:
    return S.build_lan_restore_script(base_url=BASE_URL, token=TOKEN, have_sha=have_sha,
                                      ranges=S.plan_restore(manifest("raw"), skip_fw))


def usb_snapshot(compression: str, have: set, nor: bool) -> str:
    return S.build_usb_snapshot_script(label=LABEL, chunks=CHUNKS, compression=compression,
                                       have=have, nor=nor)


def usb_restore(compression: str, skip_fw: bool) -> str:
    m = manifest(compression)
    return S.build_usb_restore_script(label=LABEL, manifest=m, ranges=S.plan_restore(m, skip_fw))


def vyos_write(staged: bool) -> str:
    if not staged:
        return V.build_write_script(url=ISO_URL, dev=USB_DEV, iso_bytes=ISO_BYTES,
                                    iso_sha256=FAKE_SHA, report_url=VYOS_REPORT_URL)
    return V.build_write_script(url=ISO_URL, dev=S.EMMC_DEV, iso_bytes=ISO_BYTES,
                                iso_sha256=FAKE_SHA, report_url=VYOS_REPORT_URL,
                                seek_lba=V.STAGE_BASE_LBA, partition_table=PARTITION_TABLE,
                                expect_p2=EXPECT_P2)


# case name -> (builder function name, zero-argument producer)
Producer = Callable[[], "str | list[str]"]
CASES: dict[str, tuple[str, Producer]] = {
    "probe": ("build_probe_script", lambda: S.build_probe_script()),
    "inventory": ("build_inventory_script", lambda: I.build_inventory_script()),
    "fs_check": ("build_fs_check_script", lambda: S.build_fs_check_script()),
    "gpt_fix": ("build_gpt_fix_script", lambda: S.build_gpt_fix_script()),
    "log": ("build_log_script", lambda: S.build_log_script()),
    "dump_gpt_head": ("build_dump_script",
                      lambda: S.build_dump_script(S.EMMC_DEV, 0, S.GPT_STD_SECTORS)),
    "nor_env_dump": ("build_nor_env_dump_script", lambda: S.build_nor_env_dump_script()),
    "emmc_env_dump": ("build_emmc_env_dump_script", lambda: S.build_emmc_env_dump_script()),
    "emmc_env_write": ("build_emmc_env_write_script",
                       lambda: S.build_emmc_env_write_script(S.ENV_UPLOAD_TMP, FAKE_SHA)),
    "nor_env_write": ("build_nor_env_write_script",
                      lambda: S.build_nor_env_write_script(S.ENV_UPLOAD_TMP, FAKE_SHA)),
    "upload_blob": ("build_upload_blob_scripts",
                    lambda: S.build_upload_blob_scripts(BLOB_BYTES, S.ENV_UPLOAD_TMP)),
    "lan_hash_fw": ("build_lan_hash_script",
                    lambda: S.build_lan_hash_script(base_url=BASE_URL, token=TOKEN, start=S.FW_START,
                                                    length=S.FW_END - S.FW_START, tag="fw")),
    **{f"lan_snapshot_{'single' if sp else 'readback'}_{'sha' if hs else 'nosha'}_{'nor' if nr else 'nonor'}":
       ("build_lan_snapshot_script", partial(lan_snapshot, sp, hs, nr))
       for sp in (False, True) for hs in (True, False) for nr in (True, False)},
    **{f"lan_restore_{'sha' if hs else 'nosha'}_{'skipfw' if sk else 'full'}":
       ("build_lan_restore_script", partial(lan_restore, hs, sk))
       for hs in (True, False) for sk in (False, True)},
    **{f"usb_mount_{mode}": ("build_usb_mount_script",
                             partial(S.build_usb_mount_script, mode=mode))
       for mode in ("snapshot", "restore")},
    **{f"usb_snapshot_{name}": ("build_usb_snapshot_script", partial(usb_snapshot, c, h, nr))
       for name, c, h, nr in (("zst_nor", "zst", {"zstd"}, True), ("gz_nonor", "gz", set(), False),
                              ("gz1_nor", "gz", {"gzip1"}, True), ("raw_nonor", "raw", set(), False))},
    "usb_finalize": ("build_usb_finalize_script",
                     lambda: S.build_usb_finalize_script(label=LABEL, text_files=TEXT_FILES,
                                                         manifest_text=json.dumps(manifest("zst"),
                                                                                  sort_keys=True))),
    "usb_nor_env_save": ("build_usb_nor_env_save_script",
                         lambda: S.build_usb_nor_env_save_script(label=LABEL)),
    "usb_discard": ("build_usb_discard_script", lambda: S.build_usb_discard_script(label=LABEL)),
    "usb_cat": ("build_usb_cat_script", lambda: S.build_usb_cat_script(label=LABEL, name=CAT_NAME)),
    **{f"usb_restore_{c}_{'skipfw' if sk else 'full'}": ("build_usb_restore_script",
                                                         partial(usb_restore, c, sk))
       for c in ("zst", "raw") for sk in (False, True)},
    "usb_os_mount": ("build_mount_script",
                     lambda: U.build_mount_script(USB_DEV, USB_MOUNT, USB_OS_NAMES)),
    "vyos_stick_probe": ("build_stick_probe_script", lambda: V.build_stick_probe_script()),
    "vyos_write_stick": ("build_write_script", lambda: vyos_write(False)),
    "vyos_write_staged": ("build_write_script", lambda: vyos_write(True)),
}

# Builders that do not produce a device script (data or parsing), so need no golden file.
NOT_SCRIPTS = {"build_manifest"}
BUILDER_MODULES = (S, I, U, V)


def rendered() -> dict:
    """golden file name -> script bytes, for every case."""
    out = {}
    for case, (_builder, make) in CASES.items():
        script = make()
        if isinstance(script, list):
            for n, part in enumerate(script):
                out[f"{case}{SCRIPT_SEPARATOR}{n}{GOLDEN_SUFFIX}"] = part.encode()
        else:
            out[f"{case}{GOLDEN_SUFFIX}"] = script.encode()
    return out


def update(scripts: dict) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for stale in {p.name for p in GOLDEN_DIR.glob("*" + GOLDEN_SUFFIX)} - set(scripts):
        (GOLDEN_DIR / stale).unlink()
        print(f"  removed stale {stale}")
    for name, data in scripts.items():
        (GOLDEN_DIR / name).write_bytes(data)
    print(f"  wrote {len(scripts)} golden scripts to {GOLDEN_DIR}")


passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {label}")
        passed += 1
    else:
        print(f"  FAIL: {label}")
        failed += 1


def section(title):
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


scripts = rendered()
if UPDATE_FLAG in sys.argv:
    update(scripts)

section("every device-script builder has a golden case")
builders = {n for m in BUILDER_MODULES for n in dir(m)
            if n.startswith("build_") and callable(getattr(m, n))} - NOT_SCRIPTS
covered = {b for b, _ in CASES.values()}
check(f"all {len(builders)} builders covered (missing: {sorted(builders - covered)})",
      builders <= covered)

section("generated scripts match the golden files, byte for byte")
for name, data in scripts.items():
    ref = GOLDEN_DIR / name
    if not ref.is_file():
        check(f"{name}: golden file exists (run with {UPDATE_FLAG})", False)
        continue
    # git may check text files out with CRLF on Windows: compare with LF line endings
    check(f"{name} matches", ref.read_bytes().replace(CRLF, LF) == data)
    check(f"{name}: no carriage return in the script", CR not in data)

section("no dd option the recovery BusyBox lacks (conv= iflag= oflag= ibs= obs=)")
# CONFIRMED ON HARDWARE 2026-11-21: BusyBox 1.37.0 dd in recovery refuses them (rc 1, nothing written)
DD_UNSUPPORTED = re.compile(rb"\bdd\b[^\n|;&]*\b(conv|iflag|oflag|ibs|obs)=")
bad_dd = sorted(n for n, data in scripts.items() if DD_UNSUPPORTED.search(data))
check(f"no device script uses them (found in: {bad_dd})", not bad_dd)

section("no stale golden files")
stale = sorted({p.name for p in GOLDEN_DIR.glob("*" + GOLDEN_SUFFIX)} - set(scripts))
check(f"every golden file belongs to a case (stale: {stale})", not stale)

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
