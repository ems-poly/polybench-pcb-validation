"""Defect-detection stage of the protocol run: solder bridges, missing parts, lifted/tombstoned/skewed
parts and anomalies between boards, from the registered top-side photos.

Everything here is a prompt to look. The output is `review` (or `fail` for a clear large-part
absence, decided by protocol_logic, or a high-confidence bridge on a power-net pair); nothing here
can say `pass` for a joint, a placement or a bridge. The pure functions work on a rectified
"view" (a crop of one board in board-millimetre coordinates at `ppmm` pixels per mm) so they can be
tested on drawn boards and on edited real crops.

Resolution first: the phone photos are ~92 px/mm at the board plane, so a 0.4 mm pitch QFN has
~37 px per pitch and a 0.2 mm gap is ~18 px wide. See `resolution_report`.

Registration: the protocol run registers each photo with the outline and four mounting holes, which
is good to a fraction of a millimetre only near the holes. `refine_warp` aligns the solder-mask
openings of the released CAM package (Gerber) to the photo (ECC, homography); that brought the pad
rectangles onto the pads over the whole board on the five photos. Tall parts still sit off the
board plane.
"""
from __future__ import annotations


import cv2
import numpy as np

from polybench.aoi import pads as padlib

CFG = {
    'ppmm': 100,              # view scale for measurements
    'snap_mm': 0.08,          # local pad snapping range around the registered pad position
    'snap_step_mm': 0.02,
    'gap_inner': 0.3,         # fraction of the gap trimmed on each side before testing it
    'min_gap_mm': 0.08,       # smaller "gaps" are paste splits of one pad
    'pair_max_gap_mm': 0.50,  # any two pads of any parts closer than this are checked (see docs/AOI_PROTOCOL.md)
    'max_gap_mm': 0.45,       # facing pads further apart than this are not a bridge risk
    'bridge_review_mm': 0.10,   # bridged length along the pad that raises a candidate
    'bridge_peer_margin_mm': 0.06,  # and must exceed the peer median by this
    'bridge_min_visible_mm': 0.12,  # visible gap length needed to say anything
    'bridge_high_mm': 0.22,   # high-confidence bridge (power-net pairs may then fail)
    # Chosen from the sweep of real false-flag rate against synthetic detection (AOI_PROTOCOL.md): the same
    # part differs by 0.1-0.6 mm and tens of degrees between the five boards through shadows and parallax
    # alone, so a weaker setting buries the review page in noise.
    'lift_offset_mm': 0.50,
    'lift_angle_deg': 10.0,
    'lift_asym': 0.60,
    'missing_red': 0.55,         # share of bare mask between the pads that says 'no body'
    'missing_red_rel_min': 0.30,  # or at least this much, and
    'missing_red_rel': 0.25,      # this much more than the same part on the other boards
    'ic_offset_mm': 0.25,         # many-pad parts: body centre vs the other boards
    'ic_angle_deg': 4.0,
    'ic_area_ratio': 0.6,
    'anom_top_n': 6,
    'anom_min_z': 4.0,
    'anom_min_score': 0.10,
}

SMALL_PACKAGES = ('CAPC1005', 'RESC1005', '0603', 'CAPC2012', 'SOD-323', 'SODFL', 'APGF')


# ---------------------------------------------------------------- colour classes

MASK_COLOUR = 'red'      # solder-mask colour the defect stage looks for: 'red' (the PoC) or 'dark' (black mask); protocol registration.board_colour
DARK_MASK_V = 60         # HSV value (0..255) below which a pixel is black solder mask when MASK_COLOUR == 'dark'


def set_mask_colour(colour):
    global MASK_COLOUR
    MASK_COLOUR = colour if colour in ('red', 'dark') else 'red'


def mask_of(hsv, s_min=100, v_min=50):
    """Boolean image: bare solder mask. The PoC's red rule, or 'dark' (value below DARK_MASK_V) for a black mask. The names
    `red` below mean 'mask colour' in both."""
    h, s, v = hsv[..., 0].astype(np.int16), hsv[..., 1].astype(np.int16), hsv[..., 2].astype(np.int16)
    if MASK_COLOUR == 'dark':
        return v < DARK_MASK_V
    return ((h < 14) | (h > 165)) & (s > s_min) & (v > v_min)


