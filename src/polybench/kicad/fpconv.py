"""KiCad footprint (nm -> mm, y down) -> the `polybench.footprints` dict (board mm, x right, y up, top view) used by the polarity
card and the hand-assembly rank (KC-46). Pure function over the model; no KiCad import.

`verified` (TBR-10): in Fusion it means the library pad, rotated by this module, sits where Fusion's own placed pad is. KiCad hands
over the placed pad itself (position, size, orientation, net in one read), so there is no second derivation to compare: `verified`
is true when KiCad gave a pad with a position and a non-zero size.
"""
from __future__ import annotations

import math

from .model import FootprintInfo, ShapeInfo


def up(p):
    return (p[0], -p[1])


def _turn(pt, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (pt[0] * c - pt[1] * s, pt[0] * s + pt[1] * c)


def to_local(pt_up, origin_up, angle, mirror):
    x, y = _turn((pt_up[0] - origin_up[0], pt_up[1] - origin_up[1]), -angle)
    return (-x if mirror else x, y)


def pad_poly(centre, w, h, rot):
    out = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        d = _turn((sx * w / 2, sy * h / 2), rot)
        out.append([centre[0] + d[0], centre[1] + d[1]])
    return out


def _arc_curve(p1, mid, p2) -> float:
    """Signed sweep in degrees from p1 to p2 through mid (y-up frame, counter-clockwise positive)."""
    ax, ay = p1
    bx, by = mid
    cx, cy = p2
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return 0.0
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    a1, am, a2 = (math.atan2(p[1] - uy, p[0] - ux) for p in (p1, mid, p2))

    def ccw(a, b):
        return (b - a) % (2 * math.pi)
    sweep = ccw(a1, a2) if ccw(a1, am) <= ccw(a1, a2) else -ccw(a2, a1)
    return math.degrees(sweep)


def convert(fp: FootprintInfo) -> dict:
    origin = up((fp.x, fp.y))
    mirror = int(fp.bottom)
    el = {"x": origin[0], "y": origin[1], "angle": fp.angle, "mirror": mirror}
    pads = []
    for p in fp.pads:
        at = up((p.x, p.y))
        w, h = (p.w, p.h) if p.w > 0 and p.h > 0 else (0.4, 0.4)
        loc = to_local(at, origin, fp.angle, mirror)
        pads.append({"name": p.number, "local": [loc[0], loc[1]], "at": [at[0], at[1]], "net": p.net, "size": [w, h],
                     "rot": p.angle, "verified": p.w > 0 and p.h > 0, "poly": pad_poly(at, w, h, p.angle)})
    silk = {"lines": [], "arcs": [], "circles": []}
    fab: list[tuple[float, float]] = []
    want_silk = "B.SilkS" if fp.bottom else "F.SilkS"
    want_fab = "B.Fab" if fp.bottom else "F.Fab"
    for s in fp.shapes:
        pts = [up(q) for q in s.pts]
        if s.layer == want_silk:
            if s.kind == "line":
                silk["lines"].append({"p1": list(pts[0]), "p2": list(pts[1]), "width": s.width})
            elif s.kind == "arc":
                silk["arcs"].append({"p1": list(pts[0]), "p2": list(pts[2]), "width": s.width, "curve": _arc_curve(pts[0], pts[1], pts[2])})
            elif s.kind == "circle":
                silk["circles"].append({"c": list(pts[0]), "r": math.dist(pts[0], pts[1]), "width": s.width})
            elif s.kind == "rect":
                (x1, y1), (x2, y2) = pts
                for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
                    silk["lines"].append({"p1": list(a), "p2": list(b), "width": s.width})
        elif s.layer == want_fab and s.kind in ("line", "rect"):
            fab += pts
    outline = outline_local = None
    if fab:
        loc = [to_local(q, origin, fp.angle, mirror) for q in fab]
        xs, ys = [q[0] for q in loc], [q[1] for q in loc]
        outline_local = [min(xs), min(ys), max(xs), max(ys)]
        outline = []
        for q in ((min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))):
            x, y = (-q[0] if mirror else q[0]), q[1]
            d = _turn((x, y), fp.angle)
            outline.append([origin[0] + d[0], origin[1] + d[1]])
    lib, _, entry = fp.lib_id.partition(":")
    return {"program": "KiCad", "designator": fp.ref, "package": entry or fp.lib_id, "library": lib if entry else "",
            "element": dict(el, side="bottom" if mirror else "top", value=fp.value),
            "pads": pads, "silk": silk, "outline": outline, "outline_local": outline_local, "origin": [origin[0], origin[1]]}
