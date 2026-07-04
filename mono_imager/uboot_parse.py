"""
mono-imager: U-Boot boot-output parsing.

Pure parsing of raw U-Boot serial output into structured data (board
identity + power-on self-test). No I/O, no state — domain logic that
used to sit at module level in tui.py. Rendering of the parsed result
lives in console.show_device_stats(); this module never prints.

Author:  H.A. Hermsen
License: GPLv3
"""

import re

_RE_SOC       = re.compile(r'^SoC:\s+(.+)$',              re.MULTILINE)
_RE_MODEL     = re.compile(r'^Model:\s+(.+)$',            re.MULTILINE)
_RE_DRAM      = re.compile(r'^DRAM:\s+(.+)$',             re.MULTILINE)
_RE_CPU_CLK   = re.compile(r'CPU\d+\([^)]*\):(\d+)\s*MHz')
_RE_BUS_CLK   = re.compile(r'Bus:\s+(\d+)\s*MHz')
_RE_DDR_CLK   = re.compile(r'DDR:\s+(\d+)\s*MT/s')
_RE_FMAN_CLK  = re.compile(r'FMAN:\s+(\d+)\s*MHz')

_UBOOT_FIELDS = [
    ("SoC",        _RE_SOC,      lambda m: m.group(1).strip()),
    ("Model",      _RE_MODEL,    lambda m: m.group(1).strip()),
    ("DRAM",       _RE_DRAM,     lambda m: m.group(1).strip()),
    ("Bus clock",  _RE_BUS_CLK,  lambda m: f"{m.group(1)} MHz"),
    ("DDR clock",  _RE_DDR_CLK,  lambda m: f"{m.group(1)} MT/s"),
    ("FMAN clock", _RE_FMAN_CLK, lambda m: f"{m.group(1)} MHz"),
]


def parse_uboot_identity(raw_output: str) -> dict:
    """
    Parse SoC/board identity and clock configuration from raw U-Boot
    boot output. Patterns validated against a real capture on this
    hardware (LS1046A / Mono Gateway Development Kit):

        SoC:  LS1046AE Rev1.0 (0x87070010)
        Clock Configuration:
               CPU0(A72):1600 MHz  CPU1(A72):1600 MHz  CPU2(A72):1600 MHz
               CPU3(A72):1600 MHz
               Bus:      600  MHz  DDR:      2100 MT/s  FMAN:     700  MHz
        Model: Mono Gateway Development Kit
        DRAM:  7.9 GiB (DDR4, 64-bit, CL=16, ECC on)

    Returns a dict of only the fields actually found — missing fields
    are simply absent, never guessed or defaulted.
    """
    result = {}
    for label, pattern, fmt in _UBOOT_FIELDS:
        if (m := pattern.search(raw_output)):
            result[label] = fmt(m)
    cpu_clocks = _RE_CPU_CLK.findall(raw_output)
    if cpu_clocks:
        unique = sorted(set(cpu_clocks), key=int)
        if len(unique) == 1:
            result["CPU clock"] = f"{unique[0]} MHz (all cores)"
        else:
            result["CPU clock"] = ", ".join(f"{c} MHz" for c in cpu_clocks)
    return result


_STATUS_PREFIXES = {
    "[ OK ]": True,
    "[FAIL]": False,
}


def parse_uboot_self_test(raw_output: str) -> list:
    """
    Parse the power-on self-test block from raw U-Boot boot output.
    Matches lines of the form "[ OK ] Label            value" or
    "[FAIL] Label            value", e.g.:

        [ OK ] DDR4 Memory          Bank0: 1982 MB, Bank1: 6144 MB
        [ OK ] USB PD controller    ID 0x25
        [FAIL] Temperatures         CPU sensor not responding

    Both statuses are captured — a failed self-test item (bad DDR, a
    dead temperature sensor, etc.) used to be silently dropped here,
    which meant a real hardware fault would just show up as a shorter
    list instead of a visible failure. console.show_device_stats()
    renders the two differently.

    Label/value are split on the first run of 2+ spaces, not the
    first single space — labels are frequently multiple words
    ("DDR4 Memory", "USB PD controller"), and splitting on the first
    single space cut those in half, silently misparsing every
    multi-word label since this function was first written.

    Returns a list of (label, value, passed) tuples in the order they
    appeared. value is "" if only the label is present with no
    value/detail. passed is True for "[ OK ]", False for "[FAIL]".
    """
    out = []
    for line in raw_output.splitlines():
        line = line.strip()
        prefix = next((p for p in _STATUS_PREFIXES if line.startswith(p)), None)
        if prefix is None:
            continue
        passed = _STATUS_PREFIXES[prefix]
        rest = line[len(prefix):].strip()
        parts = re.split(r'\s{2,}', rest, maxsplit=1)
        label = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else ""
        if label.lower().startswith("self-test"):
            continue
        out.append((label, value, passed))
    return out
