"""AOI diff mode: board-to-board difference without a layout.

One photo is the reference board; every other photo is registered onto it (SIFT matches, RANSAC homography, ECC refinement) and
compared on structure. The result is a ranked list of regions that DIFFER FROM THE REFERENCE BOARD. With two boards a difference cannot
say which board is wrong; the operator decides. Nothing here says "defect". HOST-ONLY, UNVALIDATED on real batches.

Colour: the method never reads hue. Photos are reduced to grey, contrast-equalised (CLAHE), the local mean is removed and the structure term of
SSIM is compared, so a red, green, blue or black solder mask is treated the same (tests/aoi/test_aoi_diff.py recolours a board to check).

Known limits (stated in docs/AOI.md): tall parts (USB-C shells, the module can) shift with the viewing angle (parallax) and show up as
differences when the two photos were taken from different angles; saturated glare is masked and reported, not compared; a missing part, a
reversed part or a bridge is not understood, only that the picture differs.
"""
from __future__ import annotations

import cv2
import numpy as np

WORK = 1600               # long side of the working image, px (TBR: enough for SMD passives at phone-photo distance, fast enough for a laptop)
MIN_INLIERS = 40          # TBR: below this the homography is not trusted (good pairs of board photos give hundreds)
MAX_REPROJ = 3.0          # TBR: RMS px at WORK resolution over the inliers; the board is flat, so a larger spread means a bad match
MIN_INLIER_RATIO = 0.25
MIN_COVER = 0.08          # fraction of the reference frame the matched board covers
THRESH = 0.62             # TBR: 1 - structure similarity above which a pixel differs; chosen on the demo pair and a synthetic board
FLOW_MAX = 6.0            # TBR: largest local correction in px at WORK resolution
GLARE = 250               # a pixel with any channel at or above this is saturated
MAX_CANDIDATES = 12
MAX_AREA = 0.02          # TBR: a candidate larger than this share of the frame is split at a higher level (a crop of 2 % shows a part, not a quarter board)


class RegistrationError(ValueError):
    """The photos cannot be registered well enough to compare. The message starts with 'retake the photo:'."""


def _work(img):
    s = WORK / max(img.shape[:2])
    return (cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img.copy()), min(s, 1.0)


def _gray_eq(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)


def _hull_mask(shape, pts, grow=0.04):
    m = np.zeros(shape[:2], np.uint8)
    hull = cv2.convexHull(np.float32(pts).reshape(-1, 1, 2)).astype(np.int32)
    cv2.fillConvexPoly(m, hull, 255)
    k = max(3, int(grow * max(shape[:2]))) | 1
    return cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))


