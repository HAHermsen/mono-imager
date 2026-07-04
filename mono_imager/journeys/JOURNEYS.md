# Adding Journeys to mono-imager

mono-imager uses a declarative step registry. You add journeys by writing decorated functions — no orchestrator rewrites, no hardcoded sequences, no dispatch tables to update.

---

## How it works

Every flash journey is built from **steps**. Each step is a plain Python function decorated with `@register_step`, which declares:

- **`os`** — which operating systems this step applies to
- **`transfer`** — which transfer methods (`"lan"`, `"usb"`) this step applies to
- **`requires`** — state keys that must exist before this step can run
- **`produces`** — state keys this step marks as done on success

When a journey runs, `FlowRunner` filters the registry to steps matching the chosen OS and transfer method, topologically sorts them by `requires`/`produces`, and executes them in order. If any step fails, the journey stops.

The resolved sequences for all current journeys:

```
OPNsense + lan   →  8 steps
OPNsense + usb   →  9 steps
OpenWRT  + lan   →  6 steps
OpenWRT  + usb   →  7 steps
Armbian  + lan   →  6 steps
Armbian  + usb   →  6 steps
```

OpenWRT and Armbian both follow the official documented procedure
(we-are-mono/docs) as of the last update: eMMC's own firmware/
bootloader region gets genuinely refreshed via `firmware update`
after flashing, and DIP ends up flipped to eMMC — rather than the
earlier NOR-stays-boot-source shortcut. OPNsense is unchanged (not
yet revisited).