def classify(bgr):
    """(solder mask, bright metal/solder, body-or-dark) boolean masks of a BGR image (mask colour: see MASK_COLOUR)."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0].astype(np.int16), hsv[..., 1].astype(np.int16), hsv[..., 2].astype(np.int16)
    red = mask_of(hsv)
    metal = (~red) & (s < 110) & (v > 130)
    return red, metal, ~(red | metal)


# ---------------------------------------------------------------- registration and views

def read_mask_openings(zip_path):
    # rectangles only: the registration template draws axis-aligned openings, so rotated (region) pads stay out
    return [o for o in padlib.read_paste_pads(zip_path, layer='soldermask_top.gbr') if 'poly' not in o]


def _template(canon, size, openings):
    t = np.zeros((size[1], size[0]), np.float32)
    for p in openings:
        if p['shape'] == 'C' and p['w'] > 1.0:   # mounting holes
            continue
        a = canon @ [p['cx'] - p['w'] / 2, p['cy'] + p['h'] / 2, 1]
        b = canon @ [p['cx'] + p['w'] / 2, p['cy'] - p['h'] / 2, 1]
        cv2.rectangle(t, (int(round(a[0])), int(round(a[1]))), (int(round(b[0])), int(round(b[1]))), 1.0, -1)
    return t


def refine_warp(entry, openings):
    """Refine the image->canonical warp so solder-mask openings of the CAM package land on the
    pads of the photo. Returns (warp, info). Falls back to the input warp when ECC does not converge."""
    canon = np.asarray(entry['canonical'], float)
    crop = entry['crop']
    size = (crop.shape[1], crop.shape[0])
    t = cv2.GaussianBlur(_template(canon, size, openings), (0, 0), 3)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    red = mask_of(hsv, 110, 70)
    f = cv2.GaussianBlur((~red).astype(np.float32), (0, 0), 3)
    w = np.eye(3, dtype=np.float32)
    try:
        cc = 0.0
        for lvl in (4, 2, 1):
            s = 1 / lvl
            ts, fs = cv2.resize(t, None, fx=s, fy=s), cv2.resize(f, None, fx=s, fy=s)
            ws = w.copy()
            ws[0, 2] *= s; ws[1, 2] *= s; ws[2, 0] /= s; ws[2, 1] /= s
            cc, ws = cv2.findTransformECC(ts, fs, ws, cv2.MOTION_HOMOGRAPHY,
                                          (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6), None, 5)
            w = ws.copy()
            w[0, 2] /= s; w[1, 2] /= s; w[2, 0] *= s; w[2, 1] *= s
    except cv2.error as error:
        return np.asarray(entry['warp'], float), {'refined': False, 'reason': str(error)[:80]}
    shift_px = float(np.hypot(w[0, 2], w[1, 2]))
    if not np.isfinite(w).all() or shift_px > 60 or abs(np.linalg.det(w)) < .5:
        return np.asarray(entry['warp'], float), {'refined': False, 'reason': f'implausible correction ({shift_px:.0f} px)'}
    new = np.linalg.inv(w.astype(float)) @ np.asarray(entry['warp'], float)
    return new, {'refined': True, 'ecc': round(float(cc), 3), 'shift_px_canonical': round(shift_px, 1)}


def view_matrix(box_mm, ppmm):
    x0, y0, x1, y1 = box_mm
    return np.array([[ppmm, 0, -x0 * ppmm], [0, -ppmm, y1 * ppmm], [0, 0, 1]], float)


def frame_matrix(angle_deg, cx, cy):
    """Board mm -> a frame rotated by `angle_deg` about (cx, cy): q = R(-angle) (p - c)."""
    c, s = np.cos(np.radians(angle_deg)), np.sin(np.radians(angle_deg))
    return np.array([[c, s, -(c * cx + s * cy)], [-s, c, s * cx - c * cy], [0, 0, 1]], float)


def make_view(entry, box_mm, ppmm=CFG['ppmm'], warp=None, frame=None):
    """Rectified crop of the box (board mm, y up). Returns (image, V) with V mapping mm -> view px.
    With `frame=(angle_deg, cx, cy)` the box is in a frame rotated by that angle about (cx, cy) and V maps
    that frame's mm to view px (the pad pair under test is then axis-aligned along x)."""
    warp = np.asarray(entry.get('warp_fine', entry['warp']) if warp is None else warp, float)
    v = view_matrix(box_mm, ppmm)
    pre = np.eye(3) if frame is None else frame_matrix(*frame)
    m = v @ pre @ np.linalg.inv(np.asarray(entry['canonical'], float)) @ warp
    w = int(round((box_mm[2] - box_mm[0]) * ppmm))
    h = int(round((box_mm[3] - box_mm[1]) * ppmm))
    return cv2.warpPerspective(entry['image'], m, (w, h), flags=cv2.INTER_CUBIC), v


