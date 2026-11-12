"""Run of the AOI diff mode for a batch (called by runner.run when the protocol's `aoi:` block says `mode: diff`).

`aoi: {mode: diff, reference: D01}`; optional `crop: [x0, y0, x1, y1]` (fractions of the reference photo) replaces the automatic board outline.
One top photo per board (the sharpest, as in the layout mode). Every board other than the reference is registered onto the reference and
compared; the result is written in the same `polystage.aoi_result/0` shape the review page reads. Items carry `diff` metadata: the boards
a decision can name as the one with the issue, the box in reference pixels (the hook for a later odd-one-out across three or more boards:
regions that coincide on several boards point at the reference).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2

from polybench.aoi import diff as D
from polybench.aoi.vision import load_photo

LABEL = "differs from the reference board"


def run(mapping: dict, cfg: dict, out_dir: Path, log=print):
    out_dir = Path(out_dir)
    boards = sorted(b for b in mapping if "top" in mapping[b])
    if len(boards) < 2:
        raise RuntimeError("diff mode needs a photo for the reference board and at least one other board: assign them to tiles in the AOI view first")
    ref_label = cfg.get("reference") or boards[0]
    if ref_label not in boards:
        raise RuntimeError(f"the reference board {ref_label} has no photo: assign its photo to its tile first")
    crop = cfg.get("crop")
    thresh = float(cfg.get("threshold", D.THRESH))
    ref_img = load_photo(mapping[ref_label]["top"])
    doc = {"schema": "polystage.aoi_result/0", "report_ref": "", "tool": "polybench-aoi diff (no layout)", "mode": "diff", "reference": ref_label,
           "generated": datetime.now().astimezone().isoformat(timespec="seconds"), "boards": [{"board": ref_label, "photo": str(mapping[ref_label]["top"]), "results": []}]}
    items_meta, registration = {}, {}
    for b in boards:
        if b == ref_label:
            continue
        log(f"{b}: registering onto the reference {ref_label}")
        try:
            r = D.compare(ref_img, load_photo(mapping[b]["top"]), crop, thresh)
        except D.RegistrationError as e:
            log(f"{b}: {e}", "error")
            raise RuntimeError(f"{b}: {e}") from e
        i = r["info"]
        log(f"{b}: registration {i['inliers']} inliers of {i['matches']} matches, reprojection RMS {i['reproj_rms_px']} px, outline {i['outline']}, "
            f"ECC {i['ecc']}; flow {i.get('flow', '')}")
        rep = r["report"]
        log(f"{b}: glare masked on {rep['glare_fraction']*100:.1f} % of the board; {len(r['cands'])} regions {LABEL}")
        registration[b] = {**i, **rep}
        cv2.imwrite(str(out_dir / f"registration_{b}.jpg"), cv2.resize(D.overlay(r["ref"], r["warped"], r["mask"]), None, fx=.6, fy=.6), [cv2.IMWRITE_JPEG_QUALITY, 85])
        results = []
        for n, c in enumerate(r["cands"], 1):
            rid = f"AOI-DIFF-{n}"
            path = out_dir / "evidence" / b / f"{rid}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), D.evidence_sheet(r["ref"], r["warped"], r["dmap"], c, ref_label, b, n, len(r["cands"])), [cv2.IMWRITE_JPEG_QUALITY, 88])
            note = (f"{b} {LABEL} {ref_label} here (structure difference {c['score']}, {c['area_frac']*100:.1f} % of the frame). "
                    f"This is a difference, not a verdict: with two boards it cannot say which one is wrong. Tall parts (connectors, the module can) "
                    f"move with the viewing angle and glare is masked, so check by eye.")
            results.append({"id": rid, "status": "review", "value": f"differs from {ref_label}", "evidence": [str(path.resolve())],
                            "confidence": round(min(0.99, c["score"]), 2), "note": note})
            items_meta[f"{b}|{rid}"] = {"mode": "diff", "reference": ref_label, "boards": [b, ref_label], "box_ref_px": c["box"],
                                         "work_px": i["work_px"], "score": c["score"], "suggestion": None}
        doc["boards"].append({"board": b, "photo": str(mapping[b]["top"]), "results": results})
    doc["registration"] = registration
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "aoi_result.json").write_text(json.dumps(doc, indent=2))
    return doc, items_meta, registration
