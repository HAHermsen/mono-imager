"""
mono-imager: snapshot_orchestrator.py — the device-step helpers' error and
edge branches that test_snapshot*.py leave out: probing, finishing and
stopping the device, the USB snapshot / manifest / restore steps, the
firmware-region hash, env and inventory reads, clock sync and blob upload.

The device is a fake whose run_script() answers per marker (or raises).
The USB log poller is patched to hand back a prepared log, so every
verdict branch is reached without a stick. step() and _say() are recorded.

Run: python tests/unit/test_snapshot_device_steps.py
"""

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import recovery_orchestrator as R
from mono_imager import snapshot as S
from mono_imager import snapshot_orchestrator as SO

LABEL = "armbian-20261114-2010"
EMMC_BYTES = 4 * S.MIB                 # one chunk
SHA_A = "a" * S.SHA256_HEX_LEN
SHA_B = "b" * S.SHA256_HEX_LEN
NOR_ENV = b"\x00" * 16 + b"bootcmd=run x\x00"
ERR = RuntimeError("serial timeout")
MAC = "e8:f6:d7:00:17:df"

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


def device(**answers):
    """run_script(marker=m) -> answers[m]; an Exception is raised; unknown markers raise."""
    d = MagicMock()

    def run_script(script, marker=None, **kw):
        a = answers.get(marker, ERR)
        if isinstance(a, Exception):
            raise a
        return a
    d.run_script.side_effect = run_script
    return d


class Rec:
    """Records step() and _say() calls made by snapshot_orchestrator."""
    def __init__(self):
        self.steps, self.said = [], []

    def step(self, n, text, ok, reason=""):
        self.steps.append((text, ok, reason))
        return ok

    def has_said(self, text):
        return any(text in s for s in self.said)

    def failed_step(self, text):
        return any(text in s and not ok for s, ok, _ in self.steps)


@contextlib.contextmanager
def recorded(**patches):
    rec = Rec()
    with patch.object(SO, "step", rec.step), patch.object(SO, "_say", rec.said.append), \
         patch.object(SO, "_banner", lambda *lines: rec.said.extend(lines)), contextlib.ExitStack() as es:
        for name, value in patches.items():
            es.enter_context(patch.object(SO, name, value))
        yield rec


def probe(have=("sha256sum", "gzip")):
    return {"emmc_bytes": EMMC_BYTES, "have": set(have), "mac": MAC, "serial": "MT-1", "model": "Mono",
            "usb_present": True, "nor_present": True, "emmc_mounted": False, "boot_medium": "qspi", "ok": True}


def capture(**kw):
    return SO.Capture(label=LABEL, probe=probe(), identity={"mac": MAC, "serial": "MT-1", "model": "Mono",
                                                            "uboot_version": "U-Boot"},
                      env={"bootcmd": "run x"}, boot_dip="EMMC", **kw)


# ----------------------------------------------------------------------------
section("probe_device")
good = f"PROBE=done\nEMMC_BYTES={EMMC_BYTES}\nHAVE_OD=1\nMAC={MAC}"
p = SO.probe_device(device(snap_probe1=ERR, snap_probe2=good))
check("first attempt fails, second answers -> probe", p is not None and p["emmc_bytes"] == EMMC_BYTES and "od" in p["have"])
check("no eMMC size -> None", SO.probe_device(device(snap_probe1="PROBE=done", snap_probe2="PROBE=done")) is None)
check("never answers -> None", SO.probe_device(device()) is None)


# ----------------------------------------------------------------------------
section("_finish_device / _stop_device_job")
d = MagicMock()
d.ser.write.side_effect = OSError("port closed")
with recorded() as rec:
    SO._finish_device(d, {}, SO.DIP_EMMC, any_dip=True)
check("write error ignored; any DIP -> 'either position'", rec.has_said("either position"))
with recorded() as rec:
    SO._finish_device(MagicMock(), {"flipped": True, "dip_now": SO.DIP_EMMC}, SO.DIP_EMMC)
check("DIP was flipped -> tells where to set it back", rec.has_said(f"SET THE DIP SWITCH TO {SO.DIP_TEXT[SO.DIP_EMMC]}"))
with recorded() as rec:
    SO._finish_device(MagicMock(), {"dip_now": SO.DIP_EMMC}, SO.DIP_EMMC)
check("DIP already right -> 'DIP stays'", rec.has_said(f"DIP stays {SO.DIP_TEXT[SO.DIP_EMMC]}"))
SO._stop_device_job(d)
check("stop job: write error ignored", True)


# ----------------------------------------------------------------------------
section("usb_mount")
check("mount script fails -> None", SO.usb_mount(device(), "snapshot") is None)
check("no partition mounted -> None", SO.usb_mount(device(snap_mount="MOUNT=done"), "snapshot") is None)
m = SO.usb_mount(device(snap_mount="MOUNT=done\nUSB_PART=/dev/sda1\nSNAP_0=old"), "restore")
check("mounted -> partition and existing snapshots", m is not None and m["part"] == "/dev/sda1" and m["snapshots"] == ["old"])


# ----------------------------------------------------------------------------
section("snapshot_usb")
DONE_KV = {"SNAP_DONE": "1", "C_0": f"0 {SHA_A} {SHA_A}", "FW_SHA": SHA_A, "BOOT_SHA": SHA_B, "NOR": f"0 {SHA_B}"}


def usb_snap(kv=None, cap=None, **answers):
    with recorded(_poll_log=MagicMock(return_value=dict(kv if kv is not None else DONE_KV))) as rec:
        m = SO.snapshot_usb(device(**answers), cap or capture())
    return m, rec