def to_px(v, x, y):
    p = v @ [x, y, 1.0]
    return float(p[0]), float(p[1])


def rect_px(v, rect):
    """(x1, y1, x2, y2) in mm -> integer pixel slice bounds (x0, y0, x1, y1) in the view."""
    x1, y1, x2, y2 = rect
    a, b = to_px(v, x1, y2), to_px(v, x2, y1)
    return int(round(min(a[0], b[0]))), int(round(min(a[1], b[1]))), int(round(max(a[0], b[0]))), int(round(max(a[1], b[1])))


def pad_rect(p):
    return (p['cx'] - p['w'] / 2, p['cy'] - p['h'] / 2, p['cx'] + p['w'] / 2, p['cy'] + p['h'] / 2)


def native_px_per_mm(entry, at_mm=(12.5, 13.0)):
    """Photo pixels per board millimetre at a point of the board, from the registration (local Jacobian)."""
    m = np.linalg.inv(np.asarray(entry.get('warp_fine', entry['warp']), float)) @ np.asarray(entry['canonical'], float)
    x, y = at_mm
    w = m[2, 0] * x + m[2, 1] * y + m[2, 2]
    jac = np.array([[m[0, 0] * w - (m[0, 0] * x + m[0, 1] * y + m[0, 2]) * m[2, 0], m[0, 1] * w - (m[0, 0] * x + m[0, 1] * y + m[0, 2]) * m[2, 1]],
                    [m[1, 0] * w - (m[1, 0] * x + m[1, 1] * y + m[1, 2]) * m[2, 0], m[1, 1] * w - (m[1, 0] * x + m[1, 1] * y + m[1, 2]) * m[2, 1]]]) / w ** 2
    return float(np.sqrt(abs(np.linalg.det(jac))))


def resolution_report(ppmm_native, pitch_mm=0.4, gap_mm=0.2):
    return {'px_per_mm': round(ppmm_native, 1), 'px_per_pitch_0.4mm': round(ppmm_native * pitch_mm, 1),
            'px_per_gap_0.2mm': round(ppmm_native * gap_mm, 1),
            'px_per_0402_gap_0.41mm': round(ppmm_native * .41, 1)}


# ---------------------------------------------------------------- bridges

def _sum_in(integral, x0, y0, x1, y1):
    h, w = integral.shape[0] - 1, integral.shape[1] - 1
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0
    s = integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
    return float(s), (x1 - x0) * (y1 - y0)