def board_outline(img):
    """Board mask without any colour assumption: a populated board is dense in edges (silkscreen, pads, parts), the mat or desk around it is
    smooth. Edge energy of the equalised grey image, blurred, Otsu, largest blob, convex hull. The operator's crop replaces it."""
    g = _gray_eq(img)
    e = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    e = cv2.GaussianBlur(e, (0, 0), max(8, max(g.shape) // 60))
    e8 = np.uint8(np.clip(e / (np.percentile(e, 99.5) + 1e-6), 0, 1) * 255)
    _, bw = cv2.threshold(e8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    n, lab, st, _ = cv2.connectedComponentsWithStats(bw, 8)
    if n < 2:
        return None
    big = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    cnts, _ = cv2.findContours((lab == big).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    m = np.zeros(g.shape, np.uint8)
    cv2.fillConvexPoly(m, cv2.convexHull(max(cnts, key=cv2.contourArea)), 255)
    return m


def _ncc(a, b, mask):
    sel = mask > 0
    if sel.sum() < 100:
        return -1.0
    x, y = a[sel].astype(np.float32), b[sel].astype(np.float32)
    x, y = x - x.mean(), y - y.mean()
    return float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-6))


def register(ref, img, crop=None):
    """Register `img` onto `ref` (both BGR, already at working size). Returns dict: warped, mask (255 = board in the reference frame),
    H, inliers, matches, reproj_rms, ecc. Raises RegistrationError("retake the photo: ...") when the match is poor.
    `crop`: optional [x0, y0, x1, y1] as fractions of the reference frame; the operator's crop replaces the automatic outline."""
    h, w = ref.shape[:2]
    gr, gi = _gray_eq(ref), _gray_eq(img)
    sift = cv2.SIFT_create(nfeatures=6000)
    kr, dr = sift.detectAndCompute(gr, None)
    ki, di = sift.detectAndCompute(gi, None)
    if dr is None or di is None or len(kr) < 30 or len(ki) < 30:
        raise RegistrationError("retake the photo: too few features found (blurred, too dark, or the board fills too little of the frame). "
                                f"Features found: reference {0 if dr is None else len(kr)}, this photo {0 if di is None else len(ki)}.")
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(di, dr, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.78 * n.distance]
    if len(good) < MIN_INLIERS:
        raise RegistrationError(f"retake the photo: only {len(good)} feature matches with the reference (need {MIN_INLIERS}). "
                                "Same distance, flat even light, looking straight down, board fully in the frame.")
    src = np.float32([ki[m.queryIdx].pt for m in good])
    dst = np.float32([kr[m.trainIdx].pt for m in good])
    H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0, maxIters=5000, confidence=0.999)
    if H is None:
        raise RegistrationError("retake the photo: no consistent homography between this photo and the reference.")
    # guided second pass: with a first plane estimate, accept looser matches that agree with it (spreads the inliers over the whole board,
    # so the fit does not extrapolate from one corner), then refit
    loose = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.92 * n.distance]
    s2 = np.float32([ki[m.queryIdx].pt for m in loose])
    d2 = np.float32([kr[m.trainIdx].pt for m in loose])
    pred = cv2.perspectiveTransform(s2.reshape(1, -1, 2), H)[0]
    near = np.linalg.norm(pred - d2, axis=1) < 10
    if near.sum() > inl.sum():
        H2, inl2 = cv2.findHomography(s2[near], d2[near], cv2.RANSAC, 3.0, maxIters=3000)
        if H2 is not None:
            H, src, dst, inl, good = H2, s2[near], d2[near], inl2, [m for m, k in zip(loose, near) if k]
    inl = inl.ravel().astype(bool)
    n_in = int(inl.sum())
    err = np.linalg.norm(cv2.perspectiveTransform(src[inl].reshape(1, -1, 2), H)[0] - dst[inl], axis=1)
    rms = float(np.sqrt((err ** 2).mean())) if n_in else 99.0
    info = {"matches": len(good), "inliers": n_in, "inlier_ratio": round(n_in / len(good), 3), "reproj_rms_px": round(rms, 2),
            "reproj_median_px": round(float(np.median(err)), 2) if n_in else 99.0, "work_px": [w, h]}
    ratio = n_in / len(good)
    if n_in < MIN_INLIERS or ratio < MIN_INLIER_RATIO:
        raise RegistrationError(f"retake the photo: registration is poor ({n_in} inliers of {len(good)} matches, need {MIN_INLIERS} and {int(MIN_INLIER_RATIO*100)} %). "
                                "Check focus, glare and that the board is the same side up.")
    if rms > MAX_REPROJ:
        raise RegistrationError(f"retake the photo: registration error {rms:.1f} px is above {MAX_REPROJ} px. The board may be bent, or the photo taken "
                                "at a very different angle or distance.")
    corners = cv2.perspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(1, -1, 2), H)[0]
    if not cv2.isContourConvex(corners.astype(np.float32)) or cv2.contourArea(corners.astype(np.float32)) < 0.1 * w * h:
        raise RegistrationError("retake the photo: the fitted warp is degenerate (folded or tiny). The photos probably show different boards or sides.")
    # outline of the board in the reference frame: the hull of the matched points that agree with the plane (colour independent)
    mask = _hull_mask(ref.shape, dst[inl])
    outline = board_outline(ref)
    info["outline"] = "texture"
    if outline is not None and (outline > 0).mean() >= 0.8 * (mask > 0).mean():
        mask = outline
    else:
        info["outline"] = "matched points"
    if crop:
        mask = np.zeros((h, w), np.uint8)
        x0, y0, x1, y1 = crop
        mask[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = 255
    cover = float((mask > 0).mean())
    info["cover"] = round(cover, 3)
    if cover < MIN_COVER:
        raise RegistrationError(f"retake the photo: the matched board covers only {cover*100:.0f} % of the frame. Move closer so the board fills the frame.")
    warped = cv2.warpPerspective(img, H, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    valid = cv2.warpPerspective(np.full(img.shape[:2], 255, np.uint8), H, (w, h), flags=cv2.INTER_NEAREST)
    mask = cv2.bitwise_and(mask, cv2.erode(valid, np.ones((5, 5), np.uint8)))
    # ECC refinement of the residual (sub-feature accuracy); kept only when it improves the correlation inside the board
    info["ecc"] = "not run"
    try:
        a = cv2.GaussianBlur(_gray_eq(ref), (0, 0), 1.5)
        b = cv2.GaussianBlur(_gray_eq(warped), (0, 0), 1.5)
        before = _ncc(a, b, mask)
        W = np.eye(3, dtype=np.float32)
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5)
        _, W = cv2.findTransformECC(a, b, W, cv2.MOTION_HOMOGRAPHY, crit, mask, 7)
        w2 = cv2.warpPerspective(warped, W, (w, h), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
        after = _ncc(a, cv2.GaussianBlur(_gray_eq(w2), (0, 0), 1.5), mask)
        if after > before:
            warped = w2
            # H stays the feature homography; ECC is a residual correction applied to the warped image only
            info["ecc"] = f"applied (ncc {before:.3f} -> {after:.3f})"
        else:
            info["ecc"] = f"rejected (ncc {before:.3f} -> {after:.3f})"
    except cv2.error as e:
        info["ecc"] = "failed: " + str(e).splitlines()[-1][:60]
    # local residual (lens distortion, board flex, tall parts at a different angle): dense flow, smoothed and clipped so that a real
    # difference in appearance is not explained away as motion
    try:
        ga, gb = _gray_eq(ref), _gray_eq(warped)
        flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM).calc(ga, gb, None)
        flow = cv2.GaussianBlur(flow, (0, 0), 12)
        flow = np.clip(flow, -FLOW_MAX, FLOW_MAX)
        gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        w3 = cv2.remap(warped, gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_LINEAR)
        n0 = _ncc(cv2.GaussianBlur(ga, (0, 0), 1.5), cv2.GaussianBlur(gb, (0, 0), 1.5), mask)
        n1 = _ncc(cv2.GaussianBlur(ga, (0, 0), 1.5), cv2.GaussianBlur(_gray_eq(w3), (0, 0), 1.5), mask)
        info["flow"] = f"{'applied' if n1 > n0 else 'rejected'} (ncc {n0:.3f} -> {n1:.3f}, max shift {FLOW_MAX} px)"
        if n1 > n0:
            warped = w3
    except cv2.error as e:
        info["flow"] = "failed: " + str(e).splitlines()[-1][:60]
    return {"warped": warped, "mask": mask, "H": H, "info": info}


def structure_map(ref, warped, mask, shift=2, sigma=3.0):
    """1 - structure similarity per pixel (0 = same, up to 2), on grey, CLAHE, local-mean removed images. The warped image is compared at
    +-`shift` px offsets and the best match per pixel is kept, so a registration residual of a pixel or two does not light every edge."""
    def prep(img):
        g = _gray_eq(img).astype(np.float32)
        g = g - cv2.GaussianBlur(g, (0, 0), 15)
        return g / (g[mask > 0].std() + 1e-6)

    a = prep(ref)
    b0 = prep(warped)
    mu_a = cv2.GaussianBlur(a, (0, 0), sigma)
    var_a = cv2.GaussianBlur(a * a, (0, 0), sigma) - mu_a ** 2
    C = 0.08
    best = np.full(a.shape, -1.0, np.float32)
    for dy in range(-shift, shift + 1, shift):
        for dx in range(-shift, shift + 1, shift):
            b = np.roll(np.roll(b0, dy, 0), dx, 1)
            mu_b = cv2.GaussianBlur(b, (0, 0), sigma)
            var_b = cv2.GaussianBlur(b * b, (0, 0), sigma) - mu_b ** 2
            cov = cv2.GaussianBlur(a * b, (0, 0), sigma) - mu_a * mu_b
            cs = (2 * cov + C) / (var_a + var_b + C)
            best = np.maximum(best, cs)
    return np.clip(1.0 - best, 0, 2)


def glare_mask(ref, warped):
    sat = ((ref.max(axis=2) >= GLARE) | (warped.max(axis=2) >= GLARE)).astype(np.uint8) * 255
    return cv2.dilate(sat, np.ones((9, 9), np.uint8))


def candidates(ref, warped, mask, thresh=THRESH, max_n=MAX_CANDIDATES, min_area_frac=0.0004):
    """(list of candidates, report). Each candidate: box [x0,y0,x1,y1] in the reference frame (px of the working image), score, area_frac."""
    glare = glare_mask(ref, warped)
    valid = cv2.bitwise_and(mask, cv2.bitwise_not(glare))
    valid = cv2.erode(valid, np.ones((9, 9), np.uint8))
    d = structure_map(ref, warped, mask)
    d = cv2.blur(d, (11, 11)) * (valid > 0)
    h, w = d.shape
    out = []
    close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))

    def split(region_mask, level, depth):
        """Connected regions above `level` inside region_mask; one that is larger than MAX_AREA is split again at a higher level, so a
        whole half board never becomes one candidate."""
        binary = cv2.morphologyEx(((d > level) & region_mask).astype(np.uint8) * 255, cv2.MORPH_CLOSE, close) > 0
        n, lab, stats, _ = cv2.connectedComponentsWithStats(binary.astype(np.uint8), 8)
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if area < min_area_frac * h * w:
                continue
            region = lab == i
            if area > MAX_AREA * h * w and depth < 6:
                split(region, level + 0.06, depth + 1)
                continue
            out.append({"box": [int(x), int(y), int(x + bw), int(y + bh)], "score": round(float(d[region].mean()), 3),
                        "peak": round(float(d[region].max()), 3), "area_frac": round(float(area) / (h * w), 5),
                        "rank_value": float((d[region] - thresh).clip(0).sum())})

    split(np.ones(d.shape, bool), thresh, 0)
    out.sort(key=lambda c: -c["rank_value"])
    out = out[:max_n]
    board_px = max(1, int((mask > 0).sum()))
    report = {"glare_fraction": round(float(((glare > 0) & (mask > 0)).sum()) / board_px, 4), "board_fraction": round(board_px / (h * w), 3),
              "threshold": thresh, "candidates": len(out)}
    return out, report, d


