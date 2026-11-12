"""A decoded meter reading: value from the display text, unit from the model table, and the reasons it may not be proposed.
No verdicts here: limits and pass/fail stay in the core."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from . import models, units
from .frames import Measurement

_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)$")


@dataclass
class Reading:
    model: str
    meas: Measurement
    ts: float
    kind: str = "number"            # number | ol | negol | invalid
    value: Decimal | None = None    # the displayed number (in `unit`), None for ol / invalid
    decimals: int = 0
    unit: str | None = None         # plan-vocabulary unit of the displayed number, None when not in the table
    status: str = ""                # table status: verified | manual | ""
    mode_name: str = ""
    ac: bool = False
    why_not: str = ""               # empty = proposable as far as the reading itself goes
    text: str = field(default="")   # the display text, trimmed

    @property
    def count(self) -> Decimal:
        """One count of the last displayed digit."""
        return Decimal(1).scaleb(-self.decimals)

    @property
    def context(self) -> tuple:
        """Mode, range and flags that make readings comparable; a change resets the stability window."""
        m = self.meas
        return (m.mode, m.range_idx, m.flags.ac, tuple(m.flags.active()))


def decode_display(display: str) -> tuple[str, Decimal | None, int]:
    t = display.strip()
    up = t.upper()
    if "O" in up and "L" in up:
        return ("negol" if "-" in t else "ol"), None, 0
    s = t.replace(" ", "")
    if _NUM.match(s):
        try:
            d = Decimal(s.rstrip("."))
        except InvalidOperation:
            return "invalid", None, 0
        dec = len(s.split(".")[1]) if "." in s else 0
        return "number", d, dec
    return "invalid", None, 0


def decode(model: str, meas: Measurement, ts: float) -> Reading:
    kind, value, dec = decode_display(meas.display)
    info = models.lookup(model, meas.mode, meas.range_idx)
    ac = meas.mode in models.AC_MODES or meas.flags.ac
    r = Reading(model=model, meas=meas, ts=ts, kind=kind, value=value, decimals=dec, unit=info.unit if info else None,
                status=info.status if info else "", mode_name=models.mode_name(meas.mode), ac=ac, text=meas.display.strip())
    r.why_not = why_not_proposed(r)
    return r


def why_not_proposed(r: Reading) -> str:
    """R4, R5: the plain-words reason this reading is never proposed, or ''."""
    if r.kind in ("ol", "negol"):
        return "overload: " + ("-OL" if r.kind == "negol" else "OL") + " is shown, never proposed"
    if r.kind == "invalid":
        return "the display shows no number"
    fl = r.meas.flags.active()
    if fl:
        return "/".join(fl) + " is on: the display holds a stored or relative value"
    if r.ac:
        return "AC and AC+DC readings are shown, not proposed (the plan has no AC units yet)"
    if r.meas.mode not in models.QUANTITY:
        return f"{r.mode_name} has no unit in the plan vocabulary"
    if r.unit is None:
        return f"range {r.meas.range_idx} of {r.mode_name} is not in the {r.model or 'meter'} table, so its unit is not known"
    return ""


def label(r: Reading) -> str:
    """The display line: OL, -OL or the number with its unit."""
    if r.kind == "ol":
        return "OL"
    if r.kind == "negol":
        return "-OL"
    if r.kind == "invalid":
        return r.text or "--"
    return r.text + ((" " + units.shown(r.unit)) if r.unit else "")


def to_unit(r: Reading, test_unit: str | None) -> tuple[str | None, str]:
    """(value text in the focused test's unit, '') or (None, reason). R6: same quantity only."""
    if r.value is None or r.unit is None:
        return None, "no usable number"
    if not test_unit:
        return None, "the focused test has no unit"
    qm, qt = units.quantity(r.unit), units.quantity(test_unit)
    if qt is None:
        return None, f"the focused test's unit {test_unit} is not one the meter can give"
    if qm != qt:
        return None, f"the meter is measuring {qm} ({units.shown(r.unit)}); this test wants {qt} ({units.shown(test_unit)})"
    return units.fmt(units.convert(r.value, r.unit, test_unit)), ""
