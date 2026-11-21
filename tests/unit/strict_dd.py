"""
Test helper: a `dd` that behaves like the one in the Mono's recovery Linux.

The recovery BusyBox (1.37.0, seen on hardware 2026-11-21) has a dd without
conv= / iflag= / oflag= / ibs= / obs=: any of them is a usage error (rc 1) and
nothing is written. This shim refuses them the same way, so a device script
that uses one fails in the unit tests too.

The unit tests write to a plain FILE standing in for the eMMC. A block device
is never truncated by dd, a plain file is (without conv=notrunc). So when
of= is a regular file, the shim adds conv=notrunc itself: the fake eMMC then
behaves like the real one. Test-only: never part of a device script.
"""

import shutil
from pathlib import Path

UNSUPPORTED = ("conv=", "iflag=", "oflag=", "ibs=", "obs=")
SHIM_MODE = 0o755
DD_REFUSED_RC = 1                    # what the recovery BusyBox dd returns on an unknown option


def shim_text(real_dd: str) -> str:
    """Shell text of the shim; real_dd is the command that does the work ("/bin/dd" or "busybox dd")."""
    refused = "|".join(f"{p}*" for p in UNSUPPORTED)
    return (
        "#!/bin/sh\n"
        "# test-only dd: refuses what the recovery BusyBox dd lacks (see tests/unit/strict_dd.py)\n"
        f'for a; do case "$a" in {refused}) echo "dd: unsupported option $a" >&2; '
        f"exit {DD_REFUSED_RC};; esac; done\n"
        'o=; for a; do case "$a" in of=*) o=${a#of=};; esac; done\n'
        f'if [ -n "$o" ] && [ -f "$o" ]; then exec {real_dd} "$@" conv=notrunc; fi\n'
        f'exec {real_dd} "$@"\n'
    )


def install(bin_dir: Path, busybox: str | None = None) -> Path:
    """Write `dd` into bin_dir (replacing a symlink there). busybox: use `<busybox> dd`,
    else the host's dd. Returns the shim's path."""
    real = f"{busybox} dd" if busybox else shutil.which("dd")
    if not real:
        raise RuntimeError("no dd on this host")
    target = Path(bin_dir) / "dd"
    if target.is_symlink() or target.exists():
        target.unlink()
    target.write_text(shim_text(real))
    target.chmod(SHIM_MODE)
    return target


def ash_prelude(busybox: str) -> str:
    """The same dd as a shell function, for BusyBox ash.

    BusyBox ash runs its own applets directly (standalone shell), so a `dd`
    file in PATH is never used there. A shell function named dd is: ash looks
    up functions before applets. Put this text in front of the script."""
    refused = "|".join(f"{p}*" for p in UNSUPPORTED)
    return (
        "dd() {\n"
        f'  for a; do case "$a" in {refused}) echo "dd: unsupported option $a" >&2; '
        f"return {DD_REFUSED_RC};; esac; done\n"
        '  o=; for a; do case "$a" in of=*) o=${a#of=};; esac; done\n'
        f'  if [ -n "$o" ] && [ -f "$o" ]; then {busybox} dd "$@" conv=notrunc; '
        f'else {busybox} dd "$@"; fi\n'
        "}\n"
    )


def install_ash_wrapper(bin_dir: Path, busybox: str) -> Path:
    """`<bin_dir>/sh`: BusyBox ash that runs `-c SCRIPT` with ash_prelude() in front
    (other arguments, e.g. `-n -c`, pass through unchanged). Returns its path."""
    prelude = Path(bin_dir) / "strict_dd_prelude.sh"
    prelude.write_text(ash_prelude(busybox))
    target = Path(bin_dir) / "sh"
    if target.is_symlink() or target.exists():
        target.unlink()
    target.write_text(
        "#!/bin/sh\n"
        "# test-only: BusyBox ash with the strict dd function (see tests/unit/strict_dd.py)\n"
        f'if [ "$1" = -c ]; then exec {busybox} sh -c "$(cat {prelude})\n$2"; fi\n'
        f'exec {busybox} sh "$@"\n'
    )
    target.chmod(SHIM_MODE)
    return target
