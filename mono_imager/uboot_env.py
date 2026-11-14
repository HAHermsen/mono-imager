"""
mono-imager: raw U-Boot environment regions — parse, rebuild, boot closure.

A raw env region (as stored on NOR / eMMC) is:

    CRC32 (4 bytes, little endian) [+ 1 flag byte when redundant] + data

where data is "name=value\\0name=value\\0...\\0\\0" padded to the region
size, and the CRC covers all of data. Pure functions, no I/O.

Author:  H.A. Hermsen
License: GPLv3
"""

import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional

CRC_BYTES = 4
FLAG_BYTES = 1                  # redundant env: one "active" flag byte after the CRC
CRC_MASK = 0xFFFFFFFF
PAD = b"\0"

# `run name` and ${name} / $name references inside a U-Boot command
_RUN_RE = re.compile(r"\brun\s+((?:[A-Za-z0-9_]+\s*)+)")
_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


@dataclass
class EnvRegion:
    vars: dict = field(default_factory=dict)   # insertion order = order in the region
    redundant: bool = False
    flag: Optional[int] = None
    size: int = 0


def parse(blob: Optional[bytes]) -> Optional[EnvRegion]:
    """EnvRegion of a raw env region, or None when no layout's CRC matches."""
    if not blob or len(blob) <= CRC_BYTES + FLAG_BYTES:
        return None
    crc = struct.unpack_from("<I", blob, 0)[0]
    for redundant in (False, True):
        start = CRC_BYTES + (FLAG_BYTES if redundant else 0)
        data = blob[start:]
        if zlib.crc32(data) & CRC_MASK != crc:
            continue
        env = {}
        for item in data.split(PAD):
            if not item:
                break
            k, _, v = item.decode("utf-8", errors="replace").partition("=")
            env[k] = v
        return EnvRegion(env, redundant, blob[CRC_BYTES] if redundant else None, len(blob))
    return None


def build(region: EnvRegion) -> bytes:
    """Raw env region for region.vars, same size and layout. ValueError if it does not fit."""
    head = CRC_BYTES + (FLAG_BYTES if region.redundant else 0)
    body = b"".join(f"{k}={v}".encode() + PAD for k, v in region.vars.items()) + PAD
    room = region.size - head
    if len(body) > room:
        raise ValueError(f"environment needs {len(body)} bytes, region holds {room}")
    data = body + PAD * (room - len(body))
    flag = bytes([region.flag]) if region.redundant else b""
    return struct.pack("<I", zlib.crc32(data) & CRC_MASK) + flag + data


# Never copied between envs: each medium keeps its own recovery path.
NOT_COPIED = frozenset({"recovery"})


def boot_closure(env: dict, root: str = "bootcmd", exclude=NOT_COPIED) -> dict:
    """
    The variables a boot needs: `root` plus everything it reaches through
    `run name` and $name / ${name}, transitively — only names defined in
    env, and never the names in `exclude` (the fallback `run recovery`
    must stay this medium's own).
    """
    need, todo = {}, [root]
    while todo:
        name = todo.pop()
        if name in need or name in exclude or name not in env:
            continue
        need[name] = env[name]
        value = env[name]
        for group in _RUN_RE.findall(value):
            todo.extend(group.split())
        todo.extend(_VAR_RE.findall(value))
    return need


def merge_for_boot(target: dict, source: dict, root: str = "bootcmd") -> dict:
    """{name: value} of source's boot closure that target lacks or has differently."""
    return {k: v for k, v in boot_closure(source, root).items() if target.get(k) != v}
