"""Operator review of the AOI candidates: decisions file, and the merge into a reviewed `aoi_result`.

The operator looks at each candidate and marks it `confirmed` (a real defect), `rejected` (not a defect)
or `unsure`. Decisions are saved durably in `aoi_review_decisions.json` next to `aoi_result.json`, keyed
by `<board>|<result id>`. Export writes `aoi_result.reviewed.json` (the machine result stays untouched):

  confirmed -> `fail`            (operator-confirmed defect)
  rejected  -> `not_assessable`  (informational: AOI flagged it, the operator says it is not a defect;
                                  value and evidence are cleared as the v0 contract requires)
  unsure    -> `review`          (stays open, even if AOI had said `fail`)
  undecided -> unchanged

The contract `polystage.aoi_result/0` has a fixed key set per result, so the operator is recorded as the
source of the change in the result's `note` (prefix `[operator ...]`) and in the provenance file
`aoi_result.reviewed.provenance.json`, one record per changed result with the previous status.
Pure functions plus small file helpers; no GUI, no server.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

DECISIONS = ('confirmed', 'rejected', 'unsure')
DECISIONS_SCHEMA = 'polystage.aoi_review_decisions/0'
DECISIONS_NAME = 'aoi_review_decisions.json'
REVIEWED_NAME = 'aoi_result.reviewed.json'
PROVENANCE_NAME = 'aoi_result.reviewed.provenance.json'
# polarity first: a reversed polarised part is a certain functional defect (D3, 15 Oct), and a batch-level repeat of it comes before the single results
KIND_ORDER = {'POLSYS': -2, 'POL': -1, 'BRIDGE': 0, 'MISSING': 1, 'LIFT': 2, 'ANOM': 3, 'DIFF': 4}
POL_REVIEW_MIN_CONFIDENCE = 0.4   # polarity at review is queued only where the detector is validated (aoi.polarity.confidence_of); the others stay in the result and in V07a
NEW_STATUS = {'confirmed': 'fail', 'rejected': 'not_assessable', 'unsure': 'review'}


class ReviewError(ValueError):
    pass


def item_key(board, result_id):
    return f'{board}|{result_id}'


def kind_of(result_id):
    parts = result_id.split('-')
    if len(parts) > 2 and parts[1] == 'POL':
        return 'POLSYS' if parts[2] == 'SYS' else 'POL'
    return parts[1] if len(parts) > 2 and parts[1] in KIND_ORDER else 'PART'


def designator_of(result_id):
    """The designator a candidate id is about: AOI-LIFT-C4 -> C4, AOI-BRIDGE-IC4-6-7 -> IC4, AOI-POL-SYS-D3 -> D3, AOI-C23 -> C23."""
    parts = result_id.split('-')
    if len(parts) > 2 and parts[1] == 'DIFF':
        return ''                 # a difference region belongs to no part (layout-free mode)
    if len(parts) > 2 and parts[1] == 'POL':
        return parts[-1]          # AOI-POL-D3 and AOI-POL-SYS-D3
    if len(parts) > 2 and parts[1] in KIND_ORDER:
        return parts[2]
    return parts[1] if len(parts) > 1 else ''


def attach_peers(items):
    """Adds to every item `designator` and `peers`: the candidates of the same designator on the other boards,
    any kind, as {key, board, kind}. The decision of a peer is not stored here; the page reads it live."""
    for item in items:
        item['designator'] = designator_of(item['id'])
    by_des = {}
    for item in items:
        by_des.setdefault(item['designator'], []).append(item)
    for item in items:
        item['peers'] = [{'key': o['key'], 'board': o['board'], 'kind': o['kind']} for o in by_des[item['designator']]
                         if o['board'] != item['board'] and item['designator']]
    return items


def _natural(text):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', text)]


def review_items(document):
    """Every defect candidate still needing a look, most important first: the POL / POLSYS / BRIDGE / MISSING / LIFT / ANOM
    results in review or fail, and any other `AOI-*` result that is a `fail`. The presence-row notes of the
    protocol (`AOI-<designator>` at review) stay in the result for the report and are not queued here."""
    items = []
    for board in document['boards']:
        for r in board['results']:
            if r['id'].startswith('AOI-') and r['status'] in ('review', 'fail') and (kind_of(r['id']) in KIND_ORDER or r['status'] == 'fail') \
                    and not (kind_of(r['id']) == 'POL' and r['status'] == 'review' and r['confidence'] < POL_REVIEW_MIN_CONFIDENCE):
                items.append({'key': item_key(board['board'], r['id']), 'board': board['board'], 'id': r['id'],
                              'kind': kind_of(r['id']), 'status': r['status'], 'value': r['value'], 'note': r['note'],
                              'confidence': r['confidence'], 'evidence': list(r['evidence'])})
    items.sort(key=lambda i: (i['status'] != 'fail', KIND_ORDER.get(i['kind'], 9), 0 if i['kind'] == 'DIFF' else -i['confidence'], i['board'], _natural(i['id'])))
    return items


# ---------------------------------------------------------------- decisions file

def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def empty_decisions(result_path):
    document = json.loads(Path(result_path).read_text())
    return {'schema': DECISIONS_SCHEMA,
            'result': {'file': Path(result_path).name, 'generated': document.get('generated'), 'sha256': file_sha(result_path)},
            'cursor': 0, 'decisions': {}}


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(data, indent=1, sort_keys=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_decisions(decisions_path, result_path):
    """The decisions file, or a fresh one. A file from another run of the tool is kept and flagged `stale`:
    its keys still apply to every result id that exists in this run."""
    path = Path(decisions_path)
    if not path.exists():
        return empty_decisions(result_path)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ReviewError(f'{path} is not valid JSON ({error}); it was left untouched. Move it aside to start again.') from error
    if data.get('schema') != DECISIONS_SCHEMA or not isinstance(data.get('decisions'), dict):
        raise ReviewError(f'{path} is not a {DECISIONS_SCHEMA} file; it was left untouched.')
    data.setdefault('cursor', 0)
    return data


def set_decision(decisions_path, result_path, key, decision, note=''):
    """Record (or clear, with decision None) one decision and write the file atomically. Returns the data."""
    if decision is not None and decision not in DECISIONS:
        raise ReviewError(f'decision must be one of {DECISIONS} or null')
    document = json.loads(Path(result_path).read_text())
    valid = {i['key']: i for i in review_items(document)}
    if key not in valid:
        raise ReviewError(f'{key} is not a candidate of this result')
    data = load_decisions(decisions_path, result_path)
    note = (note or '').strip()
    if decision is None and not note:
        data['decisions'].pop(key, None)
    else:
        item = valid[key]
        old = data['decisions'].get(key, {})
        data['decisions'][key] = {'board': item['board'], 'id': item['id'], 'decision': decision, 'note': note,
                                  'decided_at': datetime.now().astimezone().isoformat(timespec='seconds')
                                  if decision != old.get('decision') or not old else old.get('decided_at')}
    data['result'] = {'file': Path(result_path).name, 'generated': document.get('generated'), 'sha256': file_sha(result_path)}
    _atomic_write(decisions_path, data)
    return data


def set_cursor(decisions_path, result_path, index):
    data = load_decisions(decisions_path, result_path)
    data['cursor'] = max(0, int(index))
    _atomic_write(decisions_path, data)


def progress(items, data):
    done = [i for i in items if data['decisions'].get(i['key'], {}).get('decision')]
    counts = {d: sum(data['decisions'][i['key']]['decision'] == d for i in done) for d in DECISIONS}
    keys = {i['key'] for i in items}
    return {'total': len(items), 'decided': len(done), 'undecided': len(items) - len(done), **counts,
            'stale_keys': sorted(k for k in data['decisions'] if k not in keys)}


# ---------------------------------------------------------------- merge

def merge(document, decisions):
    """(reviewed document, provenance records, summary). `document` is not modified."""
    reviewed = copy.deepcopy(document)
    valid = {i['key'] for i in review_items(document)}
    records = []
    for board in reviewed['boards']:
        for r in board['results']:
            key = item_key(board['board'], r['id'])
            d = decisions['decisions'].get(key)
            if not d or not d.get('decision') or key not in valid:
                continue
            before, was_note = r['status'], r['note']
            who = f'[operator {d["decision"]} {str(d.get("decided_at", ""))[:16].replace("T", " ")}]'.replace('  ', ' ')
            operator_note = (' Operator note: ' + d['note']) if d.get('note') else ''
            new = NEW_STATUS[d['decision']]
            r['status'] = new
            if d['decision'] == 'confirmed':
                r['confidence'] = 1.0
                r['note'] = f'{who} Confirmed defect by the operator.{operator_note} AOI said: {was_note}'
            elif d['decision'] == 'rejected':
                r['value'], r['evidence'], r['confidence'] = '', [], 0.0
                r['note'] = f'{who} Not a defect, per the operator.{operator_note} AOI had flagged: {was_note}'
            else:
                r['note'] = f'{who} Operator is unsure; stays open.{operator_note} AOI said: {was_note}'
            records.append({'board': board['board'], 'id': r['id'], 'decision': d['decision'], 'operator_note': d.get('note', ''),
                            'decided_at': d.get('decided_at'), 'previous_status': before, 'status': new, 'source': 'operator'})
    items = review_items(document)
    summary = {**progress(items, decisions), 'changed': len(records)}
    reviewed['tool'] = document['tool'] + ' + operator review'
    return reviewed, records, summary


def export(result_path, decisions_path=None):
    """Write aoi_result.reviewed.json and its provenance file next to the result. Returns (paths, summary)."""
    result_path = Path(result_path)
    decisions_path = Path(decisions_path) if decisions_path else result_path.with_name(DECISIONS_NAME)
    document = json.loads(result_path.read_text())
    decisions = load_decisions(decisions_path, result_path)
    reviewed, records, summary = merge(document, decisions)
    out = result_path.with_name(REVIEWED_NAME)
    prov = result_path.with_name(PROVENANCE_NAME)
    _atomic_write(out, reviewed)
    _atomic_write(prov, {'source_result': result_path.name, 'source_sha256': file_sha(result_path),
                         'decisions_file': decisions_path.name, 'exported_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                         'summary': summary, 'changes': records})
    return {'reviewed': str(out), 'provenance': str(prov)}, summary
