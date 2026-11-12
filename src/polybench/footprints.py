"""Placed footprints of board parts, read from Fusion Electronics (read-only) for the polarity card.

`read(entity_type, object_or_None) -> dict` is the only dependency: FusionClient passes its structured read tool, the tests and
the simulator pass a replay of a recorded read. Nothing here writes, selects or opens anything.

For each designator the result holds, in board millimetres (x right, y up, top view):
  element   x, y, angle, mirror, side ('top' or 'bottom')
  pads      name, local (package frame), at (placed), poly (4 corners of the placed pad), net, size, rot, verified
  silk      lines, arcs, circles of the package's silkscreen (layer 21), placed
  outline   the package documentation outline (layer 51, tDocu) bounding box, placed corners, when the library has one
`verified` is true when Fusion's own placed pad of that name (the board's Smd or Pad list) sits where this module's rotation puts
the library pad. A footprint is usable for a picture only when every pad is verified: that proves the rotation, mirror and side.
"""
from __future__ import annotations

import math

SILK = 21
DOCU = 51
POS_TOL = 0.03     # mm: library pad placed by our rotation versus Fusion's own placed pad


class FootprintError(Exception):
    pass


def place(pt, el):
    """Package-frame point -> board frame. Fusion: a mirrored element (bottom side) flips the package in x first, then rotates
    counter-clockwise by `angle` about the element origin."""
    x, y = pt
    if el.get("mirror"):
        x = -x
    a = math.radians(float(el.get("angle", 0)))
    c, s = math.cos(a), math.sin(a)
    return (el["x"] + x * c - y * s, el["y"] + x * s + y * c)


def turn(pt, angle_deg):
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return (pt[0] * c - pt[1] * s, pt[0] * s + pt[1] * c)


def pad_poly(centre, w, h, rot):
    return [(centre[0] + turn((sx * w / 2, sy * h / 2), rot)[0], centre[1] + turn((sx * w / 2, sy * h / 2), rot)[1])
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _eq(prop, value):
    return {"property": prop, "op": "eq", "value": value}


def _all(read, entity):
    items, off = [], 0
    while True:
        r = read(entity, {"pagination": {"limit": 1000, "offset": off}})
        items += r.get("items", [])
        pg = r.get("pagination") or {}
        if not pg.get("hasMore"):
            return items
        off = pg.get("nextOffset", off + 1000)


def board_pads(read):
    """Fusion's own placed pads (surface-mount and through-hole): the independent check of the rotation."""
    pads = _all(read, "electronics.Smd")
    try:
        pads += [dict(p, dx=p.get("dx", p.get("diameter", 1.0)), dy=p.get("dy", p.get("diameter", 1.0))) for p in _all(read, "electronics.Pad")]
    except Exception:
        pass
    return pads


def _footprint(read, name, bpads):
    r = read("electronics.Element", {"filters": [_eq("name", name)]})
    if not r.get("items"):
        raise FootprintError(f"{name} is not on the open board")
    e = r["items"][0]
    el = {"x": e["x"], "y": e["y"], "angle": e.get("angle", 0), "mirror": int(e.get("mirror", 0))}
    pk = e.get("package_object_id")
    if not pk:
        raise FootprintError(f"{name}: Fusion gives no package for the element")
    pkg = read("electronics.Package", {"filters": [_eq("object_id", pk)]}).get("items") or [{}]
    contacts = read("electronics.Contact", {"filters": [_eq("package_object_id", pk)]}).get("items", [])
    if not contacts:
        raise FootprintError(f"{name}: the package has no pads")
    pads = []
    for c in contacts:
        at = place((c["x"], c["y"]), el)
        hit = None
        for b in bpads:
            if str(b.get("name")) == str(c["name"]) and math.hypot(b["x"] - at[0], b["y"] - at[1]) <= POS_TOL:
                hit = b
                break
        w, h, rot = (hit["dx"], hit["dy"], hit.get("angle", 0)) if hit else (0.4, 0.4, 0)
        pads.append({"name": str(c["name"]), "local": [c["x"], c["y"]], "at": list(at), "net": (hit or {}).get("signal", "") or "",
                     "size": [w, h], "rot": rot, "verified": bool(hit),
                     "poly": [list(p) for p in pad_poly(at, w, h, rot)]})
    silk = {"lines": [], "arcs": [], "circles": []}
    raw = []
    for w in read("electronics.Wire", {"filters": [_eq("package_object_id", pk)]}).get("items", []):
        p1, p2 = place((w["x1"], w["y1"]), el), place((w["x2"], w["y2"]), el)
        if w.get("layer") == SILK:
            curve = w.get("curve", 0) or 0
            if el["mirror"]:
                curve = -curve
            (silk["arcs"] if curve else silk["lines"]).append({"p1": list(p1), "p2": list(p2), "width": w.get("width", 0.15), **({"curve": curve} if curve else {})})
        elif w.get("layer") == DOCU:
            raw += [(w["x1"], w["y1"]), (w["x2"], w["y2"])]
    for c in read("electronics.Circle", {"filters": [_eq("package_object_id", pk)]}).get("items", []):
        if c.get("layer") == SILK:
            silk["circles"].append({"c": list(place((c["x"], c["y"]), el)), "r": c["radius"], "width": c.get("width", 0.15)})
    outline = outline_local = None
    if raw:     # the library outline is an axis-aligned box in the package frame: place its four corners
        xs, ys = [p[0] for p in raw], [p[1] for p in raw]
        outline_local = [min(xs), min(ys), max(xs), max(ys)]
        outline = [list(place(p, el)) for p in ((min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys)))]
    p0 = pkg[0]
    return {"designator": name, "package": p0.get("name", ""), "library": p0.get("library", ""),
            "element": dict(el, side="bottom" if el["mirror"] else "top", value=e.get("value", "")),
            "pads": pads, "silk": silk, "outline": outline, "outline_local": outline_local, "origin": [el["x"], el["y"]]}


def read_footprints(read, designators):
    """{designator: footprint dict | {'error': str}} for the given designators, from the active Electronics board."""
    bpads = board_pads(read)
    out = {}
    for d in designators:
        try:
            out[d] = _footprint(read, d, bpads)
        except FootprintError as e:
            out[d] = {"designator": d, "error": str(e)}
        except Exception as e:           # a Fusion or transport problem for one part must not hide the others
            out[d] = {"designator": d, "error": f"Fusion read failed: {e}"}
    return out
