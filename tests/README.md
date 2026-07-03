# mono-imager tests

Tests are organised into four folders by what they need and what they risk.

```
tests/
  unit/          No hardware. Fast. Run on every commit.
  hardware/      Real device required. Non-destructive. Safe to run anytime.
  destructive/   Real device required. Writes to hardware. Run deliberately.
  archive/       Historical investigation scripts. Not maintained.
```

---

## unit/

No hardware required. All device interactions are mocked.

```
python tests/unit/test_recovery_orchestrator.py
python tests/unit/test_recovery_fallback.py
python tests/unit/test_journey_resolution.py
python tests/unit/test_device_network.py
python tests/unit/test_usb_mount.py
python tests/unit/test_uboot_env.py
python tests/unit/test_flash_orchestrator.py
python tests/unit/test_step_registry.py
python tests/unit/test_config.py
python tests/unit/test_journeys_common.py
```

| File | What it tests |
|------|--------------|
| `test_recovery_orchestrator.py` | `recovery_orchestrator.py` pure-logic functions: `detect_modern_firmware_tool`, `get_device_mac`, `run_firmware_update`, `_stream_command`, `check_internet_reachable`, `try_dhcp`, `legacy_flash_emmc/nor`, `verify_boot_source` |
| `test_recovery_fallback.py` | Recovery fallback logic: 5 scenarios covering modern→legacy eMMC fallback, modern→legacy NOR fallback, and full modern success |
| `test_uboot_env.py` | `parse_uboot_env`/`capture_uboot_env`/`restore_uboot_env` (issue #8) and `SerialDevice.probe_uboot_prompt()` already-at-prompt detection (issue #12) |
| `test_flash_orchestrator.py` | `flash_orchestrator.py` phase implementations and helpers: result tracking (`step`/`reset_results`/`print_report`), `parse_active_eth_iface`, the `/report` store (`wait_for_report`/`peek_report`), `start_http_server`, `phase1_uboot`/`phase1_recovery`/`phase1_bootstrap` gating, `phase3_flash` step09-12 gating and buffered-vs-streaming script selection at `FLASH_SIZE_CAP`, `phase4_postflash` |
| `test_step_registry.py` | `FlowRunner.run()` execution engine: success path + ordering, missing-requires abort, exception-in-step handling, produces-marking (default vs. explicit), stop-on-first-failure; `register_uboot_steps`/`run_uboot_steps`; `register_staging_boot`/`get_staging_boot_methods` |
| `test_config.py` | `config.py`: `load_config`/`save_config` roundtrip, corrupt-JSON recovery, OSError-on-write handling, `save_last_port`/`get_last_port`, `is_known_uart` |
| `test_journeys_common.py` | `journeys/_common.py`'s `_step_network_ready()`: fails when `device_net` unresolved/empty, forwards `device_ip` from `device_net` without clobbering an already-set value |
| `test_journey_resolution.py` | Step registry: all 6 journeys resolve to the correct step sequence, OS/transfer isolation, dependency ordering, no circular dependencies |
| `test_device_network.py` | `MonoImager._setup_recovery_network()` (issue #9): DHCP-first, manual fallback (IP/mask/gateway/DNS), session-cache reuse across calls, stale-cache re-resolution, manual-entry retry loop |
| `test_usb_mount.py` | `MonoImager.menu_test_usb_mount()`: partitioned-device mount, bare-device fallback, mount failure, three-way OS image scan (pass/fail), unmount-always, serial port persistence |

Run all unit tests:
```bash
for f in tests/unit/test_*.py; do python $f; done
```

---

## hardware/

Requires a Mono Gateway connected via USB-to-UART. Nothing is written.

DIP switch should be on **NOR** for all tests unless noted.

```
python tests/hardware/test_serial_connect.py --port COM5
python tests/hardware/test_serial_hotplug.py --port COM5 --mode simulated
python tests/hardware/test_serial_hotplug.py --port COM5 --mode interactive
python tests/hardware/test_uboot_dump.py --port COM5
python tests/hardware/test_uboot_dump.py --port COM5 --section printenv
python tests/hardware/test_emmc_inspect.py --port COM5
python tests/hardware/test_emmc_inspect.py --port COM5 --firmware-region
python tests/hardware/test_recovery_detect.py --port COM5
python tests/hardware/test_recovery_dryrun.py --port COM5
```

| File | What it tests | Manual action required |
|------|--------------|----------------------|
| `test_serial_connect.py` | Serial detection, connection, U-Boot interrupt, recovery boot | None — soft reboot via serial |
| `test_serial_hotplug.py` | USB disconnect/reconnect resilience | `--mode interactive` requires physical unplug/replug |
| `test_uboot_dump.py` | Full U-Boot environment dump: version, printenv, mmc info, bdinfo, i2c probe, boot source | None — soft reboot via serial |
| `test_emmc_inspect.py` | eMMC partition table, filesystem signatures, first 512 bytes | None — soft reboot via serial |
| `test_recovery_detect.py` | `detect_modern_firmware_tool()` and device MAC address | None — soft reboot via serial |
| `test_recovery_dryrun.py` | Full recovery sequence flow and timing | Two DIP switch flips (physical — cannot be automated) |

Run before a flash session as a pre-flight check:
```bash
python tests/hardware/test_serial_connect.py --port COM5
python tests/hardware/test_recovery_detect.py --port COM5
```

---

## destructive/

Requires a real device. **Writes to hardware.** Run deliberately, not as part of CI.

```
python tests/destructive/test_firmware_update.py --port COM5
```

| File | What it writes | Notes |
|------|---------------|-------|
| `test_firmware_update.py` | eMMC firmware region (first 32MB) | Runs real `firmware update` command. Does not touch OS partition. |

Each destructive test requires explicit confirmation (`Type 'yes' to proceed`) before anything is sent to the device.

---

## archive/

Historical investigation scripts from development. Kept for reference — they answered specific questions that are now resolved and baked into the main codebase.

Not maintained. May require adjustment to run against current code.

| File | Original question it answered |
|------|------------------------------|
| `test_probe_fdisk.py` | What are the exact BusyBox fdisk prompt strings on this device? |
| `test_probe_step6_emmc_recovery.py` | Does `run recovery` succeed from eMMC before a firmware update? |
| `test_check_current_device_state.py` | What is the device's current state (passive read-only)? |

**Key findings from these investigations** (now in the codebase):
- `run_script()` + `launch_script()` fire-and-forget + TCP/IP report-back is 100% reliable
- Direct serial stdout capture of curl output is intermittently unreliable (~50% failure rate)
- BusyBox dd does not support `status=progress` (use SIGUSR1 for progress, or skip)
- Recovery shell on NOR does NOT have a `firmware update` command on older devices

---

## Logs

All tests write timestamped logs to `logs/` at the project root.

```
logs/
  test_serial_connect_20260625_184201.log
  test_recovery_detect_20260625_184312.log
  test_firmware_update_20260625_185001.log
  ...
```

---

## Adding a test

**Unit test for a new journey step:**
Add assertions to `tests/unit/test_journey_resolution.py` — update `EXPECTED` with the new step label and add isolation checks if the step is OS- or transfer-specific.

**Unit test for new orchestrator logic:**
Add `check()` calls to `tests/unit/test_recovery_orchestrator.py` following the same pattern. Use `MagicMock` for the device.

**Hardware test for a new feature:**
Add to `tests/hardware/`. Follow the pattern: `phase1_bootstrap()` to get to recovery shell, run read-only checks, `d.disconnect()`.
