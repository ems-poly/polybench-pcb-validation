"""Photo-first registration: red PCB outline, CAD mounting holes, planar feature matching.

The colour detector is deliberately scoped to the supplied red PCBs on a neutral bench.
A successful homography is a geometric match, never proof of correct assembly.
"""
from __future__ import annotations

import cv2
import numpy as np

from polybench.aoi.vision import transform


DARK_MAX_V = 85       # HSV value (0..255) below which a pixel counts as black mask in colour='dark'


def ordered_quad(points):
    points = np.float32(points).reshape(4, 2)
    centre = points.mean(axis=0)
    points = points[np.argsort(np.arctan2(points[:, 1]-centre[1], points[:, 0]-centre[0]))]
    return np.roll(points, -np.argmin(points.sum(axis=1)), axis=0)


class BoardOutlineError(ValueError):
    """Outline detection failed; the message says what was seen and what to change."""


def _aspect_of(quad):
    q = np.float32(quad)
    w = (np.linalg.norm(q[1]-q[0]) + np.linalg.norm(q[2]-q[3]))/2
    h = (np.linalg.norm(q[3]-q[0]) + np.linalg.norm(q[2]-q[1]))/2
    return float(w/max(h, 1e-6))


def detect_board(image, edge_margin=3, aspect=None, colour='red'):
    """Find the red board outline. `aspect` is the layout's width/height (from the CAD outline): when the
    red mask is ragged (gold pads, dark parts at the edge) and the hull is not a clean quadrilateral, the
    minimum-area rectangle is accepted if it is solid and has the layout's aspect ratio. The limits are
    fractions of the image and of the layout, not fixed pixel constants."""
    scale = min(1., 1200/max(image.shape[:2]))
    small = cv2.resize(image, None, fx=scale, fy=scale)
    H, W = small.shape[:2]
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    b, g, r = cv2.split(small.astype(float))
    if colour == 'dark':     # black solder mask on a lighter mat (protocol registration.board_colour): the board is the dark region
        mask = np.uint8(hsv[:, :, 2] < DARK_MAX_V)*255
    else:
        mask = np.uint8(((hsv[:, :, 0] < 15) | (hsv[:, :, 0] > 165)) &
                        (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 80) & (r > 1.4*g) & (r > 1.3*b))*255
    kernel = max(5, round(max(small.shape[:2])*.013)) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((kernel, kernel), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates, reasons = [], []
    red_fraction = float(np.count_nonzero(mask))/(H*W)
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < .04*H*W:
            continue
        hull = cv2.convexHull(contour)
        hull_area = cv2.contourArea(hull)
        approx = cv2.approxPolyDP(hull, .025*cv2.arcLength(hull, True), True)
        quad = None
        if len(approx) == 4:
            quad = ordered_quad(approx)
        elif aspect is not None:
            rect = cv2.boxPoints(cv2.minAreaRect(hull))
            rect_area = max(cv2.contourArea(np.float32(rect)), 1)
            cand = ordered_quad(rect)
            ratio = _aspect_of(cand)
            fits = min(ratio/aspect, aspect/ratio) if ratio > 0 else 0
            if hull_area/rect_area >= .85 and (fits >= .85 or min(ratio*aspect, 1/(ratio*aspect)) >= .85):
                quad = cand
            else:
                reasons.append(f'the red region has {len(approx)} corners and is not a rectangle of the board shape '
                               f'(aspect {max(ratio, 1/ratio):.2f}, expected {max(aspect, 1/aspect):.2f})')
                continue
        else:
            reasons.append(f'the red region has {len(approx)} corners instead of 4')
            continue
        xs, ys = quad[:, 0], quad[:, 1]
        if xs.min() < edge_margin or ys.min() < edge_margin or xs.max() > W-1-edge_margin or ys.max() > H-1-edge_margin:
            reasons.append('the board touches or crosses the edge of the frame: leave about 10% margin on every side')
            continue
        quad_area = max(cv2.contourArea(quad), 1)
        # A ragged mask (parts that are not red) lowers the contour area; the hull is what must fill the quad.
        if area/quad_area < .7 and hull_area/quad_area < .9:
            reasons.append('the red region is too fragmented to be a board: more light, a plain background, or fewer reflections')
            continue
        candidates.append((hull_area if len(approx) != 4 else area, quad/scale))
    if not candidates:
        if reasons:
            detail = reasons[0]
        elif red_fraction < .01:
            detail = 'almost no red soldermask was found: check the exposure and that the board fills the frame'
        else:
            detail = 'no red region is large enough (at least 4% of the frame): move closer'
        raise BoardOutlineError(f'No complete {"dark" if colour == "dark" else "red"} board outline found: ' + detail + '. Show the entire board on a '
                                'neutral background with a margin, or use manual landmarks.')
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and candidates[1][0] > .6*candidates[0][0]:
        raise BoardOutlineError('Two similar-size boards are visible. Photograph one board at a time.')
    quad = candidates[0][1]
    return {'corners': quad.tolist(), 'area_fraction': round(candidates[0][0]/(H*W), 4)}


def board_bounds(layout, overhang=None):
    wires = layout.get('outline', [])
    if not wires or any(abs(w.get('curve', 0)) > .01 for w in wires):
        raise ValueError('Automatic CAD mapping needs a straight rectangular board outline. Refresh the Fusion layout or use manual landmarks.')
    points = np.float32([[w['x1'], w['y1']] for w in wires] + [[w['x2'], w['y2']] for w in wires])
    low, high = points.min(axis=0), points.max(axis=0)
    width, height = high-low
    if min(width, height) <= 0:
        raise ValueError('Degenerate CAD board outline.')
    # Reject shapes with a significant edge inside their bounding rectangle.
    for w in wires:
        if abs(w['x1']-w['x2']) < .01:
            if min(abs(w['x1']-low[0]), abs(w['x1']-high[0])) > .02*width:
                raise ValueError('Non-rectangular CAD outline: use manual landmarks.')
        elif abs(w['y1']-w['y2']) < .01:
            if min(abs(w['y1']-low[1]), abs(w['y1']-high[1])) > .02*height:
                raise ValueError('Non-rectangular CAD outline: use manual landmarks.')
        elif np.linalg.norm([w['x2']-w['x1'], w['y2']-w['y1']]) > .03*max(width, height):
            raise ValueError('Non-rectangular CAD outline: use manual landmarks.')
    # `overhang`: {'left'|'right'|'top'|'bottom': mm} the photographed board extends beyond the layout outline (an antenna tongue, a tab):
    # the outline is then the rectangle of the layout plus that margin, so that the detected board and the layout have one shape.
    for side, mm in (overhang or {}).items():
        mm = float(mm)
        if side == 'left': low[0] -= mm
        elif side == 'right': high[0] += mm
        elif side == 'bottom': low[1] -= mm
        elif side == 'top': high[1] += mm
        else: raise ValueError(f'registration.overhang_mm: unknown side {side!r}')
    return low, high


def mechanical_reference(image, layout, side='top', turns=0, edge_margin=3, colour='red', overhang=None):
    low, high = board_bounds(layout, overhang)
    detection = detect_board(image, edge_margin, aspect=float((high[0]-low[0])/(high[1]-low[1])), colour=colour)
    # Initial reference orientation is user-confirmed. A rectangular outline cannot identify pin one.
    corners = np.roll(np.float32(detection['corners']), -turns % 4, axis=0)
    # The quarter-turn must be consistent with the board shape: a landscape board in a landscape photo
    # cannot be rotated by 90 degrees. Refusing it removes the impossible orientations.
    layout_aspect = float((high[0]-low[0])/(high[1]-low[1]))
    seen = _aspect_of(corners)
    if min(seen/layout_aspect, layout_aspect/seen) < .6:
        raise ValueError(f'Quarter-turn {turns % 4} does not fit the board shape '
                         f'(photo aspect {seen:.2f}, layout {layout_aspect:.2f}).')
    cad = np.float32([[low[0], high[1]], [high[0], high[1]], [high[0], low[1]], [low[0], low[1]]])
    if side == 'bottom':
        cad = cad[[1, 0, 3, 2]]
    initial = cv2.getPerspectiveTransform(cad, corners)
    width = 1400
    height = round(width*float((high[1]-low[1])/(high[0]-low[0])))
    if not 500 <= height <= 2800:
        raise ValueError('Board aspect ratio outside automatic mapping limits.')
    sx = (width-1)/(high[0]-low[0]); sy = (height-1)/(high[1]-low[1])
    canonical = np.array([[sx, 0, -low[0]*sx], [0, -sy, high[1]*sy], [0, 0, 1]], float)
    rough = cv2.warpPerspective(image, canonical @ np.linalg.inv(initial), (width, height))
    gray = cv2.cvtColor(rough, cv2.COLOR_BGR2GRAY)
    holes = layout.get('holes', [])
    # Four widely spaced corner mounting holes; package-local holes were filtered at export.
    if len(holes) != 4:
        # No four-hole set in the layout (the CAM export carries none): register on the four outline corners.
        # Later photos are aligned to this reference by board-plane features and the defect stage refines
        # locally, so only the reference needs this mapping.
        crop = cv2.warpPerspective(image, canonical @ np.linalg.inv(initial), (width, height))
        detection.update(method='outline_corners', matrix=initial.tolist(),
                         crop_matrix=(canonical @ np.linalg.inv(initial)).tolist(),
                         canonical_matrix=canonical.tolist(), canonical_size=[width, height],
                         orientation_confirmed=False, turns=turns)
        return crop, detection
    cad_holes = np.float32([[h['x'], h['y']] for h in holes])
    expected = transform(cad_holes, canonical)
    measured, radii = [], []
    for hole, point in zip(holes, expected):
        radius = float(hole['drill'])*float(sx)*.5
        if radius < 8:
            raise ValueError('Mounting holes too small for automatic detection.')
        search = max(35, round(radius*2))
        x0, y0 = np.maximum(np.int32(point)-search, 0)
        x1, y1 = np.minimum(np.int32(point)+search, [width, height])
        patch = gray[y0:y1, x0:x1]
        circles = None
        for sensitivity in (24, 18):
            circles = cv2.HoughCircles(cv2.GaussianBlur(patch, (5, 5), 0), cv2.HOUGH_GRADIENT,
                dp=1, minDist=radius*.9, param1=100, param2=sensitivity,
                minRadius=max(5, round(radius*.65)), maxRadius=round(radius*1.5))
            if circles is not None:
                break
        if circles is None:
            raise ValueError('Could not identify all four mounting holes. Use a sharper photo or manual landmarks.')
        candidates = circles[0].copy(); candidates[:, :2] += [x0, y0]
        # Prefer the circular outer drill/rim edge rather than a lighting edge inside the hole.
        distances = np.linalg.norm(candidates[:, :2]-point, axis=1)
        scores = distances + np.abs(candidates[:, 2]-radius*1.15)*.5
        best = candidates[np.argmin(scores)]
        if np.linalg.norm(best[:2]-point) > radius*1.5:
            raise ValueError('Mounting-hole match too far from the CAD position; check board orientation.')
        measured.append(best[:2]); radii.append(float(best[2]))
    # Map measured points back to original photo; retain original image evidence.
    inverse_rough = initial @ np.linalg.inv(canonical)
    image_holes = transform(measured, inverse_rough)
    matrix = cv2.getPerspectiveTransform(cad_holes, np.float32(image_holes))
    crop_matrix = canonical @ np.linalg.inv(matrix)
    crop = cv2.warpPerspective(image, crop_matrix, (width, height))
    detection.update(method='outline_and_four_mounting_holes', matrix=matrix.tolist(),
                     photo_holes=image_holes.tolist(), cad_holes=cad_holes.tolist(),
                     crop_matrix=crop_matrix.tolist(), canonical_matrix=canonical.tolist(),
                     canonical_size=[width, height], orientation_confirmed=False,
                     turns=turns)
    return crop, detection


def _feature_mask(image, colour='red'):
    if colour == 'dark':     # no mask colour to select by: every pixel inside the board crop, away from its edge
        mask = np.full(image.shape[:2], 255, np.uint8)
        mask[:15] = mask[-15:] = 0; mask[:, :15] = mask[:, -15:] = 0
        return mask
    # Use solder-mask/printed board features, excluding raised black IC/inductor surfaces.
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    red = np.uint8(((hsv[:, :, 0] < 15) | (hsv[:, :, 0] > 165)) & (hsv[:, :, 1] > 75) & (hsv[:, :, 2] > 60))*255
    mask = cv2.dilate(red, np.ones((13, 13), np.uint8))
    mask[:15] = mask[-15:] = 0; mask[:, :15] = mask[:, -15:] = 0
    return mask


def align_to_reference(reference, candidate, reference_matrix, layout, edge_margin=3, colour='red', overhang=None, tall=()):
    """Match board-plane features, checking spread, residuals and board corner agreement."""
    low, high = board_bounds(layout, overhang)
    aspect = float((high[0]-low[0])/(high[1]-low[1]))
    ref_detection = detect_board(reference, edge_margin, aspect=aspect, colour=colour)
    detection = detect_board(candidate, edge_margin, aspect=aspect, colour=colour)
    width, height = 1200, round(1200*float((high[1]-low[1])/(high[0]-low[0])))
    target = np.float32([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]])
    ref_crop = cv2.getPerspectiveTransform(np.float32(ref_detection['corners']), target)
    cand_crop = cv2.getPerspectiveTransform(np.float32(detection['corners']), target)
    ref = cv2.warpPerspective(reference, ref_crop, (width, height))
    cur = cv2.warpPerspective(candidate, cand_crop, (width, height))
    sift = cv2.SIFT_create(nfeatures=7000, contrastThreshold=.025)
    # Parts that stand off the board plane (connector shells, a module can) shift with the viewing angle: their features are
    # left out so that the planar fit follows the board and not the tallest thing in the picture (protocol `defects.tall`).
    off_plane = np.full((height, width), 255, np.uint8)
    names = set(tall)
    for part in layout.get('parts', []):
        if part.get('name') in names and part.get('polygon'):
            pts = np.float32(part['polygon'])
            px = np.stack([(pts[:, 0]-low[0])/(high[0]-low[0])*(width-1), (high[1]-pts[:, 1])/(high[1]-low[1])*(height-1)], axis=1)
            cv2.fillPoly(off_plane, [np.int32(np.round(px))], 0)
    off_plane = cv2.erode(off_plane, np.ones((15, 15), np.uint8))
    kr, dr = sift.detectAndCompute(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), cv2.bitwise_and(_feature_mask(ref, colour), off_plane))
    kc, dc = sift.detectAndCompute(cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY), cv2.bitwise_and(_feature_mask(cur, colour), off_plane))
    if dr is None or dc is None:
        raise ValueError('Insufficient board-plane features; use manual landmarks.')
    matches = cv2.BFMatcher().knnMatch(dc, dr, k=2)
    good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < .7*pair[1].distance]
    if len(good) < 20:
        raise ValueError('Too few matching PCB features. Check board side/revision and focus.')
    source = np.float32([kc[m.queryIdx].pt for m in good])
    dest = np.float32([kr[m.trainIdx].pt for m in good])
    homography, inliers = cv2.findHomography(source, dest, cv2.RANSAC, 3., maxIters=5000, confidence=.999)
    if homography is None or inliers is None:
        raise ValueError('Could not align PCB features.')
    keep = inliers.ravel().astype(bool)
    count = int(keep.sum()); ratio = count/len(good)
    spread = cv2.contourArea(cv2.convexHull(dest[keep]))/(width*height) if count >= 3 else 0
    residuals = np.linalg.norm(transform(source[keep], homography)-dest[keep], axis=1)
    median_error = float(np.median(residuals)) if len(residuals) else float('inf')
    # Permit any quarter rotation, never reflection or non-overlapping/wild projective fits.
    projected = transform(target, homography)
    corner_error = float(np.max(np.min(np.linalg.norm(projected[:, None]-target[None, :], axis=2), axis=1)))
    signed_area = cv2.contourArea(np.float32(projected), oriented=True)
    if count < 20 or ratio < .25 or spread < .18 or median_error > 2 or corner_error > .12*max(width, height) or signed_area <= 0:
        raise ValueError(f'Alignment unreliable ({count} inliers, spread {spread:.0%}, median error {median_error:.1f}px). Use manual landmarks or retake photo.')
    candidate_to_reference = np.linalg.inv(ref_crop) @ homography @ cand_crop
    matrix = np.linalg.inv(candidate_to_reference) @ np.asarray(reference_matrix, float)
    detection.update(method='outline_and_board_plane_features', matrix=matrix.tolist(),
                     matched_features=len(good), inliers=count, inlier_fraction=round(ratio, 4),
                     feature_spread=round(spread, 4), median_error_px=round(median_error, 3),
                     matching_canvas=[width, height], candidate_to_reference=candidate_to_reference.tolist(),
                     orientation_confirmed=False)
    return detection