Neither OS ends on an automated eMMC-boot verification anymore.
Real-hardware testing showed that check (`verify_boot_source()`'s
U-Boot marker-line poll) could time out even after a fully clean
flash, and its failure wasn't being recorded in the step report
either — a genuine failure could still show up as a full success.
OpenWRT now simply ends after the firmware update, relying on
tui.py's own always-shown end-of-flash instruction to flip the DIP
switch. Armbian still needs one pause before its NOR-round-trip
firmware refresh (that step can't start until eMMC is actually up),
but it's now a manual "press Enter once it's booted" confirmation,
not an automated poll — and the round-trip's own final DIP flip back
to eMMC is a static instruction with no check at all, same as OpenWRT.

You never write that sequence by hand. It falls out of the `requires`/`produces` declarations.

---

## Files involved

| File | Purpose |
|------|---------|
| `mono_imager/step_registry.py` | `@register_step` decorator, `StepContext`, `FlowRunner` |
| `mono_imager/journeys/__init__.py` | Auto-discovery, `get_journey()`, `discovered_journeys()`, flash targets |
| `mono_imager/journeys/<os>_<transfer>.py` | Step implementations — one file per OS/transfer pair |
| `mono_imager/journeys/usb_utils.py` | Shared USB file detection helpers |
| `mono_imager/journeys/_common.py` | Shared steps reused across journeys (e.g. "Device network ready") — leading `_` means it's not auto-discovered; a journey file must explicitly `import` it to trigger its `@register_step` calls |

To add or modify journey steps, edit or create the appropriate `journeys/<os>_<transfer>.py` file. Everything else (`flash_orchestrator.py`, `tui.py`, etc.) is infrastructure you don't touch when adding journeys.

Current journey files:
- `journeys/armbian_lan.py`
- `journeys/armbian_usb.py`
- `journeys/openwrt_lan.py`
- `journeys/openwrt_usb.py`
- `journeys/opnsense_lan.py`
- `journeys/opnsense_usb.py`

---

## StepContext — what every step receives

Every step function takes a single `StepContext` argument. It carries all runtime state:

```python
@dataclass
class StepContext:
    device:        SerialDevice   # connected serial device
    os_name:       str            # "OPNsense", "OpenWRT", "Armbian", ...
    transfer:      str            # "lan" | "usb"

    # Network journeys
    host_ip:       str            # host PC IP address
    device_ip:     str            # device's own IP (mirrors device_net["ip"])
    http_port:     int            # port for firmware HTTP server
    device_mac:    str            # device MAC (for firmware auth)
    device_net:    dict | None    # {"ip","prefix","gateway","dns","iface","source"} —
                                   # resolved once by MonoImager._setup_recovery_network()
                                   # (DHCP-first, verified, manual fallback) before the
                                   # journey starts. Steps that need the network declare
                                   # requires=["network_up"], produced by _common.py's
                                   # shared "Device network ready" step.

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

Say you want to add **VyOS** with both LAN and USB support.

VyOS flashes to the whole eMMC (`/dev/mmcblk0`) and reboots — identical to Armbian. So you need zero new steps. Just:

**Step 1: Add the flash target** in `journeys/__init__.py`:

```python
_FLASH_TARGETS = {
    "OPNsense": "/dev/mmcblk0",
    "OpenWRT":  "/dev/mmcblk0p1",
    "Armbian":  "/dev/mmcblk0",
    "VyOS":     "/dev/mmcblk0",   # ← add this
}
```

**Step 2: Create journey files** `journeys/vyos_lan.py` and `journeys/vyos_usb.py`.

Tag existing steps with `"VyOS"` wherever they apply, or import and re-register steps from an existing journey file (see `openwrt_usb.py` importing `_uboot_steps_openwrt_lan` from `openwrt_lan.py` as an example).

Done. `FlowRunner` now builds correct journeys for `VyOS + lan` and `VyOS + usb` automatically.

---

## Adding a new transfer method

Say you want to add **TFTP** as a transfer method alongside `lan` and `usb`.

**Step 1: Add transfer-specific steps.** TFTP needs a step to fetch the image via TFTP. Create `journeys/<os>_tftp.py` with:

```python
@register_step(
    os=[ALL_OS],
    transfer=["tftp"],
    requires=[],
    produces=["firmware_ready"],
    label="Fetch firmware via TFTP"
)
def step_tftp_fetch(ctx: StepContext) -> bool:
    ...
```

**Step 2: Update `journeys/__init__.py`** to add `"tftp"` to the known transfer methods if you want it discoverable via `discovered_journeys()`.

---

## Adding a step to an existing journey

Say OPNsense needs a **SHA256 checksum verification** step after flashing.

```python
@register_step(
    os=["OPNsense"],
    transfer=["lan", "usb"],
    requires=["os_flashed"],        # runs after flash completes
    produces=["flash_verified"],    # consumed by the next OPNsense step
    label="Verify flash SHA256"
)
def step_verify_sha256(ctx: StepContext) -> bool:
    ...
```

Then update the step that previously required `"os_flashed"` to require `"flash_verified"` instead. `FlowRunner` inserts the verify step automatically. No other files change.

---

## Adding a new field to StepContext

If your step needs runtime data that isn't already in `StepContext` — say a `sha256_hash` field — add it to the dataclass in `step_registry.py`:

```python
@dataclass
class StepContext:
    ...
    sha256_hash: str = ""    # ← add this
```

Then pass it from `get_journey()` in `journeys/__init__.py`:

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
  1. Device network ready
  2. Confirm DIP switch is RIGHT (NOR)
  3. Mount USB stick
  4. Detect firmware file on USB
  5. Flash OPNsense image (bzip2 | dd)
  6. Unmount USB stick
  7. Detect device MAC address
  8. Re-image eMMC firmware (firmware update)
  9. Reboot into OPNsense
```

---

## Checklist for common tasks

**New OS, same flash behaviour as existing OS:**
- Add flash target to `_FLASH_TARGETS` in `journeys/__init__.py`
- Create `journeys/<os>_lan.py` and `journeys/<os>_usb.py`
- Tag or import steps that apply to the new OS

**New OS, unique post-flash step:**
- All of the above, plus write one new `@register_step` function with `os=["NewOS"]`

**New transfer method:**
- Create `journeys/<os>_<transfer>.py` for each OS
- Write transfer-specific steps (mount/fetch/verify equivalent)

**New step in existing journey:**
- Write `@register_step` with the right `os=`, `transfer=`, `requires=`, `produces=`
- Update the `requires=` of whatever step should come after it

**New runtime data:**
- Add field to `StepContext` in `step_registry.py`
- Pass it from `get_journey()` in `journeys/__init__.py`

---

## What not to touch

When adding journeys, you never need to edit:

- `flash_orchestrator.py` — bootstrap, HTTP server, logging infrastructure
- `tui.py` — calls `get_journey()` and `runner.run()`, nothing else
- `recovery_orchestrator.py` — firmware update flow, separate from flash journeys
- `serial_device.py` — serial comms layer
- `step_registry.py` — only touch this to add fields to `StepContext`

All journey logic lives in the per-OS/transfer files under `journeys/`.
