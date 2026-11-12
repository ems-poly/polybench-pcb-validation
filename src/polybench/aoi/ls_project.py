"""Create or refresh the Label Studio project "AOI defects" for one AOI run.

    tools/ls_aoi.sh setup ~/.cache/aoi-defects/run2 [--decisions ~/.cache/aoi-defects/final/aoi_review_decisions.json]

* renders the five registered top photos at full resolution (default 110 px/mm, the photo's own scale) in the
  layout orientation (upright, y up) into `<data>/serve/files/<board>.jpg`,
* creates the project with the labelling config below and a local-files storage so LS may serve the images,
* imports one task per board with PRE-ANNOTATIONS (LS predictions): every AOI candidate of the run, shaped by
  the operator's decisions: confirmed -> score 1.0, unsure -> 0.5 and a FLAGGED note, undecided -> 0.3, rejected
  -> left out. A prediction is a suggestion: it becomes a label when the annotator accepts it.

An image with no regions counts as "no defect" only when its "fully reviewed" box is ticked.
The LS instance is the AOI one (`tools/ls_aoi.sh start`, port 8090), never the SumCheck one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from polybench.aoi import board_view as BV
from polybench.aoi import ls_coords as C

PROJECT_TITLE = 'AOI defects'
MODEL_VERSION = 'aoi-candidates'
CLASSES = ('bridge', 'missing', 'lifted_tombstone', 'misaligned', 'solder_ball', 'insufficient_solder', 'excess_solder',
           'wrong_part_or_polarity', 'damage', 'other')
COLOURS = ('#e11d48', '#2563eb', '#f59e0b', '#7c3aed', '#0d9488', '#ca8a04', '#ea580c', '#db2777', '#4b5563', '#16a34a')
DEFAULT_PPMM = 110
DATA = Path(os.environ.get('AOI_LS_DATA', Path.home() / '.cache' / 'aoi-labelstudio'))
URL = f'http://127.0.0.1:{os.environ.get("AOI_LS_PORT", "8090")}'
SCORE = {'confirmed': 1.0, 'unsure': 0.5, 'undecided': 0.3}


def label_config():
    labels = ''.join(f'<Label value="{c}" background="{col}"/>' for c, col in zip(CLASSES, COLOURS))
    return ('<View>'
            '<Header value="$board  top side, layout orientation. Draw a box (or polygon) round each defect. '
            'Tick fully reviewed when you looked at the whole board."/>'
            '<Image name="image" value="$image" zoom="true" zoomControl="true" rotateControl="false" brightnessControl="true" contrastControl="true"/>'
            f'<RectangleLabels name="label" toName="image" strokeWidth="2">{labels}</RectangleLabels>'
            f'<PolygonLabels name="poly" toName="image" strokeWidth="2">{labels}</PolygonLabels>'
            '<TextArea name="note" toName="image" perRegion="true" editable="true" rows="2" placeholder="note on this region"/>'
            '<Choices name="reviewed" toName="image" choice="multiple"><Choice value="fully reviewed"/></Choices>'
            '</View>')


# ---------------------------------------------------------------- candidates -> labels (pure)

def class_of_candidate(result_id, fine_pitch=('IC1', 'IC2', 'IC4', 'U1')):
    parts = result_id.split('-')
    kind = parts[1] if len(parts) > 2 else 'PART'
    if kind == 'POL':
        return 'wrong_part_or_polarity'
    if kind == 'BRIDGE':
        return 'bridge'
    if kind == 'MISSING':
        return 'missing'
    if kind == 'LIFT':
        return 'misaligned' if parts[2] in fine_pitch else 'lifted_tombstone'
    return 'other'


def preannotations(board, candidates, decisions, bv, meta, margin=0.15):
    """LS prediction `result` list for one board.
    candidates: result ids of the run on this board; decisions: {'<board>|<id>': {'decision', 'note'}}."""
    ids = list(dict.fromkeys(candidates))
    for key, d in decisions.items():            # an operator-confirmed id the run no longer produces stays visible
        b, rid = key.split('|', 1)
        if b == board and d['decision'] == 'confirmed' and rid not in ids:
            ids.append(rid)
    out = []
    for rid in ids:
        d = decisions.get(f'{board}|{rid}')
        state = d['decision'] if d else 'undecided'
        if state == 'rejected':
            continue
        marker = BV.marker_for_id(rid, bv, margin=margin)
        if not marker:
            continue
        value = C.rect_mm_to_ls(meta, marker['box'])
        cls = class_of_candidate(rid)
        tag = {'confirmed': 'operator CONFIRMED', 'unsure': 'FLAGGED: operator unsure', 'undecided': 'AOI candidate, not reviewed'}[state]
        text = f'{rid} | {tag}' + (f' | {d["note"]}' if d and d.get('note') else '')
        region = 'aoi' + hashlib.sha1(f'{board}|{rid}'.encode()).hexdigest()[:8]
        base = {'id': region, 'from_name': 'label', 'to_name': 'image', 'type': 'rectanglelabels',
                'original_width': meta['size_px'][0], 'original_height': meta['size_px'][1], 'image_rotation': 0,
                'score': SCORE[state], 'value': {**value, 'rectanglelabels': [cls]}}
        out.append(base)
        out.append({'id': region, 'from_name': 'note', 'to_name': 'image', 'type': 'textarea', 'value': {**value, 'text': [text]}})
    return out


# ---------------------------------------------------------------- Label Studio API

class LS:
    def __init__(self, url=URL, token=None):
        self.url = url
        self.token = token or (DATA / '.token').read_text(encoding="utf-8").strip()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Authorization': f'Token {self.token}', 'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # localhost only, never via a proxy
        try:
            with opener.open(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'{method} {path}: {e.code} {e.read().decode()[:400]}') from None
        return json.loads(raw) if raw else None

    def paged(self, path):
        out, page = [], 1
        while True:
            sep = '&' if '?' in path else '?'
            r = self.call('GET', f'{path}{sep}page={page}&page_size=100')
            rows = r['tasks'] if isinstance(r, dict) and 'tasks' in r else r.get('results', r) if isinstance(r, dict) else r
            out += rows
            if len(rows) < 100:
                return out
            page += 1


def render_images(run_folder, out_dir, ppmm):
    """Register the run's top photos and write <board>.jpg at `ppmm`. Returns {board: meta}."""
    from polybench.aoi import protocol_run as pr
    from polybench.aoi.defect_stage import DefectContext
    from polybench.aoi.protocol import load_protocol
    result_path = Path(run_folder).expanduser().resolve() / 'aoi_result.json'
    runlog = json.loads(result_path.with_suffix('.runlog.json').read_text(encoding="utf-8"))
    protocol = load_protocol(proto_default())
    layout = json.loads(next(pr.RECORDED.glob('*_layout_*.json')).read_text(encoding="utf-8"))
    files = {b: Path(v['photos']['top']) for b, v in runlog['boards'].items() if v['photos'].get('top')}
    data, _ = pr.analyse_side('top', layout, protocol, files, protocol['reference_board'], runlog['sides']['top']['turns'], print)
    ctx = DefectContext(data, layout, protocol, runlog['layout_guard']['cam_zip'], print)
    tmp = Path(out_dir) / '_render'
    info = BV.write(ctx.entries, layout, ctx.assigned, tmp, ppmm=ppmm, log=print)
    metas = {}
    for b, rel in info['boards'].items():
        dst = Path(out_dir) / f'{b}.jpg'
        (tmp / rel).replace(dst)
        metas[b] = {'box_mm': info['box_mm'], 'ppmm': ppmm, 'size_px': info['size_px'], 'photo': str(files[b].name)}
    for f in (tmp / BV.DIR).glob('*'):
        f.unlink()
    (tmp / BV.DIR).rmdir()
    tmp.rmdir()
    return metas


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run', help='AOI run folder (aoi_result.json, boardview/boardview.json)')
    ap.add_argument('--decisions', default=str(Path.home() / '.cache/aoi-defects/final/aoi_review_decisions.json'))
    ap.add_argument('--ppmm', type=int, default=DEFAULT_PPMM)
    ap.add_argument('--no-render', action='store_true', help='keep the images already in the data dir')
    args = ap.parse_args(argv)
    run = Path(args.run).expanduser()
    files_dir = DATA / 'serve' / 'files'
    files_dir.mkdir(parents=True, exist_ok=True)
    meta_path = DATA / 'images.json'
    if args.no_render and meta_path.is_file():
        metas = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        metas = render_images(run, files_dir, args.ppmm)
        meta_path.write_text(json.dumps(metas, indent=1), encoding="utf-8")
    bv = BV.load(run)
    doc = json.loads((run / 'aoi_result.json').read_text(encoding="utf-8"))
    decisions = json.loads(Path(args.decisions).expanduser().read_text(encoding="utf-8"))['decisions'] if Path(args.decisions).expanduser().is_file() else {}
    cands = {b['board']: [r['id'] for r in b['results'] if r['id'].startswith(('AOI-BRIDGE-', 'AOI-LIFT-', 'AOI-MISSING-', 'AOI-ANOM-'))] for b in doc['boards']}
    ls = LS()
    project = next((p for p in ls.paged('/api/projects') if p['title'] == PROJECT_TITLE), None)
    config = {'title': PROJECT_TITLE, 'label_config': label_config(), 'model_version': MODEL_VERSION, 'show_collab_predictions': True,
              'description': 'Defect boxes on the registered top photo of each board. Pre-annotations come from AOI candidates and the operator review.'}
    if project is None:
        project = ls.call('POST', '/api/projects', config)
    else:
        ls.call('PATCH', f'/api/projects/{project["id"]}', config)
    pid = project['id']
    path = str(files_dir)
    if not any(s['path'] == path for s in ls.call('GET', f'/api/storages/localfiles?project={pid}')):
        ls.call('POST', '/api/storages/localfiles', {'project': pid, 'title': 'aoi images', 'path': path, 'use_blob_urls': False})
    tasks = {t['data'].get('board'): t for t in ls.paged(f'/api/tasks?project={pid}&fields=all')}
    summary = {}
    for board in sorted(metas):
        meta = metas[board]
        data = {'image': f'/data/local-files/?d=files/{board}.jpg', 'board': board, 'run': run.name, **meta}
        result = preannotations(board, cands.get(board, []), decisions, bv, meta)
        summary[board] = sum(1 for r in result if r['type'] == 'rectanglelabels')
        if board in tasks:
            t = tasks[board]
            ls.call('PATCH', f'/api/tasks/{t["id"]}', {'data': data})
            for p in ls.call('GET', f'/api/predictions?task={t["id"]}'):
                ls.call('DELETE', f'/api/predictions/{p["id"]}')
            tid = t['id']
        else:
            tid = ls.call('POST', '/api/tasks', {'project': pid, 'data': data})['id']
        ls.call('POST', '/api/predictions', {'task': tid, 'project': pid, 'model_version': MODEL_VERSION, 'score': 0.5, 'result': result})
    print(json.dumps({'project': pid, 'url': f'{URL}/projects/{pid}', 'pre_annotated_regions': summary}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
