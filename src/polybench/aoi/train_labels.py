"""Per-batch training labels: `aoi/labels.jsonl`, one Label Studio task per board, rewritten after every decision.

Every operator decision on an AOI candidate (Y confirmed, N not a defect, U unsure) and every operator-found issue is kept,
in board millimetres through `ls_coords`, so the file survives a new registration and a new photo of the same design.
An operator-found issue is also a missed detection (false negative) of the AOI; it is flagged `aoi_missed`.

One line per board, the shape `ls_export.export()` takes (`data`, `annotations`) plus `predictions` as `ls_project.preannotations`
builds them, plus a plain `decisions` list for training code that wants every Y/N/U:

    data         board, image (boardview/<board>.jpg of the run), box_mm, ppmm, size_px, photo, run
    annotations  one annotation: a rectangle (+ note) per confirmed candidate and per operator-found box; 'fully reviewed'
                 is ticked when every issue on that board has a decision
    predictions  the AOI candidates as Label Studio pre-annotations (rejected left out, as ls_project does)
    decisions    every decision: key, id, source ('aoi' | 'operator'), decision, class, box_mm, aoi_missed, note, operator, decided_at
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from polybench.aoi import board_view as BV
from polybench.aoi import ls_coords as C
from polybench.aoi import ls_project as P

SCHEMA = 'polystage.aoi_labels_task/0'
NAME = 'labels.jsonl'
MANUAL_CLASS = {'bridge': 'bridge', 'missing': 'missing', 'lifted': 'lifted_tombstone', 'tombstone': 'lifted_tombstone',
                'polarity': 'wrong_part_or_polarity', 'damage': 'damage', 'other': 'other'}
LS_KINDS = ('AOI-BRIDGE-', 'AOI-LIFT-', 'AOI-MISSING-', 'AOI-ANOM-')


def _region(key):
    return 'rev' + hashlib.sha1(key.encode()).hexdigest()[:8]


def build(state):
    """Label Studio tasks (list of dicts), one per board of the current run's board view. [] before a run."""
    run = state.aoi_current_run()
    if not run:
        return []
    bv = BV.load(Path(run['dir']))
    if not bv:
        return []
    photos = {x['board']: x for x in run.get('photos', []) if x.get('side', 'top') == 'top'}
    items = state.aoi_all_items(run)
    tasks = []
    for board in sorted(bv['boards']):
        meta = {'box_mm': bv['box_mm'], 'ppmm': bv['ppmm'], 'size_px': bv['size_px']}
        mine = [i for i in items if i['board'] == board]
        decisions, regions, aoi_decisions, cands = [], [], {}, []
        undecided = 0
        for it in mine:
            d = state.aoi_decision_for(it['key'], run)
            manual = bool(it.get('manual'))
            if manual:
                box = it['box_mm']
                cls = MANUAL_CLASS.get(it['manual_kind'], 'other')
            else:
                marker = BV.marker_for_id(it['id'], bv, margin=0.15)
                box = marker['box'] if marker else None
                cls = P.class_of_candidate(it['id'])
                if it['id'].startswith(LS_KINDS):
                    cands.append(it['id'])
                if d:
                    aoi_decisions[it['key']] = {'decision': d['decision'], 'note': d.get('note', '')}
            if not d:
                undecided += 1
                continue
            rec = {'key': it['key'], 'id': it['id'], 'source': 'operator' if manual else 'aoi', 'decision': d['decision'],
                   'kind': it['kind'], 'class': cls, 'box_mm': [round(float(v), 3) for v in box] if box else None,
                   'aoi_missed': manual, 'designators': it.get('designators') or [], 'note': d.get('note', ''),
                   'operator': d.get('operator', ''), 'decided_at': d.get('decided_at', '')}
            if manual:
                rec['manual_kind'] = it['manual_kind']
            decisions.append(rec)
            if d['decision'] == 'confirmed' and box:
                value = C.rect_mm_to_ls(meta, box)
                rid = _region(it['key'])
                regions.append({'id': rid, 'from_name': 'label', 'to_name': 'image', 'type': 'rectanglelabels',
                                'original_width': meta['size_px'][0], 'original_height': meta['size_px'][1], 'image_rotation': 0,
                                'value': {**value, 'rectanglelabels': [cls]}})
                text = f"{it['id']} | " + ('operator-found, AOI missed' if manual else 'operator CONFIRMED') + (f" | {d['note']}" if d.get('note') else '')
                regions.append({'id': rid, 'from_name': 'note', 'to_name': 'image', 'type': 'textarea', 'value': {**value, 'text': [text]}})
        reviewed = bool(mine) and undecided == 0
        if reviewed:
            regions.append({'id': 'reviewed', 'from_name': 'reviewed', 'to_name': 'image', 'type': 'choices', 'value': {'choices': ['fully reviewed']}})
        last = max((r['decided_at'] for r in decisions), default='')
        ops = sorted({r['operator'] for r in decisions if r['operator']})
        annotations = [{'id': 1, 'result': regions, 'completed_by': ops[0] if ops else '', 'updated_at': last, 'was_cancelled': False}] if (regions or decisions) else []
        tasks.append({'schema': SCHEMA, 'data': {'board': board, 'image': bv['boards'][board], 'run': run['run'], **meta,
                                                 'photo': (photos.get(board) or {}).get('name')},
                      'annotations': annotations,
                      'predictions': [{'model_version': P.MODEL_VERSION, 'score': 0.5, 'result': P.preannotations(board, cands, aoi_decisions, bv, meta)}],
                      'decisions': decisions, 'undecided': undecided})
    return tasks


def write(state, batch_dir):
    """Rewrite `<batch>/aoi/labels.jsonl` atomically. Returns the path, or None when there is nothing to write yet."""
    tasks = build(state)
    if not tasks:
        return None
    path = Path(batch_dir) / 'aoi' / NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.labels', suffix='.tmp')
    with os.fdopen(fd, 'w') as handle:
        handle.write(''.join(json.dumps(t, sort_keys=True) + '\n' for t in tasks))
    os.replace(tmp, path)
    return path
