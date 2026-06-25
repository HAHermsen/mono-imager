# Adding Journeys to mono-imager

mono-imager v0.9.1 uses a declarative step registry. You add journeys by writing decorated functions — no orchestrator rewrites, no hardcoded sequences, no dispatch tables to update.

---

## How it works

Every flash journey is built from **steps**. Each step is a plain Python function decorated with `@register_step`, which declares:

- **`os`** — which operating systems this step applies to
- **`transfer`** — which transfer methods (`"network"`, `"usb"`) this step applies to
- **`requires`** — state keys that must exist before this step can run
- **`produces`** — state keys this step marks as done on success

When a journey runs, `FlowRunner` filters the registry to steps matching the chosen OS and transfer method, topologically sorts them by `requires`/`produces`, and executes them in order. If any step fails, the journey stops.

The resolved sequences for all current journeys:

```
OPNsense + network   →  10 steps
OPNsense + usb       →  10 steps
OpenWRT  + network   →   5 steps
OpenWRT  + usb       →   5 steps
Armbian  + network   →   5 steps
Armbian  + usb       →   5 steps
```

You never write that sequence by hand. It falls out of the `requires`/`produces` declarations.

---

## Files involved

| File | Purpose |
|------|---------|
| `mono_imager/step_registry.py` | `@register_step` decorator, `StepContext`, `FlowRunner` |
| `mono_imager/journey_steps.py` | All step implementations — the only file you edit |

Everything else (`flash_orchestrator.py`, `tui.py`, etc.) is infrastructure. You don't touch it when adding journeys.

---

## StepContext — what every step receives

Every step function takes a single `StepContext` argument. It carries all runtime state:

```python
@dataclass
class StepContext:
    device:        SerialDevice   # connected serial device
    os_name:       str            # "OPNsense", "OpenWRT", "Armbian", ...
    transfer:      str            # "network" | "usb"

    # Network journeys
    host_ip:       str            # host PC IP address
    device_ip:     str            # IP to assign to device
    http_port:     int            # port for firmware HTTP server
    device_mac:    str            # device MAC (for firmware auth)

    # Firmware
    firmware_path: Path           # path to firmware image on host
    flash_target:  str            # e.g. "/dev/mmcblk0" or "/dev/mmcblk0p1"

    # USB journeys
    usb_device:    str            # block device, e.g. "/dev/sda"
    usb_mount:     str            # mount point, e.g. "/mnt/usb"

    # Runtime state bag (produced/consumed between steps)
    state:         dict
```

Steps read from context fields and communicate with later steps via `ctx.set()` / `ctx.get()`:

```python
ctx.set("firmware_source", url)   # write — makes "firmware_source" available downstream
url = ctx.get("firmware_source")  # read — retrieve what an earlier step set
ctx.has("firmware_source")        # check — True if the key exists
```

---

## Adding a new OS

Say you want to add **VyOS** with both network and USB support.

VyOS flashes to the whole eMMC (`/dev/mmcblk0`) and reboots — identical to Armbian. So you need zero new steps. Just:

**Step 1: Add the flash target** in `journey_steps.py`:

```python
_FLASH_TARGETS = {
    "OPNsense": "/dev/mmcblk0",
    "OpenWRT":  "/dev/mmcblk0p1",
    "Armbian":  "/dev/mmcblk0",
    "VyOS":     "/dev/mmcblk0",   # ← add this
}

SUPPORTED_OS = list(_FLASH_TARGETS.keys())   # automatically includes VyOS
```

**Step 2: Tag existing steps** with `"VyOS"` wherever they apply. For VyOS the reboot step is the only OS-specific one:

```python
@register_step(
    os=["OpenWRT", "Armbian", "VyOS"],   # ← add VyOS here
    transfer=[ALL_TRANSFER],
    requires=["os_flashed"],
    produces=["rebooted"],
    label="Reboot device"
)
def step_reboot(ctx: StepContext) -> bool:
    ...
```

Done. `FlowRunner` now builds correct 5-step journeys for `VyOS + network` and `VyOS + usb` automatically.

---

## Adding a new transfer method

Say you want to add **TFTP** as a transfer method alongside `network` and `usb`.

**Step 1: Add the sentinel** to `SUPPORTED_TRANSFER`:

```python
SUPPORTED_TRANSFER = ["network", "usb", "tftp"]
```

**Step 2: Add transfer-specific steps.** TFTP needs a step to configure the TFTP client on-device and fetch the image. Write two new steps:

```python
@register_step(
    os=[ALL_OS],
    transfer=["tftp"],
    requires=[],
    produces=["tftp_ready"],
    label="Configure TFTP client"
)
def step_tftp_setup(ctx: StepContext) -> bool:
    d = ctx.device
    try:
        # Set server IP in busybox tftp syntax
        response = d.send_command(
            f"tftp -g -r firmware.img -l /tmp/firmware.img {ctx.host_ip} 2>&1; echo RC=$?",
            timeout=120
        )
        ok = "RC=0" in response
        if ok:
            ctx.set("firmware_source", "/tmp/firmware.img")
        return step(4, f"TFTP fetch from {ctx.host_ip}", ok,
                   response[-100:] if not ok else "")
    except Exception as e:
        return step(4, "TFTP fetch", False, str(e))
```

Because `step_tftp_setup` produces `"firmware_source"` and the existing `step_flash_dd` requires `"firmware_ready"` — not `"firmware_source"` — you also need a small bridge step:

