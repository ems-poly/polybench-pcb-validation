"""Glue between the defect stage and the protocol run: evidence images, result dicts, run-log entry."""
from __future__ import annotations

import json
from pathlib import Path

import cv2

from polybench.aoi import board_view as BV
from polybench.aoi import defect_stage as S
from polybench.aoi import defects as D
from polybench.aoi.protocol import class_of


def _write(path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 88]):
        raise OSError(f'Cannot write {path}')
    return str(path.resolve())


def run_stage(entries, layout, protocol, cam_zip, parts, evidence_dir, log=print, exclude_confirmed=None):
    """Returns ({board: [result dicts]}, runlog section)."""
    ctx, candidates = S.run_defects(entries, layout, protocol, cam_zip, log, exclude_confirmed=exclude_confirmed)
    results = {}
    for c in candidates:
        title = f'{c["board"]} {c["id"]}  {c["value"]}'
        sheet = D.evidence_sheet(ctx.entries, c['board'], c['box'], c['rects'], title, ppmm=200 if c['kind'] != 'ANOM' else 120,
                                 tile=420, columns=3)
        path = _write(Path(evidence_dir) / c['board'] / f'{c["id"]}.jpg', sheet)
        results.setdefault(c['board'], []).append(S.to_result(c, [path]))
    try:   # the full-board images for the review page; additive, never loses the defect results
        BV.write(ctx.entries, layout, ctx.assigned, Path(evidence_dir).parent, log=log)
    except Exception as error:
        log(f'defects: board views not written: {type(error).__name__}: {error}')
    for board, rows in results.items():
        rows.sort(key=lambda r: (r['status'] != 'fail', -r['confidence'], S.natural(r['id'])))
    try:   # every examined pad pair with its nets, and the same-net ones that were kept out of the results
        info = {'schema': 'polystage.aoi_bridge_pairs/0', 'pair_max_gap_mm': D.CFG['pair_max_gap_mm'], 'netlist': 'aoi/protocols/netlist.json',
                'pairs': ctx.bridge_pairs,
                'same_net_flagged': [{k: c[k] for k in ('board', 'id', 'score', 'nets', 'note')} for c in ctx.bridge_info]}
        (Path(evidence_dir).parent / 'bridge_pairs.json').write_text(json.dumps(info, indent=1))
    except Exception as error:
        log(f'defects: bridge_pairs.json not written: {type(error).__name__}: {error}')
    counts = {}
    for c in candidates:
        counts[c['kind']] = counts.get(c['kind'], 0) + 1
    section = {'enabled': True, 'candidates': counts, 'registration': ctx.refinement, 'config': dict(D.CFG),
               'pads_assigned': {d: len(p) for d, p in ctx.assigned.items()},
               'resolution': {b: D.resolution_report(D.native_px_per_mm(e)) for b, e in ctx.entries.items()}, 'side': 'top only',
               'bridge_pairs': {'examined': len(ctx.bridge_pairs), 'different_net': sum(p['relation'] == 'different' for p in ctx.bridge_pairs),
                                'unknown_net': sum(p['relation'] == 'unknown' for p in ctx.bridge_pairs), 'same_net': sum(p['relation'] == 'same' for p in ctx.bridge_pairs),
                                'same_net_flagged': len(ctx.bridge_info), 'exclude_confirmed': exclude_confirmed},
               'not_covered': ['joints under QFN/LGA bodies', 'bottom side', 'polarity', 'wrong value']}
    return results, section


def missing_large(final, protocol):
    """AOI-MISSING-<d> for a large part that the exposed-mask test already reports absent (fail), so the
    absence appears under the same id family as the other defects. Returns extra result dicts."""
    out = []
    for r in final:
        if not r['id'].startswith('AOI-') or r['status'] != 'fail' or not r['value'].endswith(' absent'):
            continue
        designator = r['id'][4:]
        if class_of(protocol, designator) != 'large':
            continue
        out.append({'id': f'AOI-MISSING-{designator}', 'status': 'fail', 'value': r['value'], 'evidence': list(r['evidence']),
                    'confidence': r['confidence'], 'note': r['note']})
    return out
