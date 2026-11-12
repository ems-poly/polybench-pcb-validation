"""Pad geometry for the defect stage, read from the released CAM package (solder-paste layer).

The recorded layout carries package outlines and a connected-pad box per part, not individual pads.
The Gerber solderpaste_top layer has one rectangular flash per pad, in board millimetres with the
same origin as the layout, so the pads are recovered from it and assigned to the part whose
inspection polygon contains them. The zip is read in memory; nothing is extracted.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import numpy as np

_AD = re.compile(r'%ADD(\d+)([RC]),([0-9.]+)(?:X([0-9.]+))?\*%')
_RR = re.compile(r'%ADD(\d+)RoundRect,([0-9.]+)X((?:-?[0-9.]+X?)+)\*%')    # KiCad: rounded-rectangle macro, radius then the 4 corners
_FS = re.compile(r'%FSLAX(\d)(\d)Y(\d)(\d)\*%')
_FLASH = re.compile(r'X(-?\d+)Y(-?\d+)D03\*')
_DSEL = re.compile(r'^D(\d+)\*$')
_XY = re.compile(r'^X(-?\d+)Y(-?\d+)D0[12]\*$')


def _region_pad(points):
    """A G36 region that is one rectangular pad drawn at an angle (EAGLE writes pads of parts rotated by a
    non-multiple of 90 deg as regions, not as flashes). Returns the pad with the width along its first edge,
    `angle` (deg, direction of the width axis, 0 <= angle < 180) and the polygon corners."""
    pts = np.asarray(points[:-1] if points[0] == points[-1] else points, float)
    e1, e2 = pts[1] - pts[0], pts[2] - pts[1]
    w, h = float(np.hypot(*e1)), float(np.hypot(*e2))
    angle = float(np.degrees(np.arctan2(e1[1], e1[0])) % 180.0)
    c = pts.mean(axis=0)
    return {'cx': float(c[0]), 'cy': float(c[1]), 'w': round(w, 4), 'h': round(h, 4), 'shape': 'R', 'angle': round(angle, 2),
            'poly': [[round(float(x), 4), round(float(y), 4)] for x, y in pts]}


def parse_gerber_flashes(text):
    """Return [{cx, cy, w, h, shape}] in mm for every D03 flash of a rectangular/circular aperture, and every
    G36 region that is a rectangle (with angle and poly), in file order."""
    fs = _FS.search(text)
    decimals = int(fs.group(2)) if fs else 4
    apertures = {}
    for m in _AD.finditer(text):
        dcode, shape, a, b = m.groups()
        w = float(a)
        h = float(b) if b else w
        apertures[int(dcode)] = (shape, w, h)
    for m in _RR.finditer(text):      # KiCad rounded rectangle: radius, 4 corner points (x1 y1 .. x4 y4, closing point); extent = corners + radius
        v = [float(x) for x in m.group(3).rstrip('X').split('X')]
        r, xs, ys = float(m.group(2)), v[0:8:2], v[1:8:2]
        apertures[int(m.group(1))] = ('R', round(max(xs) - min(xs) + 2 * r, 6), round(max(ys) - min(ys) + 2 * r, 6))
    pads, current = [], None
    region = None
    for line in text.splitlines():
        line = line.strip()
        if line == 'G36*':
            region = []
            continue
        if line == 'G37*':
            if region and len(region) >= 4:
                pads.append(_region_pad(region))
            region = None
            continue
        if region is not None:
            m = _XY.search(line)
            if m:
                region.append((int(m.group(1)) / 10 ** decimals, int(m.group(2)) / 10 ** decimals))
            continue
        sel = _DSEL.match(line)
        if sel and int(sel.group(1)) >= 10:
            current = apertures.get(int(sel.group(1)))
            continue
        m = _FLASH.search(line)
        if m and current:
            shape, w, h = current
            pads.append({'cx': int(m.group(1)) / 10 ** decimals, 'cy': int(m.group(2)) / 10 ** decimals,
                         'w': w, 'h': h, 'shape': shape})
    return pads


def read_paste_pads(zip_path, layer='solderpaste_top.gbr'):
    with zipfile.ZipFile(zip_path) as archive:
        names = [n for n in archive.namelist() if Path(n).name == layer]
        if len(names) != 1:
            raise ValueError(f'{Path(zip_path).name}: expected one {layer}, found {len(names)}')
        return parse_gerber_flashes(archive.read(names[0]).decode('utf-8', 'replace'))


def _box(part):
    polygon = part.get('inspection_polygon') or part.get('polygon')
    if not polygon:
        return None
    pts = np.asarray(polygon, float)
    return pts.min(axis=0), pts.max(axis=0)


def assign_pads(layout, pads, tolerance=0.05):
    """{designator: [pad, ...]} for top-side parts; pads sorted by angle around the part centre
    start. A pad goes to the smallest inspection box that contains its centre."""
    boxes, centres = {}, {}
    for part in layout['parts']:
        if part['side'] != 'top':
            continue
        b = _box(part)
        if b is not None and part.get('inspection_polygon'):
            boxes[part['name']] = b
            centres[part['name']] = (np.array([part['x'], part['y']], float) if 'x' in part and 'y' in part else (b[0] + b[1]) / 2)
    out = {name: [] for name in boxes}
    for pad in pads:
        best, best_area = None, None
        if 'poly' in pad:
            # pads drawn as regions (parts rotated by 45 deg): the boxes of neighbouring rotated parts overlap, so
            # the smallest box is not reliable; take the part whose origin is nearest among those containing it
            near = [(float(np.hypot(*(centres[n] - [pad['cx'], pad['cy']]))), n) for n, (lo, hi) in boxes.items()
                    if lo[0] - tolerance <= pad['cx'] <= hi[0] + tolerance and lo[1] - tolerance <= pad['cy'] <= hi[1] + tolerance]
            if near:
                out[min(near)[1]].append(dict(pad))
            continue
        for name, (lo, hi) in boxes.items():
            if lo[0] - tolerance <= pad['cx'] <= hi[0] + tolerance and lo[1] - tolerance <= pad['cy'] <= hi[1] + tolerance:
                area = float(np.prod(hi - lo))
                if best is None or area < best_area:
                    best, best_area = name, area
        if best:
            out[best].append(dict(pad))
    return {k: v for k, v in out.items() if v}


def gap_pairs(pads, max_gap_mm=0.45):
    """Neighbouring pad pairs with a copper-to-copper gap below `max_gap_mm`, each with the gap
    rectangle between them (mm): (i, j, gap_mm, (x1, y1, x2, y2)). Only pairs whose facing edges
    overlap along the gap are returned, so a corner-to-corner diagonal is not a pair."""
    pairs = []
    for i in range(len(pads)):
        a = pads[i]
        for j in range(i + 1, len(pads)):
            b = pads[j]
            ax1, ax2, ay1, ay2 = a['cx'] - a['w'] / 2, a['cx'] + a['w'] / 2, a['cy'] - a['h'] / 2, a['cy'] + a['h'] / 2
            bx1, bx2, by1, by2 = b['cx'] - b['w'] / 2, b['cx'] + b['w'] / 2, b['cy'] - b['h'] / 2, b['cy'] + b['h'] / 2
            ox = min(ax2, bx2) - max(ax1, bx1)   # overlap along x
            oy = min(ay2, by2) - max(ay1, by1)
            if ox > 0.05 and oy <= 0:            # stacked vertically
                gap = -oy
                rect = (max(ax1, bx1), min(ay2, by2), min(ax2, bx2), max(ay1, by1))
            elif oy > 0.05 and ox <= 0:          # side by side
                gap = -ox
                rect = (min(ax2, bx2), max(ay1, by1), max(ax1, bx1), min(ay2, by2))
            else:
                continue
            if 0 < gap <= max_gap_mm:
                x1, y1, x2, y2 = rect
                pairs.append((i, j, round(gap, 4), (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))))
    return pairs


def bridge_pairs(assigned, max_gap_mm=0.45, min_gap_mm=0.08):
    """Pairs of pads that a solder bridge could join, across the whole board.

    Within one part only parts with three or more pads count: the two pads of a resistor, capacitor or
    diode are joined by the part itself and the gap under the body is hidden. Across parts every close
    pair counts (a 0402 pad next to a neighbouring pad). Returns a list of dicts
    {a: (designator, index), b: (designator, index), gap_mm, rect_mm}."""
    flat = [(d, i, p) for d, plist in assigned.items() for i, p in enumerate(plist)]
    out = []
    boxes = [(p['cx'] - p['w'] / 2, p['cy'] - p['h'] / 2, p['cx'] + p['w'] / 2, p['cy'] + p['h'] / 2) for _, _, p in flat]
    for m in range(len(flat)):
        da, ia, pa = flat[m]
        for n in range(m + 1, len(flat)):
            db, ib, pb = flat[n]
            if da == db and len(assigned[da]) < 3:
                continue
            if 'poly' in pa or 'poly' in pb:     # rotated pads are handled by close_pairs (polygon gaps)
                continue
            # quick reject
            if abs(pa['cx'] - pb['cx']) > 3 or abs(pa['cy'] - pb['cy']) > 3:
                continue
            for _i, _j, gap, rect in gap_pairs([pa, pb], max_gap_mm):
                if gap >= min_gap_mm:
                    out.append({'a': (da, ia), 'b': (db, ib), 'gap_mm': gap, 'rect_mm': rect})
    return out


# ---------------------------------------------------------------- every close pair of pads, any parts, with nets

def pad_polygon(p):
    """Corners of a pad (mm): the region polygon of a rotated pad, else the axis-aligned rectangle."""
    if 'poly' in p:
        return np.asarray(p['poly'], float)
    x1, y1, x2, y2 = p['cx'] - p['w'] / 2, p['cy'] - p['h'] / 2, p['cx'] + p['w'] / 2, p['cy'] + p['h'] / 2
    return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], float)


def _seg_dist(p, a, b):
    ab = b - a
    t = 0.0 if not ab.any() else float(np.clip(np.dot(p - a, ab) / np.dot(ab, ab), 0, 1))
    c = a + t * ab
    return float(np.hypot(*(p - c))), c


def polygon_gap(pa, pb):
    """Edge-to-edge distance of two convex pad polygons (mm) and the two nearest points (0.0 when they touch)."""
    best = (1e9, None, None)
    for A, B in ((pa, pb), (pb, pa)):
        for p in A:
            for k in range(len(B)):
                d, c = _seg_dist(p, B[k], B[(k + 1) % len(B)])
                if d < best[0]:
                    best = (d, p.copy(), c) if A is pa else (d, c, p.copy())
    return best


def pad_axis_frame(p):
    """0 for a pad with axis-aligned edges, 45 for one drawn at 45 deg, else its angle: the rotation of the
    frame in which the pad is a plain rectangle."""
    a = p.get('angle', 0.0) % 90.0
    return 0.0 if min(a, 90 - a) < 0.5 else round(a, 2)


def pad_net(netlist, designator, index):
    """Net of pad number `index + 1` of `designator` (pads are numbered in CAM flash order, which the
    consistency test below checks against the netlist), '' when the netlist has none, None when unknown."""
    if not netlist:
        return None
    return netlist.get(f'{designator}.{index + 1}')


def load_netlist(path):
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))['pads']


def nets_relation(na, nb):
    """'same' when both pads carry the same named net, 'different' when both carry a net and they differ,
    'unknown' when either pad has no net in the netlist (unrouted or not covered)."""
    if not na or not nb:
        return 'unknown'
    return 'same' if na == nb else 'different'


def close_pairs(assigned, netlist=None, max_gap_mm=0.5, min_gap_mm=0.08, skip=()):
    """EVERY pair of pads on the board, whatever their parts, whose edge-to-edge distance is
    `min_gap_mm` <= gap <= `max_gap_mm`. Pairs inside one part count only for parts with three or more pads
    (the two pads of a resistor or capacitor are joined by the part). `skip` is a set of designators (tall
    parts whose pads do not lie in the board plane). Returns dicts
    {a: (designator, index), b: (designator, index), gap_mm, points_mm, relation, nets, frame} sorted by gap."""
    flat = [(d, i, p, pad_polygon(p)) for d, pl in assigned.items() if d not in skip for i, p in enumerate(pl)]
    out = []
    for m in range(len(flat)):
        da, ia, pa, ga = flat[m]
        for n in range(m + 1, len(flat)):
            db, ib, pb, gb = flat[n]
            if da == db and len(assigned[da]) < 3:
                continue
            if abs(pa['cx'] - pb['cx']) > 4 or abs(pa['cy'] - pb['cy']) > 4:
                continue
            gap, qa, qb = polygon_gap(ga, gb)
            if not (min_gap_mm <= gap <= max_gap_mm):
                continue
            na, nb = pad_net(netlist, da, ia), pad_net(netlist, db, ib)
            fa, fb = pad_axis_frame(pa), pad_axis_frame(pb)
            out.append({'a': (da, ia), 'b': (db, ib), 'gap_mm': round(gap, 4),
                        'points_mm': (qa.round(4).tolist(), qb.round(4).tolist()),
                        'nets': (na, nb), 'relation': nets_relation(na, nb), 'frame': fa if fa == fb else None})
    out.sort(key=lambda r: (r['gap_mm'], r['a'], r['b']))
    return out


def pair_id(pr):
    """`AOI-BRIDGE-<des>-<i>-<j>` inside one part, `AOI-BRIDGE-<dA>.<i>-<dB>.<j>` across parts (pad numbers 1-based)."""
    (da, ia), (db, ib) = pr['a'], pr['b']
    return f'AOI-BRIDGE-{da}-{ia + 1}-{ib + 1}' if da == db else f'AOI-BRIDGE-{da}.{ia + 1}-{db}.{ib + 1}'
