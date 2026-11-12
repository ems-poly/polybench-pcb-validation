"""Defect stage orchestration: registered top-side photos -> candidate results `AOI-BRIDGE-...`,
`AOI-LIFT-...`, `AOI-MISSING-...`, `AOI-ANOM-...` in the `polystage.aoi_result/0` result shape.

Rules that cannot be configured away: a joint-level check never returns `pass`; `fail` is only
a large-part absence already established by protocol_logic, or a high-confidence bridge on a pair
the protocol lists in `power_pairs`. Everything else is `review`, to be confirmed by eye.
Only the top side is analysed (no bottom photos are taken for this board).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from polybench.aoi import defects as D
from polybench.aoi import pads as padlib
from polybench.aoi.protocol import class_of

FINE_PITCH_ICS = ('IC1', 'IC2', 'IC4', 'U1')
KINDS = ('BRIDGE', 'LIFT', 'MISSING', 'ANOM')
# parts whose pads are displaced by parallax (tall bodies), left out of the pad-pair check as before
TALL = ('D2', 'L1', 'C11', 'D3')
NETLIST = Path(__file__).parent / 'protocols' / 'netlist.json'


def designators_of(result_id):
    """Designators named by a result id (`AOI-LIFT-C4` -> {C4}; `AOI-BRIDGE-C18.1-Y1.3` -> {C18, Y1})."""
    body = result_id[4:]
    if body.startswith(('BRIDGE-', 'LIFT-', 'MISSING-', 'ANOM-')):
        body = body.split('-', 1)[1]
    return set(re.findall(r'[A-Za-z]+\d+', body))


def confirmed_from_decisions(path):
    """{designator: {board, ...}} for every operator-confirmed candidate in a review decisions file (read only)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))['decisions']
    out = {}
    for d in data.values():
        if d.get('decision') == 'confirmed':
            for des in designators_of(d['id']):
                out.setdefault(des, set()).add(d['board'])
    return out


def natural(name):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


class DefectContext:
    """Registered boards, refined, plus the CAM pad geometry."""

    def __init__(self, entries, layout, protocol, cam_zip, log=print, netlist=None, exclude_confirmed=None):
        self.layout, self.protocol = layout, protocol
        cfg_d = protocol.get("defects") or {}
        self.fine = tuple(cfg_d.get("fine_pitch_ics", FINE_PITCH_ICS))     # per design: the protocol names them, these values are the default
        self.tall = tuple(cfg_d.get("tall", TALL))
        netlist = netlist or cfg_d.get("netlist")
        self.netlist = padlib.load_netlist(netlist) if netlist and Path(netlist).is_file() else None
        self.excluded = confirmed_from_decisions(exclude_confirmed) if exclude_confirmed else {}
        self.bridge_info = []      # same-net pairs: informational, not results
        self.bridge_pairs = []     # every pair examined, with its nets and per-board state
        self.parts = {p['name']: p for p in layout['parts']}
        self.entries = {b: e for b, e in entries.items() if not e.get('error') and e.get('image') is not None}
        D.set_mask_colour((protocol.get('registration') or {}).get('board_colour', 'red'))
        openings = D.read_mask_openings(cam_zip)
        self.refinement = {}
        for b, e in self.entries.items():
            e['warp_fine'], info = D.refine_warp(e, openings)
            self.refinement[b] = info
            log(f'defects: {b} registration refined: {info}')
        self.assigned = padlib.assign_pads(layout, padlib.read_paste_pads(cam_zip))
        self.gain = self._gains()

    # expected state of a designator on a board, from the protocol
    def expected(self, designator, board):
        from polybench.aoi.protocol_logic import expectation
        return expectation(designator, board, self.protocol)

    def peers(self, designator, board):
        skip = self.excluded.get(designator, ())     # boards whose part is an operator-confirmed defect are no reference
        return [b for b in self.entries if b != board and b not in skip and self.expected(designator, b) != 'absent']

    def reference_boards(self, designators, board, among):
        """Boards usable as peer reference of `designators` on `board`: not itself, not an operator-confirmed defect."""
        skip = set().union(*[self.excluded.get(d, set()) for d in designators])
        return [o for o in among if o != board and o not in skip]

    def _gains(self):
        """Per board colour gain that matches its solder-mask colour to the median of the batch."""
        med = {}
        for b, e in self.entries.items():
            hsv = cv2_hsv(e['crop'])
            red = D.mask_of(hsv, 110, 70)
            med[b] = np.median(e['crop'][red].reshape(-1, 3), axis=0) if red.sum() > 500 else None
        ref = np.median(np.stack([m for m in med.values() if m is not None]), axis=0) if any(m is not None for m in med.values()) else None
        return {b: (ref / np.maximum(m, 1) if (m is not None and ref is not None) else None) for b, m in med.items()}

    def view(self, board, box, ppmm=D.CFG['ppmm']):
        return D.make_view(self.entries[board], box, ppmm)


