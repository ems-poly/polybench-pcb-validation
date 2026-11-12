"""Run a test protocol over a batch of board photos and export `polystage.aoi_result/0`.

    .venv/bin/python -m aoi.protocol_run --protocol aoi/protocols/visual.json \\
        --photos <folder> --map <map.json> --out aoi_result.json

Layout comes from the active Fusion board (read-only) or, headless, from `--layout recorded.json`.
Either way it is compared with the released CAM package before anything is inspected; a mismatch
stops the run. Nothing here edits or saves a design.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from polybench.aoi import photo_map, protocol_logic
from polybench.aoi.alignment import align_to_reference, mechanical_reference
from polybench.aoi.batch import compare_batch, empty_board
from polybench.aoi.layout_guard import LayoutMismatch, verify_layout
from polybench.aoi.protocol import ProtocolError, class_of, load_protocol
from polybench.aoi.vision import focus_score, inspect, load_photo, transform

TOOL = 'assembly-inspector-aoi-protocol 0.1.0'
SCHEMA = 'polystage.aoi_result/0'
TURNS_ORDER = (0, 1, 2, 3)
# Phone photos of a small board are often shot edge to edge, so a corner of the red outline may touch the frame.
# The interactive app keeps its 3 px margin; the protocol run accepts a touching corner and relies on the
# SIFT residual, spread and corner-agreement gates to refuse a really cropped board.
EDGE_MARGIN = 0


class RunError(RuntimeError):
    pass


# ---------------------------------------------------------------- geometry helpers

def region_polygon(part, canonical, half_mm=1.2):
    """Region of a part in canonical board pixels: connected-pad box, package box, or a fixed box
    around the origin for pads that have neither (pigtail and pogo pads)."""
    polygon = part.get('inspection_polygon') or part.get('polygon')
    if polygon is None:
        x, y = part['x'], part['y']
        polygon = [[x - half_mm, y - half_mm], [x + half_mm, y - half_mm], [x + half_mm, y + half_mm], [x - half_mm, y + half_mm]]
    return transform(polygon, np.asarray(canonical, float))


def box_polygon(box_mm, canonical):
    x1, y1, x2, y2 = box_mm
    return transform([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], np.asarray(canonical, float))


def _inside(polygon, shape):
    x, y, w, h = cv2.boundingRect(np.float32(polygon))
    return x >= 0 and y >= 0 and x + w <= shape[1] and y + h <= shape[0] and min(w, h) >= 12


def measure_without_row(part, crop, canonical):
    """Usable/blur check for a region that `inspect` does not cover (pads). No exposure value."""
    polygon = region_polygon(part, canonical)
    if not _inside(polygon, crop.shape):
        return {'usable': False, 'reason': 'Region falls outside the photographed board.'}
    x, y, w, h = cv2.boundingRect(np.float32(polygon))
    patch = crop[y:y + h, x:x + w]
    if float(np.mean(np.all(patch == 0, axis=2))) > .02:
        return {'usable': False, 'reason': 'Region not fully visible in the photo.'}
    if focus_score(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)) < 25:
        return {'usable': False, 'reason': 'Low texture or blur; inspect manually.'}
    return {'usable': True, 'exposure': None, 'peer': 'not_assessed', 'polygon': polygon.tolist()}


def measure_from_row(row):
    if 'difference' not in row:
        return {'usable': False, 'reason': row.get('reason', 'Region not usable.')}
    return {'usable': True, 'exposure': row.get('board_exposure'), 'peer': row.get('peer_status', 'not_assessed'),
            'peer_difference': row.get('peer_difference'), 'polygon': row.get('polygon')}


# ---------------------------------------------------------------- registration of one side

def exposure_of(crop, polygon):
    from polybench.aoi.vision import board_exposure
    x, y, w, h = cv2.boundingRect(np.float32(polygon))
    if not _inside(polygon, crop.shape):
        return None
    return board_exposure(crop[y:y + h, x:x + w])


def colour_of(protocol):
    """Board colour for outline detection: protocol `registration.board_colour`, 'red' (the default, as in the PoC) or 'dark'."""
    return (protocol.get('registration') or {}).get('board_colour', 'red')


def overhang_of(protocol):
    """Protocol `registration.overhang_mm`: {side: mm} the photographed board extends beyond the layout outline (default none)."""
    return (protocol.get('registration') or {}).get('overhang_mm')


def choose_orientation(original, layout, protocol, side, expected_present, turns, log=print):
    """Pick the quarter-turn of the reference photo. A rectangle cannot tell orientation, so score
    each turn by how many large parts show a body (not exposed mask) where the layout puts them,
    and refuse an ambiguous result. An explicit `turns` skips the scoring."""
    thresholds = protocol['thresholds']
    large = [p for p in layout['parts'] if p['side'] == side and class_of(protocol, p['name']) == 'large'
             and p['name'] in expected_present]
    candidates = [turns] if isinstance(turns, int) else TURNS_ORDER
    scored = []
    for t in candidates:
        try:
            crop, meta = mechanical_reference(original, layout, side=side, turns=t, edge_margin=EDGE_MARGIN, colour=colour_of(protocol), overhang=overhang_of(protocol))
        except ValueError as error:
            scored.append((-1, t, None, None, str(error)))
            continue
        canonical = meta['canonical_matrix']
        score = 0
        for part in large:
            e = exposure_of(crop, region_polygon(part, canonical))
            if e is not None and e <= thresholds['present_max_exposure']:
                score += 1
        scored.append((score, t, crop, meta, ''))
    ok = [s for s in scored if s[2] is not None]
    if not ok:
        raise RunError(f'{side}: reference photo could not be mapped to the layout in any orientation: {next((x[4] for x in scored if not x[4].startswith('Quarter-turn')), scored[0][4])}')
    ok.sort(key=lambda s: (-s[0], s[1]))
    summary = {str(s[1]): s[0] for s in scored}
    if not isinstance(turns, int):
        need = max(1, len(large) - 1)
        if not large:
            # No calibrated large part to tell a rectangle from its 180 degree turn (a board of small passives has none):
            # take the lowest quarter-turn that fits the board shape, assume upright, and say so.
            fit = sorted(s[1] for s in ok)
            log(f'{side}: no large part to score orientation; assuming the photo is upright (quarter-turn {fit[0]}); '
                'check the overlay, or set --reference-turns 0..3')
            ok = [s for s in ok if s[1] == fit[0]]
            return ok[0][2], ok[0][3], ok[0][1], summary
        if ok[0][0] < need or (len(ok) > 1 and ok[0][0] == ok[1][0]):
            raise RunError(f'{side}: reference orientation is ambiguous (large parts found per quarter-turn {summary}). '
                           'Set --reference-turns 0..3 after checking the overlay by eye.')
    return ok[0][2], ok[0][3], ok[0][1], summary


def analyse_side(side, layout, protocol, files, reference_board, turns='auto', log=print):
    """files: {board id: Path}. Returns per-board dicts and the side log. Boards that fail to
    register are returned with an `error` and are not compared."""
    order = [reference_board] + [b for b in files if b != reference_board]
    ref_id = next((b for b in order if b in files and not protocol['boards'].get(b, {}).get('expect_absent')), None)
    if ref_id is None:
        ref_id = next(b for b in order if b in files)
    original = load_photo(files[ref_id])
    expected_present = {p['name'] for p in layout['parts']
                        if protocol_logic.expectation(p['name'], ref_id, protocol) == 'present'}
    reference, meta, chosen, scores = choose_orientation(original, layout, protocol, side, expected_present,
                                                         turns if turns == 'auto' else int(turns), log)
    log(f'{side}: reference {ref_id}, quarter-turn {chosen} (large parts found per turn {scores})')
    canonical = np.asarray(meta['canonical_matrix'])
    boards, data = [], {}
    for board_id, path in files.items():
        entry = {'board': board_id, 'path': path, 'error': None}
        data[board_id] = entry
        try:
            image = load_photo(path)
            alignment = align_to_reference(original, image, meta['matrix'], layout, edge_margin=EDGE_MARGIN, colour=colour_of(protocol), overhang=overhang_of(protocol), tall=((protocol.get('defects') or {}).get('tall', ()) if (protocol.get('registration') or {}).get('fit_without_tall') else ()))
        except ValueError as error:
            entry['error'] = str(error)
            log(f'{side}: {board_id} NOT registered: {error}')
            continue
        warp = np.asarray(meta['crop_matrix']) @ np.asarray(alignment['candidate_to_reference'])
        crop = cv2.warpPerspective(image, warp, tuple(meta['canonical_size']))
        entry.update(image=image, warp=warp, crop=crop, canonical=canonical.tolist(),
                     alignment={k: alignment[k] for k in ('inliers', 'median_error_px', 'feature_spread') if k in alignment})
        board = empty_board(int(board_id[1:]))
        board.update(id=board_id, candidate=crop, candidate_alignment={'matrix': canonical.tolist()})
        boards.append(board)
        log(f'{side}: {board_id} registered, {alignment["inliers"]} features, residual {alignment["median_error_px"]} px')
    recipe = {'schema_version': 1, 'layout': layout, 'side': side, 'reference_matrix': canonical.tolist(),
              'reference_alignment': {'matrix': canonical.tolist(), 'method': 'protocol_canonical_crop'},
              'reference_anchors': [], 'reference_verified': False, 'regions': {}}
    if len(boards) >= 2:
        for out in compare_batch(reference, recipe, boards):
            data[out['id']]['rows'] = out['results']
    else:
        for board in boards:
            _, rows = inspect(reference, board['candidate'], recipe, canonical)
            data[board['id']]['rows'] = rows
    for board_id, entry in data.items():
        if entry['error']:
            continue
        rows = {r['name']: r for r in entry['rows']}
        measures = {}
        for part in layout['parts']:
            if part['name'] in rows:
                measures[part['name']] = measure_from_row(rows[part['name']])
            elif part['side'] == side:
                measures[part['name']] = measure_without_row(part, entry['crop'], canonical)
        entry['measures'] = measures
    return data, {'reference_board': ref_id, 'turns': chosen, 'orientation_scores': scores,
                  'compared_boards': [b['id'] for b in boards], 'peer_comparison': len(boards) >= 2}


# ---------------------------------------------------------------- evidence

def hires_crop(image, warp, polygon, label, scale=2.5, limit=1000):
    x, y, w, h = cv2.boundingRect(np.float32(polygon))
    pad = max(12, round(max(w, h) * .15))
    x0, y0 = x - pad, y - pad
    width, height = w + 2 * pad, h + 2 * pad
    scale = min(scale, limit / max(width, height))
    shift = np.array([[scale, 0, -x0 * scale], [0, scale, -y0 * scale], [0, 0, 1]], float)
    out = cv2.warpPerspective(image, shift @ np.asarray(warp, float), (max(8, round(width * scale)), max(8, round(height * scale))),
                              flags=cv2.INTER_CUBIC)
    cv2.polylines(out, [np.int32(transform(polygon, shift))], True, (0, 190, 255), 2)
    return out, label


def contact_sheet(tiles, tile_w=420, tile_h=320, columns=3):
    if not tiles:
        return None
    rows = (len(tiles) + columns - 1) // columns
    sheet = np.full((rows * (tile_h + 28), min(columns, len(tiles)) * (tile_w + 8), 3), 235, np.uint8)
    for index, (image, label) in enumerate(tiles):
        r, c = divmod(index, columns)
        scale = min(tile_w / image.shape[1], tile_h / image.shape[0])
        small = cv2.resize(image, (max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))), interpolation=cv2.INTER_AREA)
        top, left = r * (tile_h + 28), c * (tile_w + 8)
        sheet[top + 24:top + 24 + small.shape[0], left + 4:left + 4 + small.shape[1]] = small
        cv2.putText(sheet, label, (left + 6, top + 17), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 0), 1, cv2.LINE_AA)
    return sheet


def overview(entry, polygons, label):
    scale = 1000 / entry['crop'].shape[1]
    image = cv2.resize(entry['crop'], None, fx=scale, fy=scale)
    for name, polygon in polygons:
        pts = np.int32(np.float32(polygon) * scale)
        cv2.polylines(image, [pts], True, (0, 190, 255), 1)
        cv2.putText(image, name, tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, .38, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(image, label, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2, cv2.LINE_AA)
    return image


def render_evidence(path, board, sides, parts, result):
    """Write the evidence image for one result; return its path or None."""
    names, regions = result['_evidence'], result['_regions']
    tiles = []
    if len(names) > 8:
        side = parts[names[0]]['side']
        entry = sides.get(side, {}).get(board)
        if not entry or entry.get('error'):
            return None
        polys = [(n, region_polygon(parts[n], entry['canonical'])) for n in names if n in parts and parts[n]['side'] == side]
        image = overview(entry, polys, f'{board} {result["id"]}: {len(polys)} regions marked')
        return _write(path, image)
    for name in names:
        part = parts.get(name)
        entry = sides.get(part['side'], {}).get(board) if part else None
        if entry and not entry.get('error'):
            tiles.append(hires_crop(entry['image'], entry['warp'], region_polygon(part, entry['canonical']), f'{board} {name} ({part["side"]})'))
    for region in regions:
        entry = sides.get(region['side'], {}).get(board)
        if entry and not entry.get('error'):
            tiles.append(hires_crop(entry['image'], entry['warp'], box_polygon(region['box_mm'], entry['canonical']),
                                    f'{board} {region["label"]} ({region["side"]})'))
    sheet = contact_sheet(tiles)
    return _write(path, sheet) if sheet is not None else None


def _write(path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 88]):
        raise OSError(f'Cannot write {path}')
    return str(path.resolve())


# ---------------------------------------------------------------- orchestration

def load_layout(path=None, log=print):
    if path:
        return json.loads(Path(path).read_text())
    raise RunError('no recorded layout given (Polybench runs AOI from a recorded layout only)')


def check_protocol_against_layout(protocol, layout):
    names = {p['name'] for p in layout['parts']}
    used = set(protocol['dnf']) | set(protocol['optional']) | set(protocol['watch_fitted'])
    for row in protocol['rows']:
        used |= set(row['designators'])
    for board in protocol['boards'].values():
        used |= set(board['expect_absent'])
    unknown = sorted(used - names)
    if unknown:
        raise ProtocolError(f'Protocol names designators that are not in the layout: {unknown}')
    layout_dnf = {p['name'] for p in layout['parts'] if str(p.get('value', '')).upper() == 'DNF'}
    if layout_dnf != set(protocol['dnf']):
        raise ProtocolError(f'Protocol DNF list {sorted(protocol["dnf"])} disagrees with the layout/CAM value DNF {sorted(layout_dnf)}.')


def run(protocol, layout, mapping, out_path, cam_zip, photos_dir=None, reference_board=None, turns='auto',
        include_not_assessable=False, log=print, defects=True, exclude_confirmed=None, polarity=True):
    out_path = Path(out_path)
    guard = verify_layout(layout, cam_zip, protocol['layout']['design'])
    log(f'Layout matches {Path(cam_zip).name} ({guard["parts_compared"]} parts compared).')
    check_protocol_against_layout(protocol, layout)
    unknown = sorted(set(mapping) - set(protocol['boards']))
    if unknown:
        raise RunError(f'Photos mapped to boards the protocol does not define: {unknown}')
    reference_board = reference_board or protocol['reference_board']
    parts = {p['name']: p for p in layout['parts']}
    sides, side_logs, side_errors = {}, {}, {}
    for side in ('top', 'bottom'):
        files = {b: m[side] for b, m in mapping.items() if side in m}
        if not files:
            continue
        try:
            sides[side], side_logs[side] = analyse_side(side, layout, protocol, files, reference_board, turns, log)
        except (RunError, ValueError) as error:
            side_errors[side] = str(error)
            sides[side] = {}
            log(f'{side}: whole side not assessed: {error}')
    captions = photo_map.caption_boards(photos_dir) if photos_dir else set()
    evidence_dir = out_path.parent / 'evidence'
    defect_results, defect_log = {}, {'enabled': bool(defects)}
    if defects and sides.get('top'):
        from polybench.aoi import defect_report
        try:
            defect_results, defect_log = defect_report.run_stage(sides['top'], layout, protocol, cam_zip, parts, evidence_dir, log,
                                                         exclude_confirmed=exclude_confirmed)
        except Exception as error:  # the defect stage is additive: its failure must not lose the protocol result
            defect_log = {'enabled': True, 'error': f'{type(error).__name__}: {error}'}
            log(f'defects: stage failed and was skipped: {defect_log["error"]}')
    polarity_batch, polarity_log = None, {'enabled': bool(polarity)}
    if polarity and sides.get('top'):
        from polybench.aoi import polarity as pol
        try:   # the polarity check is not optional: a reversed part must reach the review queue (15 Oct, D3 on all five boards)
            spec_path = (protocol.get('polarity') or {}).get('spec')
            if not spec_path:
                raise pol.PolarityError('this protocol names no polarity spec (polarity.spec): polarity is NOT assessed on this design')
            polarity_batch = pol.run_batch(sides['top'], pol.load_spec(spec_path), out_path.parent, log, reference=reference_board)
            polarity_log = {'enabled': True, 'validation': polarity_batch['validation'], 'systematic': polarity_batch['systematic']}
        except Exception as error:   # additive like the defect stage, but the failure is loud: it is in the runlog and the log
            polarity_log = {'enabled': True, 'error': f'{type(error).__name__}: {error}'}
            log(f'POLARITY CHECK FAILED AND WAS SKIPPED: {polarity_log["error"]}')
    document = {'schema': SCHEMA, 'report_ref': protocol['report_ref'],
                'generated': datetime.now().astimezone().isoformat(timespec='seconds'),
                'tool': TOOL, 'boards': []}
    runlog = {'tool': TOOL, 'protocol': protocol['protocol'], 'protocol_version': protocol.get('version'),
              'layout_guard': guard, 'thresholds': protocol['thresholds'], 'sides': side_logs,
              'side_errors': side_errors, 'reference_board_requested': reference_board, 'boards': {}}
    runlog['defects'] = defect_log
    runlog['polarity'] = polarity_log
    for board in protocol['boards']:
        photos = {s: (str(mapping[board][s]) if board in mapping and s in mapping[board] else None) for s in ('top', 'bottom')}
        errors = dict(side_errors)
        measures = {}
        for side in ('top', 'bottom'):
            entry = sides.get(side, {}).get(board)
            if entry is None:
                continue
            if entry['error']:
                errors[side] = entry['error']
            else:
                for name, m in entry['measures'].items():
                    if name in parts and parts[name]['side'] == side:
                        measures[name] = m
        ctx = {'board': board, 'photos': photos, 'side_errors': errors, 'measures': measures, 'captioned': board in captions}
        if polarity_batch:
            ctx['polarity'] = {d: {'status': res[board]['status'], 'note': res[board]['note']}
                               for d, res in polarity_batch['parts'].items() if board in res}
        results = protocol_logic.evaluate_board(protocol, parts, ctx, include_not_assessable)
        final = []
        for result in results:
            files = []
            if result['status'] != 'not_assessable' and (result['_evidence'] or result['_regions']):
                written = render_evidence(evidence_dir / board / f'{result["id"]}.jpg', board, sides, parts, result)
                if written:
                    files.append(written)
            final.append({'id': result['id'], 'status': result['status'], 'value': result['value'], 'evidence': files,
                          'confidence': result['confidence'], 'note': result['note']})
        if defects:
            from polybench.aoi import defect_report
            final.extend(defect_report.missing_large(final, protocol))
        final.extend(defect_results.get(board, []))
        if polarity_batch:
            from polybench.aoi import polarity as pol
            pol.annotate_rows(final, polarity_batch, board)
            final.extend(pol.contract_results(polarity_batch, None, board))
        document['boards'].append({'board': board, 'photo': photos['top'] or '', 'results': final})
        counts = {s: sum(r['status'] == s for r in final) for s in protocol_logic_statuses()}
        runlog['boards'][board] = {'photos': photos, 'errors': errors, 'counts': counts,
                                   'alignment': {s: sides.get(s, {}).get(board, {}).get('alignment') for s in sides}}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=2))
    out_path.with_suffix('.runlog.json').write_text(json.dumps(runlog, indent=2, default=str))
    return document


def protocol_logic_statuses():
    from polybench.aoi.protocol import STATUSES
    return STATUSES


def check_ids_against_sheet(protocol, sheet_path):
    """Compare protocol row ids with the ids in the results sheet's As-built and Stage V tables."""
    import re
    text = Path(sheet_path).read_text()
    ids, section = [], None
    for line in text.splitlines():
        if line.startswith('## '):
            section = line[3:].strip()
        elif section and section.startswith(('As-built record', 'Stage V')):
            m = re.match(r'\|\s*([A-Z]\d+[a-z]?)\s*\|', line)
            if m:
                ids.append(m.group(1))
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    mine = {r['id'] for r in protocol['rows']}
    visual = [i for i in ids if i.startswith('V')]
    return {'sheet_ids': ids, 'duplicates_in_sheet': duplicates,
            'in_protocol_not_in_sheet': sorted(mine - set(ids)),
            'in_sheet_not_in_protocol': sorted(set(visual) - mine)}


