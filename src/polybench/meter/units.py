"""Unit conversion inside one quantity, in the plan's unit vocabulary (docs/TEST_PLAN_FORMAT.md). Exact decimal arithmetic."""
from __future__ import annotations

from decimal import Decimal

FACTORS: dict[str, dict[str, Decimal]] = {
    "voltage": {"uV": Decimal("0.000001"), "mV": Decimal("0.001"), "V": Decimal(1), "kV": Decimal(1000)},
    "resistance": {"mOhm": Decimal("0.001"), "Ohm": Decimal(1), "kOhm": Decimal(1000), "MOhm": Decimal(1000000)},
    "current": {"uA": Decimal("0.000001"), "mA": Decimal("0.001"), "A": Decimal(1)},
    "frequency": {"Hz": Decimal(1), "kHz": Decimal(1000), "MHz": Decimal(1000000)},
    "percent": {"%": Decimal(1)},
    "temperature": {"degC": Decimal(1)},
}
QUANTITY_OF = {u: q for q, m in FACTORS.items() for u in m}
SPOKEN = {"V": "volts", "mV": "millivolts", "uV": "microvolts", "kV": "kilovolts", "Ohm": "ohms", "kOhm": "kilo ohms", "MOhm": "mega ohms",
          "mOhm": "milli ohms", "A": "amps", "mA": "milliamps", "uA": "microamps", "Hz": "hertz", "kHz": "kilohertz",
          "MHz": "megahertz", "%": "percent", "degC": "degrees Celsius"}
SHOWN = {"Ohm": "Ω", "kOhm": "kΩ", "MOhm": "MΩ", "mOhm": "mΩ", "uA": "µA", "uV": "µV", "degC": "°C"}


def quantity(unit: str | None) -> str | None:
    return QUANTITY_OF.get(unit or "")


def convert(value: Decimal, src: str, dst: str) -> Decimal:
    """src and dst must be in the same quantity (the caller checks). Raises ValueError otherwise."""
    q = quantity(src)
    if q is None or q != quantity(dst):
        raise ValueError(f"cannot convert {src} to {dst}")
    return value * FACTORS[q][src] / FACTORS[q][dst]


def fmt(d: Decimal) -> str:
    """Plain decimal text without an exponent or trailing zeros (5007, 5.007, -0.012, 0)."""
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    return s


def shown(unit: str | None) -> str:
    return SHOWN.get(unit or "", unit or "")


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _int_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + _int_words(n % 100))
    return " ".join(_ONES[int(c)] for c in str(n))


def spoken_value(text: str, unit: str | None) -> str:
    """'5.007', 'V' -> 'five point zero zero seven volts'. Digits after the point are read one by one."""
    t = text.strip()
    neg = t.startswith("-")
    t = t.lstrip("+-")
    whole, _, frac = t.partition(".")
    words = ("minus " if neg else "") + (_int_words(int(whole)) if whole.isdigit() else "zero")
    if frac:
        words += " point " + " ".join(_ONES[int(c)] for c in frac if c.isdigit())
    return f"{words} {SPOKEN.get(unit or '', unit or '')}".strip()
