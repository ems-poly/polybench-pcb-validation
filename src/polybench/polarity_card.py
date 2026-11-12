"""Polarity card of an Assembly aid line: symbol, part, board, built only from design data.

The BOM line carries a structured `polmark` (what the marking is, which terminal it identifies, the datasheet reference). The
placed footprint comes from Fusion Electronics (polybench.footprints). The card is `confirmed` only when every link holds:

  * the footprint was read and every library pad matches Fusion's own placed pad (rotation, mirror and side are proven);
  * the polarity pad is found: by net (`polmark.net`, e.g. the cathode goes to VIN) or by name (`polmark.pad`), exactly one;
  * any `polmark.nets` pad -> net expectations hold (they tie the library pad numbering to the datasheet);
  * for a pin-1 mark on an IC the numbered pads run counter-clockwise from pad 1 (as every datasheet top view);
  * a silkscreen polarity mark in the footprint, when there is one, sits beside the polarity pad.

Otherwise the card is `amber`: the symbol and the generic body marking are still shown, never a board orientation.
Coordinates leave here in board millimetres (x right, y up, top view); the page draws them.
"""
from __future__ import annotations

import math
import re

from polybench.footprints import place

AMBER_TEXT = "Orientation not confirmed from design data, check the silkscreen"
MARKINGS = {"band", "pin1_dot", "triangle", "chamfer_underside"}
TERMINALS = {"cathode", "anode", "pin1", "pad1"}
EIGHT = ["right", "upper-right", "top", "upper-left", "left", "lower-left", "bottom", "lower-right"]


def is_polarised(line) -> bool:
    t = str(line.get("polarity") or "").strip()
    return bool(t) and not t.lower().startswith("none") and not line.get("dnf")


def compass(v):
    """Eight-way word of a board-frame vector (y up)."""
    return EIGHT[int(round(math.degrees(math.atan2(v[1], v[0])) / 45.0)) % 8]


def corner_word(v):
    return ("upper" if v[1] > 0 else "lower") + "-" + ("right" if v[0] > 0 else "left")


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def pad_by_name(fp, name):
    return next((p for p in fp["pads"] if p["name"] == str(name)), None)


def numbered_ccw(fp) -> bool:
    """True when pads 1..N (numeric names, the exposed pad excluded by its position at the centre) run counter-clockwise about the
    part centre, as in a datasheet top view. Same test as aoi.polarity._angle_sequence_ccw."""
    o = fp["origin"]
    pts = [(int(p["name"]), math.atan2(p["at"][1] - o[1], p["at"][0] - o[0])) for p in fp["pads"]
           if p["name"].isdigit() and math.hypot(p["at"][0] - o[0], p["at"][1] - o[1]) > 0.3]
    pts.sort()
    total = sum((b - a + math.pi) % (2 * math.pi) - math.pi for (_, a), (_, b) in zip(pts, pts[1:]))
    if fp["element"]["side"] == "bottom":      # board-frame angles of a bottom part run the other way in its own top view
        total = -total
    return total > 0


def polarity_pad(fp, pm):
    """(pad dict | None, reason when None)."""
    net, name = pm.get("net"), pm.get("pad")
    if net:
        hits = [p for p in fp["pads"] if p["net"] == net]
        if len(hits) != 1:
            return None, f"{len(hits)} pads on net {net} in the footprint, one expected"
        if name and hits[0]["name"] != str(name):
            return None, f"BOM says pad {name}, but net {net} is on pad {hits[0]['name']}"
        return hits[0], ""
    if name:
        p = pad_by_name(fp, name)
        return (p, "") if p else (None, f"no pad {name} in the footprint")
    return None, "the marking names no pad or net"


def silk_marks(fp):
    """Points of the footprint's silkscreen polarity marks: its circles and arcs (the outline lines are not marks)."""
    pts = [tuple(c["c"]) for c in fp["silk"]["circles"]]
    pts += [((a["p1"][0] + a["p2"][0]) / 2, (a["p1"][1] + a["p2"][1]) / 2) for a in fp["silk"]["arcs"]]
    return pts


def _nearest(fp, pt):
    return min(fp["pads"], key=lambda p: math.hypot(p["at"][0] - pt[0], p["at"][1] - pt[1]))


def _label(pm, key, default):
    return (pm.get("symbol_pins") or {}).get(key, default)