def bridge_pair(view, v, pa, pb, cfg=CFG, masks=None):
    """Measure one gap between two pads on a view.

    The pad rectangles are first snapped to the photo (a small search for the shift that puts most
    bright solder on both pads), then the middle of the gap is read row by row along the pads: a row
    is bridged when every pixel of the gap's inner band is bright metal. Returns bridge_mm (longest run),
    visible_mm (rows where the gap shows mask or metal rather than a dark body), the snap shift and
    the gap/pad rectangles used (mm)."""
    red, metal, dark = masks if masks is not None else classify(view)
    ppmm = cfg['ppmm']
    ra, rb = pad_rect(pa), pad_rect(pb)
    ox = min(ra[2], rb[2]) - max(ra[0], rb[0])
    side_by_side = ox <= 0                       # gap runs along y; pads are neighbours in x
    ik = cv2.integral(metal.astype(np.uint8))
    step, rng = cfg['snap_step_mm'], cfg['snap_mm']
    best, best_s = (0.0, 0.0), -1.0
    for dx in np.arange(-rng, rng + 1e-9, step):
        for dy in np.arange(-rng, rng + 1e-9, step):
            tot = 0.0
            for r in (ra, rb):
                x0, y0, x1, y1 = rect_px(v, (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy))
                s, n = _sum_in(ik, x0, y0, x1, y1)
                tot += s / max(n, 1)
            if tot > best_s + 1e-9:
                best, best_s = (float(dx), float(dy)), tot
    if best_s < 0.05:
        best = (0.0, 0.0)
    dx, dy = best
    sa = (ra[0] + dx, ra[1] + dy, ra[2] + dx, ra[3] + dy)
    sb = (rb[0] + dx, rb[1] + dy, rb[2] + dx, rb[3] + dy)
    if side_by_side:
        g0, g1 = min(sa[2], sb[2]), max(sa[0], sb[0])       # gap interval in x
        l0, l1 = max(sa[1], sb[1]), min(sa[3], sb[3])       # shared length in y
        gap = (g0, l0, g1, l1)
    else:
        g0, g1 = min(sa[3], sb[3]), max(sa[1], sb[1])
        l0, l1 = max(sa[0], sb[0]), min(sa[2], sb[2])
        gap = (l0, g0, l1, g1)
    inner = (g1 - g0) * cfg['gap_inner']
    band = (g0 + inner, g1 - inner)
    if side_by_side:
        x0, y0, x1, y1 = rect_px(v, (band[0], l0, band[1], l1))
    else:
        x0, y0, x1, y1 = rect_px(v, (l0, band[0], l1, band[1]))
    h, w = metal.shape
    x0, x1, y0, y1 = max(0, x0), min(w, x1), max(0, y0), min(h, y1)
    out = {'shift_mm': (round(dx, 3), round(dy, 3)), 'gap_mm': round(g1 - g0, 3), 'gap_rect': tuple(round(t, 3) for t in gap),
           'pad_rects': (tuple(round(t, 3) for t in sa), tuple(round(t, 3) for t in sb)),
           'bridge_mm': 0.0, 'visible_mm': 0.0, 'metal_fraction': 0.0}
    if x1 - x0 < 2 or y1 - y0 < 2:
        return out
    m = metal[y0:y1, x0:x1]
    r_ = red[y0:y1, x0:x1]
    axis = 1 if side_by_side else 0               # collapse across the gap, keep position along the pads
    full = m.mean(axis=axis) >= 0.9
    shows = (m | r_).mean(axis=axis) >= 0.6
    run = best_run = 0
    for flag in np.convolve(full.astype(float), [1, 1, 1], 'same') >= 2.5:   # tolerate a 1 px specular dropout
        run = run + 1 if flag else 0
        best_run = max(best_run, run)
    out.update(bridge_mm=round(best_run / ppmm, 3), visible_mm=round(float(shows.sum()) / ppmm, 3),
               metal_fraction=round(float(m.mean()), 3))
    return out


def _poly_x_extent(poly, y):
    """(xmin, xmax) of a convex polygon on the horizontal line `y`, or None."""
    xs = []
    n = len(poly)
    for k in range(n):
        (x1, y1), (x2, y2) = poly[k], poly[(k + 1) % n]
        if (y1 - y) * (y2 - y) <= 0 and y1 != y2:
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        elif y1 == y2 == y:
            xs += [x1, x2]
    return (min(xs), max(xs)) if xs else None


def _fill(shape, v, poly, dx=0.0, dy=0.0):
    m = np.zeros(shape, np.uint8)
    pts = np.array([to_px(v, x + dx, y + dy) for x, y in poly], np.int32)
    cv2.fillConvexPoly(m, pts, 1)
    return m.astype(bool)