def cv2_hsv(img):
    import cv2
    return cv2.cvtColor(img, cv2.COLOR_BGR2HSV)


# ---------------------------------------------------------------- the four detectors

def legacy_pair_ids(ctx, cfg=D.CFG):
    """The pairs the first defect stage already examined (same ids, same measurement, so earlier results and
    operator decisions stay valid): gaps of the fine-pitch ICs and 0402-class parts next to them."""
    cls = lambda d: class_of(ctx.protocol, d)
    out = {}
    for pr in padlib.bridge_pairs(ctx.assigned, cfg['max_gap_mm'], cfg['min_gap_mm']):
        da, db = pr['a'][0], pr['b'][0]
        ok = lambda d: d in getattr(ctx, 'fine', FINE_PITCH_ICS) or cls(d) == 'small'
        if (ok(da) and ok(db) and not (da not in getattr(ctx, 'fine', FINE_PITCH_ICS) and db not in getattr(ctx, 'fine', FINE_PITCH_ICS))) or (da == db and da in getattr(ctx, 'fine', FINE_PITCH_ICS)):
            out[padlib.pair_id(pr)] = pr
    return out


def pair_frame(pr):
    """(angle_deg, cx, cy) of the frame whose +x runs along the nearest-point line from pad A to pad B."""
    qa, qb = (np.asarray(q, float) for q in pr['points_mm'])
    d = qb - qa
    c = (qa + qb) / 2
    return float(np.degrees(np.arctan2(d[1], d[0]))), float(c[0]), float(c[1])


def to_board(frame, pts_q):
    """Points of the pair frame back to board mm."""
    ang, cx, cy = frame
    c, s = np.cos(np.radians(ang)), np.sin(np.radians(ang))
    r = np.array([[c, -s], [s, c]])
    return (np.asarray(pts_q, float) @ r.T + [cx, cy]).tolist()


def measure_poly(ctx, pr, board, cfg=D.CFG):
    """Measure one pad pair on one board in the frame of the pair (any pad angle). Adds the pads and the gap
    in board mm for the evidence drawing."""
    pa, pb = ctx.assigned[pr['a'][0]][pr['a'][1]], ctx.assigned[pr['b'][0]][pr['b'][1]]
    frame = pair_frame(pr)
    m = D.frame_matrix(*frame)
    q = lambda poly: (np.c_[padlib.pad_polygon(poly), np.ones(4)] @ m.T)[:, :2]
    half = pr['gap_mm'] / 2 + 0.5
    box_q = (-half, -0.9, half, 0.9)
    view, v = D.make_view(ctx.entries[board], box_q, cfg['ppmm'], frame=frame)
    own = D.bridge_pair_poly(view, v, q(pa), q(pb), cfg)
    own['frame'] = frame
    own['_view'] = (view, v, q(pa), q(pb))      # for the evaluation painters; not serialised
    own['pad_polys'] = (to_board(frame, own['poly_a']), to_board(frame, own['poly_b']))
    if own['rows']:
        r0, r1 = own['rows'][0], own['rows'][-1]
        own['gap_poly'] = to_board(frame, [(r0[2], r0[1]), (r0[3], r0[1]), (r1[3], r1[1]), (r1[2], r1[1])])
    else:
        own['gap_poly'] = own['pad_polys'][0]
    return own


