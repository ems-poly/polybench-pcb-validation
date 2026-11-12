"""Pull the Label Studio annotations of "AOI defects" into a label file in BOARD coordinates.

    python -m aoi.ls_export [--out aoi/labels/labels.json]

Labels are stored in layout millimetres (y up, same frame as the CAM and the layout), not in image pixels, so
they survive a new registration, another image resolution and new photos of the same board design.
Per task the newest annotation that was not cancelled counts. A board is `reviewed` only when its "fully
reviewed" box is ticked: an un-drawn board that is not reviewed says nothing about defects.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from polybench.aoi import ls_coords as C
from polybench.aoi import ls_project as P

DEFAULT_OUT = Path(__file__).resolve().parent / 'labels' / 'labels.json'


def annotation_to_labels(board, meta, annotation):
    """Region labels (board mm) and the reviewed flag of one LS annotation (`result` list)."""
    labels, notes, reviewed = {}, {}, False
    for item in annotation.get('result', []):
        t, v = item['type'], item.get('value', {})
        if t == 'textarea':
            notes[item['id']] = ' '.join(v.get('text', []))
        elif t == 'choices':
            reviewed = reviewed or 'fully reviewed' in v.get('choices', [])
        elif t == 'rectanglelabels' and v.get('rectanglelabels'):
            poly = C.rect_ls_to_polygon_mm(meta, v)
            labels[item['id']] = {'class': v['rectanglelabels'][0], 'shape': 'rectangle', 'polygon_mm': poly, 'box_mm': C.bbox(poly)}
        elif t == 'polygonlabels' and v.get('polygonlabels'):
            poly = C.polygon_ls_to_mm(meta, v['points'])
            labels[item['id']] = {'class': v['polygonlabels'][0], 'shape': 'polygon', 'polygon_mm': poly, 'box_mm': C.bbox(poly)}
    out = []
    for rid, lab in labels.items():
        out.append({'board': board, **lab, 'note': notes.get(rid, ''), 'region_id': rid})
    return out, reviewed


def export(tasks, board_name='board', source=None):
    labels, boards = [], {}
    for task in tasks:
        d = task['data']
        board = d['board']
        meta = {'box_mm': d['box_mm'], 'ppmm': d['ppmm'], 'size_px': d['size_px']}
        anns = [a for a in task.get('annotations', []) if not a.get('was_cancelled')]
        if not anns:
            boards[board] = {'reviewed': False, 'annotation_id': None, 'labels': 0, 'photo': d.get('photo')}
            continue
        a = max(anns, key=lambda x: (x.get('updated_at') or '', x['id']))
        regions, reviewed = annotation_to_labels(board, meta, a)
        for r in regions:
            r.update(annotation_id=a['id'], annotator=a.get('completed_by'), updated_at=a.get('updated_at'))
        labels += regions
        boards[board] = {'reviewed': reviewed, 'annotation_id': a['id'], 'labels': len(regions), 'photo': d.get('photo')}
    return {'schema': C.SCHEMA, 'board': board_name,
            'frame': 'layout board mm, y up; the labelling image pixel is ((x - box[0]) * ppmm, (box[3] - y) * ppmm)',
            'frame_box_mm': next((t['data']['box_mm'] for t in tasks), None), 'classes': list(P.CLASSES),
            'exported': dt.datetime.now().astimezone().isoformat(timespec='seconds'), 'source': source or {},
            'boards': dict(sorted(boards.items())), 'labels': labels}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    a = ap.parse_args(argv)
    ls = P.LS()
    project = next((p for p in ls.paged('/api/projects') if p['title'] == P.PROJECT_TITLE), None)
    if project is None:
        print(f'project "{P.PROJECT_TITLE}" not found on {ls.url}', file=sys.stderr)
        return 1
    tasks = ls.paged(f'/api/tasks?project={project["id"]}&fields=all')
    doc = export(tasks, source={'label_studio': ls.url, 'project': project['id'], 'title': P.PROJECT_TITLE})
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1))
    print(f'{out}: {len(doc["labels"])} labels, boards reviewed {[b for b, v in doc["boards"].items() if v["reviewed"]]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