def symbol_of(pm, fp=None, ppad=None):
    """The schematic symbol: kind and the pins with their pad numbers when known."""
    kind = pm.get("symbol", "")
    pins = []
    if kind in ("diode", "tvs", "led") and fp is not None and ppad is not None:
        cathode = pm.get("terminal") == "cathode"
        other = [p for p in fp["pads"] if p["name"] != ppad["name"]]
        pins = [{"name": "K" if cathode else "A", "pad": ppad["name"]}] + ([{"name": "A" if cathode else "K", "pad": other[0]["name"]}] if len(other) == 1 else [])
    elif kind in ("diode", "tvs", "led"):
        pins = [{"name": "A", "pad": ""}, {"name": "K", "pad": ""}]
    elif kind == "ic":
        pins = [{"name": "1", "pad": pm.get("pad", "1")}]
    elif kind == "led_rgb_ca":
        pins = [{"name": n, "pad": p} for p, n in (("1", "A"), ("2", "G"), ("3", "B"), ("4", "R"))]
    elif kind == "crystal":
        pins = [{"name": "1", "pad": "1"}, {"name": "3", "pad": "3"}]
    return {"kind": kind, "pins": pins}


def _shown_net(net):
    return "" if not net or net.startswith("N$") else net


def _text(pm, ppad, fp):
    centre = _sub(tuple(ppad["at"]), tuple(fp["origin"]))
    net = _shown_net(ppad["net"])
    tail = f" ({net})" if net else ""
    m = pm["marking"]
    if m == "band":
        return f"Cathode band → pad {ppad['name']}{tail}, {compass(centre)} end on the board"
    if m == "pin1_dot":
        return f"Pin-1 dot → pad {ppad['name']}{tail}, {corner_word(centre)} corner on the board"
    if m == "triangle":
        return f"Black triangle → pad {ppad['name']}{tail}, {corner_word(centre)} corner on the board"
    return f"Pad-{ppad['name']} corner{tail} → {corner_word(centre)} on the board" + (", a 180° turn is harmless" if pm.get("harmless_180") else "")


def _source(pm, fp, doc, cached):
    pads = f"pads: {(fp or {}).get('program') or 'Fusion'} {doc or 'board'}" + (f" ({fp['library']}/{fp['package']})" if fp and fp.get("library") else "") + (" [cached read]" if cached else "")
    return f"{pads} · marking: {pm.get('ref') or 'not referenced'}"


_IPC_BODY = re.compile(r"^(?:V|W)?(?:QFN|SON|DFN)\d+P(\d{3,4})X(\d{3,4})", re.I)


def local_box(fp):
    """Body box in the package frame as (cx, cy, half-x, half-y) and where it came from: the body size in the IPC package name
    (QFN50P300X300... is 3.0 x 3.0 mm), else the library's documentation outline, else the extent of the pads."""
    m = _IPC_BODY.match(fp.get("package", ""))
    if m:
        return (0.0, 0.0, int(m.group(1)) / 200.0, int(m.group(2)) / 200.0), "package name"
    o = fp.get("outline_local")
    if o:
        return ((o[0] + o[2]) / 2, (o[1] + o[3]) / 2, (o[2] - o[0]) / 2, (o[3] - o[1]) / 2), "library outline"
    xs = [q for p in fp["pads"] for q in (p["local"][0] - p["size"][0] / 2, p["local"][0] + p["size"][0] / 2)]
    ys = [q for p in fp["pads"] for q in (p["local"][1] - p["size"][1] / 2, p["local"][1] + p["size"][1] / 2)]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2), "pad extent"