def detect_bridges(ctx, cfg=D.CFG):
    """Candidates for every close pair of copper pads on the board, any parts: edge-to-edge gap of at most
    `pair_max_gap_mm`, from the CAM paste layer (rectangular and 45-degree pads). Pairs the first stage already
    examined keep their id and their measurement; the others are measured in the frame of the pair. A pair
    whose two pads carry the SAME named net in the netlist is informational (solder between two GND pads is
    not a short): it is recorded in `ctx.bridge_info`, not returned. Different or unknown nets are candidates."""
    out = []
    legacy = legacy_pair_ids(ctx, cfg)
    pairs = padlib.close_pairs(ctx.assigned, ctx.netlist, cfg['pair_max_gap_mm'], cfg['min_gap_mm'], skip=getattr(ctx, 'tall', TALL))
    # pad pairs of ONE part are only read for the fine-pitch ICs, whose toes show outside the body. The pads of the
    # crystal Y1, the LED D1 and the connector J2 lie under their bodies, and the metal lid reads as 'bright metal'.
    pairs = [p for p in pairs if p['a'][0] != p['b'][0] or p["a"][0] in getattr(ctx, 'fine', FINE_PITCH_ICS)]
    for pr in legacy.values():     # a legacy pair beyond the new range or on a tall part is still examined
        if padlib.pair_id(pr) not in {padlib.pair_id(x) for x in pairs}:
            (da, ia), (db, ib) = pr['a'], pr['b']
            na, nb = padlib.pad_net(ctx.netlist, da, ia), padlib.pad_net(ctx.netlist, db, ib)
            pairs.append({**pr, 'nets': (na, nb), 'relation': padlib.nets_relation(na, nb), 'frame': 0.0, 'points_mm': None})
    measured, state_of = {}, {}
    for pr in pairs:
        key = id_of(pr)
        is_legacy = padlib.pair_id(pr) in legacy
        pa, pb = ctx.assigned[pr['a'][0]][pr['a'][1]], ctx.assigned[pr['b'][0]][pr['b'][1]]
        pr['legacy'] = is_legacy
        for b in ctx.entries:
            if any(ctx.expected(d, b) == 'absent' for d in (pr['a'][0], pr['b'][0])):
                continue
            if is_legacy:
                view, v = ctx.view(b, D.part_window([pa, pb], 0.35))
                measured[(key, b)] = D.bridge_pair(view, v, pa, pb, cfg)
            else:
                measured[(key, b)] = measure_poly(ctx, pr, b, cfg)
    for pr in pairs:
        key = id_of(pr)
        boards = [b for b in ctx.entries if (key, b) in measured]
        (da, ia), (db, ib) = pr['a'], pr['b']
        pa, pb = ctx.assigned[da][ia], ctx.assigned[db][ib]
        rec = {'id': padlib.pair_id(pr), 'a': [da, ia + 1], 'b': [db, ib + 1], 'gap_mm': pr['gap_mm'], 'nets': list(pr['nets']),
               'relation': pr['relation'], 'legacy': pr['legacy'], 'boards': {}}
        for b in boards:
            own = measured[(key, b)]
            ref = ctx.reference_boards((da, db), b, boards)
            peers = [measured[(key, o)] for o in ref]
            state, why = D.bridge_flag(own, peers, cfg)
            rec['boards'][b] = {'state': state, 'bridge_mm': own['bridge_mm'], 'visible_mm': own['visible_mm']}
            if state not in ('candidate', 'high'):
                continue
            if pr['legacy']:
                rects = [{'rect': own['pad_rects'][0], 'colour': (0, 220, 255)}, {'rect': own['pad_rects'][1], 'colour': (0, 220, 255)},
                         {'rect': own['gap_rect'], 'colour': (255, 0, 255)}]
            else:
                rects = [{'poly': own['pad_polys'][0], 'colour': (0, 220, 255)}, {'poly': own['pad_polys'][1], 'colour': (0, 220, 255)},
                         {'poly': own['gap_poly'], 'colour': (255, 0, 255)}]
            power = f'{da}:{ia + 1}-{ib + 1}' in ctx.protocol.get('power_pairs', [])
            status = 'fail' if (state == 'high' and power) else 'review'
            nets = '' if pr['relation'] == 'unknown' else f' Nets {pr["nets"][0]} and {pr["nets"][1]}.'
            where = f'pads {ia + 1} and {ib + 1} of {da}' if da == db else f'{da} pad {ia + 1} and {db} pad {ib + 1}'
            cand = dict(board=b, kind='BRIDGE', id=padlib.pair_id(pr), designator=da, status=status,
                        score=own['bridge_mm'], confidence=0.6 if state == 'high' else 0.4,
                        value=f'{own["bridge_mm"]:.2f} mm bridged',
                        note=f'Possible solder bridge between {where}: {why}. Pad windows snapped by {own["shift_mm"]} mm.{nets}'
                             ' Look at the joint by eye or microscope.',
                        box=D.part_window([pa, pb], 0.5), rects=rects, relation=pr['relation'], nets=list(pr['nets']))
            if pr['relation'] == 'same':
                ctx.bridge_info.append(cand)
            else:
                out.append(cand)
        ctx.bridge_pairs.append(rec)
    return out, measured