REFUSALS = (ProtocolError, photo_map.PhotoMapError, LayoutMismatch, RunError)


def run_paths(protocol_path=None, photos=None, map_file=None, out='aoi_result.json', layout_path=None,
              sheet=None, cam_zip=None, reference_board=None, turns='auto', include_not_assessable=False, log=print,
              defects=True, exclude_confirmed=None, polarity=True):
    """The whole run from file paths. Shared by the CLI and the window button.
    Raises REFUSALS (exit 2 in the CLI) or any other exception (exit 1). Returns (document, sheet_report or None)."""
    protocol = load_protocol(protocol_path)
    report = None
    if sheet:
        report = check_ids_against_sheet(protocol, sheet)
        log('Sheet id check: ' + json.dumps({k: v for k, v in report.items() if k != 'sheet_ids'}))
    mapping = photo_map.build(photos, map_file)
    layout = load_layout(layout_path, log)
    document = run(protocol, layout, mapping, out, cam_zip or protocol['layout']['cam_zip'], photos_dir=photos,
                   reference_board=reference_board, turns=turns, include_not_assessable=include_not_assessable, log=log, defects=defects,
                   exclude_confirmed=exclude_confirmed, polarity=polarity)
    return document, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--protocol', default=None)
    parser.add_argument('--photos', default=None)
    parser.add_argument('--map', help='JSON mapping B1..B5 to photo files; otherwise filenames must carry B1..B5')
    parser.add_argument('--out', default='aoi_result.json')
    parser.add_argument('--layout', help='recorded layout JSON (headless); otherwise the active Fusion board is read')
    parser.add_argument('--cam-zip', help='released CAM package to verify the layout against (default from the protocol)')
    parser.add_argument('--reference-board', help='board whose photo defines the board frame (default from the protocol)')
    parser.add_argument('--reference-turns', default='auto', help='auto or 0..3 quarter-turns of the reference photo')
    parser.add_argument('--sheet', help='results sheet .md: report protocol ids that are missing from it or duplicated in it')
    parser.add_argument('--defects', choices=('on', 'off'), default='on',
                        help='defect stage (bridges, missing, lifted, anomalies between boards); default on')
    parser.add_argument('--polarity', choices=('on', 'off'), default='on',
                        help='polarity check against footprint and datasheet (V07a, AOI-POL-*); default on, switch off only for a speed test')
    parser.add_argument('--exclude-confirmed', metavar='DECISIONS.json',
                        help='review decisions file (read only): boards whose part the operator confirmed as a defect are not used '
                             'as peer reference for that designator in the LIFT / MISSING / ANOM / bridge comparisons')
    parser.add_argument('--no-open', action='store_true',
                        help='do not start the review page (local server) and open it in the browser at the end of the run')
    parser.add_argument('--include-not-assessable', action='store_true', help='also list AOI-<designator> rows with no finding')
    args = parser.parse_args(argv)
    try:
        document, _ = run_paths(args.protocol, args.photos, args.map, args.out, args.layout, args.sheet, args.cam_zip,
                                args.reference_board, args.reference_turns, args.include_not_assessable,
                                defects=args.defects == 'on', exclude_confirmed=args.exclude_confirmed,
                                polarity=args.polarity == 'on')
    except REFUSALS as error:
        print(f'REFUSED: {error}', file=sys.stderr)
        return 2
    except Exception as error:  # Fusion unreachable and similar: stop and say so, never fall back silently
        print(f'FAILED: {type(error).__name__}: {error}', file=sys.stderr)
        return 1
    for board in document['boards']:
        counts = {}
        for r in board['results']:
            counts[r['status']] = counts.get(r['status'], 0) + 1
        print(board['board'], counts)
    print(f'Wrote {Path(args.out).resolve()} and its evidence folder. Review items only: no board is certified good.')
    if not args.no_open:
        from polybench.aoi import review_server
        url = review_server.launch_detached(args.out)
        if url:
            print(f'Review page (local, saves decisions beside the result): {url}')
        else:
            print('Review page not started (AOI_NO_OPEN set, or the server did not answer). Start it with: '
                  f'.venv/bin/python -m aoi.review_server --result {Path(args.out).resolve()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
