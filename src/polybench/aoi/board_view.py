"""Full-board view for the review page: one registered, upright board image per board plus the geometry
(part outlines, pad windows) from which a marker for any candidate is derived.

Files, beside `aoi_result.json`:
    boardview/<board>.jpg    the registered top photo warped to board millimetres (layout orientation: y up,
                             cropped to the board outline), the same frame for every board
    boardview/boardview.json box (mm), ppmm, image size, per-part pad windows and outlines

Image pixel of a board point (x, y) mm:  ((x - box[0]) * ppmm, (box[3] - y) * ppmm).
The marker of a result is derived from its id (`marker_for_id`), so it works for the defect candidates and for
the `AOI-<designator>` fails alike, and nothing about the detection has to be stored.

    .venv/bin/python -m aoi.board_view --result <folder>/aoi_result.json     # add the views to an existing run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

from polybench.aoi import defects as D
from polybench.aoi.alignment import board_bounds

DIR = 'boardview'
JSON_NAME = 'boardview.json'
SCHEMA = 'polystage.aoi_boardview/0'
PPMM = 60
BRIDGE_ID = re.compile(r'^AOI-BRIDGE-(?:(?P<d>[A-Za-z0-9_]+)-(?P<i>\d+)-(?P<j>\d+)|(?P<da>[A-Za-z0-9_]+)\.(?P<ia>\d+)-(?P<db>[A-Za-z0-9_]+)\.(?P<ib>\d+))$')
POL_ID = re.compile(r'^AOI-POL-(?P<sys>SYS-)?(?P<d>[A-Za-z0-9_]+)$')
KIND_ID = re.compile(r'^AOI-(?:(?:BRIDGE|LIFT|MISSING|ANOM)-)?(?P<d>[A-Za-z0-9_]+)$')


def board_box(layout):
    low, high = board_bounds(layout)
    return [float(low[0]), float(low[1]), float(high[0]), float(high[1])]


def _outline(part):
    poly = part.get('inspection_polygon') or part.get('polygon')
    if not poly or part.get('side') != 'top':
        return None
    return [[round(float(x), 3), round(float(y), 3)] for x, y in poly]


def geometry(layout, assigned):
    """Per top-side designator: outline polygon (mm), bounding box (mm) and the pad rectangles (mm)."""
    parts = {}
    for part in layout['parts']:
        poly = _outline(part)
        pads = [[round(r, 3) for r in D.pad_rect(p)] for p in assigned.get(part['name'], [])]
        if poly is None and not pads:
            continue
        pts = np.asarray(poly if poly else [[pads[0][0], pads[0][1]]], float)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        for r in pads:
            lo, hi = np.minimum(lo, [r[0], r[1]]), np.maximum(hi, [r[2], r[3]])
        parts[part['name']] = {'outline': poly, 'box': [round(float(v), 3) for v in (*lo, *hi)], 'pads': pads}
    return parts


def render(entry, box, ppmm=PPMM):
    image, _ = D.make_view(entry, box, ppmm)
    return image


def write(entries, layout, assigned, folder, ppmm=PPMM, log=print):
    """Write the board images and boardview.json into `<folder>/boardview/`. Returns the json dict."""
    out = Path(folder) / DIR
    out.mkdir(parents=True, exist_ok=True)
    box = board_box(layout)
    boards, size = {}, None
    for name, entry in sorted(entries.items()):
        if entry.get('error') or entry.get('image') is None:
            continue
        image = render(entry, box, ppmm)
        size = [image.shape[1], image.shape[0]]
        if not cv2.imwrite(str(out / f'{name}.jpg'), image, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise OSError(f'Cannot write {out / (name + ".jpg")}')
        boards[name] = f'{DIR}/{name}.jpg'
    data = {'schema': SCHEMA, 'box_mm': box, 'ppmm': ppmm, 'size_px': size, 'boards': boards, 'parts': geometry(layout, assigned)}
    (out / JSON_NAME).write_text(json.dumps(data))
    log(f'board views: {len(boards)} images at {ppmm} px/mm in {out}')
    return data


def _union(rects, margin):
    a = np.asarray(rects, float)
    return [float(a[:, 0].min() - margin), float(a[:, 1].min() - margin), float(a[:, 2].max() + margin), float(a[:, 3].max() + margin)]


def marker_for_id(result_id, bv, margin=0.25):
    """{'designator', 'label', 'box' (mm), 'kind'} for a result id, or None when the geometry has nothing for it."""
    parts = bv.get('parts', {})
    m = BRIDGE_ID.match(result_id)
    if m:
        if m['d']:
            part = parts.get(m['d'])
            i, j = int(m['i']) - 1, int(m['j']) - 1
            if not part or max(i, j) >= len(part['pads']):
                return None
            return {'kind': 'BRIDGE', 'designator': m['d'], 'label': f'{m["d"]}  pins {m["i"]}-{m["j"]}',
                    'box': _union([part['pads'][i], part['pads'][j]], margin)}
        pa, pb = parts.get(m['da']), parts.get(m['db'])
        ia, ib = int(m['ia']) - 1, int(m['ib']) - 1
        if not pa or not pb or ia >= len(pa['pads']) or ib >= len(pb['pads']):
            return None
        return {'kind': 'BRIDGE', 'designator': f'{m["da"]}/{m["db"]}',
                'label': f'{m["da"]} pin {m["ia"]} to {m["db"]} pin {m["ib"]}', 'box': _union([pa['pads'][ia], pb['pads'][ib]], margin)}
    m = POL_ID.match(result_id)
    if m:
        part = parts.get(m['d'])
        if not part:
            return None
        b = part['box']
        return {'kind': 'POLSYS' if m['sys'] else 'POL', 'designator': m['d'], 'label': f'{m["d"]}  polarity' + ('  (batch)' if m['sys'] else ''),
                'box': [b[0] - margin, b[1] - margin, b[2] + margin, b[3] + margin]}
    m = KIND_ID.match(result_id)
    if not m or m['d'] not in parts:
        return None
    kind = result_id.split('-')[1] if result_id.count('-') >= 2 else 'PART'
    box = parts[m['d']]['box']
    return {'kind': kind, 'designator': m['d'], 'label': m['d'], 'box': [box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin]}


def crop_sheet(folder, bv, box, boards, suspect, title, tile=520, columns=3, margin=1.5, min_mm=5.0):
    """The same crop from the board view of every board, laid out like the AOI evidence sheet (`defects.evidence_sheet`: 3 columns,
    30 px title strip, 22 px label strip over each tile) so the review page can overlay the peer tiles on it. For an
    operator-found issue, which has no evidence sheet of its own. `boards` lists the tile order (suspect first). Returns an image."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = max((x1 - x0) / 2 + margin, min_mm / 2), max((y1 - y0) / 2 + margin, min_mm / 2)
    bx = bv['box_mm']
    ppmm = bv['ppmm']
    u0, u1 = int(max(0, (cx - hw - bx[0]) * ppmm)), int(min(bv['size_px'][0], (cx + hw - bx[0]) * ppmm))
    v0, v1 = int(max(0, (bx[3] - (cy + hh)) * ppmm)), int(min(bv['size_px'][1], (bx[3] - (cy - hh)) * ppmm))
    tiles = []
    for b in boards:
        path = Path(folder) / bv['boards'].get(b, '')
        img = cv2.imread(str(path)) if b in bv['boards'] and path.is_file() else None
        if img is None or u1 <= u0 or v1 <= v0:
            continue
        crop = img[v0:v1, u0:u1].copy()
        rx0, ry1 = int((x0 - bx[0]) * ppmm) - u0, int((bx[3] - y0) * ppmm) - v0
        rx1, ry0 = int((x1 - bx[0]) * ppmm) - u0, int((bx[3] - y1) * ppmm) - v0
        cv2.rectangle(crop, (rx0, ry0), (rx1, ry1), (255, 255, 0), 2 if b == suspect else 1)
        s = min(tile / crop.shape[1], tile / crop.shape[0], 1.0 if crop.shape[1] > tile else tile / crop.shape[1])
        crop = cv2.resize(crop, (max(1, round(crop.shape[1] * s)), max(1, round(crop.shape[0] * s))), interpolation=cv2.INTER_AREA)
        if b == suspect:
            crop = cv2.copyMakeBorder(crop, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=(40, 50, 240))
        tiles.append((crop, b + (' (operator-found)' if b == suspect else '')))
    if not tiles:
        return None
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


