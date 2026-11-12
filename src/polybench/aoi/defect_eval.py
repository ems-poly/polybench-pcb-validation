"""Measure the defect detectors: synthetic edits of real crops, and the flag rate on the unedited photos.

    .venv/bin/python -m aoi.defect_eval --photos <folder> [--map map.json] [--out eval.json]

Ground truth is thin (five photos, no labelled defects), so the numbers are of two kinds:
  * detection rate on SYNTHETIC defects painted into real crops (bridge across a pair, removed part,
    shifted / rotated part, tombstone) with the other boards left untouched as peers;
  * flag rate on the UNEDITED real crops. That is an upper bound on the false-flag rate, because some of
    those flags may be real defects; they are listed for confirmation by eye.
The painters are also used by the unit tests on drawn boards.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from polybench.aoi import defects as D
from polybench.aoi import pads as padlib
from polybench.aoi.defect_stage import FINE_PITCH_ICS, TALL, DefectContext, legacy_pair_ids, measure_poly, small_two_pad_parts
from polybench.aoi.protocol import proto_default, class_of

BRIDGE_LENGTHS_MM = (0.10, 0.15, 0.25)


# ---------------------------------------------------------------- painters (operate on a view)

def red_colour(view, window_px):
    x0, y0, x1, y1 = window_px
    patch = view[max(0, y0):y1, max(0, x0):x1]
    red, _, _ = D.classify(patch)
    if red.sum() < 20:
        return np.array([40, 40, 170], np.uint8)
    return np.median(patch[red].reshape(-1, 3), axis=0).astype(np.uint8)


def paint_bridge(view, v, measure, pa, pb, length_mm, ppmm=D.CFG['ppmm']):
    """Paint bright solder across the gap of a pair, `length_mm` long along the pads, centred on the part of
    the toes where both pads show solder. Returns the edited view, or None when no row has solder on both
    pads (a bridge there could not be seen on this photo, so it is not a test case)."""
    red, metal, _ = D.classify(view)
    ra, rb = measure['pad_rects']
    gx0, gy0, gx1, gy1 = measure['gap_rect']
    side_by_side = (gx1 - gx0) < (gy1 - gy0)
    ax = rect_bounds(v, ra)
    bx = rect_bounds(v, rb)
    h, w = metal.shape
    clamp = lambda b: (max(0, b[0]), max(0, b[1]), min(w, b[2]), min(h, b[3]))
    ax, bx = clamp(ax), clamp(bx)
    if side_by_side:
        rows_a = metal[ax[1]:ax[3], ax[0]:ax[2]].mean(axis=1)
        rows_b = metal[bx[1]:bx[3], bx[0]:bx[2]].mean(axis=1)
        y_off = ax[1]
        n = min(len(rows_a), len(rows_b))
    else:
        rows_a = metal[ax[1]:ax[3], ax[0]:ax[2]].mean(axis=0)
        rows_b = metal[bx[1]:bx[3], bx[0]:bx[2]].mean(axis=0)
        y_off = ax[0]
        n = min(len(rows_a), len(rows_b))
    both = (rows_a[:n] >= 0.5) & (rows_b[:n] >= 0.5)
    if both.sum() < 4:
        return None
    best, run, start = (0, 0), 0, 0
    for i, flag in enumerate(both):
        if flag:
            if run == 0:
                start = i
            run += 1
            if run > best[1]:
                best = (start, run)
        else:
            run = 0
    centre = y_off + best[0] + best[1] / 2
    half = max(1.0, length_mm * ppmm / 2)
    colour = np.median(view[ax[1]:ax[3], ax[0]:ax[2]][metal[ax[1]:ax[3], ax[0]:ax[2]]].reshape(-1, 3), axis=0).astype(np.uint8)
    out = view.copy()
    ov = np.zeros(view.shape[:2], np.uint8)
    if side_by_side:
        x0 = int(round(min(ax[2], bx[2]))) - 3
        x1 = int(round(max(ax[0], bx[0]))) + 3
        x0, x1 = min(x0, x1), max(x0, x1)
        ov[int(round(centre - half)):int(round(centre + half)), x0:x1] = 255
    else:
        y0 = int(round(min(ax[3], bx[3]))) - 3
        y1 = int(round(max(ax[1], bx[1]))) + 3
        y0, y1 = min(y0, y1), max(y0, y1)
        ov[y0:y1, int(round(centre - half)):int(round(centre + half))] = 255
    soft = cv2.GaussianBlur(ov, (0, 0), 1.2).astype(np.float32)[..., None] / 255
    out = (out * (1 - soft) + colour.astype(np.float32) * soft).astype(np.uint8)
    return out


def paint_bridge_poly(view, v, measure, length_mm, ppmm=D.CFG['ppmm']):
    """`paint_bridge` for a pair measured in its own frame (`D.bridge_pair_poly`): bright solder across the gap,
    `length_mm` long along the facing edges, centred on the longest run of rows where solder sits on both pads
    next to the gap. Returns the edited view, or None when no such rows exist (not a test case)."""
    red, metal, _ = D.classify(view)
    rows = measure['rows']
    h, w = metal.shape

    def solder_at(x, y):
        px, py = D.to_px(v, x, y)
        px, py = int(round(px)), int(round(py))
        return 0 <= px < w and 0 <= py < h and bool(metal[py, px])

    both = [solder_at(xa - 0.05, y) and solder_at(xb + 0.05, y) for _, y, xa, xb in rows]
    best, run, start = (0, 0), 0, 0
    for i, flag in enumerate(both):
        if flag:
            run = run + 1 if run else 1
            start = i if run == 1 else start
            if run > best[1]:
                best = (start, run)
        else:
            run = 0
    if best[1] < 4:
        return None
    centre = best[0] + best[1] / 2
    half = max(1.0, length_mm * ppmm / 2)
    a_poly = np.array([D.to_px(v, x, y) for x, y in measure['poly_a']], np.int32)
    amask = np.zeros(metal.shape, np.uint8)
    cv2.fillConvexPoly(amask, a_poly, 1)
    sel = metal & (amask > 0)
    colour = np.median(view[sel].reshape(-1, 3), axis=0).astype(np.uint8) if sel.sum() > 10 else np.array([200, 200, 200], np.uint8)
    ov = np.zeros(metal.shape, np.uint8)
    r0, r1 = int(round(rows[0][0] + centre - half)), int(round(rows[0][0] + centre + half))
    xa = np.median([r[2] for r in rows])
    xb = np.median([r[3] for r in rows])
    c0, c1 = sorted((int(round(D.to_px(v, xa - 0.03, 0)[0])), int(round(D.to_px(v, xb + 0.03, 0)[0]))))
    ov[max(0, r0):r1, max(0, c0):c1] = 255
    soft = cv2.GaussianBlur(ov, (0, 0), 1.2).astype(np.float32)[..., None] / 255
    return (view * (1 - soft) + colour.astype(np.float32) * soft).astype(np.uint8)


def rect_bounds(v, rect):
    return D.rect_px(v, rect)


def _part_mask(view, v, pads, margin=0.12):
    red, metal, dark = D.classify(view)
    x0, y0, x1, y1 = D.rect_px(v, D.part_window(pads, margin))
    h, w = red.shape
    window = np.zeros((h, w), np.uint8)
    window[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = 255
    part = ((~red) & (window > 0)).astype(np.uint8) * 255
    part = cv2.morphologyEx(part, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return part, window, (x0, y0, x1, y1)


def edit_part(view, v, pads, kind, amount, ppmm=D.CFG['ppmm']):
    """Synthetic part defects on a view. kind: missing | shift | rotate | tombstone.
    shift: amount in mm along the pad axis; rotate: degrees; tombstone/missing: amount unused."""
    axis, centre, pitch, zone = D.two_pad_geometry(pads)
    part, window, (x0, y0, x1, y1) = _part_mask(view, v, pads)
    fill = red_colour(view, (x0, y0, x1, y1))
    out = view.copy()
    bare = np.array([185, 190, 195], np.uint8)
    if kind in ('missing', 'tombstone'):
        region = window.copy() if kind == 'missing' else np.zeros_like(window)
        if kind == 'tombstone':
            pb = pads[1]
            bx0, by0, bx1, by1 = D.rect_px(v, D.pad_rect(pb))
            zx0, zy0, zx1, zy1 = D.rect_px(v, zone)
            # the far end lifts: its pad and half of the body area go bare
            region[min(by0, zy0 if abs(axis[1]) > .5 else by0):max(by1, zy1 if abs(axis[1]) > .5 else by1),
                   min(bx0, zx0 if abs(axis[0]) > .5 else bx0):max(bx1, zx1 if abs(axis[0]) > .5 else bx1)] = 255
        out[region > 0] = fill
        for p in (pads if kind == 'missing' else pads[1:]):
            a0, b0, a1, b1 = D.rect_px(v, D.pad_rect(p))
            sub = out[b0:b1, a0:a1]
            sub[:] = bare
        return cv2.GaussianBlur(out, (3, 3), 0) if False else out
    cx, cy = D.to_px(v, *centre)
    if kind == 'shift':
        dx, dy = axis[0] * amount * ppmm, -axis[1] * amount * ppmm      # view y is flipped
        m = np.float32([[1, 0, dx], [0, 1, dy]])
    elif kind == 'rotate':
        m = cv2.getRotationMatrix2D((cx, cy), amount, 1.0)
    else:
        raise ValueError(kind)
    moved = cv2.warpAffine(view, m, (view.shape[1], view.shape[0]), flags=cv2.INTER_LINEAR)
    moved_mask = cv2.warpAffine(part, m, (view.shape[1], view.shape[0]), flags=cv2.INTER_NEAREST) > 0
    out[(part > 0)] = fill
    for p in pads:     # the pads stay where the board has them
        a0, b0, a1, b1 = D.rect_px(v, D.pad_rect(p))
        out[b0:b1, a0:a1] = view[b0:b1, a0:a1]
    out[moved_mask] = moved[moved_mask]
    return out


# ---------------------------------------------------------------- real photo context

def build_context(photos, map_file, layout_path, protocol_path=None, cam_zip=None, log=lambda *_: None):
    from polybench.aoi import photo_map, protocol_run as pr
    from polybench.aoi.protocol import load_protocol
    protocol = load_protocol(protocol_path or proto_default())
    layout = json.loads(Path(layout_path).read_text(encoding="utf-8"))
    mapping = photo_map.build(photos, map_file)
    files = {b: m['top'] for b, m in mapping.items() if 'top' in m}
    data, _ = pr.analyse_side('top', layout, protocol, files, protocol['reference_board'], 'auto', log)
    return DefectContext(data, layout, protocol, cam_zip or protocol['layout']['cam_zip'], log), protocol


# ---------------------------------------------------------------- measurements

def eval_bridges(ctx, cfg=D.CFG):
    pairs = []
    for pr_ in padlib.bridge_pairs(ctx.assigned, cfg['max_gap_mm'], cfg['min_gap_mm']):
        if pr_['a'][0] == pr_['b'][0] and pr_['a'][0] in FINE_PITCH_ICS:
            pairs.append(pr_)
    real = {}
    for pr_ in pairs:
        pa, pb = ctx.assigned[pr_['a'][0]][pr_['a'][1]], ctx.assigned[pr_['b'][0]][pr_['b'][1]]
        box = D.part_window([pa, pb], 0.35)
        for b in ctx.entries:
            if ctx.expected(pr_['a'][0], b) == 'absent':
                continue
            view, v = ctx.view(b, box)
            real[(pr_['a'], pr_['b'], b)] = (D.bridge_pair(view, v, pa, pb, cfg), view, v, pa, pb)
    counts = {'assessable': 0, 'flagged_real': 0}
    detect = {l: {'tested': 0, 'detected': 0} for l in BRIDGE_LENGTHS_MM}
    by_ic = {}
    for (a, b_, b), (own, view, v, pa, pb) in real.items():
        peers = [real[(a, b_, o)][0] for o in ctx.entries if o != b and (a, b_, o) in real]
        state, _ = D.bridge_flag(own, peers, cfg)
        if state == 'not_assessable':
            continue
        counts['assessable'] += 1
        counts['flagged_real'] += state in ('candidate', 'high')
        for l in BRIDGE_LENGTHS_MM:
            edited = paint_bridge(view, v, own, pa, pb, l)
            if edited is None:
                continue
            m2 = D.bridge_pair(edited, v, pa, pb, cfg)
            st2, _ = D.bridge_flag(m2, peers, cfg)
            detect[l]['tested'] += 1
            detect[l]['detected'] += st2 in ('candidate', 'high')
            ic = by_ic.setdefault(a[0], {}).setdefault(l, [0, 0])
            ic[0] += 1
            ic[1] += st2 in ('candidate', 'high')
    return {'pairs': len(pairs), **counts, 'detection': {str(l): d for l, d in detect.items()},
            'by_ic': {k: {str(l): v for l, v in x.items()} for k, x in by_ic.items()}}


def eval_pairs(ctx, cfg=D.CFG, relations=('different', 'unknown', 'same')):
    """The pad pairs added by the all-pad check (every pair of pads of any parts at most `pair_max_gap_mm`
    apart, minus the pairs the first stage already read), measured in the frame of the pair:
      * flag rate on the unedited real photos (upper bound of the false-flag rate),
      * detection of bright solder painted across the gap, 0.10 / 0.15 / 0.25 mm, other boards unedited as peers.
    Before this check these pairs were not examined at all, so their detection before is 0 by construction."""
    legacy = legacy_pair_ids(ctx, cfg)
    pairs = [p for p in padlib.close_pairs(ctx.assigned, ctx.netlist, cfg['pair_max_gap_mm'], cfg['min_gap_mm'], skip=TALL)
             if padlib.pair_id(p) not in legacy and (p['a'][0] != p['b'][0] or p['a'][0] in FINE_PITCH_ICS)]
    res = {r: {'pairs': 0, 'assessable': 0, 'flagged_real': 0, 'flagged_ids': [],
               'detection': {str(l): {'tested': 0, 'detected': 0} for l in BRIDGE_LENGTHS_MM}} for r in relations}
    rotated = {d for d, pl in ctx.assigned.items() if any('poly' in p for p in pl)}   # Y1, C18, C19, C23, J2: no pads before
    res['rotated_parts'] = {'pairs': 0, 'assessable': 0, 'flagged_real': 0, 'flagged_ids': [],
                            'detection': {str(l): {'tested': 0, 'detected': 0} for l in BRIDGE_LENGTHS_MM}, 'parts': sorted(rotated)}
    for pr_ in pairs:
        rel = pr_['relation']
        if rel not in res:
            continue
        keys = [rel] + (['rotated_parts'] if {pr_['a'][0], pr_['b'][0]} & rotated and rel != 'same' else [])
        for k in keys:
            res[k]['pairs'] += 1
        meas = {b: measure_poly(ctx, pr_, b, cfg) for b in ctx.entries
                if not any(ctx.expected(d, b) == 'absent' for d in (pr_['a'][0], pr_['b'][0]))}
        for b, own in meas.items():
            peers = [meas[o] for o in meas if o != b]
            state, _ = D.bridge_flag(own, peers, cfg)
            if state == 'not_assessable':
                continue
            for k in keys:
                res[k]['assessable'] += 1
                if state in ('candidate', 'high'):
                    res[k]['flagged_real'] += 1
                    res[k]['flagged_ids'].append(f'{b}|{padlib.pair_id(pr_)}')
            view, v, qa, qb = own['_view']
            for l in BRIDGE_LENGTHS_MM:
                edited = paint_bridge_poly(view, v, own, l)
                if edited is None:
                    continue
                m2 = D.bridge_pair_poly(edited, v, qa, qb, cfg)
                st2, _ = D.bridge_flag(m2, peers, cfg)
                for k in keys:
                    res[k]['detection'][str(l)]['tested'] += 1
                    res[k]['detection'][str(l)]['detected'] += st2 in ('candidate', 'high')
    return res


def eval_parts(ctx, cfg=D.CFG):
    kinds = [('missing', 0), ('shift', 0.3), ('shift', 0.5), ('shift', 0.7), ('rotate', 15), ('rotate', 30), ('rotate', 45), ('tombstone', 0)]
    res = {f'{k}:{a}': {'tested': 0, 'detected': 0} for k, a in kinds}
    real_flags = {'parts_boards': 0, 'missing_flagged': 0, 'lift_flagged': 0}
    for d in small_two_pad_parts(ctx):
        pads = ctx.assigned[d]
        if class_of(ctx.protocol, d) != 'small' or ctx.expected(d, 'B1') != 'present':
            continue
        box = D.part_window(pads, 0.4)
        views = {b: ctx.view(b, box) for b in ctx.entries}
        metrics = {b: D.lift_metrics(vw, v, pads) for b, (vw, v) in views.items()}
        for b, (vw, v) in views.items():
            if ctx.expected(d, b) != 'present':
                continue
            peers_b = [o for o in ctx.peers(d, b)]
            peers = [metrics[o] for o in peers_b]
            peer_zr = [p['zone_red'] for p in peers]
            own = metrics[b]
            real_flags['parts_boards'] += 1
            real_flags['missing_flagged'] += D.missing_rule(own['zone_red'], peer_zr, cfg)
            real_flags['lift_flagged'] += bool(D.lift_flag(own, peers, cfg)) and not D.missing_rule(own['zone_red'], peer_zr, cfg)
            if D.missing_rule(own['zone_red'], peer_zr, cfg):
                continue   # already a missing candidate on the real photo: not a clean base for edits
            for k, a in kinds:
                edited = edit_part(vw, v, pads, k, a)
                m = D.lift_metrics(edited, v, pads)
                key = f'{k}:{a}'
                res[key]['tested'] += 1
                hit = D.missing_rule(m['zone_red'], peer_zr, cfg) if k == 'missing' else (
                    bool(D.lift_flag(m, peers, cfg)) or D.missing_rule(m['zone_red'], peer_zr, cfg))
                res[key]['detected'] += bool(hit)
    return {'detection': res, 'real': real_flags}


def eval_anomaly(ctx, cfg=D.CFG, ppmm=80):
    """Edited crops of small two-pad parts against the unedited other boards: does the part's anomaly
    score pass the ANOM thresholds (score and robust z)?"""
    kinds = [('missing', 0), ('shift', 0.5), ('rotate', 30), ('unedited', 0)]
    res = {f'{k}:{a}': {'tested': 0, 'detected': 0} for k, a in kinds}
    for d in small_two_pad_parts(ctx):
        pads = ctx.assigned[d]
        if class_of(ctx.protocol, d) != 'small' or ctx.expected(d, 'B1') != 'present':
            continue
        box = D.part_window(pads, 0.35)
        views = {b: ctx.view(b, box, ppmm) for b in ctx.entries}
        for target in ctx.entries:
            if ctx.expected(d, target) != 'present':
                continue
            for k, a in kinds:
                crops = {}
                for b, (vw, v) in views.items():
                    img = vw if (b != target or k == 'unedited') else edit_part(vw, v, pads, k, a, ppmm=ppmm)
                    g = ctx.gain.get(b)
                    crops[b] = np.clip(img * g, 0, 255).astype(np.uint8) if g is not None else img
                sc = D.anomaly_scores(D.match_medians(crops))
                z = D.robust_z(sc[target], [x for o, x in sc.items() if o != target], 0.03)
                res[f'{k}:{a}']['tested'] += 1
                res[f'{k}:{a}']['detected'] += bool(sc[target] >= cfg['anom_min_score'] and z >= cfg['anom_min_z'])
    return res


# ---------------------------------------------------------------- scoring against human labels

# which candidate kinds can be a hit for a label class (strict); every class can also be "seen" by ANY candidate
# at its location, which is reported separately because ANOM and the other kinds are not class-specific
CLASS_KINDS = {'bridge': {'BRIDGE'}, 'missing': {'MISSING'}, 'lifted_tombstone': {'LIFT', 'MISSING'}, 'misaligned': {'LIFT'},
               'solder_ball': {'ANOM'}, 'insufficient_solder': {'ANOM'}, 'excess_solder': {'ANOM', 'BRIDGE'},
               'wrong_part_or_polarity': {'ANOM', 'POL', 'POLSYS'}, 'damage': {'ANOM'}, 'other': {'ANOM'}}
NO_DETECTOR = ('solder_ball', 'insufficient_solder', 'excess_solder', 'damage', 'other')   # polarity has one since 15 Oct (aoi.polarity)


def _overlap(a, b):
    return min(a[2], b[2]) - max(a[0], b[0]) > 0 and min(a[3], b[3]) - max(a[1], b[1]) > 0


def candidate_kind(result_id):
    parts = result_id.split('-')
    if len(parts) > 2 and parts[1] == 'POL':
        return 'POLSYS' if parts[2] == 'SYS' else 'POL'
    return parts[1] if len(parts) > 2 and parts[1] in ('BRIDGE', 'LIFT', 'MISSING', 'ANOM') else None


def score_labels(candidates, labels_doc, geometry, margin=0.25):
    """Score detector candidates against human labels.

    candidates: [{'board', 'id'}] (the defect candidates of one run); labels_doc: the `polystage.aoi_labels/0`
    document of `aoi.ls_export`; geometry: the run's boardview.json (to place a candidate id on the board).
    Recall per class: labels of the class with a candidate of a compatible kind overlapping them (`hit_kind`), and
    with ANY candidate overlapping them (`hit_any`). Only labels on boards are counted (a label is a defect
    wherever drawn). False flags per board are counted ONLY on boards the annotator marked fully reviewed: a
    candidate overlapping no label of any class there. On a board not marked reviewed nothing is a false flag."""
    from polybench.aoi import board_view as BV
    placed = []
    for c in candidates:
        kind = candidate_kind(c['id'])
        marker = BV.marker_for_id(c['id'], geometry, margin=margin)
        if kind and marker:
            placed.append({**c, 'kind': kind, 'box': marker['box']})
    labels = labels_doc['labels']
    per_class = {}
    for lab in labels:
        row = per_class.setdefault(lab['class'], {'labelled': 0, 'hit_kind': 0, 'hit_any': 0})
        row['labelled'] += 1
        near = [c for c in placed if c['board'] == lab['board'] and _overlap(c['box'], lab['box_mm'])]
        row['hit_any'] += bool(near)
        row['hit_kind'] += any(c['kind'] in CLASS_KINDS.get(lab['class'], set()) for c in near)
    for row in per_class.values():
        row['recall_kind'] = round(row['hit_kind'] / row['labelled'], 3)
        row['recall_any'] = round(row['hit_any'] / row['labelled'], 3)
    reviewed = [b for b, v in labels_doc['boards'].items() if v.get('reviewed')]
    flags = {}
    for b in reviewed:
        mine = [c for c in placed if c['board'] == b]
        false = [c['id'] for c in mine if not any(l['board'] == b and _overlap(c['box'], l['box_mm']) for l in labels)]
        flags[b] = {'candidates': len(mine), 'false_flags': len(false), 'false_ids': false}
    return {'labels': len(labels), 'boards_reviewed': reviewed, 'per_class': per_class,
            'no_dedicated_detector': [c for c in NO_DETECTOR if c in per_class], 'false_flags_per_board': flags,
            'false_flags_mean_per_reviewed_board': round(sum(v['false_flags'] for v in flags.values()) / len(flags), 2) if flags else None}


def candidates_of_run(result_path):
    doc = json.loads(Path(result_path).read_text(encoding="utf-8"))
    return [{'board': b['board'], 'id': r['id']} for b in doc['boards'] for r in b['results']
            if r['id'].startswith(('AOI-BRIDGE-', 'AOI-LIFT-', 'AOI-MISSING-', 'AOI-ANOM-', 'AOI-POL-')) and r['status'] in ('review', 'fail')]


# ---------------------------------------------------------------- scoring polarity against human truth

POLARITY_TRUTH_SCHEMA = 'polystage.aoi_polarity_truth/0'


def score_polarity(result_doc, truth_doc):
    """Score the `AOI-POL-<designator>` results of a run against the polarity truth file (`aoi/labels/*_polarity_truth.json`).
    Each truth entry is {board, designator, truth: 'reversed' | 'correct'}. A reversed part is `detected` when the result is
    `fail`, `surfaced` when it is `review` (it reaches the queue but AOI could not decide) and `missed` when it is `pass`,
    absent or not assessable (the dangerous outcomes: `missed_as_pass` is a wrong verdict). A correct part is `ok` on `pass`,
    `false_fail` on `fail`, `undecided` otherwise. Results of designators with no truth entry are listed, not scored."""
    got = {(b['board'], r['id'][len('AOI-POL-'):]): r['status'] for b in result_doc['boards'] for r in b['results']
           if r['id'].startswith('AOI-POL-') and not r['id'].startswith('AOI-POL-SYS-')}
    rows, counts = [], {}
    for t in truth_doc['truth']:
        status = got.get((t['board'], t['designator']), 'absent')
        if t['truth'] == 'reversed':
            outcome = {'fail': 'detected', 'review': 'surfaced', 'pass': 'missed_as_pass'}.get(status, 'missed')
        else:
            outcome = {'pass': 'ok', 'fail': 'false_fail'}.get(status, 'undecided')
        rows.append({'board': t['board'], 'designator': t['designator'], 'truth': t['truth'], 'status': status, 'outcome': outcome})
        counts[outcome] = counts.get(outcome, 0) + 1
    reversed_total = sum(r['truth'] == 'reversed' for r in rows)
    scored = {(r['board'], r['designator']) for r in rows}
    return {'source': truth_doc.get('source'), 'reversed_labelled': reversed_total, 'counts': counts,
            'detected_rate': round(counts.get('detected', 0) / reversed_total, 3) if reversed_total else None,
            'reaches_queue_rate': round((counts.get('detected', 0) + counts.get('surfaced', 0)) / reversed_total, 3) if reversed_total else None,
            'rows': rows, 'unlabelled': {f'{b}|{d}': s for (b, d), s in sorted(got.items()) if (b, d) not in scored and s == 'fail'}}


def rates(section):
    for v in section.values():
        if isinstance(v, dict) and 'tested' in v:
            v['rate'] = round(v['detected'] / v['tested'], 3) if v['tested'] else None
    return section


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--photos', help='folder of the board photos (synthetic and flag-rate measurements)')
    ap.add_argument('--labels', help='label file from aoi.ls_export: score the run in --result against these human labels instead')
    ap.add_argument('--polarity-truth', help='polarity truth file (aoi/labels/polarity_truth.json): score the AOI-POL-* results of --result against it')
    ap.add_argument('--result', help='aoi_result.json of the run to score with --labels (boardview/boardview.json beside it)')
    ap.add_argument('--map')
    ap.add_argument('--layout', default=str(Path(__file__).parent / 'protocols' / 'layout.json'))
    ap.add_argument('--out')
    args = ap.parse_args(argv)
    if args.polarity_truth:
        if not args.result:
            ap.error('--polarity-truth needs --result')
        out = score_polarity(json.loads(Path(args.result).read_text(encoding="utf-8")), json.loads(Path(args.polarity_truth).read_text(encoding="utf-8")))
        text = json.dumps(out, indent=1)
        print(text)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return 0
    if args.labels:
        from polybench.aoi import board_view as BV
        if not args.result:
            ap.error('--labels needs --result')
        geometry = BV.load(Path(args.result).parent)
        out = score_labels(candidates_of_run(args.result), json.loads(Path(args.labels).read_text(encoding="utf-8")), geometry)
        text = json.dumps(out, indent=1)
        print(text)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return 0
    if not args.photos:
        ap.error('--photos is required (or --labels with --result)')
    ctx, protocol = build_context(args.photos, args.map, args.layout, log=print)
    out = {'bridges': eval_bridges(ctx), 'pairs': eval_pairs(ctx), 'parts': eval_parts(ctx), 'anomaly': eval_anomaly(ctx)}
    for section in (out['bridges']['detection'], out['parts']['detection'], out['anomaly']):
        rates(section)
    for rel in out['pairs'].values():
        rates(rel['detection'])
    for l, v in out['bridges']['detection'].items():
        pass
    text = json.dumps(out, indent=1)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