def bridge_pair_poly(view, v, poly_a, poly_b, cfg=CFG, masks=None):
    """`bridge_pair` for any two convex pads, in a frame where the gap runs along +x from pad A to pad B
    (`poly_a`, `poly_b` are the pad corners in that frame, mm; `view`, `v` come from `make_view(frame=...)`).

    The pads are snapped to the photo (same search as `bridge_pair`), then every pixel row that crosses BOTH
    pads is read: the open interval between the pads on that row, trimmed by `gap_inner` on each side, is
    bridged when every pixel is bright metal. Rows are counted along the facing edges, so a bridge is the
    longest run of bridged rows, in mm. Returns the keys of `bridge_pair` (`shift_mm`, `gap_mm` median gap
    over the rows, `bridge_mm`, `visible_mm`, `metal_fraction`) plus `poly_a`, `poly_b` (snapped, frame mm)
    and `rows` [(row, y_mm, xa, xb)] for the painter. `rows` is empty when no row crosses both pads."""
    red, metal, dark = masks if masks is not None else classify(view)
    ppmm = cfg['ppmm']
    h, w = metal.shape
    pa, pb = np.asarray(poly_a, float), np.asarray(poly_b, float)
    step, rng = cfg['snap_step_mm'], cfg['snap_mm']
    best, best_s = (0.0, 0.0), -1.0
    for dx in np.arange(-rng, rng + 1e-9, step):
        for dy in np.arange(-rng, rng + 1e-9, step):
            tot = 0.0
            for poly in (pa, pb):
                m = _fill(metal.shape, v, poly, dx, dy)
                tot += float(metal[m].mean()) if m.any() else 0.0
            if tot > best_s + 1e-9:
                best, best_s = (float(dx), float(dy)), tot
    if best_s < 0.05:
        best = (0.0, 0.0)
    dx, dy = best
    pa, pb = pa + [dx, dy], pb + [dx, dy]
    out = {'shift_mm': (round(dx, 3), round(dy, 3)), 'gap_mm': 0.0, 'bridge_mm': 0.0, 'visible_mm': 0.0, 'metal_fraction': 0.0,
           'poly_a': pa.round(4).tolist(), 'poly_b': pb.round(4).tolist(), 'rows': []}
    inv = np.linalg.inv(v)
    rows, gaps, full, shows, mets = [], [], [], [], []
    for r in range(h):
        y = (inv @ [0.0, r + 0.5, 1.0])[1]
        ea, eb = _poly_x_extent(pa, y), _poly_x_extent(pb, y)
        if ea is None or eb is None:
            continue
        xa, xb = ea[1], eb[0]
        if xb - xa < 0.02:
            continue
        inner = (xb - xa) * cfg['gap_inner']
        c0, c1 = to_px(v, xa + inner, y)[0], to_px(v, xb - inner, y)[0]
        c0, c1 = int(round(min(c0, c1))), int(round(max(c0, c1)))
        c0, c1 = max(0, c0), min(w, c1)
        if c1 - c0 < 1:
            continue
        seg_m, seg_r = metal[r, c0:c1], red[r, c0:c1]
        rows.append((r, round(float(y), 4), round(float(xa), 4), round(float(xb), 4)))
        gaps.append(xb - xa)
        full.append(float(seg_m.mean()) >= 0.9)
        shows.append(float((seg_m | seg_r).mean()) >= 0.6)
        mets.append(float(seg_m.mean()))
    if not rows:
        return out
    run = best_run = 0
    for flag in np.convolve(np.array(full, float), [1, 1, 1], 'same') >= 2.5:
        run = run + 1 if flag else 0
        best_run = max(best_run, run)
    out.update(gap_mm=round(float(np.median(gaps)), 3), bridge_mm=round(best_run / ppmm, 3), visible_mm=round(sum(shows) / ppmm, 3),
               metal_fraction=round(float(np.mean(mets)), 3), rows=rows)
    return out


def bridge_flag(own, peers, cfg=CFG):
    """Decide from a pair's measurement and the same pair on the other boards.
    Returns (state, reason): 'candidate', 'high', 'clear' or 'not_assessable'."""
    if own['visible_mm'] < cfg['bridge_min_visible_mm']:
        return 'not_assessable', f'only {own["visible_mm"]:.2f} mm of the gap is visible'
    peer_vals = [p['bridge_mm'] for p in peers if p['visible_mm'] >= cfg['bridge_min_visible_mm']]
    base = float(np.median(peer_vals)) if peer_vals else 0.0
    if own['bridge_mm'] >= cfg['bridge_review_mm'] and own['bridge_mm'] >= base + cfg['bridge_peer_margin_mm']:
        high = own['bridge_mm'] >= cfg['bridge_high_mm']
        return ('high' if high else 'candidate',
                f'bright solder crosses the {own["gap_mm"]:.2f} mm gap over {own["bridge_mm"]:.2f} mm (peers {base:.2f} mm)')
    return 'clear', ''


# ---------------------------------------------------------------- body pose, lift, missing