m, rec = usb_snap(snap_usb=ERR)
check("start fails -> None", m is None and rec.failed_step("Snapshot started on the device"))
m, rec = usb_snap(snap_usb="mkdir: read-only file system")
check("start not confirmed -> None, device output as reason", m is None and rec.steps[-1][2].endswith("read-only file system"))
for what, kv, why in (("chunk 3 failed", {"FAILED": "3"}, "failed at chunk 3"),
                      ("timed out", {"TIMEOUT": "1"}, "timed out"),
                      ("read-back mismatch", dict(DONE_KV, C_0=f"0 {SHA_A} {SHA_B}"), "failed at chunk ?")):
    m, rec = usb_snap(kv, snap_usb="STARTED=1", snap_discard=ERR)
    check(f"{what} -> discarded (discard error ignored), reason '{why}'",
          m is None and rec.failed_step("Snapshot on the USB stick") and why in rec.steps[-1][2])
cap = capture(nor_dump=True, nor_env=NOR_ENV)
m, rec = usb_snap(cap=cap, snap_usb="STARTED=1", snap_norenv=ERR, snap_final="FINAL=ok")
check("NOR env copy unreadable on the stick -> not kept, snapshot still saved",
      m is not None and cap.nor_env is None and rec.failed_step("NOR U-Boot env saved"))
check("NOR dump recorded from the log", m is not None and m["nor_dump"]["sha256"] == SHA_B)
check("chunk recorded with the device's hash", m is not None and m["chunks"][0]["sha256"] == SHA_A)
for what, fin in (("finalize raises", ERR), ("finalize not confirmed", "sha256sum: write error")):
    m, rec = usb_snap(snap_usb="STARTED=1", snap_final=fin)
    check(f"{what} -> None", m is None and rec.failed_step("Snapshot saved on the USB stick"))


# ----------------------------------------------------------------------------
section("usb_read_manifest")


def cat(text):
    return "".join(f"L{i}={line}\n" for i, line in enumerate(text.splitlines()))


check("manifest unreadable -> (None, {})", SO.usb_read_manifest(device(), LABEL) == (None, {}))
man = {"label": LABEL, "uboot_env_file": "uboot_env.txt"}
m, env = SO.usb_read_manifest(device(snap_cat=cat(json.dumps(man)), snap_cat_env=ERR), LABEL)
check("env file unreadable -> manifest, empty env", m == man and env == {})
m, env = SO.usb_read_manifest(device(snap_cat=cat(json.dumps(man)), snap_cat_env=cat("bootcmd=run x")), LABEL)
check("env file read -> parsed env", env.get("bootcmd") == "run x")


# ----------------------------------------------------------------------------
section("restore_usb")
RANGES = [{"chunk": 0, "off": 0, "len": EMMC_BYTES, "abs": 0}]
MANIFEST = {"chunks": [{"index": 0, "offset": 0, "bytes": EMMC_BYTES, "compression": "raw",
                        "file": S.chunk_file(0, "raw"), "sha256": SHA_A}]}
OK_KV = {"SUMS": "ok", "RESTORE_DONE": "1", "R_0": f"0 {SHA_A} {SHA_A}"}
for what, ans, kv, want in (
        ("start fails", ERR, OK_KV, "could not start"),
        ("start not confirmed", "nope", OK_KV, "could not start"),
        ("SHA256SUMS bad", "STARTED=1", {"SUMS": "bad"}, "SHA256SUMS check on the stick: bad"),
        ("range failed", "STARTED=1", dict(OK_KV, FAILED="0"), "failed at range 0"),
        ("timed out", "STARTED=1", {"SUMS": "ok", "TIMEOUT": "1"}, "(timed out)"),
        ("re-hash mismatch", "STARTED=1", dict(OK_KV, R_0=f"0 {SHA_A} {SHA_B}"), "re-hash mismatch in range(s) [0]")):
    with patch.object(SO, "_poll_log", return_value=dict(kv)):
        ok, why = SO.restore_usb(device(snap_urestore=ans), LABEL, MANIFEST, RANGES)
    check(f"{what} -> refused: {want}", ok is False and want in why)
with patch.object(SO, "_poll_log", return_value=dict(OK_KV)):
    check("all ranges written and re-hashed -> OK", SO.restore_usb(device(snap_urestore="STARTED=1"), LABEL, MANIFEST, RANGES) == (True, ""))


# ----------------------------------------------------------------------------
section("firmware-region hash, env/inventory reads, clock, upload")
check("fw hash (USB): script fails -> None", SO._device_fw_sha_usb(device()) is None)
check("fw hash (USB): short answer -> None", SO._device_fw_sha_usb(device(snap_curfw="CURFW=abc")) is None)
check("fw hash (USB): full digest -> it", SO._device_fw_sha_usb(device(snap_curfw=f"CURFW={SHA_A}")) == SHA_A)
d = MagicMock()
d.launch_script.side_effect = ERR
check("fw hash (LAN): launch fails -> None", SO._device_fw_sha_lan(d, MagicMock(), "http://h:1") is None)
check("NOR env read fails -> None", SO.read_nor_env(device()) is None)
check("eMMC env read fails -> None", SO.read_emmc_env(device()) is None)
d = device()
d.last_boot_output = None
with recorded() as rec:
    inv = SO.collect_inventory(d, probe(), ntp_synced=False)
check("inventory unreadable -> partial inventory, noted", inv["complete"] is False and "partial" in rec.steps[-1][2])
with patch.object(R, "sync_device_clock", side_effect=OSError("ntp")):
    check("clock sync raises -> False", SO._sync_clock(MagicMock()) is False)
check("blob upload fails -> False", SO._upload(device(), NOR_ENV, S.ENV_UPLOAD_TMP) is False)


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
