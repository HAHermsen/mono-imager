"""
mono-imager: snapshot_orchestrator.py — the flow steps' error branches that
test_snapshot*.py leave out: reaching recovery (DIP flips, failed boots and
logins), the LAN snapshot and restore loops, publishing, USB log polling,
the opt-in NOR-env and dual-boot writes, and the health check + review.

Phases, device answers and the snapshot server session are fakes; step()
and _say() are recorded so both the verdicts and the user's messages are
checked.

Run: python tests/unit/test_snapshot_flow_steps.py
"""

import contextlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import constants as C
from mono_imager import snapshot as S
from mono_imager import snapshot_orchestrator as SO

PORT = "COM5"
LABEL = "armbian-20261114-2010"
MAC = "e8:f6:d7:00:17:df"
EMMC_BYTES = 4 * S.MIB                 # one chunk
SHA_A = "a" * S.SHA256_HEX_LEN
SHA_B = "b" * S.SHA256_HEX_LEN
ERR = RuntimeError("serial timeout")
BASE = "http://192.168.168.89:8080"
SHORT_POLL_TIMEOUT_S = 0.05
NOR_ENV_BLOB = b"nor-env"
FS_ERROR_RC = "4"                      # e2fsck: errors left uncorrected

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


def run_spinner(fn, *args, message="", status=None, **kwargs):
    """with_spinner() without the thread: (result, error) like the real one (status is not passed on)."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - mirrors with_spinner(), which captures any error
        return None, e


def device(**answers):
    """run_script(marker=m) -> answers[m]; an Exception is raised; unknown markers raise."""
    d = MagicMock()
    d.captured_uboot_env = {"ver": "U-Boot 2024.04"}

    def run_script(script, marker=None, **kw):
        a = answers.get(marker, ERR)
        if isinstance(a, Exception):
            raise a
        return a
    d.run_script.side_effect = run_script
    return d


def probe(have=("sha256sum", "od")):
    return {"emmc_bytes": EMMC_BYTES, "have": set(have), "mac": MAC, "serial": "MT-1", "model": "Mono",
            "usb_present": True, "nor_present": True, "emmc_mounted": False, "boot_medium": "qspi", "ok": True}


def capture(**kw):
    return SO.Capture(label=LABEL, probe=probe(), identity={"mac": MAC, "serial": "MT-1", "model": "Mono",
                                                            "uboot_version": "U-Boot"},
                      env={"bootcmd": "run x"}, boot_dip="EMMC", **kw)


# ----------------------------------------------------------------------------
section("reach_recovery")


def reach(dip, phases, *, boot=True, in_ram=C.BOOT_MEDIUM_EMMC, login=True):
    devs = [device() if p else None for p in phases]
    for d in devs:
        if d is not None:
            d.login_recovery.return_value = login
    soft, ask = MagicMock(), MagicMock(return_value="")
    with recorded(phase1_uboot=MagicMock(side_effect=devs), _boot=MagicMock(return_value=boot),
                  _boot_recovery_in_ram=MagicMock(return_value=in_ram), with_spinner=run_spinner) as rec:
        d, info = SO.reach_recovery(PORT, dip, soft_reboot=soft, ask=ask)
    return d, info, rec, devs, soft, ask


d, info, rec, devs, soft, ask = reach(SO.DIP_NOR, [False])
check("DIP RIGHT, U-Boot not caught -> None, no flip asked", d is None and not ask.called and soft.called)
d, info, rec, devs, soft, ask = reach(SO.DIP_EMMC, [False, False])
check("DIP LEFT, not caught, flipped, still nothing -> None", d is None and info["flipped"] and ask.called)
d, info, rec, devs, *_ = reach(SO.DIP_EMMC, [True, False], in_ram=None)
check("recovery won't start in RAM -> flip asked; nothing after flip -> None",
      d is None and info["flipped"] and devs[0].disconnect.called and rec.has_said("could not be started"))
d, info, rec, devs, *_ = reach(SO.DIP_NOR, [True], boot=False)
check("NOR recovery won't boot -> step fails, disconnected",
      d is None and rec.failed_step("Recovery Linux running from RAM") and devs[0].disconnect.called)
d, info, rec, devs, *_ = reach(SO.DIP_NOR, [True], login=False)
check("no login -> step fails, disconnected", d is None and rec.failed_step("Logged into recovery shell"))
d, info, rec, devs, *_ = reach(SO.DIP_NOR, [True])
check("NOR U-Boot, boots and logs in -> device, NOR recovery",
      d is devs[0] and info["recovery_medium"] == C.BOOT_MEDIUM_NOR and info["env"]["ver"].startswith("U-Boot"))


# ----------------------------------------------------------------------------
section("_capture_env / _boot_recovery_in_ram / _load_nor_recovery_fixed")
d = MagicMock()
d.captured_uboot_env = None
d.send_command.side_effect = OSError("no prompt")
with recorded():
    check("empty capture, retry fails -> {}", SO._capture_env(d) == {})

d = MagicMock()
d.send_command.side_effect = OSError("no prompt")
with recorded(_boot=MagicMock(return_value=True)):
    check("prompt re-sync fails (ignored), eMMC recovery boots -> eMMC", SO._boot_recovery_in_ram(d) == C.BOOT_MEDIUM_EMMC)
with recorded(_boot=MagicMock(return_value=False)):
    check("eMMC recovery fails, NOR preparation raises -> None", SO._boot_recovery_in_ram(d) is None)


def uboot(d, cmd, timeout):
    return C.FIT_MAGIC_HEX if S.RAM_DTB in cmd else "00 00"


with recorded(_uboot_cmd=uboot):
    check("DTB found but no gzip kernel at NOR -> False", SO._load_nor_recovery_fixed(MagicMock()) is False)


# ----------------------------------------------------------------------------
section("snapshot_lan")


class Sess:
    """Snapshot-server session: wait(kind, key) answers from a table."""
    token = "t" * 32

    def __init__(self, table):
        self.table = table

    def begin_snapshot(self, partial):
        self.partial = partial

    def begin_restore(self, folder, manifest):
        pass

    def wait(self, kind, key, timeout):
        return self.table.get((kind, key))


GOOD = {("verify", "fw"): {"bytes": S.FW_END - S.FW_START, "sha256": SHA_A},
        ("verify", "boot"): {"bytes": S.FW_START, "sha256": SHA_B},
        ("uploads", 0): {"bytes": EMMC_BYTES, "sha256": SHA_A, "file": "c0.raw", "compression": "raw"}}


def lan_snapshot(table, *, launch=None, report=f"UP_RC=0\nVSHA={SHA_A}", nor=False, nor_report="NOR_RC=1"):
    d = MagicMock()
    if launch:
        d.launch_script.side_effect = launch
    reports = {"snap_0": report, "snap_nor": nor_report}
    with tempfile.TemporaryDirectory() as tmp, \
         recorded(with_spinner=run_spinner, wait_for_report=lambda step, timeout=None: reports.get(step)) as rec:
        m = SO.snapshot_lan(d, Sess(table), BASE, Path(tmp), capture(nor_dump=nor))
        left = sorted(p.name for p in Path(tmp).iterdir())
    return m, rec, left


m, rec, left = lan_snapshot(GOOD, launch=ERR)
check("launch fails -> None, nothing left on the PC", m is None and rec.failed_step("Snapshot started") and left == [])
m, rec, left = lan_snapshot({k: v for k, v in GOOD.items() if k != ("verify", "boot")})
check("boot region not hashed -> None", m is None and rec.failed_step("Firmware region hashed"))
m, rec, left = lan_snapshot(GOOD, report=None)
check("no chunk report -> None", m is None and "no report from the device" in rec.steps[-1][2])
m, rec, left = lan_snapshot(GOOD, nor=True)
check("NOR dump fails -> optional step fails, snapshot still saved",
      m is not None and rec.failed_step("NOR dump (optional)") and left == [LABEL])


# ----------------------------------------------------------------------------
section("_publish_host / _poll_log")
with tempfile.TemporaryDirectory() as tmp:
    partial, final = Path(tmp) / "p.partial", Path(tmp) / LABEL
    partial.mkdir()
    final.mkdir()
    with recorded() as rec:
        res = SO._publish_host(partial, final, {"label": LABEL}, capture())
    check("target folder already exists -> None, kept as .partial", res is None and partial.exists()
          and rec.failed_step("Snapshot saved"))

d = device(snap_log=ERR)
d.run_script.side_effect = [ERR, "C_0=0 x x\nSNAP_DONE=1"]
with recorded(USB_POLL=0) as rec:
    kv = SO._poll_log(d, "SNAP_DONE", "C", 1, "chunks", timeout=SHORT_POLL_TIMEOUT_S * 100)
check("log read error, then done -> done log, progress shown", kv.get("SNAP_DONE") == "1" and rec.has_said("chunks: 1/1"))
with recorded(USB_POLL=0):
    kv = SO._poll_log(device(snap_log="C_0=0 x x"), "SNAP_DONE", "C", 2, "chunks", timeout=SHORT_POLL_TIMEOUT_S)
check("never done -> TIMEOUT flagged", kv.get("TIMEOUT") == "1")


# ----------------------------------------------------------------------------
section("_restore_lan")
RANGE = [{"chunk": 0, "off": 0, "len": EMMC_BYTES, "abs": 0}]
SENT: dict = {("sent", 0): {"bytes": EMMC_BYTES, "sha256": SHA_A}}
HOST_MISMATCH: dict = {**SENT, ("verify", "r0"): {"bytes": EMMC_BYTES, "sha256": SHA_B}}
for what, launch, report, table, want in (
        ("launch fails", ERR, None, SENT, "could not start the write"),
        ("no report", None, None, SENT, "no report from the device for range 0"),
        ("curl failed", None, "CURL_RC=7\nDD_RC=0", SENT, "transfer/write failed (curl 7, dd 0)"),
        ("PC could not serve", None, "CURL_RC=0\nDD_RC=0\nVSHA=x", {}, "PC could not serve it (no data)"),
        ("chunk fails its manifest hash", None, "CURL_RC=0\nDD_RC=0\nVSHA=x",
         {("sent", 0): {"bytes": EMMC_BYTES, "sha256": SHA_A, "manifest_ok": False}},
         "chunk 0 does not match its manifest hash"),
        ("host re-hash differs", None, "CURL_RC=0\nDD_RC=0\nVSHA=host",
         HOST_MISMATCH, "re-hash does not match")):
    d = MagicMock()
    if launch:
        d.launch_script.side_effect = launch
    with recorded(with_spinner=run_spinner, wait_for_report=lambda step, timeout=None, r=report: r):
        ok, why = SO._restore_lan(d, Sess(table), BASE, Path("."), {}, RANGE, have_sha=False)
    check(f"{what} -> refused: {want}", ok is False and want in why)


# ----------------------------------------------------------------------------
section("offer_nor_env_restore")
MAN = {"nor_env": {"sha256": SHA_A, "file": S.NOR_ENV_FILE}}


def nor_offer(man=MAN, have=("flashcp", "sha256sum"), answer="y", transfer="usb", **answers):
    d = device(**answers)
    with recorded() as rec:
        res = SO.offer_nor_env_restore(d, transfer=transfer, sess=Sess({}), base_url=BASE, folder=None,
                                       label=LABEL, manifest=man, snap_vars={"bootcmd": "run x"},
                                       current_blob=NOR_ENV_BLOB, have=set(have), ask=lambda q: answer)
    return res, rec, d


res, rec, d = nor_offer(man={})
check("snapshot has no NOR env -> False, nothing asked", res is False and not rec.said)
res, rec, d = nor_offer(have=("sha256sum",))
check("flashcp missing -> False, explained", res is False and rec.has_said("flashcp/sha256sum missing"))
res, rec, d = nor_offer(snap_norenv_write="NOR_ENV_WRITE=ok")
check("USB: written from the stick copy, read back OK -> True",
      res is True and f"{S.USB_ROOT}/{LABEL}/{S.NOR_ENV_FILE}" in d.run_script.call_args.args[0])
res, rec, d = nor_offer(transfer="lan", snap_norenv_write=ERR)
check("write raises -> False, error as reason", res is False and rec.failed_step("NOR U-Boot env written"))


# ----------------------------------------------------------------------------
section("offer_dual_boot")


def region(vars_, redundant=False):
    return SimpleNamespace(vars=vars_, redundant=redundant, flag=None, size=S.NOR_ENV_SIZE)


SRC = region({"bootcmd": "run armbian", "armbian": "load mmc 0 ..."})


def dual(dst, *, have=("sha256sum", "flashcp"), answer="y", build=b"env", upload=True, **answers):
    with recorded(read_emmc_env=MagicMock(return_value=b"e"), read_nor_env=MagicMock(return_value=b"n"),
                  _upload=MagicMock(return_value=upload)) as rec, \
         patch.object(SO.E, "parse", side_effect=[SRC, dst]), \
         patch.object(SO.E, "build", side_effect=build if isinstance(build, Exception) else None,
                      return_value=build):
        res = SO.offer_dual_boot(device(**answers), {"boot_dip": SO.DIP_EMMC}, have=set(have), ask=lambda q: answer)
    return res, rec


res, rec = dual(region(dict(SRC.vars)))
check("envs already agree -> True, nothing written", res is True and rec.steps[-1][1] and "already agree" in rec.steps[-1][0])
res, rec = dual(region({}, redundant=True))
check("redundant NOR env -> not offered", res is False and rec.has_said("redundant env"))
res, rec = dual(region({}), have=("sha256sum",))
check("flashcp missing for NOR -> not offered", res is False and rec.has_said("flashcp missing"))
res, rec = dual(region({}), answer="n")
check("user declines -> False, nothing written", res is False and not rec.steps)
res, rec = dual(region({}), build=ValueError("env too big"))
check("env does not fit -> step fails with the reason", res is False and rec.steps[-1][2] == "env too big")
res, rec = dual(region({}), upload=False)
check("upload fails -> False", res is False and rec.failed_step("New NOR env sent"))
res, rec = dual(region({}), snap_dual_boot=ERR)
check("write raises -> False", res is False and rec.failed_step("NOR env updated"))


# ----------------------------------------------------------------------------
section("health_check / review_health")
FS_OK = "FSCHECK=done\nFS_1=ext 0\nFS_2=other nocheck"
with recorded():
    h = SO.health_check(device(snap_fsck=FS_OK), probe(have=("sha256sum",)))
check("od missing -> GPT unchecked, warn (never 'ok')", h["status"] == "warn" and any("od missing" in n for n in h["notes"]))
with recorded():
    h = SO.health_check(device(snap_gpt_head=ERR, snap_fsck=ERR), probe())
check("GPT and fsck unreadable -> warn, both noted",
      h["status"] == "warn" and len(h["notes"]) == 2 and h["filesystems"] == {})
head, tail = b"\0" * (S.GPT_STD_SECTORS * S.SECTOR), b"\0" * S.SECTOR
with recorded(), patch.object(S, "parse_od", side_effect=[head, tail]), \
     patch.object(S, "check_gpt", return_value={"status": "ok", "findings": [("ok", "GPT valid")], "partitions": 5}):
    h = SO.health_check(device(snap_gpt_head="h", snap_gpt_tail="t",
                               snap_fsck=f"FSCHECK=done\nFS_1=ext {FS_ERROR_RC}"), probe())
check("GPT ok, a filesystem with errors -> error", h["status"] == "error" and h["gpt"]["partitions"] == 5
      and h["filesystems"]["1"]["level"] == "error")
for answer, expected in (("y", True), ("n", False)):
    with recorded() as rec:
        go = SO.review_health(h, lambda q, a=answer: a)
    check(f"errors found, answer {answer!r} -> {expected}", go is expected and rec.has_said(f"errors found (rc {FS_ERROR_RC})"))
h_none: dict = {"status": "ok", "gpt": None, "notes": [], "filesystems": {"3": {"type": "vfat", "result": "notool", "level": "none"}}}
with recorded() as rec:
    go = SO.review_health(h_none, lambda q: "n")
check("no fsck tool -> shown as not checked, goes ahead", go is True and rec.has_said("not checked (no fsck tool)"))


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