def _body(fp, pm):
    """Body box corners placed on the board."""
    (cx, cy, hx, hy), _ = local_box(fp)
    return [list(place((cx + sx * hx, cy + sy * hy), fp["element"])) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _mark_geometry(fp, pm, ppad):
    """Where the body marking sits on the placed part, derived from the polarity pad's position in the package frame (never from an
    assumed frame), then placed with the footprint's own rotation and side."""
    el = fp["element"]
    lx, ly = ppad["local"]
    m = pm["marking"]
    if m == "band":
        d = math.hypot(lx, ly) or 1.0
        ux, uy = lx / d, ly / d
        at = place((ux * d * 0.5, uy * d * 0.5), el)
        dx, dy = _sub(place((ux, uy), el), place((0, 0), el))
        return {"type": "band", "at": list(at), "dir": [dx, dy], "len": max(1.0, 1.2 * min(ppad["size"]))}
    (cx, cy, hx, hy), _ = local_box(fp)
    sx, sy = (1 if lx > cx else -1), (1 if ly > cy else -1)
    r = min(0.45, 0.28 * min(hx, hy) * 2) if m != "triangle" else 0.06
    at = place((cx + sx * (hx - r), cy + sy * (hy - r)), el)
    return {"type": "dot" if m == "pin1_dot" else m, "at": list(at), "dir": [sx, sy], "r": r * 0.8}


def _generic(pm, line, designators, designator, reason):
    return {"line": line["line"], "designator": designator, "designators": list(designators), "status": "amber",
            "amber": AMBER_TEXT, "reason": reason, "symbol": symbol_of(pm), "part": _part(pm),
            "board": None, "text": _generic_text(pm), "source": f"marking: {pm.get('ref') or 'not referenced'} · pads: not available"}


def _part(pm):
    return {k: pm.get(k) for k in ("family", "marking", "terminal", "legend", "corner") if pm.get(k)}


def _generic_text(pm):
    return {"band": "Cathode band marks the cathode", "pin1_dot": "Pin-1 dot marks pin 1", "triangle": "Black triangle marks pad 1",
            "chamfer_underside": "Pad 1 has the chamfered corner on the underside"}.get(pm.get("marking"), "Polarised part")


def build_card(line, designator, designators, fp, *, doc=None, cached=False, error=None):
    """The card for one designator of a line. `fp` is a footprint from polybench.footprints.read_footprints (or None / an error)."""
    pm = line.get("polmark")
    if not pm or pm.get("marking") not in MARKINGS or pm.get("terminal") not in TERMINALS:
        return {"line": line["line"], "designator": designator, "designators": list(designators), "status": "amber", "amber": AMBER_TEXT,
                "reason": "the BOM line has no structured polarity marking", "symbol": {"kind": "", "pins": []}, "part": {}, "board": None,
                "text": "", "source": "marking: not structured in the BOM"}
    if error or not fp or fp.get("error"):
        return _generic(pm, line, designators, designator, error or (fp or {}).get("error") or "no footprint")
    reason = None
    if not all(p["verified"] for p in fp["pads"]):
        reason = f"pad positions do not match {fp.get('program') or 'Fusion'}'s own placed pads (rotation or side not proven)"
    ppad = None
    if not reason:
        ppad, why = polarity_pad(fp, pm)
        reason = why or None
    if not reason:
        for pad, net in (pm.get("nets") or {}).items():
            p = pad_by_name(fp, pad)
            if not p or p["net"] != net:
                reason = f"pad {pad} is on net {(p or {}).get('net', '?')!r}, the datasheet pin map needs {net!r}"
                break
    if not reason and pm["marking"] == "pin1_dot" and not numbered_ccw(fp):
        reason = "pads are not numbered counter-clockwise from pad 1"
    if not reason:
        for pt in silk_marks(fp):
            if _nearest(fp, pt)["name"] != ppad["name"]:
                reason = f"the silkscreen mark sits beside pad {_nearest(fp, pt)['name']}, not pad {ppad['name']}"
                break
    if reason:
        c = _generic(pm, line, designators, designator, reason)
        c["source"] = _source(pm, fp, doc, cached)
        return c
    body = _body(fp, pm)
    silk_note = "silkscreen mark beside pad " + ppad["name"] if silk_marks(fp) else "no silkscreen polarity mark in the footprint"
    return {"line": line["line"], "designator": designator, "designators": list(designators), "status": "confirmed",
            "symbol": symbol_of(pm, fp, ppad), "part": _part(pm),
            "board": {"side": fp["element"]["side"], "angle": fp["element"]["angle"], "origin": fp["origin"], "pol_pad": ppad["name"],
                      "pads": [{"name": p["name"], "poly": p["poly"], "net": p["net"], "pol": p["name"] == ppad["name"]} for p in fp["pads"]],
                      "silk": fp["silk"], "body": body, "mark": _mark_geometry(fp, pm, ppad), "silk_note": silk_note},
            "text": _text(pm, ppad, fp), "caution": pm.get("caution", ""), "source": _source(pm, fp, doc, cached)}
