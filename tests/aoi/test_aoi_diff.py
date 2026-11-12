"""AOI diff mode (no layout): registration, planted difference, mask-colour independence, glare, refusal. HOST-ONLY, synthetic boards."""
import pytest
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from polybench.aoi import diff as D

PLANT = (560, 330, 700, 440)      # x0, y0, x1, y1 of the planted difference in the reference frame


def board(mask_bgr=(40, 70, 30), planted=False, seed=3, glare=False):
    """A 1600 x 1200 'photo': a mat, a board of a given mask colour carrying parts, text and pads. Same seed = same board."""
    rng = np.random.default_rng(seed)
    img = np.full((1200, 1600, 3), (150, 140, 110), np.uint8)
    img = cv2.add(img, rng.integers(0, 14, img.shape, dtype=np.uint8))
    cv2.rectangle(img, (150, 200), (1450, 1000), mask_bgr, -1)
    for _ in range(60):                                            # parts
        x, y = int(rng.integers(200, 1350)), int(rng.integers(250, 900))
        w, h = int(rng.integers(20, 90)), int(rng.integers(14, 60))
        col = tuple(int(c) for c in rng.integers(20, 230, 3))
        cv2.rectangle(img, (x, y), (x + w, y + h), col, -1)
        cv2.rectangle(img, (x, y), (x + w, y + h), (230, 230, 230), 2)
    for i in range(25):                                            # pads and text
        cv2.circle(img, (200 + i * 50, 960), 12, (200, 200, 200), -1)
        cv2.putText(img, f"R{i}", (190 + i * 50, 930), cv2.FONT_HERSHEY_SIMPLEX, .5, (235, 235, 235), 2)
    if planted:
        x0, y0, x1, y1 = PLANT
        cv2.rectangle(img, (x0, y0), (x1, y1), mask_bgr, -1)          # a part missing here ...
        cv2.circle(img, ((x0 + x1) // 2, (y0 + y1) // 2), 35, (235, 235, 235), -1)    # ... and a blob where there was none
    if glare:
        cv2.circle(img, (1200, 800), 60, (255, 255, 255), -1)
    return img


def warp(img, seed=1):
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    rng = np.random.default_rng(seed)
    dst = src + rng.uniform(-35, 35, src.shape).astype(np.float32)
    out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=(150, 140, 110))
    out = cv2.convertScaleAbs(out, alpha=0.85, beta=12)             # a different exposure
    return cv2.GaussianBlur(out, (0, 0), 1.0)


def hits(cands, box=PLANT):
    x0, y0, x1, y1 = box
    return [c for c in cands if not (c["box"][2] < x0 or c["box"][0] > x1 or c["box"][3] < y0 or c["box"][1] > y1)]


def test_registration_quality_is_reported_and_a_planted_difference_is_the_top_candidate():
    ref = board()
    r = D.compare(ref, warp(board(planted=True)))
    info = r["info"]
    assert info["inliers"] >= D.MIN_INLIERS and info["reproj_rms_px"] < D.MAX_REPROJ and "ecc" in info
    assert r["cands"] and hits(r["cands"][:2]), r["cands"]


def test_the_same_board_photographed_twice_has_no_large_difference():
    r = D.compare(board(), warp(board()))
    assert len([c for c in r["cands"] if c["area_frac"] > 0.003]) == 0, r["cands"]


@pytest.mark.parametrize("mask", [(30, 30, 200), (40, 170, 40), (200, 60, 30), (8, 8, 8)], ids=["red", "green", "blue", "black"])
def test_mask_colour_does_not_matter(mask):
    """No red-mask assumption: the planted difference is found on a red, green, blue and black board alike."""
    r = D.compare(board(mask), warp(board(mask, planted=True)))
    assert hits(r["cands"][:3]), (mask, r["cands"])


def test_glare_is_masked_and_reported_not_compared():
    ref = board(glare=True)
    r = D.compare(ref, warp(board(glare=True, planted=True)))
    assert r["report"]["glare_fraction"] > 0
    assert not hits(r["cands"], (1130, 730, 1270, 870))        # the saturated disc is never a candidate


def test_poor_registration_is_refused_with_a_retake_message():
    noise = np.random.default_rng(9).integers(0, 255, (1200, 1600, 3), dtype=np.uint8)
    with pytest.raises(D.RegistrationError, match="^retake the photo:"):
        D.compare(board(), cv2.GaussianBlur(noise, (0, 0), 6))
    with pytest.raises(D.RegistrationError, match="^retake the photo:"):
        D.compare(board(), np.full((1200, 1600, 3), 120, np.uint8))        # featureless


def test_operator_crop_limits_the_comparison():
    r = D.compare(board(), warp(board(planted=True)), crop=[0.0, 0.0, 0.3, 0.3])
    assert not hits(r["cands"])


def test_evidence_sheet_has_reference_board_difference_and_locator(tmp_path):
    r = D.compare(board(), warp(board(planted=True)))
    sheet = D.evidence_sheet(r["ref"], r["warped"], r["dmap"], r["cands"][0], "D01", "D02", 1, len(r["cands"]))
    assert sheet.shape[1] > 4 * 300 and sheet.shape[0] > 150


def test_diff_run_writes_the_result_the_review_reads(tmp_path):
    """diffrun on two stored photos: same aoi_result shape, DIFF items with the boards a decision can name, no 'defect' wording."""
    from polybench.aoi import diffrun
    from polybench.aoi import review_merge as M
    cv2.imwrite(str(tmp_path / "a.jpg"), board())
    cv2.imwrite(str(tmp_path / "b.jpg"), warp(board(planted=True)))
    doc, meta, reg = diffrun.run({"D01": {"top": tmp_path / "a.jpg"}, "D02": {"top": tmp_path / "b.jpg"}}, {"reference": "D01"}, tmp_path / "run")
    items = M.review_items(doc)
    assert items and all(i["kind"] == "DIFF" and i["id"].startswith("AOI-DIFF-") for i in items)
    assert all("differs from" in i["value"] and "defect" not in (i["value"] + i["note"]).lower().replace("not a verdict", "") for i in items)
    assert meta[items[0]["key"]]["boards"] == ["D02", "D01"] and doc["reference"] == "D01" and reg["D02"]["inliers"] >= D.MIN_INLIERS
    with pytest.raises(RuntimeError, match="reference"):
        diffrun.run({"D01": {"top": tmp_path / "a.jpg"}, "D02": {"top": tmp_path / "b.jpg"}}, {"reference": "D09"}, tmp_path / "run2")
    with pytest.raises(RuntimeError, match="at least one other"):
        diffrun.run({"D01": {"top": tmp_path / "a.jpg"}}, {"reference": "D01"}, tmp_path / "run3")


def test_panel_labels_are_drawn_once():
    """The label used to be drawn twice (a black outline pass offset from the white pass): the plate is dark grey and no pure-black pixel is left."""
    img = np.full((60, 400, 3), 128, np.uint8)
    D._label(img, "REFERENCE D01")
    assert int((img.sum(axis=2) <= 3 * 8).sum()) == 0
    assert tuple(img[5, 5]) == (24, 24, 24) or tuple(img[10, 3]) == (24, 24, 24)