def part_window(pads, margin=0.25):
    corners = np.vstack([padlib.pad_polygon(p) for p in pads])
    xs, ys = corners[:, 0], corners[:, 1]
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def two_pad_geometry(pads):
    """Axis (unit vector in mm from pad 0 to pad 1), pad hull centre, pitch, between-pads rectangle."""
    a, b = pads[0], pads[1]
    centre = np.array([(a['cx'] + b['cx']) / 2, (a['cy'] + b['cy']) / 2])
    axis = np.array([b['cx'] - a['cx'], b['cy'] - a['cy']], float)
    pitch = float(np.linalg.norm(axis))
    axis /= max(pitch, 1e-9)
    ra, rb = pad_rect(a), pad_rect(b)
    if abs(axis[0]) >= abs(axis[1]):
        x0, x1 = sorted((a['cx'] + np.sign(axis[0]) * a['w'] / 2, b['cx'] - np.sign(axis[0]) * b['w'] / 2))
        y0, y1 = max(ra[1], rb[1]), min(ra[3], rb[3])
    else:
        y0, y1 = sorted((a['cy'] + np.sign(axis[1]) * a['h'] / 2, b['cy'] - np.sign(axis[1]) * b['h'] / 2))
        x0, x1 = max(ra[0], rb[0]), min(ra[2], rb[2])
    return axis, centre, pitch, (x0, y0, x1, y1)


