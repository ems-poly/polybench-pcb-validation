"""Explicit landmark registration and conservative reference comparison, all local."""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageOps


def load_photo(path):
    import pillow_heif
    pillow_heif.register_heif_opener()
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((6000, 6000))
        return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def transform(points, matrix):
    return cv2.perspectiveTransform(np.float32(points).reshape(1, -1, 2), matrix)[0]


def registration(parts, anchors):
    if len(anchors) != 4 or len({a['name'] for a in anchors}) != 4:
        raise ValueError('Choose four distinct component centres spread across the board.')
    lookup = {p['name']: p for p in parts}
    src = np.float32([[lookup[a['name']]['x'], lookup[a['name']]['y']] for a in anchors])
    dst = np.float32([a['pixel'] for a in anchors])
    for points in (src, dst):
        hull = cv2.convexHull(points)
        if len(hull) != 4 or cv2.contourArea(hull) < 1:
            raise ValueError('Landmarks must form a broad quadrilateral without collinear points.')
    matrix = cv2.getPerspectiveTransform(src, dst)
    # A projective fit can exactly fit four mistaken clicks. Operator overlay review is required.
    if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError('Unusable landmark alignment; choose landmarks again.')
    return matrix


def alignment_matrix(parts, anchors, automatic=None):
    if automatic is None:
        return registration(parts, anchors)
    matrix = np.asarray(automatic['matrix'], dtype=float)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError('Invalid automatic alignment matrix; align this photo again.')
    return matrix


def focus_score(gray):
    # A fixed upper working size prevents a high-resolution sharp crop from looking
    # blurry merely because its edge gradients are spread over more source pixels.
    scale = min(1., 256/max(gray.shape))
    sample = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    return float(cv2.Laplacian(sample, cv2.CV_64F).var())


def board_exposure(crop):
    """Fraction of the central body region showing saturated red solder mask."""
    h, w = crop.shape[:2]
    core = crop[round(h*.2):max(round(h*.8), round(h*.2)+1),
                round(w*.2):max(round(w*.8), round(w*.2)+1)]
    hsv = cv2.cvtColor(core, cv2.COLOR_BGR2HSV)
    return float(np.mean(((hsv[:, :, 0] < 15) | (hsv[:, :, 0] > 165)) &
                         (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 60)))


def inspect(reference, candidate, recipe, candidate_matrix):
    ref_matrix = np.asarray(recipe['reference_matrix'], dtype=float)
    warp = ref_matrix @ np.linalg.inv(candidate_matrix)
    size = (reference.shape[1], reference.shape[0])
    aligned = cv2.warpPerspective(candidate, warp, size)
    coverage = cv2.warpPerspective(np.full(candidate.shape[:2], 255, np.uint8), warp, size)
    results = []
    overrides = recipe.get('regions', {})
    for part in recipe['layout']['parts']:
        if part['side'] != recipe['side']:
            continue
        name = part['name']
        polygon = overrides.get(name)
        if polygon is None and (part.get('inspection_polygon') or part['polygon']):
            polygon = transform(part.get('inspection_polygon') or part['polygon'], ref_matrix).tolist()
        item = {'name': name, 'fitted': part['fitted'], 'polygon': polygon,
                'status': 'review', 'reason': 'No usable footprint region; define a region.'}
        results.append(item)
        if polygon is None:
            continue
        points = np.float32(polygon)
        x, y, w, h = cv2.boundingRect(points)
        if x < 0 or y < 0 or x+w > size[0] or y+h > size[1]:
            item['reason'] = 'Region falls outside reference photo.'
            continue
        if min(w, h) < 12:
            item['reason'] = 'Region too small; use a closer photo.'
            continue
        mask = np.zeros((h, w), np.uint8)
        cv2.fillPoly(mask, [np.int32(points - [x, y])], 255)
        mask = cv2.erode(mask, np.ones((3, 3), np.uint8)) > 0
        if not mask.any() or np.any(coverage[y:y+h, x:x+w][mask] < 250):
            item['reason'] = 'Region not fully visible in candidate.'
            continue
        ref = cv2.cvtColor(reference[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
        cur = cv2.cvtColor(aligned[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
        if min(focus_score(ref), focus_score(cur)) < 25:
            item['reason'] = 'Low texture or blur; inspect manually.'
            continue
        # Smooth sensor noise; compensate modest exposure differences, retain structural changes.
        ref = cv2.GaussianBlur(ref, (3, 3), 0).astype(float)
        cur = cv2.GaussianBlur(cur, (3, 3), 0).astype(float)
        cur = np.clip(cur + np.median(ref[mask]) - np.median(cur[mask]), 0, 255)
        difference = float(np.mean(np.abs(ref[mask] - cur[mask])) / 255)
        item['difference'] = round(difference, 4)
        item['board_exposure'] = round(board_exposure(aligned[y:y+h, x:x+w]), 4)
        item['reference_board_exposure'] = round(board_exposure(reference[y:y+h, x:x+w]), 4)
        item['status'] = 'changed' if difference > recipe.get('threshold', 0.10) else 'no_change'
        item['reason'] = ('Visible difference: check presence, placement, polarity and solder.'
                          if item['status'] == 'changed' else 'No large reference difference; not an electrical pass.')
        if part['fitted'] and item['board_exposure'] > .6 and item['reference_board_exposure'] < .15:
            item['status'] = 'changed'
            item['reason'] = 'Possible missing component: exposed red solder mask in the expected body region. Review the evidence crop.'
        if name.upper().startswith(('D', 'LED')):
            # A reference comparison alone cannot certify a cathode marking or pin mapping.
            item['status'] = 'review'
            item['reason'] = (item['reason'] + ' ' + 'Diode: verify cathode marking against layout/datasheet. '
                              + ('Region differs from reference.' if difference > recipe.get('threshold', .10)
                                 else 'No large reference difference.'))
        if item['status'] == 'changed' and not recipe.get('reference_verified', True):
            item['status'] = 'review'
            item['reason'] = 'Unverified baseline comparison: ' + item['reason']
        if not part['fitted']:
            item['reason'] = 'DNP region: compare to verified empty reference. ' + item['reason']
    return aligned, results


def annotate(image, results, selected=None):
    output = image.copy()
    for row in results:
        if row.get('polygon') is None:
            continue
        points = np.int32(row['polygon'])
        colour = {'changed': (40, 50, 240), 'review': (0, 190, 255),
                  'no_change': (80, 180, 80)}.get(row['status'], (0, 190, 255))
        cv2.polylines(output, [points], True, colour, 5 if row['name'] == selected else 2)
        cv2.putText(output, row['name'], tuple(points[0]), cv2.FONT_HERSHEY_SIMPLEX, .5, colour, 1)
    return output
