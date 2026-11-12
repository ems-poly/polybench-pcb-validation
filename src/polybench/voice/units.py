# Ported from the earlier voice prototype prg/units.py. Changes for Polybench: none.
"""Units: classify a sheet's Unit column, spoken-unit words, conversion, and limit parsing."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# dimension -> {symbol: factor to the base unit}
DIMS: dict[str, dict[str, Decimal]] = {
    "resistance": {"mΩ": Decimal("0.001"), "Ω": Decimal(1), "kΩ": Decimal(1000), "MΩ": Decimal(10) ** 6},
    "voltage": {"µV": Decimal("0.000001"), "mV": Decimal("0.001"), "V": Decimal(1), "kV": Decimal(1000)},
    "current": {"µA": Decimal("0.000001"), "mA": Decimal("0.001"), "A": Decimal(1)},
    "frequency": {"Hz": Decimal(1), "kHz": Decimal(1000), "MHz": Decimal(10) ** 6},
    "time": {"µs": Decimal("0.000001"), "ms": Decimal("0.001"), "s": Decimal(1)},
    "percent": {"%": Decimal(1)},
}
SYMBOL_DIM = {s: d for d, m in DIMS.items() for s in m}


def norm_symbol(sym: str) -> str | None:
    """Normalise a unit symbol as written (case-sensitive: mV vs MV is not guessed)."""
    sym = sym.replace("\u03bc", "\u00b5").replace("\u2126", "\u03a9").replace("ohm", "\u03a9")
    if sym in SYMBOL_DIM:
        return sym
    return None


# Spoken words (lowercase) -> symbol. Prefix handled separately.
_PREFIX = {"micro": "µ", "milli": "m", "mil": "m", "kilo": "k", "mega": "M", "meg": "M"}
_BASE = {"ohm": "Ω", "ohms": "Ω", "volt": "V", "volts": "V", "amp": "A", "amps": "A", "ampere": "A",
         "amperes": "A", "hertz": "Hz", "second": "s", "seconds": "s", "sec": "s", "percent": "%"}


def spoken_unit(words: list[str], orig: list[str] | None = None) -> tuple[str | None, int]:
    """Parse a unit at the start of `words` (lowercase). Returns (symbol, words consumed)."""
    if not words:
        return None, 0
    w0 = words[0]
    o0 = orig[0] if orig else w0
    # compact symbol forms: "mv", "kohm", "ma"
    # 1) prefix word + base word: "kilo ohm", "milli volt", "mega hertz"
    if w0 in _PREFIX and len(words) > 1 and words[1] in _BASE:
        base = _BASE[words[1]]
        return _PREFIX[w0] + base, 2
    # 2) fused: "kilohm", "millivolts", "megahertz", "milliamps", "kilohms"
    for pre, p in _PREFIX.items():
        if w0.startswith(pre) and w0[len(pre):] in _BASE:
            return p + _BASE[w0[len(pre):]], 1
    if w0 in ("mohm", "mohms"):
        return ("M\u03a9" if o0.startswith("M") else "m\u03a9"), 1
    if w0 in ("megams", "megaoms", "megs"):
        return "M\u03a9", 1
    if w0 in ("kilohm", "kilohms"):
        return "kΩ", 1
    if w0 in ("megohm", "megohms", "megaohm", "megaohms"):
        return "MΩ", 1
    # 3) single letter prefix + base word: "k ohm", "m ohm", "m volt"
    if len(w0) == 1 and w0 in "kmM" and len(words) > 1 and words[1] in _BASE:
        letter = o0 if o0 in ("k", "m", "M") else w0
        if w0 == "m" and o0 == "M":
            letter = "M"
        return letter + _BASE[words[1]], 2
    if w0 in _BASE:
        return _BASE[w0], 1
    for pre_w, sym in (("kilohertz", "kHz"), ("megahertz", "MHz"), ("millisecond", "ms"), ("milliseconds", "ms"),
                       ("microsecond", "µs"), ("microseconds", "µs"), ("khz", "kHz"), ("mhz", "MHz"),
                       ("hz", "Hz"), ("ms", "ms"), ("mv", "mV"), ("ma", "mA"), ("uv", "µV"), ("ua", "µA"),
                       ("v", "V"), ("a", None), ("kohm", "kΩ"), ("kΩ", "kΩ"), ("mΩ", "mΩ"), ("Ω", "Ω")):
        if w0 == pre_w and sym:
            # case rescue for mega/milli from original casing
            if o0 in ("MHz", "MΩ", "mΩ", "mV", "mA", "µV", "µA", "kHz", "kΩ"):
                return o0, 1
            return sym, 1
    if o0 in SYMBOL_DIM:
        return o0, 1
    return None, 0


@dataclass
class UnitSpec:
    raw: str
    mode: str                       # number | ok_text | yn | text | hex | count | free
    dimension: str | None = None
    allowed: list[str] = field(default_factory=list)   # symbols (or free unit strings)
    canonical: str | None = None    # single unit to write bare numbers in, or None if several allowed
    repeat: int = 1                 # Y/N x3

    def as_dict(self):
        return {"raw": self.raw, "mode": self.mode, "dimension": self.dimension,
                "allowed": self.allowed, "canonical": self.canonical, "repeat": self.repeat}


def classify_unit(raw: str) -> UnitSpec:
    t = (raw or "").strip().replace("`", "")
    low = t.lower()
    if not t:
        return UnitSpec(raw, "free")
    if low.startswith("y/n"):
        m = re.search(r"x\s*(\d+)", low)
        return UnitSpec(raw, "yn", repeat=int(m.group(1)) if m else 1)
    if low in ("ok/text", "ok", "text/ok"):
        return UnitSpec(raw, "ok_text")
    if low == "text":
        return UnitSpec(raw, "text")
    if low == "hex":
        return UnitSpec(raw, "hex")
    if low == "count":
        return UnitSpec(raw, "count")
    if t == "B":
        return UnitSpec(raw, "free", None, ["B"])
    t = re.sub(r"^u(?=[AVs]\b)", "\u00b5", t)
    if low.startswith("%"):
        return UnitSpec(raw, "number", "percent", ["%"], "%")
    # "mΩ or Ω", "mV pk-pk", "mA", "AWG or mm²"
    parts = [p.strip() for p in re.split(r"\s+or\s+|/", t) if p.strip()]
    syms = []
    for p in parts:
        first = p.split()[0]
        s = norm_symbol(first)
        if s:
            syms.append(s)
    if syms and len(syms) == len(parts):
        dims = {SYMBOL_DIM[s] for s in syms}
        if len(dims) == 1:
            return UnitSpec(raw, "number", dims.pop(), syms, syms[0] if len(syms) == 1 else None)
    return UnitSpec(raw, "free", None, parts, None)


def to_decimal(x) -> Decimal | None:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None


def fmt_decimal(d: Decimal) -> str:
    if d == d.to_integral_value() and abs(d) < Decimal(10) ** 15:
        return str(int(d))
    s = format(d.normalize(), "f")
    return s


def convert(value: Decimal, frm: str, to: str) -> Decimal:
    dim = SYMBOL_DIM[frm]
    if SYMBOL_DIM[to] != dim:
        raise ValueError("incompatible")
    return (value * DIMS[dim][frm]) / DIMS[dim][to]


def compatible(sym: str, spec: UnitSpec) -> bool:
    return spec.dimension is not None and SYMBOL_DIM.get(sym) == spec.dimension


# ---- limits parsing -------------------------------------------------------------------------

_NUM = r"[-+]?\d+(?:\.\d+)?"
_UN = r"(?:\s*(?P<{n}>mΩ|kΩ|MΩ|Ω|µV|mV|kV|V|µA|mA|A|MHz|kHz|Hz|µs|ms|s|%)\b)?"
RANGE_RE = re.compile(r"(?P<a>%s)%s\s+to\s+(?P<b>%s)%s" % (_NUM, _UN.format(n="ua"), _NUM, _UN.format(n="ub")))
BELOW_RE = re.compile(r"\b(?:below|under|less than)\s+(?P<a>%s)%s" % (_NUM, _UN.format(n="ua")), re.I)
ABOVE_RE = re.compile(r"\b(?:above|over|greater than|at least)\s+(?P<a>%s)%s" % (_NUM, _UN.format(n="ua")), re.I)
IS_BAD = re.compile(r"\b(defect|means|is a short|is a defect|reversed)\b", re.I)


def _lim(kind, lo, hi, unit, bad, soft, clause):
    return {"kind": kind, "lo": lo, "hi": hi, "unit": unit, "bad": bad, "soft": soft, "clause": clause.strip()}


def parse_limits(expected: str, spec: UnitSpec) -> dict:
    """Extract machine-checkable limits from an Expected cell. Best effort; unparseable text yields no limits."""
    text = (expected or "").replace("`", "")
    clauses = [c for c in re.split(r"[;,()]", text) if c.strip()]
    # `1 MΩ` thousand separators are not used in the sheets
    limits = []
    accepts_ol = bool(re.search(r"\bOL\b", text))
    taken_good = False
    for c in clauses:
        bad = bool(IS_BAD.search(c))
        soft = bool(re.search(r"guess|TBR", c))
        if not bad and taken_good:
            continue
        m = RANGE_RE.search(c)
        if m:
            u = m.group("ub") or m.group("ua") or spec.canonical
            limits.append(_lim("range", m.group("a"), m.group("b"), u, bad, soft, c))
            taken_good = taken_good or not bad
            continue
        m = BELOW_RE.search(c)
        if m:
            u = m.group("ua") or spec.canonical
            limits.append(_lim("max", None, m.group("a"), u, bad, soft, c))
            taken_good = taken_good or not bad
            continue
        m = ABOVE_RE.search(c)
        if m:
            u = m.group("ua") or spec.canonical
            limits.append(_lim("min", m.group("a"), None, u, bad, soft, c))
            taken_good = taken_good or not bad
            continue
        m = re.search(r"\b(?:0\.00)\s*(?P<ua>V)\s+is a short", c)
        if m:
            limits.append(_lim("range", "0", "0", m.group("ua"), True, False, c))
    return {"limits": limits, "accepts_ol": accepts_ol}


def check_value(value_text: str, spec: UnitSpec, parsed_limits: dict, expected: str) -> str | None:
    """Return a flag string when `value_text` is outside the expected value, else None."""
    v = value_text.strip()
    exp = (expected or "").strip()
    if spec.mode in ("yn", "ok_text"):
        if exp in ("Y", "N", "OK") and v and v.upper() != exp.upper() and spec.repeat == 1:
            return f"expected {exp}"
        return None
    if spec.mode != "number":
        return None
    if v.upper() == "OL":
        if parsed_limits["accepts_ol"]:
            return None
        if parsed_limits["limits"]:
            return "over-range (OL) but a number is expected"
        return None
    m = re.match(r"^(%s)(?:\s*(\S+))?$" % _NUM, v)
    if not m:
        return None
    val = to_decimal(m.group(1))
    usym = norm_symbol(m.group(2)) if m.group(2) else spec.canonical
    if usym is None or val is None:
        return None
    flags = []
    if not parsed_limits["limits"]:
        if parsed_limits["accepts_ol"] and re.fullmatch(r"\s*OL\s*(\(.*\))?\s*", expected or ""):
            return "expected OL"
        return None
    for lim in parsed_limits["limits"]:
        lu = lim["unit"] or usym
        try:
            x = convert(val, usym, lu) if usym in SYMBOL_DIM and lu in SYMBOL_DIM and SYMBOL_DIM[usym] == SYMBOL_DIM[lu] else val
        except Exception:
            x = val
        lo = to_decimal(lim["lo"]) if lim["lo"] is not None else None
        hi = to_decimal(lim["hi"]) if lim["hi"] is not None else None
        if lim["kind"] == "range":
            inside = (lo is None or x >= lo) and (hi is None or x <= hi)
        elif lim["kind"] == "max":
            inside = x < hi
        else:
            inside = x >= lo
        suffix = " (guess limit)" if lim["soft"] else ""
        if lim["bad"]:
            if inside:
                flags.append("in defect zone: " + lim["clause"] + suffix)
        elif not inside:
            flags.append("outside " + lim["clause"] + suffix)
    return "; ".join(flags) or None