def body_pose(view, v, window, masks=None, min_area_mm2=0.15, ppmm=CFG['ppmm']):
    """Largest non-mask, non-metal blob in the window: centroid (mm), angle (deg, folded to +-45), area (mm2).
    None when nothing body-like is there."""
    red, metal, dark = masks if masks is not None else classify(view)
    x0, y0, x1, y1 = rect_px(v, window)
    h, w = dark.shape
    x0, x1, y0, y1 = max(0, x0), min(w, x1), max(0, y0), min(h, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    m = cv2.morphologyEx(dark[y0:y1, x0:x1].astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m)
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    area = stats[k, cv2.CC_STAT_AREA] / ppmm ** 2
    if area < min_area_mm2:
        return None
    ys, xs = np.nonzero(lab == k)
    pts = np.column_stack([xs, ys]).astype(np.float32)
    (cx, cy), (rw, rh), ang = cv2.minAreaRect(pts)
    ang = ((ang + 45) % 90) - 45
    inv = np.linalg.inv(v)
    cxy = inv @ [x0 + float(xs.mean()), y0 + float(ys.mean()), 1.0]
    return {'cx': float(cxy[0]), 'cy': float(cxy[1]), 'angle': float(ang), 'area_mm2': float(area),
            'aspect': float(max(rw, rh) / max(min(rw, rh), 1e-6))}


def pad_states(view, v, pads, masks=None):
    """Per pad: fractions of metal / body (dark) / mask-red in the pad rectangle."""
    red, metal, dark = masks if masks is not None else classify(view)
    out = []
    h, w = red.shape
    for p in pads:
        x0, y0, x1, y1 = rect_px(v, pad_rect(p))
        x0, x1, y0, y1 = max(0, x0), min(w, x1), max(0, y0), min(h, y1)
        n = max(1, (x1 - x0) * (y1 - y0))
        out.append({'metal': float(metal[y0:y1, x0:x1].sum() / n), 'dark': float(dark[y0:y1, x0:x1].sum() / n),
                    'red': float(red[y0:y1, x0:x1].sum() / n)})
    return out


def zone_red_fraction(view, v, rect, masks=None):
    red, _, _ = masks if masks is not None else classify(view)
    x0, y0, x1, y1 = rect_px(v, rect)
    h, w = red.shape
    x0, x1, y0, y1 = max(0, x0), min(w, x1), max(0, y0), min(h, y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return float(red[y0:y1, x0:x1].mean())


def lift_metrics(view, v, pads, masks=None):
    """Placement measurements of a two-terminal part: body centroid offset along/across the pad axis (mm),
    body angle vs the pad axis (deg), end asymmetry (body over one pad but not the other), and the red
    fraction of the zone between the pads (bare mask there means no body)."""
    masks = masks if masks is not None else classify(view)
    axis, centre, pitch, zone = two_pad_geometry(pads)
    pose = body_pose(view, v, part_window(pads, 0.15), masks)
    states = pad_states(view, v, pads[:2], masks)
    out = {'zone_red': zone_red_fraction(view, v, zone, masks), 'pitch_mm': round(pitch, 3),
           'asym': abs(states[0]['dark'] - states[1]['dark']), 'pads': states}
    if pose is None:
        out.update(along_mm=None, across_mm=None, angle_deg=None)
        return out
    d = np.array([pose['cx'], pose['cy']]) - centre
    ax_angle = np.degrees(np.arctan2(axis[1], axis[0]))
    rel = ((pose['angle'] - ax_angle + 45) % 90) - 45
    out.update(along_mm=float(d @ axis), across_mm=float(d @ np.array([-axis[1], axis[0]])), angle_deg=float(rel),
               area_mm2=pose['area_mm2'], aspect=pose['aspect'])
    return out


def missing_rule(zone_red, peer_zone_red, cfg=CFG):
    """True when the area between the pads of an expected-present part shows bare mask: a lot of it, or
    clearly more than the same part on the other boards."""
    if zone_red is None:
        return False
    if zone_red >= cfg['missing_red']:
        return True
    peers = [z for z in peer_zone_red if z is not None]
    return len(peers) >= 3 and zone_red >= cfg['missing_red_rel_min'] and zone_red >= float(np.median(peers)) + cfg['missing_red_rel']


def lift_flag(own, peers, cfg=CFG):
    """Compare one board's lift metrics with the median of the others."""
    reasons = []
    ok = [p for p in peers if p.get('along_mm') is not None]
    if own.get('along_mm') is None or len(ok) < 2:
        return []
    med = lambda key: float(np.median([p[key] for p in ok]))
    d_al = abs(own['along_mm'] - med('along_mm'))
    d_ac = abs(own['across_mm'] - med('across_mm'))
    d_an = abs(own['angle_deg'] - med('angle_deg'))
    if max(d_al, d_ac) >= cfg['lift_offset_mm']:
        reasons.append(f'body centre differs from the other boards by {max(d_al, d_ac):.2f} mm')
    if d_an >= cfg['lift_angle_deg'] and own.get('aspect', 0) >= 1.3 and own.get('area_mm2', 0) >= 0.25:
        reasons.append(f'body angle differs from the other boards by {d_an:.1f} deg')
    d_as = own['asym'] - float(np.median([p['asym'] for p in ok]))
    if d_as >= cfg['lift_asym']:
        reasons.append(f'one end pad is covered by the body and the other is not (tombstone signature, asymmetry {own["asym"]:.2f})')
    return reasons


def ic_pose(view, v, pads, masks=None):
    """Body of a many-pad part against its pad hull: centre offset (mm), angle, body area (mm2); None fields when the
    body is not found inside the hull."""
    pose = body_pose(view, v, part_window(pads, 0.0), masks, min_area_mm2=0.5)
    xs = [p['cx'] for p in pads]
    ys = [p['cy'] for p in pads]
    hull = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    if pose is None:
        return {'found': False}
    return {'found': True, 'dx': pose['cx'] - hull[0], 'dy': pose['cy'] - hull[1], 'angle': pose['angle'], 'area': pose['area_mm2']}


def ic_flag(own, peers, cfg=CFG):
    """Reasons a many-pad part looks displaced, rotated or only partly seen compared with the other boards."""
    ok = [p for p in peers if p['found']]
    if len(ok) < 2:
        return []
    if not own['found']:
        return ['the body is not where the pads expect it (no body inside the pad outline)']
    med = lambda k: float(np.median([p[k] for p in ok]))
    reasons = []
    shift = float(np.hypot(own['dx'] - med('dx'), own['dy'] - med('dy')))
    if shift >= cfg['ic_offset_mm']:
        reasons.append(f'body centre is {shift:.2f} mm from where it sits on the other boards')
    if abs(own['angle'] - med('angle')) >= cfg['ic_angle_deg']:
        reasons.append(f'body is rotated {abs(own["angle"] - med("angle")):.1f} deg against the other boards')
    if own['area'] < cfg['ic_area_ratio'] * med('area'):
        reasons.append(f'only {own["area"] / med("area"):.0%} of the usual body area is seen inside the pad outline')
    return reasons


# ---------------------------------------------------------------- anomalies between boards

def lab_view(bgr, gain=None):
    img = bgr.astype(np.float32)
    if gain is not None:
        img = np.clip(img * gain, 0, 255)
    return cv2.cvtColor(cv2.GaussianBlur(img.astype(np.uint8), (0, 0), 1.5), cv2.COLOR_BGR2LAB).astype(np.float32)


def match_medians(crops, exclude=()):
    """Scale each crop's channel medians to the median of the batch, so exposure and white-balance
    differences between photos do not count as differences between parts."""
    meds = {b: np.median(c.reshape(-1, 3), axis=0) for b, c in crops.items()}
    ref = [m for b, m in meds.items() if b not in exclude] or list(meds.values())
    target = np.median(np.stack(ref), axis=0)
    return {b: np.clip(c.astype(np.float32) * (target / np.maximum(meds[b], 1)), 0, 255).astype(np.uint8) for b, c in crops.items()}


def anomaly_scores(crops, tolerance_px=3, delta=22.0, exclude=()):
    """crops: {board: BGR crop of the same window}. For each board, the fraction of pixels whose colour
    lies outside the +-`tolerance_px` neighbourhood envelope of the per-pixel median of the other boards
    by more than `delta` (Lab units). Boards in `exclude` are scored but not used as reference. A shift of a few pixels does not count; a missing or moved part does."""
    labs = {b: lab_view(c) for b, c in crops.items()}
    out = {}
    k = np.ones((2 * tolerance_px + 1, 2 * tolerance_px + 1), np.uint8)
    for b, img in labs.items():
        others = [labs[o] for o in labs if o != b and o not in exclude]   # `exclude`: boards that must not serve as reference
        if len(others) < 2:
            out[b] = None
            continue
        med = np.median(np.stack(others), axis=0)
        hi = cv2.dilate(med, k)
        lo = cv2.erode(med, k)
        excess = np.maximum(np.maximum(img - hi, lo - img), 0)
        dist = np.sqrt((excess[..., 1:] ** 2).sum(axis=-1) + (0.5 * excess[..., 0]) ** 2)
        out[b] = float((dist > delta).mean())
    return out


def robust_z(value, others, floor):
    others = np.asarray([o for o in others if o is not None], float)
    if len(others) < 2:
        return 0.0
    med = float(np.median(others))
    mad = float(np.median(np.abs(others - med))) * 1.4826
    return (value - med) / max(mad, floor)


# ---------------------------------------------------------------- evidence

def _draw_rects(img, v, rects, thick):
    for r in rects:
        if 'poly' in r:       # a rotated pad or gap
            pts = np.array([to_px(v, x, y) for x, y in r['poly']], np.int32)
            cv2.polylines(img, [pts], True, r.get('colour', (0, 220, 255)), thick)
            continue
        x0, y0, x1, y1 = rect_px(v, r['rect'])
        cv2.rectangle(img, (x0, y0), (x1, y1), r.get('colour', (0, 220, 255)), thick)


def evidence_sheet(entries, suspect, box_mm, rects, title, ppmm=160, tile=520, columns=3):
    """The suspect board's crop first (red border), then the same crop from every other board, with the
    pad / gap windows drawn (full weight on the suspect, thin on the others).
    entries: {board: registered entry}. rects: [{'rect': (x1,y1,x2,y2) mm, 'colour': BGR}]"""
    order = [suspect] + [b for b in entries if b != suspect]
    tiles = []
    for b in order:
        e = entries[b]
        if e.get('error'):
            continue
        img, v = make_view(e, box_mm, ppmm)
        _draw_rects(img, v, rects, 2 if b == suspect else 1)
        s = min(tile / img.shape[1], tile / img.shape[0], 1.0 if img.shape[1] > tile else tile / img.shape[1])
        img = cv2.resize(img, (max(1, round(img.shape[1] * s)), max(1, round(img.shape[0] * s))), interpolation=cv2.INTER_AREA)
        if b == suspect:
            img = cv2.copyMakeBorder(img, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=(40, 50, 240))
        tiles.append((img, b + (' (suspect)' if b == suspect else '')))
    h = max(t[0].shape[0] for t in tiles) + 26
    w = max(t[0].shape[1] for t in tiles) + 8
    rows = (len(tiles) + columns - 1) // columns
    sheet = np.full((rows * h + 30, columns * w, 3), 235, np.uint8)
    cv2.putText(sheet, title, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 0, 0), 1, cv2.LINE_AA)
    for k, (img, label) in enumerate(tiles):
        r, c = divmod(k, columns)
        top, left = 30 + r * h, c * w
        cv2.putText(sheet, label, (left + 4, top + 16), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 0), 1, cv2.LINE_AA)
        sheet[top + 22:top + 22 + img.shape[0], left + 4:left + 4 + img.shape[1]] = img
    return sheet
