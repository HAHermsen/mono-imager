"""
mono-imager: recovery_orchestrator.py — the update flows' branches that
test_recovery_orchestrator.py and test_firmware_update_ladder.py leave
out: the modern/legacy phase wrappers, the env-restore after a legacy
flash, the y/N prompt on EOF, the legacy runner's error path, every early
exit of the shared eMMC/NOR update flow, and the recovery report.

Device work below the flow is patched; the step results and console lines
are the real ones.

Run: python tests/unit/test_recovery_flows.py
"""

import contextlib
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import flash_orchestrator as FO
from mono_imager import recovery_orchestrator as R

MAC = "e8:f6:d7:00:17:df"
ENV_DEV = "/dev/mtd3"
PORT = "COM5"
CORE_VERDICT = "core-report"            # stands in for flash_orchestrator.print_report()'s bool
MEDIUMS = ("emmc", "nor")

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


class Logs(logging.Handler):
    """Collects every mono_imager log/console line while active."""
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def has(self, text):
        return any(text in line for line in self.lines)


@contextlib.contextmanager
def captured():
    h, root = Logs(), logging.getLogger("mono_imager")
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        yield h
    finally:
        root.removeHandler(h)
        root.setLevel(old)


def run_spinner(fn, *args, message="", **kwargs):
    """with_spinner() without the thread: (result, error) like the real one."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - mirrors with_spinner(), which captures any error
        return None, e


def last_step():
    return R.results[-1]


# ----------------------------------------------------------------------------
section("modern phases record their step")
d = MagicMock()
for flash_fn, num, flag in ((R.phase_modern_flash_emmc, 1, True), (R.phase_modern_flash_nor, 3, False)):
    R.reset_results()
    with patch.object(R, "run_firmware_update", return_value=flag) as fw:
        ok = flash_fn(d, preserve_env=True)
    check(f"{flash_fn.__name__}: step {num} = firmware update result ({flag})",
          ok is flag and last_step()[0] == num and last_step()[2] is flag and fw.call_args.kwargs["preserve_env"])
for verify_fn, num, medium in ((R.phase_modern_verify_emmc_boot, 2, "EMMC"), (R.phase_modern_verify_nor_boot, 4, "NOR")):
    R.reset_results()
    with patch.object(R, "verify_boot_source", return_value=True) as vb:
        ok = verify_fn(d)
    check(f"{verify_fn.__name__}: step {num}, checks {medium} boot", ok and last_step()[0] == num and vb.call_args.args[1] == medium)


# ----------------------------------------------------------------------------
section("legacy phases")
for legacy_fn, num, flasher, target in ((R.phase_legacy_flash_emmc, 1, "legacy_flash_emmc", "emmc"),
                                 (R.phase_legacy_flash_nor, 2, "legacy_flash_nor", "qspi")):
    R.reset_results()
    with patch.object(R, "get_device_mac", return_value=None):
        ok = legacy_fn(d)
    check(f"{legacy_fn.__name__}: no MAC -> step {num} fails with the reason",
          ok is False and last_step()[0] == num and "could not determine device MAC" in last_step()[3])
    R.reset_results()
    with patch.object(R, "get_device_mac", return_value=MAC), patch.object(R, flasher, return_value=True) as fl, \
         patch.object(R, "restore_uboot_env_region", return_value=True) as rst, captured() as log:
        ok = legacy_fn(d, env_dev=ENV_DEV)
    check(f"{legacy_fn.__name__}: flashes with the MAC, then restores the env",
          ok and fl.call_args.args[1] == MAC and rst.call_args.args[1:] == (target, ENV_DEV)
          and log.has(f"U-Boot env restored ({ENV_DEV})"))

with patch.object(R, "restore_uboot_env_region", return_value=False), captured() as log:
    ok = R._legacy_flash_with_env(d, "qspi", lambda: True, ENV_DEV)
check("flash OK but env restore fails -> flash still OK, user warned",
      ok is True and log.has("restoring the U-Boot env") and log.has("FAILED"))
with patch.object(R, "restore_uboot_env_region") as rst:
    R._legacy_flash_with_env(d, "qspi", lambda: False, ENV_DEV)
check("flash failed -> no env restore attempted", not rst.called)


# ----------------------------------------------------------------------------
section("_ask_yes_no / _run_legacy")


def eof(prompt=""):
    raise EOFError


with patch("builtins.input", eof):
    check("EOF (no keyboard) -> default Yes", R._ask_yes_no("Continue?", True) is True)
    check("EOF (no keyboard) -> default No", R._ask_yes_no("Continue?", False) is False)

cfg = dict(R._MEDIUM_CONFIG["emmc"], legacy_flash=MagicMock(side_effect=OSError("serial gone")))
with patch.object(R, "with_spinner", run_spinner), captured() as log:
    ok = R._run_legacy(d, cfg, preserve_env=False)
check("legacy flash raises -> False, logged", ok is False and log.has("legacy flash raised: serial gone"))


# ----------------------------------------------------------------------------
section("_run_medium_update early exits")


def medium_update(medium="emmc", *, bootstrap=True, detect=True, detect_raises=False, network=True,
                  legacy_ok=True):
    dev = MagicMock() if bootstrap else None
    detector = MagicMock(side_effect=OSError("probe failed")) if detect_raises else MagicMock(return_value=detect)
    with patch.object(FO, "phase1_bootstrap", return_value=dev) as boot, \
         patch.object(FO, "print_report", return_value=CORE_VERDICT), \
         patch.object(R, "with_spinner", run_spinner), \
         patch.object(R, "detect_modern_firmware_tool", detector), \
         patch.object(R, "ask_preserve_env", return_value=False), \
         patch.object(R, "_run_legacy", return_value=legacy_ok) as leg, captured() as log:
        res = R._run_medium_update(medium, PORT, lambda _d: network)
    return res, log, dev, boot, leg


res, log, dev, boot, _ = medium_update(bootstrap=False)
check("no recovery shell -> flash_orchestrator's report, explained",
      res == CORE_VERDICT and log.has("Could not bootstrap into the recovery shell"))
check("eMMC update boots NOR recovery (boot_medium=qspi)", boot.call_args.kwargs["boot_medium"] == "qspi")

for what, kw in (("detection raises", {"detect_raises": True}), ("detection unsure", {"detect": None})):
    res, log, dev, _, leg = medium_update(**kw)
    check(f"{what} -> NOK report, explained, disconnected",
          res is False and log.has("Could not determine the device's firmware tool type")
          and dev.disconnect.called and not leg.called)

res, log, dev, _, leg = medium_update(network=False)
check("network fails -> NOK report, nothing flashed", res is False and not leg.called and dev.disconnect.called)

res, log, dev, _, leg = medium_update("nor", detect=False, legacy_ok=False)
check("legacy tool, flash fails -> NOK report", res is False and leg.called)
check("NOR legacy path notes no DIP flip is needed", log.has("No DIP-switch flip needed"))


# ----------------------------------------------------------------------------
section("run_emmc_update / run_nor_update")
for update_fn, medium in ((R.run_emmc_update, "emmc"), (R.run_nor_update, "nor")):
    with patch.object(R, "_run_medium_update", return_value=True) as mu:
        res = update_fn(PORT, MagicMock(), MagicMock())
    check(f"{update_fn.__name__} -> shared flow for {medium}", res is True and mu.call_args.args[:2] == (medium, PORT))


# ----------------------------------------------------------------------------
section("print_report")
R.reset_results()
with captured() as log:
    res = R.print_report()
check("no steps at all -> NOK (nothing was proven)", res is False and log.has("Result: NOK (0/0"))

R.reset_results()
R.step(1, "Flash eMMC via 'firmware update'", True)
R.step(2, "Verify eMMC boot", False, "no boot marker")
with captured() as log:
    res = R.print_report()
check("a failed step -> NOK, failed step listed with its reason",
      res is False and log.has("Result: NOK (1/2") and log.has("  - Verify eMMC boot") and log.has("no boot marker"))

R.reset_results()
for medium in MEDIUMS:
    R.step(1, f"Flash {medium}", True)
with captured() as log:
    res = R.print_report()
check("all steps pass -> OK", res is True and log.has("Recovery completed successfully"))


print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
