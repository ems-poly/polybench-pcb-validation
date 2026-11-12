"""Automatic polarity / pin-1 check of the polarised parts of the top side.

Two independent halves, never mixed:

1. EXPECTED orientation comes from the design and the datasheet, not from the other boards. The data file
   `protocols/polarity.json` stores, per designator, the placed footprint (element origin, angle, pads with
   their nets, as read from Fusion Electronics), which pad is the cathode / pin 1, which physical marking the
   datasheet says identifies it (document, page), and the side of the body on which that marking must therefore
   appear in board coordinates (x right, y up, top view). `derive_expected` recomputes that side from the
   footprint and `audit_part` checks footprint against datasheet; both are unit tested, a disagreement is a design
   finding and is reported, never resolved here.

2. MEASURED marking comes from the registered photo: a band detector (full-height stripe lighter than the black
   epoxy, inside the body, in a zone fixed by the datasheet body size) and a corner-mark detector (dot / triangle
   at a body corner). The result is compared with the expected side.

Status rules (review assistance only, contract `polystage.aoi_result/0` is unchanged):
  * `pass`   marking found on the expected side, absent on the opposite side, by a stated margin.
  * `fail`   marking found on the opposite side, absent on the expected side, by the same margin.
  * `review` ambiguous, too small, or the detector is not validated for this part.
A part may report pass or fail only when its detector is validated on the photo set being run: every board that
gives a decision must give the opposite decision when the part crop is rotated by 180 degrees (a synthetic flip),
and at least `MIN_DECIDED` boards must decide. Otherwise the part stays at `review` with the measured scores in
the note. Agreement of all boards is never used as evidence.

    .venv/bin/python -m aoi.polarity --photos <folder> [--map map.json] --out <dir> [--result aoi_result.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

SCHEMA = 'polystage.aoi_polarity_spec/0'
SPEC_PATH = None    # polarity data file of a design; the protocol names it (polarity.spec), tests set it
PPMM = 100
MIN_DECIDED = 3          # boards that must give a decision before pass/fail may be reported for a part
TAU_BAND = 10.0          # flat detector, grey levels (0..255): the smallest accepted band was 14.2 (D2), the largest score on a side with no band 5.7 (D3)
RATIO = 2.5              # found side must beat the other side by this factor
TAU_RIDGE = 8.0          # ridge detector (D3): weakest accepted band 8.6 (B3, hollow outline), largest score on a side with no band 6.0 (B2)
RATIO_RIDGE = 2.0        # B3: 8.6 against 3.6
TAU_CORNER = 18.0
SIDE_VEC = {'left': (-1, 0), 'right': (1, 0), 'up': (0, 1), 'down': (0, -1)}
QUAD_VEC = {'NE': (1, 1), 'NW': (-1, 1), 'SE': (1, -1), 'SW': (-1, -1)}
OPPOSITE = {'left': 'right', 'right': 'left', 'up': 'down', 'down': 'up', 'NE': 'SW', 'SW': 'NE', 'NW': 'SE', 'SE': 'NW'}


class PolarityError(ValueError):
    pass


def load_spec(path=None):
    path = path or SPEC_PATH
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get('schema') != SCHEMA:
        raise PolarityError(f'{path}: schema must be {SCHEMA}')
    return data


# ---------------------------------------------------------------- design side: footprint and datasheet

def side_of(dx, dy):
    """Dominant board side of a vector (board coordinates, y up)."""
    if abs(dx) >= abs(dy):
        return 'right' if dx > 0 else 'left'
    return 'up' if dy > 0 else 'down'


def quadrant_of(dx, dy):
    return ('N' if dy > 0 else 'S') + ('E' if dx > 0 else 'W')


def rotate_quadrant(quadrant, angle_deg):
    """Quadrant after rotating a part counter-clockwise by a multiple of 90 degrees (Fusion angle, y up)."""
    turns = int(round(angle_deg / 90.0)) % 4
    if abs(angle_deg - 90 * round(angle_deg / 90.0)) > 1e-6:
        raise PolarityError(f'angle {angle_deg} is not a multiple of 90')
    x, y = QUAD_VEC[quadrant]
    for _ in range(turns):
        x, y = -y, x
    return quadrant_of(x, y)


def pad_xy(entry, name):
    for pad in entry['footprint']['pads']:
        if pad['name'] == name:
            return pad['x'], pad['y'], pad.get('net', '')
    raise PolarityError(f'{entry.get("designator", "?")}: no pad {name} in the footprint data')


def derive_expected(entry):
    """Side (band parts) or quadrant (corner marks) of the body where the datasheet marking must be, from the footprint."""
    origin = entry['footprint']['element']
    kind = entry['kind']
    if kind == 'band':
        x, y, _ = pad_xy(entry, entry['cathode_pad'])
        return side_of(x - origin['x'], y - origin['y'])
    if kind == 'corner_mark':
        x, y, _ = pad_xy(entry, entry['pin1_pad'])
        return quadrant_of(x - origin['x'], y - origin['y'])
    return None


def _angle_sequence_ccw(entry):
    """True when the numbered pads run counter-clockwise around the part centre in the board top view."""
    origin = entry['footprint']['element']
    pads = [(int(p['name']), math.atan2(p['y'] - origin['y'], p['x'] - origin['x']))
            for p in entry['footprint']['pads'] if p['name'].isdigit()]
    pads.sort()
    total = 0.0
    for (_, a), (_, b) in zip(pads, pads[1:]):
        step = (b - a + math.pi) % (2 * math.pi) - math.pi
        total += step
    return total > 0


def audit_part(entry):
    """Footprint-versus-datasheet checks. Returns [{check, ok, detail}]. Any ok == False is a design finding."""
    out = []

    def add(check, ok, detail):
        out.append({'check': check, 'ok': bool(ok), 'detail': detail})

    kind = entry['kind']
    expected = derive_expected(entry)
    if expected is not None:
        add('stored expected side equals the side derived from the footprint', entry['expected']['side'] == expected,
            f'stored {entry["expected"]["side"]}, derived {expected}')
    if kind == 'band':
        nets = {p['name']: p.get('net', '') for p in entry['footprint']['pads']}
        add('cathode pad is the pad on the cathode net', nets.get(entry['cathode_pad']) == entry['cathode_net'],
            f'pad {entry["cathode_pad"]} net {nets.get(entry["cathode_pad"])}, required net {entry["cathode_net"]}')
        marker = entry['footprint'].get('silk_marker_xy')
        if marker:
            origin = entry['footprint']['element']
            add('footprint silk marker lies on the cathode side', side_of(marker[0] - origin['x'], marker[1] - origin['y']) == expected,
                f'silk marker at {marker}')
    if kind == 'corner_mark':
        rotated = rotate_quadrant(entry['datasheet']['pin1_corner_top_view'], entry['footprint']['element']['angle'])
        add('datasheet pin-1 corner rotated by the placement angle equals the pad-1 corner', rotated == expected,
            f'datasheet {entry["datasheet"]["pin1_corner_top_view"]} at angle {entry["footprint"]["element"]["angle"]} gives {rotated}; pad 1 is at {expected}')
        add('pads are numbered counter-clockwise in the top view (as every datasheet top view here)', _angle_sequence_ccw(entry),
            'sign of the summed angle steps of the numbered pads')
    return out


# ---------------------------------------------------------------- photo side: board-frame view of a part

def board_view(entry, cx, cy, half, ppmm=PPMM):
    """Upright board-frame crop (x right, y up) of the registered photo, centred on (cx, cy) mm, side 2*half mm."""
    can = np.asarray(entry['canonical'], float)
    shift = np.array([[ppmm, 0, -(cx - half) * ppmm], [0, -ppmm, (cy + half) * ppmm], [0, 0, 1]], float)
    matrix = shift @ np.linalg.inv(can) @ np.asarray(entry['warp'], float)
    n = int(round(2 * half * ppmm))
    return cv2.warpPerspective(entry['image'], matrix, (n, n), flags=cv2.INTER_CUBIC)


LOCATE_MAX_MM = 1.2      # default largest shift `locate` will correct; a detector asks for it with `locate_mm` (off when absent)


def locate(view, ref_view, max_mm=LOCATE_MAX_MM, ppmm=PPMM):
    """Registration of a photo is good to a few tenths of a millimetre, more where a tall part or the viewing angle bends the plane.
    The detectors read fixed zones of the nominal body, so the part's own view is first moved onto the same part of the reference board
    (phase correlation of the edge images: the pads and body outline decide, not the marking). Returns (view, (dx_mm, dy_mm)); a shift larger
    than `max_mm` or a weak peak leaves the view unchanged."""
    def edges(v):
        g = cv2.GaussianBlur(cv2.cvtColor(v, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 2.0)
        return cv2.Laplacian(g, cv2.CV_32F)
    a, b = edges(ref_view), edges(view)
    win = cv2.createHanningWindow(a.shape[::-1], cv2.CV_32F)
    (dx, dy), resp = cv2.phaseCorrelate(a, b, win)
    if resp < 0.05 or max(abs(dx), abs(dy)) > max_mm * ppmm:
        return view, (0.0, 0.0)
    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
    return cv2.warpAffine(view, m, (view.shape[1], view.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE), (round(dx / ppmm, 2), round(dy / ppmm, 2))


def flip180(view):
    return np.ascontiguousarray(view[::-1, ::-1])


def view_usable(view):
    return float(np.mean(np.all(view == 0, axis=2))) < 0.02


# ---------------------------------------------------------------- detectors

def band_scores_ridge(view, det, ppmm=PPMM):
    """Positive full-height ridge contrast (grey levels) on the left and right of the body centre.
    The column profile is the median over the body rows (text covers only part of the rows, so it does not count),
    lightly smoothed. A printed band appears in a phone photo either as a solid stripe or as a hollow outline with
    bright edges (B3), and in both cases as a ridge a few tenths of a millimetre wide. Contrast is the ridge height
    over the median of two flanks (`flank_mm`, from `flank_mm[0]` to `flank_mm[1]` away on both sides: a ramp towards the
    sloped end face is lighter on one flank only and does not count). A window whose flanks reach a pad, fillet or the mask
    (median brighter than the body by 35 grey levels) is not inside the body and is skipped. Parameters come from the part's
    `detector` (`row_fraction`, `sigma_mm`, `flank_mm`); the defaults are those validated on D3 (15 Oct re-tune)."""
    g = cv2.cvtColor(view, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = g.shape
    cy, cx = h // 2, w // 2
    length, width = det['length_mm'], det['width_mm']
    rf = det.get('row_fraction', 0.44)
    rows = g[cy - int(rf * width * ppmm):cy + int(rf * width * ppmm)]
    prof = np.median(rows, axis=0)
    prof = cv2.GaussianBlur(prof.reshape(1, -1), (0, 0), det.get('sigma_mm', 0.03) * ppmm).ravel()
    base = float(np.median(prof[cx - int(.15 * length * ppmm):cx + int(.15 * length * ppmm)]))
    near, far = (int(v * ppmm) for v in det.get('flank_mm', (0.12, 0.45)))
    out = {}
    for side, sign in (('left', -1), ('right', 1)):
        best = (0.0, None)
        for p in np.arange(det['zone_mm'][0], det['zone_mm'][1], 0.01):
            x = cx + sign * int(round(p * ppmm))
            if x - far < 0 or x + far >= w:
                continue
            lf, rf = prof[x - far:x - near], prof[x + near:x + far]
            if np.median(lf) > base + 35 or np.median(rf) > base + 35:
                continue          # window reaches a pad, fillet or the mask: not inside the body
            flanks = np.concatenate([lf, rf])
            contrast = float(prof[x] - np.median(flanks))
            if contrast > best[0]:
                best = (contrast, round(float(p), 2))
        out[side] = {'score': round(best[0], 1), 'offset_mm': best[1]}
    out['body_level'] = round(base, 1)
    return out





def band_scores_flat(view, det, ppmm=PPMM):
    """Positive full-height stripe contrast (grey levels) on the left and right of the body centre.
    A candidate stripe of width `bw` is accepted only where both flanks are still dark body (inside the body),
    its contrast is the median over the body rows of (stripe mean minus flank mean), so text, which covers only
    part of the rows, does not count. Only a stripe LIGHTER than the epoxy counts (printed colour band)."""
    g = cv2.cvtColor(view, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = g.shape
    cy, cx = h // 2, w // 2
    length, width, bw = det['length_mm'], det['width_mm'], det['band_width_mm']
    rows = g[cy - int(0.38 * width * ppmm):cy + int(0.38 * width * ppmm)]
    prof = np.median(rows, axis=0)
    base = float(np.median(prof[cx - int(.15 * length * ppmm):cx + int(.15 * length * ppmm)]))
    flank, hb = int(0.12 * ppmm), int(bw * ppmm / 2)
    out = {}
    for side, sign in (('left', -1), ('right', 1)):
        best = (0.0, None)
        for p in np.arange(det['zone_mm'][0], det['zone_mm'][1], 0.01):
            x = cx + sign * int(round(p * ppmm))
            if x - hb - flank < 0 or x + hb + flank > w:
                continue
            lf, rf = rows[:, x - hb - flank:x - hb], rows[:, x + hb:x + hb + flank]
            if np.median(lf) > base + 35 or np.median(rf) > base + 35:
                continue          # window reaches a pad, fillet or the mask: not inside the body
            stripe = rows[:, x - hb:x + hb].mean(axis=1)
            # a ridge: lighter than BOTH flanks (a ramp towards the sloped end face is lighter on one flank only)
            contrast = float(np.median(stripe - np.maximum(lf.mean(axis=1), rf.mean(axis=1))))
            if contrast > best[0]:
                best = (contrast, round(float(p), 2))
        out[side] = {'score': round(best[0], 1), 'offset_mm': best[1]}
    out['body_level'] = round(base, 1)
    return out


def band_scores(view, det, ppmm=PPMM):
    """Dispatch on the part's detector method: `ridge` (D3, re-tuned 15 Oct) or the original `flat` stripe detector (D2, D4)."""
    fn = band_scores_ridge if det.get('method') == 'ridge' else band_scores_flat
    return fn(view, det, ppmm)