def id_of(pr):
    return (pr['a'], pr['b'])


def small_two_pad_parts(ctx):
    # two-terminal parts drawn with rectangular flashes; the pose / missing metrics assume axis-aligned pads
    return [d for d, pads in ctx.assigned.items() if len(pads) == 2 and class_of(ctx.protocol, d) in ('small', 'medium')
            and not any('poly' in p for p in pads)]


def detect_part_state(ctx, cfg=D.CFG):
    """Missing and lifted/skewed/tombstoned two-terminal parts, against the CAD pads and the other boards."""
    out = []
    for d in small_two_pad_parts(ctx):
        pads = ctx.assigned[d]
        box = D.part_window(pads, 0.4)
        metrics = {}
        for b in ctx.entries:
            view, v = ctx.view(b, box)
            metrics[b] = D.lift_metrics(view, v, pads)
        for b, own in metrics.items():
            expected = ctx.expected(d, b)
            peers = [metrics[o] for o in ctx.peers(d, b) if o in metrics]
            rects = [{'rect': D.pad_rect(p), 'colour': (0, 220, 255)} for p in pads] + [
                {'rect': D.two_pad_geometry(pads)[3], 'colour': (255, 0, 255)}]
            zr = own['zone_red']
            peer_zr = [p['zone_red'] for p in peers if p['zone_red'] is not None]
            med_zr = float(np.median(peer_zr)) if peer_zr else 0.0
            absolute = zr is not None and zr >= cfg['missing_red']
            if expected == 'present' and D.missing_rule(zr, peer_zr, cfg):
                med = med_zr
                if not absolute or med < cfg['missing_red'] - 0.2 or not peer_zr:
                    out.append(dict(board=b, kind='MISSING', id=f'AOI-MISSING-{d}', designator=d, status='review', score=zr,
                                    confidence=0.5 if zr >= 0.7 else 0.35, value=f'{d} not seen',
                                    note=f'Bare solder mask between the {d} pads ({zr:.0%} red; the other boards show {med:.0%}): '
                                         'the part may be missing. Look by eye.', box=D.part_window(pads, 0.6), rects=rects))
                continue
            if expected == 'absent' and zr is not None and zr < 0.3 and own.get('along_mm') is not None:
                out.append(dict(board=b, kind='ANOM', id=f'AOI-ANOM-{d}', designator=d, status='review', score=1 - zr, confidence=0.4,
                                value=f'{d} looks fitted',
                                note=f'{d} is DNF in the design but the area between its pads shows a body ({zr:.0%} bare mask). Look by eye.',
                                box=D.part_window(pads, 0.6), rects=rects))
                continue
            if expected != 'present' or zr is None or absolute:
                continue
            reasons = D.lift_flag(own, peers, cfg)
            if reasons:
                out.append(dict(board=b, kind='LIFT', id=f'AOI-LIFT-{d}', designator=d, status='review',
                                score=max(abs(own['along_mm'] or 0), abs(own['across_mm'] or 0)), confidence=0.35,
                                value='; '.join(reasons)[:80],
                                note='Possible lifted, skewed or tombstoned part: ' + '; '.join(reasons) + '. Parallax of tall parts and shadows also do this; look by eye.',
                                box=D.part_window(pads, 0.6), rects=rects))
    return out


def detect_ic_pose(ctx, cfg=D.CFG):
    """Displaced, rotated or half-seen fine-pitch ICs against the other boards (AOI-LIFT-<designator>)."""
    out = []
    for d in getattr(ctx, 'fine', FINE_PITCH_ICS):
        pads = ctx.assigned.get(d)
        if not pads:
            continue
        box = D.part_window(pads, 0.6)
        pose = {}
        for b in ctx.entries:
            if ctx.expected(d, b) == 'absent':
                continue
            view, v = ctx.view(b, box)
            pose[b] = D.ic_pose(view, v, pads)
        for b, own in pose.items():
            reasons = D.ic_flag(own, [pose[o] for o in pose if o != b and o not in ctx.excluded.get(d, ())], cfg)
            if reasons:
                out.append(dict(board=b, kind='LIFT', id=f'AOI-LIFT-{d}', designator=d, status='review', score=1.0, confidence=0.45,
                                value='; '.join(reasons)[:80],
                                note=f'Possible displaced, skewed or lifted {d}: ' + '; '.join(reasons)
                                     + '. Compared with the other boards (and not with the CAD, whose body lies off the pad plane); look by eye.',
                                box=box, rects=[{'rect': D.pad_rect(p), 'colour': (0, 220, 255)} for p in pads]))
    return out