def load(folder):
    path = Path(folder) / DIR / JSON_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def backfill(result_path, layout_path=None, protocol_path=None, cam_zip=None, log=print):
    """Register the top photos again (no detection) and write the board views beside an existing result."""
    from polybench.aoi import photo_map  # noqa: F401  (kept for symmetry with the run)
    from polybench.aoi import protocol_run as pr
    from polybench.aoi.defect_stage import DefectContext
    from polybench.aoi.protocol import load_protocol
    result_path = Path(result_path).resolve()
    runlog = json.loads(result_path.with_suffix('.runlog.json').read_text())
    protocol = load_protocol(protocol_path or proto_default())
    layout = json.loads(Path(layout_path or next((pr.RECORDED).glob('*_layout_*.json'))).read_text())
    files = {b: Path(v['photos']['top']) for b, v in runlog['boards'].items() if v['photos'].get('top')}
    data, _ = pr.analyse_side('top', layout, protocol, files, protocol['reference_board'], runlog['sides']['top']['turns'], log)
    ctx = DefectContext(data, layout, protocol, cam_zip or runlog['layout_guard']['cam_zip'], log)
    return write(ctx.entries, layout, ctx.assigned, result_path.parent, log=log)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--result', required=True)
    ap.add_argument('--layout')
    ap.add_argument('--protocol')
    ap.add_argument('--cam-zip')
    args = ap.parse_args(argv)
    try:
        backfill(args.result, args.layout, args.protocol, args.cam_zip)
    except Exception as error:
        print(f'FAILED: {type(error).__name__}: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
