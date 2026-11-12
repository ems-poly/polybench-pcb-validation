"""Hand-assembly order of the Assembly aid: small and low parts first, ICs and tall parts last.

Pure functions, no I/O. The key, coarse to fine: height class, footprint area, designator. Height and area come from data, in
this priority:
  1. the BOM line: `height_mm` and / or `body_mm: [l, w]` (optional keys);
  2. the placed ECAD footprint (outline box and package name), when a read is already cached;
  3. the package-name table (`package_table`) on the BOM `package` text, then the designator prefix.
Class and area are resolved independently, so a line can take its class from the table and its area from the footprint. A line
nothing identifies is class UNKNOWN_CLASS (after the known tall parts, before connectors) with an unknown size and a hint.
DNF lines always come last. This module only orders the to-do list; it never touches what is placed or logged.
"""
from __future__ import annotations

import math
import re

from . import package_table as T

CLASS_LABELS = {0: "chip passives", 1: "low two-terminal parts (SOD, SMA, LED)", 2: "SOT and small ICs", 3: "QFN / TSSOP / SOIC ICs",
                4: "crystals, inductors, tall parts", 5: "connectors and through-hole"}
UNKNOWN_CLASS = 4
SRC_NAME = {"bom": "BOM line", "ecad": "ECAD footprint", "table": "package table", "kind": "designator prefix", "none": "unknown"}
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*mm", re.I)
_CHIP = re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)")
_NAT = re.compile(r"(\d+)")


def natural(s) -> tuple:
    """Natural sort key: R2 before R10."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in _NAT.split(str(s)) if p != "")


def prefix(designator) -> str:
    m = re.match(r"[A-Za-z]+", str(designator))
    return m.group(0).upper() if m else ""


def kind_of(designators):
    return T.KINDS.get(prefix(designators[0])) if designators else None


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def class_from_height(kind, h, area) -> int:
    """Height ladder for an explicit `height_mm`; the designator kind keeps a 0.9 mm QFN apart from a 0.5 mm chip."""
    if kind == "conn":
        return 5
    if kind in ("inductor", "xtal"):
        return 4
    if kind == "passive":
        return 0 if h <= 1.3 else 4
    if kind == "diode":
        return 1 if h <= 2.7 else 4
    if kind == "ic":
        if h <= 1.3 and (area is None or area <= 6.0):
            return 2
        return 3 if h <= 2.0 else 4
    return 0 if h <= 0.7 else 1 if h <= 1.3 else 2 if h <= 1.8 else 3 if h <= 2.5 else 4 if h <= 8 else 5


def lookup_name(text, kind=None):
    """Package text -> (class, (l, w, h) | None, label) or None. A chip code is a chip passive for R/C/FB, a low part for a diode."""
    t = str(text or "").upper()
    if not t.strip():
        return None
    m = _CHIP.search(t)
    if m and kind in (None, "passive", "diode"):
        return (1 if kind == "diode" else 0), T.CHIP[m.group(1)], m.group(1)
    for rx, cls, dims, label in T.PATTERNS:
        if re.search(rx, t):
            return cls, dims, label
    return None


def size_in_text(text):
    m = _SIZE.search(str(text or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


def footprint_of(fps, designators):
    """First usable footprint among a line's designators (a dict from polybench.footprints, not an error entry)."""
    for d in designators:
        fp = (fps or {}).get(d)
        if fp and not fp.get("error"):
            return fp
    return None


def _fp_area(fp):
    b = fp.get("outline_local")
    if b and len(b) == 4:
        w, h = abs(b[2] - b[0]), abs(b[3] - b[1])
        if w * h > 0:
            return w * h, f"{w:.2f} x {h:.2f} mm outline"
    return None


def rank_line(line, fps=None) -> dict:
    """Rank facts of one BOM line: cls, area (mm2 | None), the source of each, the size-unknown flag and the tooltip text."""
    ds = line.get("designators") or []
    kind = kind_of(ds)
    pkg = line.get("package") or ""
    fp = footprint_of(fps, ds)
    hit_bom = lookup_name(pkg, kind)
    hit_fp = lookup_name(fp.get("package"), kind) if fp else None
    # ---- area
    area = a_src = a_txt = None
    bm = line.get("body_mm")
    if isinstance(bm, (list, tuple)) and len(bm) == 2 and all(_num(v) for v in bm):
        area, a_src, a_txt = bm[0] * bm[1], "bom", f"body_mm {bm[0]:g} x {bm[1]:g}"
    if area is None and fp and _fp_area(fp):
        (area, a_txt), a_src = _fp_area(fp), "ecad"
    if area is None:
        sz = size_in_text(pkg)
        dims = sz or ((hit_bom[1][0], hit_bom[1][1]) if hit_bom and hit_bom[1] else None)
        if dims:
            area, a_src = dims[0] * dims[1], "table"
            a_txt = f"{dims[0]:g} x {dims[1]:g} mm " + ("in the package text" if sz else f"nominal {hit_bom[2]}")
    # ---- height class
    cls = c_src = c_txt = None
    h = line.get("height_mm")
    if _num(h):
        cls, c_src, c_txt = class_from_height(kind, h, area), "bom", f"height_mm {h:g}"
    elif hit_fp:
        cls, c_src, c_txt = hit_fp[0], "ecad", f"footprint package {fp.get('package')} ({hit_fp[2]})"
    elif hit_bom:
        cls, c_src, c_txt = hit_bom[0], "table", f"package '{pkg}' ({hit_bom[2]})"
    elif kind in T.KIND_CLASS:
        cls, c_src, c_txt = T.KIND_CLASS[kind][0], "kind", T.KIND_CLASS[kind][1]
    unknown = cls is None
    if unknown:
        cls, c_src = UNKNOWN_CLASS, "none"
    srcs = {c_src, a_src}
    best = next((s for s in ("bom", "ecad", "table", "kind") if s in srcs), "none")
    why = (f"Hand order: class {cls} {CLASS_LABELS[cls]} ("
           + (f"{SRC_NAME[c_src]}: {c_txt}" if not unknown else "unknown package, placed last among tall parts")
           + "); size " + (f"{area:.2f} mm2 ({SRC_NAME[a_src]}: {a_txt})" if area is not None else "unknown"))
    return {"cls": cls, "class_label": CLASS_LABELS[cls], "area": area, "class_source": c_src, "area_source": a_src or "none",
            "source": best, "size_unknown": unknown or area is None, "why": why}


def hand_key(line, info):
    """Sort key: DNF last, then class, area (unknown last in its class), first designator naturally."""
    ds = line.get("designators") or [""]
    return (1 if line.get("dnf") else 0, info["cls"], math.inf if info["area"] is None else round(info["area"], 4),
            natural(min(ds, key=natural)), line.get("line", 0))


def rank_bom(bom, fps=None) -> dict:
    """{line_no: info + hand_rank (0 = place first)} for a whole BOM."""
    infos = {l["line"]: rank_line(l, fps) for l in bom}
    for pos, l in enumerate(sorted(bom, key=lambda l: hand_key(l, infos[l["line"]]))):
        infos[l["line"]]["hand_rank"] = pos
    return infos
