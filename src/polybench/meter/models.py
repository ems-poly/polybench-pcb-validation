"""Per-model tables: what a (mode, range index) pair means. The mode codes are the family's; the RANGE byte is per model.

Every entry carries a status: `verified` = seen on a real meter by us (the pair was captured and the display agreed with the unit),
`manual` = taken from the model's UNI-T user manual range tables only. A pair that is not in the table has no unit, so nothing is
proposed for it (the live display still shows the number). Adding a model means adding a table here; nothing else changes.

Sources: mode codes - the Polybench brief fact sheet (public protocol descriptions, facts only); UT161E ranges - UNI-T UT161 series
user manual (specification tables: DC voltage 220.00 mV / 2.2000 V / 22.000 V / 220.00 V / 1000.0 V, resistance 220.00 ohm ...
220.00 Mohm, DC uA 220.00 / 2200.0, DC mA 22.000 / 220.00, DC A 20.000); verified pairs - our captures, see docs/MULTIMETER.md.
"""
from __future__ import annotations

from dataclasses import dataclass

MODE_NAMES = {
    0x00: "AC V", 0x01: "AC mV", 0x02: "DC V", 0x03: "DC mV", 0x04: "Hz", 0x05: "Duty", 0x06: "Ohm", 0x07: "Continuity",
    0x08: "Diode", 0x09: "Capacitance", 0x0A: "Temp C", 0x0B: "Temp F", 0x0C: "DC uA", 0x0D: "AC uA", 0x0E: "DC mA",
    0x0F: "AC mA", 0x10: "DC A", 0x11: "AC A", 0x12: "hFE", 0x13: "Live", 0x14: "NCV", 0x15: "LoZ V", 0x16: "Clamp A",
    0x17: "Clamp A", 0x18: "LPF V", 0x19: "AC+DC V",
}
AC_MODES = {0x00, 0x01, 0x0D, 0x0F, 0x11, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E}   # AC, AC+DC, LPF, LoZ, clamp: shown, not proposed
# Modes whose quantity the plan can hold today (docs/TEST_PLAN_FORMAT.md unit vocabulary). Others are shown only.
QUANTITY = {0x02: "voltage", 0x03: "voltage", 0x08: "voltage", 0x06: "resistance", 0x07: "resistance", 0x04: "frequency",
            0x05: "percent", 0x0A: "temperature", 0x0C: "current", 0x0E: "current", 0x10: "current"}


@dataclass(frozen=True)
class RangeInfo:
    unit: str | None        # plan-vocabulary unit of the displayed number ("V", "kOhm", "uA", "degC" ...); None = not known
    status: str             # "verified" | "manual"
    label: str = ""         # the manual's full-scale text, for the card
    note: str = ""


def _m(unit, label="", note=""):
    return RangeInfo(unit, "manual", label, note)


_UT161E: dict[tuple[int, int], RangeInfo] = {
    (0x02, 0): _m("V", "2.2 V"), (0x02, 1): _m("V", "22 V"), (0x02, 2): _m("V", "220 V"), (0x02, 3): _m("V", "1000 V"),
    (0x03, 0): _m("mV", "220 mV"),
    (0x08, 0): _m("V", "diode"),
    (0x05, 0): _m("%", "duty"),
    (0x0A, 0): _m("degC", "temperature"), (0x0A, 1): _m("degC", "temperature"),
    (0x0C, 0): _m("uA", "220 uA"), (0x0C, 1): _m("uA", "2200 uA"),
    (0x0E, 0): _m("mA", "22 mA"), (0x0E, 1): _m("mA", "220 mA"),
    (0x10, 0): _m("A", "20 A"),
    (0x07, 0): _m("Ohm", "220 Ohm"),
}
for _i, (_u, _l) in enumerate([("Ohm", "220 Ohm"), ("kOhm", "2.2 kOhm"), ("kOhm", "22 kOhm"), ("kOhm", "220 kOhm"),
                               ("MOhm", "2.2 MOhm"), ("MOhm", "22 MOhm"), ("MOhm", "220 MOhm")]):
    _UT161E[(0x06, _i)] = _m(_u, _l)
# Frequency: the manual gives only "10 Hz ~ 220 MHz" for the UT161E, not a range ladder, so the prefix of each range index is not
# known. (0x04, n) is deliberately absent: frequency is displayed, never proposed, until the ladder is captured. UNVALIDATED.
# Verified on the real meter (27 Oct, brief section 3.4): DC V index 1 showed 5.007 / 5.008 (22 V range, volts). Ohm index 6 showed OL
# only, which says nothing about its prefix, so it stays `manual`.
_VERIFIED_UT161E = {(0x02, 1)}

TABLES: dict[str, dict[tuple[int, int], RangeInfo]] = {}


def _register(model: str, table: dict, verified: set):
    TABLES[model] = {k: (RangeInfo(v.unit, "verified", v.label, v.note) if k in verified else v) for k, v in table.items()}


_register("UT161E", _UT161E, _VERIFIED_UT161E)


def mark_verified(model: str, pairs) -> None:
    """Test/doc hook: nothing in the shipped table is marked verified without a capture."""
    for k in pairs:
        if k in TABLES.get(model, {}):
            v = TABLES[model][k]
            TABLES[model][k] = RangeInfo(v.unit, "verified", v.label, v.note)


def lookup(model: str | None, mode: int, range_idx: int) -> RangeInfo | None:
    """The table entry, or None when the model, or this (mode, range) pair, is not in the table."""
    return TABLES.get((model or "").strip(), {}).get((mode, range_idx))


def mode_name(mode: int) -> str:
    return MODE_NAMES.get(mode, f"mode 0x{mode:02x}")