def corner_scores(view, det, ppmm=PPMM):
    """Strongest dot-sized blob (difference of Gaussians, either polarity) inside each corner zone of the nominal body."""
    g = cv2.cvtColor(view, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = g.shape
    cy, cx = h // 2, w // 2
    half = det['size_mm'] / 2
    best = np.zeros_like(g)
    for sigma in np.linspace(det['sigma_mm'][0], det['sigma_mm'][1], 4):
        s = sigma * ppmm
        best = np.maximum(best, np.abs(cv2.GaussianBlur(g, (0, 0), s) - cv2.GaussianBlur(g, (0, 0), s * 1.6)))
    out = {}
    fx = det.get('corner_fraction', 0.45)
    for quadrant, (sx, sy) in QUAD_VEC.items():
        sy = -sy  # image y runs down
        xs = sorted([cx + int(sx * (1 - fx) * half * ppmm), cx + int(sx * (half + 0.05) * ppmm)])
        ys = sorted([cy + int(sy * (1 - fx) * half * ppmm), cy + int(sy * (half + 0.05) * ppmm)])
        region = best[max(0, ys[0]):ys[1], max(0, xs[0]):xs[1]]
        out[quadrant] = {'score': round(float(region.max()), 1) if region.size else 0.0}
    return out


def decide_band(scores, expected, method='flat'):
    tau, ratio = (TAU_RIDGE, RATIO_RIDGE) if method == 'ridge' else (TAU_BAND, RATIO)
    e, o = scores[expected]['score'], scores[OPPOSITE[expected]]['score']
    margin = round(e - o, 1)
    if e >= tau and e >= ratio * max(o, 1.0):
        return 'pass', margin
    if o >= tau and o >= ratio * max(e, 1.0):
        return 'fail', margin
    return 'review', margin


def decide_corner(scores, expected):
    ranked = sorted(scores, key=lambda q: -scores[q]['score'])
    top, second = scores[ranked[0]]['score'], scores[ranked[1]]['score']
    margin = round(scores[expected]['score'] - scores[OPPOSITE[expected]]['score'], 1)
    if top < TAU_CORNER or top < RATIO * max(second, 1.0):
        return 'review', margin
    return ('pass' if ranked[0] == expected else 'fail'), margin


def measure(entry, view):
    """Detector scores and raw decision for one view of one part."""
    det = entry['detector']
    expected = entry['expected']['side']
    if entry['kind'] == 'band':
        scores = band_scores(view, det)
        status, margin = decide_band(scores, expected, det.get('method', 'flat'))
    else:
        scores = corner_scores(view, det)
        status, margin = decide_corner(scores, expected)
    return scores, status, margin


# ---------------------------------------------------------------- evaluation over a batch

def evaluate_part(designator, entry, boards, reference=None, min_decided=MIN_DECIDED):
    """boards: {board id: registered side entry or None}. Returns {board: result dict} with the flip validation
    folded in. `boards` entries need image, warp, canonical."""
    origin = entry['footprint']['element']
    half = entry['detector']['view_half_mm']
    results, flips = {}, {}
    ref_entry = boards.get(reference) if reference else None
    ref_view = None
    if ref_entry and not ref_entry.get('error') and ref_entry.get('image') is not None:
        ref_view = board_view(ref_entry, origin['x'], origin['y'], half)
    for board, side_entry in boards.items():
        if not side_entry or side_entry.get('error') or side_entry.get('image') is None:
            results[board] = {'designator': designator, 'board': board, 'status': 'not_assessable', 'margin': None, 'scores': {},
                              'raw_status': 'not_assessable', 'note': 'top photo missing or not registered', 'view': None}
            continue
        view = board_view(side_entry, origin['x'], origin['y'], half)
        if not view_usable(view):
            results[board] = {'designator': designator, 'board': board, 'status': 'not_assessable', 'margin': None, 'scores': {},
                              'raw_status': 'not_assessable', 'note': 'part region not fully inside the photo', 'view': None}
            continue
        shift = (0.0, 0.0)
        if ref_view is not None and board != reference and entry['detector'].get('locate_mm') and view_usable(ref_view):     # opt-in per detector
            view, shift = locate(view, ref_view, entry['detector']['locate_mm'])
        scores, status, margin = measure(entry, view)
        f_scores, f_status, f_margin = measure(entry, flip180(view))
        results[board] = {'designator': designator, 'board': board, 'raw_status': status, 'margin': margin, 'scores': scores, 'shift_mm': shift,
                          'flip': {'status': f_status, 'margin': f_margin, 'scores': f_scores}, 'view': view}
    decided = [b for b, r in results.items() if r.get('raw_status') in ('pass', 'fail')]
    hits = [b for b in decided if results[b]['flip']['status'] == ('fail' if results[b]['raw_status'] == 'pass' else 'pass')]
    total = [b for b, r in results.items() if r.get('flip')]
    validation = {'boards': len(total), 'decided': len(decided), 'flip_detected': len(hits),
                  'flip_detection_rate': round(len(hits) / len(decided), 2) if decided else None,
                  'flip_not_detected': sorted(set(decided) - set(hits)),
                  'min_decided': min_decided, 'validated': len(decided) >= min_decided and len(hits) == len(decided)}
    for board, r in results.items():
        if 'flip' not in r:
            continue
        raw = r['raw_status']
        r['status'] = raw if (validation['validated'] or raw == 'review') else 'review'
        r['note'] = describe(designator, entry, r, validation)
    return results, validation


def describe(designator, entry, r, validation):
    expected = entry['expected']['side']
    marking = entry['expected']['marking']
    s = r['scores']
    if entry['kind'] == 'band':
        found = f'band score {expected} {s[expected]["score"]}, {OPPOSITE[expected]} {s[OPPOSITE[expected]]["score"]} grey levels'
    else:
        found = 'corner scores ' + ', '.join(f'{q} {v["score"]}' for q, v in s.items())
    text = f'Expected {marking} on the {expected} side ({entry["expected"]["why"]}). Measured: {found}; margin {r["margin"]}.'
    if r['raw_status'] != r['status']:
        text += (f' Raw reading {r["raw_status"]} withheld: detector not validated for {designator} on this photo set '
                 f'(flip test {validation["flip_detected"]} of {validation["decided"]} decided boards, need {validation.get('min_decided', MIN_DECIDED)}+ and all).')
    elif r['status'] == 'fail':
        text += f' The marking is on the {OPPOSITE[expected]} side: opposite to the design. Look at the part; check polarity electrically.'
    elif r['status'] == 'review':
        text += ' Ambiguous: no clear marking on either side. Look at the crop.'
    return text


# ---------------------------------------------------------------- evidence

def draw_evidence(entry, result, view, ppmm=PPMM):
    """View with the expected marking side outlined (yellow) and the detected stripe / corner marked (cyan)."""
    img = view.copy()
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2
    det = entry['detector']
    expected = entry['expected']['side']
    if entry['kind'] == 'band':
        zone = det['zone_mm']
        for side, colour, thick in ((expected, (0, 215, 255), 3), (OPPOSITE[expected], (150, 150, 150), 1)):
            sign = SIDE_VEC[side][0]
            x0, x1 = sorted([cx + sign * int(zone[0] * ppmm), cx + sign * int(zone[1] * ppmm)])
            y0, y1 = cy - int(0.45 * det['width_mm'] * ppmm), cy + int(0.45 * det['width_mm'] * ppmm)
            cv2.rectangle(img, (x0, y0), (x1, y1), colour, thick)
            if side == expected:
                cv2.putText(img, 'expected K', (x0, max(14, y0 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)
            sc = result['scores'].get(side, {})
            if sc.get('offset_mm') is not None and sc['score'] >= (TAU_RIDGE if det.get('method') == 'ridge' else TAU_BAND):
                x = cx + sign * int(sc['offset_mm'] * ppmm)
                cv2.line(img, (x, y0 + 4), (x, y1 - 4), (255, 255, 0), 2)
                cv2.putText(img, f'band {sc["score"]}', (x - 30, y1 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1, cv2.LINE_AA)
    else:
        half = det['size_mm'] / 2
        fx = det.get('corner_fraction', 0.45)
        for quadrant, (sx, sy) in QUAD_VEC.items():
            sy = -sy
            xs = sorted([cx + int(sx * (1 - fx) * half * ppmm), cx + int(sx * (half + 0.05) * ppmm)])
            ys = sorted([cy + int(sy * (1 - fx) * half * ppmm), cy + int(sy * (half + 0.05) * ppmm)])
            exp = quadrant == expected
            cv2.rectangle(img, (xs[0], ys[0]), (xs[1], ys[1]), (0, 215, 255) if exp else (150, 150, 150), 3 if exp else 1)
            if exp:
                cv2.putText(img, 'expected pin 1', (xs[0], max(14, ys[0] - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 215, 255), 1, cv2.LINE_AA)
    cv2.putText(img, f'{result["board"]} {result["designator"]} {result["status"]}', (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
    return img


def write_evidence(path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 90]):
        raise OSError(f'Cannot write {path}')
    return str(path.resolve())


# ---------------------------------------------------------------- run

def worst_status(statuses):
    if 'fail' in statuses:
        return 'fail'
    if statuses and all(s == 'pass' for s in statuses):
        return 'pass'
    if statuses and all(s == 'not_assessable' for s in statuses):
        return 'not_assessable'
    return 'review'


def sheet_for(tiles, suspect, title, tile=420, columns=3):
    """Evidence sheet in the geometry of `defects.evidence_sheet` (3 columns, 30 px title strip, 22 px label strip, suspect board
    first with a red border) so the review page can place its peer tags on it. tiles: {board: annotated image}, in board order."""
    order = [suspect] + [b for b in tiles if b != suspect]
    cells = []
    for b in order:
        img = tiles.get(b)
        if img is None:
            continue
        s = min(tile / img.shape[1], tile / img.shape[0])
        img = cv2.resize(img, (max(1, round(img.shape[1] * s)), max(1, round(img.shape[0] * s))), interpolation=cv2.INTER_AREA)
        if b == suspect:
            img = cv2.copyMakeBorder(img, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=(40, 50, 240))
        cells.append((img, b + (' (suspect)' if b == suspect else '')))
    h = max(c[0].shape[0] for c in cells) + 26
    w = max(c[0].shape[1] for c in cells) + 8
    rows = (len(cells) + columns - 1) // columns
    sheet = np.full((rows * h + 30, columns * w, 3), 235, np.uint8)
    cv2.putText(sheet, title, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 0, 0), 1, cv2.LINE_AA)
    for k, (img, label) in enumerate(cells):
        r, c = divmod(k, columns)
        top, left = 30 + r * h, c * w
        cv2.putText(sheet, label, (left + 4, top + 16), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 0), 1, cv2.LINE_AA)
        sheet[top + 22:top + 22 + img.shape[0], left + 4:left + 4 + img.shape[1]] = img
    return sheet


def run_batch(side_data, spec, out_dir, log=print, reference=None):
    """side_data: {board: registered top entry}. Returns {'parts': {designator: {board: result}}, 'validation': {...}}
    and writes, per part and board, `out_dir/evidence/<board>/AOI-POL-<designator>.jpg`: an evidence sheet with the part of
    that board first (expected cathode side outlined, detected band marked) and the same part of every other board after it."""
    out_dir = Path(out_dir)
    parts, validation = {}, {}
    for designator, entry in spec['parts'].items():
        entry = {**entry, 'designator': designator}
        if entry['kind'] == 'none':
            parts[designator] = {b: {'designator': designator, 'board': b, 'status': 'review', 'raw_status': 'review', 'margin': None,
                                     'scores': {}, 'note': entry['expected']['why'], 'view': None, 'flip': None}
                                 for b in side_data}
            validation[designator] = {'validated': False, 'decided': 0, 'note': 'no marking defined by the datasheet'}
            continue
        results, val = evaluate_part(designator, entry, side_data, reference, int(spec.get('min_decided', MIN_DECIDED)))
        validation[designator] = val
        tiles = {b: draw_evidence(entry, r, r['view']) for b, r in results.items() if r.get('view') is not None}
        for board, r in results.items():
            if r.get('view') is not None:
                title = f'{board} AOI-POL-{designator}  {r["status"]}  (expected {entry["expected"]["marking"]} on the {entry["expected"]["side"]} side)'
                r['evidence'] = write_evidence(out_dir / 'evidence' / board / f'AOI-POL-{designator}.jpg', sheet_for(tiles, board, title))
            r.pop('view', None)
        parts[designator] = results
        log(f'{designator}: flip test {val["flip_detected"]}/{val["decided"]} decided of {val["boards"]} boards, validated {val["validated"]}')
    batch = {'parts': parts, 'validation': validation}
    batch['systematic'] = systematic(batch)
    return batch


SYSTEMATIC_MIN = 3       # the same polarity fail on this many boards is reported once, as an assembly-process issue


def systematic(batch, min_boards=SYSTEMATIC_MIN):
    """Polarity fails that repeat: [{designator, boards, reviewing, of, carrier}] for every part failing on >= min_boards boards.
    A defect common to the boards cannot be seen by peer comparison; this is the batch-level view of it. `carrier` is the
    board that holds the one result (the first failing board)."""
    out = []
    for designator, results in batch['parts'].items():
        failing = [b for b, r in results.items() if r['status'] == 'fail']
        if len(failing) >= min_boards:
            out.append({'designator': designator, 'boards': failing, 'reviewing': [b for b, r in results.items() if r['status'] == 'review'],
                        'of': len(results), 'carrier': failing[0]})
    return out


def systematic_result(item, batch, out_dir=None):
    """`AOI-POL-SYS-<designator>` for the carrier board, in the aoi_result/0 shape (status fail: it is a reversed part)."""
    d = item['designator']
    n, of = len(item['boards']), item['of']
    note = (f'{d} is reversed on {n} of {of} boards ({", ".join(item["boards"])}). The same polarity fail on several boards is an assembly-process '
            f'issue, not {n} hand errors: check the assembly drawing, the silkscreen and the pick-and-place orientation once, before the next batch.')
    if item['reviewing']:
        note += f' Still at review (marking not resolved in the photo, look by eye): {", ".join(item["reviewing"])}.'
    note += ' Peer comparison cannot see a defect common to all boards; polarity is always checked against the footprint and datasheet.'
    ev = batch['parts'][d][item['carrier']].get('evidence')
    return {'id': f'AOI-POL-SYS-{d}', 'status': 'fail', 'value': f'{d} reversed on {n} of {of} boards', 'evidence': [ev] if ev else [],
            'confidence': 0.8, 'note': note}


def confidence_of(status, validated):
    """0.8 for a decision, 0.4 for a review in a part whose detector is validated on this batch (the marking was looked for and not
    resolved: look by eye), 0.2 for a review of a part the detector cannot decide at all (the review page does not queue those)."""
    if status == 'not_assessable':
        return 0.0
    if status in ('pass', 'fail'):
        return 0.8
    return 0.4 if validated else 0.2


def contract_results(batch, spec, board):
    """AOI-POL-<designator> results for one board in the aoi_result/0 shape."""
    out = []
    for designator, results in batch['parts'].items():
        r = results.get(board)
        if not r:
            continue
        out.append({'id': f'AOI-POL-{designator}', 'status': r['status'], 'value': '', 'evidence': [r['evidence']] if r.get('evidence') else [],
                    'confidence': confidence_of(r['status'], batch['validation'].get(designator, {}).get('validated')),
                    'note': r['note']})
    for item in batch.get('systematic', []):
        if item['carrier'] == board:
            out.insert(0, systematic_result(item, batch))
    return out


def annotate_rows(results, batch, board):
    """V07 (D4 marking) gets the D4 polarity reading in its note; V07a is decided by protocol_logic from the same batch. In place."""
    for r in results:
        if r['id'] == 'V07' and 'D4' in batch['parts']:
            d4 = batch['parts']['D4'].get(board)
            if d4 and d4['note'] not in r['note']:
                r['note'] = (r['note'] + ' D4 polarity (aoi.polarity): ' + d4['status'] + '. ' + d4['note']).strip()


def row_from_parts(row, batch_results, board):
    """Row status for V07a style rows: pass only when every part passes."""
    statuses = [batch_results['parts'][d][board]['status'] for d in row['designators'] if d in batch_results['parts'] and board in batch_results['parts'][d]]
    return worst_status(statuses), statuses


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--photos', required=True)
    ap.add_argument('--map')
    ap.add_argument('--out', required=True, help='output folder (polarity.json and evidence/)')
    ap.add_argument('--layout', default=None)
    ap.add_argument('--result', help='existing aoi_result.json to patch: V07a gets its status from this check and AOI-POL-* are added')
    ap.add_argument('--spec', default=str(SPEC_PATH or ''))
    args = ap.parse_args(argv)
    from polybench.aoi import photo_map
    from polybench.aoi import protocol_run as pr
    from polybench.aoi.protocol import load_protocol
    spec = load_spec(args.spec)
    protocol = load_protocol()
    layout = json.loads(Path(args.layout or next(pr.RECORDED.glob('*_layout_*.json'))).read_text(encoding="utf-8"))
    photos = Path(args.photos)
    mapping = photo_map.build(photos, args.map or (photos / 'map.json' if (photos / 'map.json').is_file() else None))
    files = {b: m['top'] for b, m in mapping.items() if 'top' in m}
    data, _ = pr.analyse_side('top', layout, protocol, files, protocol['reference_board'], 'auto', print)
    batch = run_batch(data, spec, args.out)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'polarity.json').write_text(json.dumps({'schema': 'polystage.aoi_polarity_run/0', 'spec': str(args.spec), **batch}, indent=1), encoding="utf-8")
    if args.result:
        patch_result(Path(args.result), batch, spec, protocol)
    for designator, results in batch['parts'].items():
        print(designator, {b: r['status'] for b, r in results.items()})
    return 0


def patch_result(result_path, batch, spec, protocol):
    """Replace V07a and annotate V07 in an existing aoi_result.json, add AOI-POL-* rows. Idempotent."""
    doc = json.loads(result_path.read_text(encoding="utf-8"))
    rows = {r['id']: r for r in protocol['rows']}
    for board in doc['boards']:
        name = board['board']
        keep = [r for r in board['results'] if not r['id'].startswith('AOI-POL-')]
        for r in keep:
            if r['id'] == 'V07a':
                status, parts = row_from_parts(rows['V07a'], batch, name)
                r['status'] = status
                r['value'] = 'OK' if status == 'pass' else ''
                r['note'] = ('Polarity from the design and datasheet versus the photo (aoi.polarity): ' +
                             '; '.join(f'{d} {batch["parts"][d][name]["status"]}' for d in rows['V07a']['designators'] if d in batch['parts']) + '. ' +
                             'Row passes only when every part passes; Y1 has no datasheet marking and D1 / the ICs are not validated.')
                ev = [batch['parts'][d][name].get('evidence') for d in rows['V07a']['designators'] if d in batch['parts']]
                r['evidence'] = [e for e in ev if e] or r.get('evidence', [])
            if r['id'] == 'V07' and 'D4' in batch['parts']:
                d4 = batch['parts']['D4'].get(name)
                if d4 and d4['note'] not in r['note']:
                    r['note'] = (r['note'] + ' D4 polarity (aoi.polarity): ' + d4['status'] + '. ' + d4['note']).strip()
        board['results'] = keep + contract_results(batch, spec, name)
    result_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


if __name__ == '__main__':
    sys.exit(main())