def detect_anomalies(ctx, cfg=D.CFG, ppmm=80):
    """Per designator crop compared with the other boards; the top N per board above the thresholds."""
    scores, boxes = {}, {}
    for d, part in ctx.parts.items():
        if part['side'] != 'top' or d in ctx.protocol['non_parts']:
            continue
        poly = part.get('inspection_polygon') or part.get('polygon')
        if not poly:
            continue
        a = np.asarray(poly, float)
        box = (a[:, 0].min() - 0.3, a[:, 1].min() - 0.3, a[:, 0].max() + 0.3, a[:, 1].max() + 0.3)
        if d in ctx.assigned and not any('poly' in p for p in ctx.assigned[d]):
            box = D.part_window(ctx.assigned[d], 0.35)
        if (box[2] - box[0]) > 9 or (box[3] - box[1]) > 9:   # package polygons that include text
            continue
        use = [b for b in ctx.entries if ctx.expected(d, b) != 'absent']
        if len(use) < 3:
            continue
        crops = {}
        for b in use:
            img, _ = ctx.view(b, box, ppmm)
            g = ctx.gain.get(b)
            crops[b] = np.clip(img * g, 0, 255).astype(np.uint8) if g is not None else img
        skip = ctx.excluded.get(d, ())
        scores[d] = D.anomaly_scores(D.match_medians(crops, skip), exclude=skip)
        boxes[d] = box
    out = []
    for b in ctx.entries:
        ranked = []
        for d, sc in scores.items():
            if b not in sc or sc[b] is None:
                continue
            z = D.robust_z(sc[b], [v for o, v in sc.items() if o != b and o not in ctx.excluded.get(d, ())], 0.03)
            ranked.append((sc[b], z, d))
        ranked.sort(reverse=True)
        for score, z, d in ranked[:cfg['anom_top_n']]:
            if score >= cfg['anom_min_score'] and z >= cfg['anom_min_z']:
                out.append(dict(board=b, kind='ANOM', id=f'AOI-ANOM-{d}', designator=d, status='review', score=round(score, 3),
                                confidence=0.3, value=f'differs from the other boards ({score:.0%} of the crop)',
                                note=f'{d} looks different from the same area on the other boards (score {score:.2f}, robust z {z:.1f}). '
                                     'Colour or shape; the cause is not classified. Look by eye.',
                                box=boxes[d], rects=[{'rect': boxes[d], 'colour': (0, 220, 255)}]))
    return out, scores


# ---------------------------------------------------------------- merge and output

def merge_candidates(candidates):
    """One result per (board, id): keep the most severe, then the highest score."""
    best = {}
    for c in candidates:
        k = (c['board'], c['id'])
        rank = (c['status'] == 'fail', c['score'])
        if k not in best or rank > (best[k]['status'] == 'fail', best[k]['score']):
            best[k] = c
    return list(best.values())


def run_defects(entries, layout, protocol, cam_zip, log=print, cfg=D.CFG, exclude_confirmed=None):
    """`exclude_confirmed`: path of a review decisions file; boards whose part is an operator-confirmed defect
    are dropped from the peer reference of that designator (LIFT, MISSING, ANOM, bridges), not from the check."""
    ctx = DefectContext(entries, layout, protocol, cam_zip, log, exclude_confirmed=exclude_confirmed)
    cand, bridge_measures = detect_bridges(ctx, cfg)
    cand += detect_part_state(ctx, cfg)
    cand += detect_ic_pose(ctx, cfg)
    anom, scores = detect_anomalies(ctx, cfg)
    cand += anom
    cand = merge_candidates(cand)
    cand.sort(key=lambda c: (c['board'], c['status'] != 'fail', -c['score']))
    return ctx, cand


def to_result(candidate, evidence_files):
    """The aoi_result/0 result dict (exact key set). Never `pass`."""
    status = candidate['status']
    assert status in ('review', 'fail'), status
    return {'id': candidate['id'], 'status': status, 'value': candidate['value'], 'evidence': list(evidence_files),
            'confidence': round(float(candidate['confidence']), 2), 'note': candidate['note']}