```python
@register_step(
    os=[ALL_OS],
    transfer=["tftp"],
    requires=["tftp_ready"],
    produces=["firmware_ready"],
    label="Verify TFTP image received"
)
def step_tftp_verify(ctx: StepContext) -> bool:
    d = ctx.device
    response = d.send_command(
        "test -f /tmp/firmware.img && echo FOUND || echo MISSING", timeout=5
    )
    ok = "FOUND" in response
    if ok:
        ctx.set("firmware_source", "/tmp/firmware.img")
    return step(5, "TFTP image present on device", ok)
```

**Step 3: Add `"tftp"` to the reboot step** (and any other transfer-agnostic steps you want to include):

```python
@register_step(
    os=["OpenWRT", "Armbian"],
    transfer=[ALL_TRANSFER],   # ALL_TRANSFER already covers tftp
    ...
)
def step_reboot(ctx: StepContext) -> bool:
    ...
```

Because `ALL_TRANSFER` is already used on most steps, they automatically apply to `"tftp"` journeys. You only need to add `"tftp"` explicitly on steps that currently list `["network"]` or `["usb"]` — like `step_network_setup`.

---

## Adding a step to an existing journey

Say OPNsense needs a **SHA256 checksum verification** step after flashing, before the firmware re-image.

```python
@register_step(
    os=["OPNsense"],
    transfer=[ALL_TRANSFER],
    requires=["os_flashed"],        # runs after flash completes
    produces=["flash_verified"],    # consumed by the next OPNsense step
    label="Verify flash SHA256"
)
def step_verify_sha256(ctx: StepContext) -> bool:
    d = ctx.device
    expected = ctx.get("expected_sha256")
    if not expected:
        verbose("⚠ No expected SHA256 in context — skipping verify", "warning")
        return step(10, "SHA256 verify", True)   # non-fatal if not provided

    response, err = with_spinner(
        d.run_script,
        f"sha256sum {ctx.flash_target} 2>&1",
        marker="sha256", exec_timeout=120,
        message="Verifying flash SHA256"
    )
    if err:
        return step(10, "SHA256 verify", False, str(err))

    ok = expected.lower() in response.lower()
    return step(10, "SHA256 matches expected", ok,
               f"got: {response[:80]}" if not ok else "")
```

Then update `step_dip_to_nor` to require `"flash_verified"` instead of `"os_flashed"`:

```python
@register_step(
    os=["OPNsense"],
    transfer=[ALL_TRANSFER],
    requires=["flash_verified"],    # ← was "os_flashed"
    produces=["dip_at_nor"],
    label="DIP flip to NOR + power cycle"
)
def step_dip_to_nor(ctx: StepContext) -> bool:
    ...
```

`FlowRunner` now inserts the verify step between flash and DIP flip automatically. No other files change.

---

## Adding a new field to StepContext

If your step needs runtime data that isn't already in `StepContext` — say a `sha256_hash` field — add it to the dataclass in `step_registry.py`:

```python
@dataclass
class StepContext:
    ...
    sha256_hash: str = ""    # ← add this
```

Then pass it from `get_journey()` in `journey_steps.py`:

```python
def get_journey(
    ...
    sha256_hash: str = "",
) -> FlowRunner:
    ctx = StepContext(
        ...
        sha256_hash = sha256_hash,
    )
    return FlowRunner(os_name, transfer, ctx)
```

Existing steps are unaffected — their signatures don't change.

---

## Previewing a journey without running it

```python
from mono_imager.step_registry import list_journey

steps = list_journey("OPNsense", "usb")
for i, label in enumerate(steps, 1):
    print(f"  {i}. {label}")
```

Output:
```
  1. Mount USB stick
  2. Verify firmware file on USB
  3. Erase eMMC (OPNsense requirement)
  4. Flash OS image (dd)
  5. Unmount USB stick
  6. Detect device MAC address
  7. DIP flip to NOR + power cycle
  8. Confirm NOR recovery boot
  9. Re-image eMMC firmware (first 32MB)
  10. DIP flip to eMMC + power cycle
```

---

## Checklist for common tasks

**New OS, same flash behaviour as existing OS:**
- Add flash target to `_FLASH_TARGETS`
- Add OS name to `SUPPORTED_OS` (automatic if using `list(_FLASH_TARGETS.keys())`)
- Add new OS name to `os=` list on each applicable step

**New OS, unique post-flash step:**
- All of the above, plus write one new `@register_step` function with `os=["NewOS"]`

**New transfer method:**
- Add to `SUPPORTED_TRANSFER`
- Write transfer-specific steps (mount/fetch/verify equivalent)
- Tag steps that use `ALL_TRANSFER` — they apply automatically
- Tag steps that list specific methods explicitly

**New step in existing journey:**
- Write `@register_step` with the right `os=`, `transfer=`, `requires=`, `produces=`
- Update the `requires=` of whatever step should come after it

**New runtime data:**
- Add field to `StepContext` in `step_registry.py`
- Pass it from `get_journey()` in `journey_steps.py`

---

## What not to touch

When adding journeys, you never need to edit:

- `flash_orchestrator.py` — bootstrap, HTTP server, logging infrastructure
- `tui.py` — calls `get_journey()` and `runner.run()`, nothing else
- `recovery_orchestrator.py` — firmware update flow, separate from flash journeys
- `serial_device.py` — serial comms layer
- `step_registry.py` — only touch this to add fields to `StepContext`

All journey logic lives in `journey_steps.py`.