def _label(img, text, y=20):
    """The text is drawn once, on a dark plate. (An outline pass drawn first with a thicker stroke lands offset from the white
    pass in the OpenCV 5 text renderer, so every label showed twice.)"""
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, .55, 1)
    cv2.rectangle(img, (0, max(0, y - th - 5)), (min(img.shape[1], tw + 12), min(img.shape[0], y + base + 3)), (24, 24, 24), -1)
    cv2.putText(img, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1, cv2.LINE_AA)


def evidence_sheet(ref, warped, dmap, cand, ref_label, board_label, n, total, panel=380, pad=0.5):
    """One JPEG: crop of the reference | the same crop of the board | difference map, and the whole reference board with the region boxed."""
    h, w = ref.shape[:2]
    x0, y0, x1, y1 = cand["box"]
    bw, bh = x1 - x0, y1 - y0
    side = max(bw, bh, 120)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = side * (1 + pad) / 2
    X0, Y0, X1, Y1 = int(max(0, cx - half)), int(max(0, cy - half)), int(min(w, cx + half)), int(min(h, cy + half))
    def tile(src):
        t = cv2.resize(src[Y0:Y1, X0:X1], (panel, int(panel * (Y1 - Y0) / (X1 - X0))), interpolation=cv2.INTER_CUBIC)
        return t
    r, b = tile(ref), tile(warped)
    heat = cv2.applyColorMap(np.uint8(np.clip(dmap / 1.2, 0, 1) * 255), cv2.COLORMAP_INFERNO)
    hm = tile(cv2.addWeighted(ref, 0.45, heat, 0.55, 0))
    for t in (r, b, hm):
        s = panel / (X1 - X0)
        cv2.rectangle(t, (int((x0 - X0) * s), int((y0 - Y0) * s)), (int((x1 - X0) * s), int((y1 - Y0) * s)), (0, 220, 255), 2)
    _label(r, f"REFERENCE {ref_label}")
    _label(b, f"{board_label} (registered)")
    _label(hm, "difference (structure)")
    loc = ref.copy()
    cv2.rectangle(loc, (x0, y0), (x1, y1), (0, 220, 255), max(4, w // 200))
    loc = cv2.resize(loc, (panel, int(panel * h / w)))
    _label(loc, "where on the board")
    hh = max(t.shape[0] for t in (r, b, hm, loc))
    row = [cv2.copyMakeBorder(t, 0, hh - t.shape[0], 0, 6, cv2.BORDER_CONSTANT, value=(30, 30, 30)) for t in (r, b, hm, loc)]
    sheet = np.concatenate(row, axis=1)
    banner = np.full((30, sheet.shape[1], 3), (40, 40, 40), np.uint8)
    _label(banner, f"{board_label} differs from the reference board {ref_label}: candidate {n}/{total}", 20)
    return np.concatenate([banner, sheet], axis=0)


def overlay(ref, warped, mask):
    """Registration check image: reference in the red/blue channels against the warped board in green, with the board outline."""
    g1, g2 = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    img = cv2.merge([g1, g2, g1])
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(img, cnts, -1, (0, 255, 255), 3)
    return img


def compare(ref_bgr, img_bgr, crop=None, thresh=THRESH):
    """Whole comparison for two photos (any size). Returns dict: info (registration), report, cands (with the working-size images for sheets)."""
    ref, _ = _work(ref_bgr)
    img, _ = _work(img_bgr)
    reg = register(ref, img, crop)
    cands, report, dmap = candidates(ref, reg["warped"], reg["mask"], thresh)
    return {"ref": ref, "warped": reg["warped"], "mask": reg["mask"], "dmap": dmap, "info": reg["info"], "report": report, "cands": cands}
