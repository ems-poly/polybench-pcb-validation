"""Run the ported AOI pipeline for a Polybench batch (called by the aoi.run action in a child process)."""
from __future__ import annotations

import json
from pathlib import Path

from polybench.aoi import review_merge as M


def resolve(cfg: dict, key: str):
    v = cfg.get(key)
    if not v:
        return None
    p = Path(v).expanduser()
    return p if p.is_absolute() else (Path(cfg["_dir"]) / p).resolve()


def photos_from_journal(batch_dir: Path):
    """Replay aoi_photo / aoi_photo_move / aoi_photo_drop / aoi_photo_removed from the journal (read only): {file: record}."""
    photos = {}
    jp = Path(batch_dir) / "journal.jsonl"
    for line in jp.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev["type"] == "aoi_photo":
            photos[ev["payload"]["file"]] = ev["payload"]
        elif ev["type"] in ("aoi_photo_drop", "aoi_photo_removed"):       # removed: the file sits in aoi/removed/ and is not analysed
            photos.pop(ev["payload"]["file"], None)
        elif ev["type"] == "aoi_photo_move" and ev["payload"]["file"] in photos:
            photos[ev["payload"]["file"]] = {**photos[ev["payload"]["file"]], "board": ev["payload"]["to"]}
    return photos


_SCORES: dict = {}      # (path, size, mtime) -> focus score, so the badge in the view and the run use one measurement


def _score(path: Path) -> float:
    import cv2
    from polybench.aoi.vision import focus_score, load_photo
    st = path.stat()
    k = (str(path), st.st_size, st.st_mtime_ns)
    if k not in _SCORES:
        img = load_photo(path)
        small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), None, fx=0.25, fy=0.25)
        _SCORES[k] = float(focus_score(small))
    return _SCORES[k]


def pick_mapping(batch_dir: Path, photos: dict, log=print):
    """One photo per board and side goes into the pipeline: the sharpest of the uploads for it (the others stay as
    stored evidence). Unassigned photos are listed and never guessed."""
    groups: dict[tuple, list] = {}
    for f, p in photos.items():
        if not p.get("board"):
            log(f"photo {p['name']} has no board assigned: not used", "warn")
            continue
        groups.setdefault((p["board"], p["side"]), []).append((f, p))
    mapping, used, extra = {}, [], []
    for (board, side), lst in sorted(groups.items()):
        best, best_score = None, -1.0
        for f, p in lst:
            path = Path(batch_dir) / f
            score = 0.0
            if len(lst) > 1:
                score = _score(path)
            if score > best_score:
                best, best_score = (f, p, path), score
        mapping.setdefault(board, {})[side] = best[2]
        used.append({"board": board, "side": side, "file": best[0], "name": best[1]["name"], "of": len(lst)})
        if len(lst) > 1:
            log(f"{board} {side}: {len(lst)} photos, using the sharpest ({best[1]['name']})")
            extra += [f for f, _ in lst if f != best[0]]
    return mapping, used, extra


RED_MIN = 0.15      # reference photos measure 0.32 to 0.36; a green or black mask measures about 0


def mask_colour_check(path):
    """The pipeline (exposed-mask tests, colour gain, bare-mask fraction) assumes a red solder mask. Fraction of red-mask pixels in the
    middle of a photo, with the PoC's own red rule (defect_stage._gains)."""
    import cv2
    from polybench.aoi.vision import load_photo
    im = load_photo(path)
    h, w = im.shape[:2]
    c = cv2.resize(im[h // 5:4 * h // 5, w // 5:4 * w // 5], None, fx=.25, fy=.25)
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    red = ((hsv[..., 0] < 14) | (hsv[..., 0] > 165)) & (hsv[..., 1] > 110) & (hsv[..., 2] > 70)
    f = round(float(red.mean()), 3)
    return {"red_fraction": f, "ok": f >= RED_MIN}


LIFT_NOTE = (" Lift is review only in Polybench: the 0.5 mm flag threshold lies inside the board-to-board spread of the same part "
             "(median 0.42 mm from photography alone, 16 Oct review). Judge by eye.")
POL_NOTE = " Polarity is not established by AOI: look at the part by eye. Undecided is never a pass."


def harden(items):
    import hashlib
    for it in items:
        ev = it.get("evidence") or []
        try:
            it["evidence_sha"] = hashlib.sha256(Path(ev[0]).read_bytes()).hexdigest() if ev else ""
        except OSError:
            it["evidence_sha"] = ""
        if it["kind"] == "LIFT":
            it["status"] = "review"
            if LIFT_NOTE not in it["note"]:
                it["note"] += LIFT_NOTE
        elif it["kind"] in ("POL", "POLSYS") and POL_NOTE not in it["note"]:
            it["note"] += POL_NOTE
    return items


def run(batch_dir: Path, out_dir: Path, cfg: dict, log=print, simulate=False):
    if cfg.get("mode") == "diff":
        return run_diff(batch_dir, out_dir, cfg, log)
    from polybench.aoi import protocol_run as PR
    from polybench.aoi.protocol import load_protocol
    protocol_path, layout_path = resolve(cfg, "protocol"), resolve(cfg, "layout")
    protocol = load_protocol(protocol_path)
    layout = json.loads(Path(layout_path).read_text())
    if (protocol.get("defects") or {}).get("netlist"):
        protocol["defects"]["netlist"] = str(Path(protocol_path).parent / protocol["defects"]["netlist"])
    if (protocol.get("polarity") or {}).get("spec"):
        protocol["polarity"]["spec"] = str(Path(protocol_path).parent / protocol["polarity"]["spec"])
    cam_zip = resolve(cfg, "cam_zip") or Path(protocol["layout"]["cam_zip"])
    photos = photos_from_journal(batch_dir)
    mapping, used, extra = pick_mapping(batch_dir, photos, log)
    if not mapping:
        if not simulate:
            raise RuntimeError("no photos assigned to a board: upload photos in the AOI view first")
        from polybench.aoi import synthetic
        doc = synthetic.make(layout, protocol, out_dir, log)
    else:
        for b in mapping:
            protocol["boards"].setdefault(b, {"label": "", "expect_absent": []})
        ref = protocol.get("reference_board")
        if ref not in mapping or "top" not in mapping.get(ref, {}):
            protocol["reference_board"] = next(b for b in mapping if "top" in mapping[b])
        doc = PR.run(protocol, layout, mapping, Path(out_dir) / "aoi_result.json", cam_zip, photos_dir=None, log=log,
                     exclude_confirmed=None)
    items = harden(M.review_items(doc))
    notes = []
    if mapping:
        first = next(iter(sorted(p for sides in mapping.values() for p in sides.values())))
        chk = mask_colour_check(first)
        dark = (protocol.get("registration") or {}).get("board_colour") == "dark"
        if dark:
            notes.append("Black solder mask (protocol registration.board_colour: dark). The board outline, the mask-colour rules of the missing and "
                         "lift checks and the pad refinement use a dark-mask rule written for this board; the thresholds were set on red masks. "
                         "Treat every AOI flag, and the absence of flags, as UNVALIDATED; a part's polarity is only reported when the detector passes its own 180 degree test.")
            log(notes[-1], "warn")
        elif not chk["ok"]:
            notes.append(f"WARNING: the solder mask in {Path(first).name} is not red (red-mask fraction {chk['red_fraction']}, reference 0.32 to 0.36). "
                         "The pipeline's presence, colour-gain, bare-mask and bridge measures assume a red mask and are not calibrated for this board: "
                         "treat every AOI flag and the absence of flags with extra caution.")
            log(notes[-1], "warn")
    counts = {}
    for b in doc["boards"]:
        counts[b["board"]] = {}
        for r in b["results"]:
            counts[b["board"]][r["status"]] = counts[b["board"]].get(r["status"], 0) + 1
    return {"result": str(Path(out_dir) / "aoi_result.json"), "dir": str(out_dir), "items": items,
            "boards": [b["board"] for b in doc["boards"] if mapping.get(b["board"]) or not mapping],
            "photos": used, "counts": counts, "notes": notes, "synthetic": not mapping}


def run_diff(batch_dir: Path, out_dir: Path, cfg: dict, log=print):
    """Layout-free board-to-board difference (docs/AOI.md, "Diff mode"). Same return shape as run()."""
    from polybench.aoi import diffrun
    photos = photos_from_journal(batch_dir)
    mapping, used, extra = pick_mapping(batch_dir, photos, log)
    if not mapping:
        raise RuntimeError("no photos assigned to a board: upload photos in the AOI view first")
    doc, meta, reg = diffrun.run(mapping, cfg, Path(out_dir), log)
    items = harden(M.review_items(doc))
    for it in items:
        it["diff"] = meta.get(it["key"])
    counts = {b["board"]: {"review": len(b["results"])} for b in doc["boards"]}
    notes = [f"Diff mode: every candidate is a difference from the reference board {doc['reference']}. Glare masked, parallax on tall parts not corrected."]
    return {"result": str(Path(out_dir) / "aoi_result.json"), "dir": str(out_dir), "items": items, "boards": [b["board"] for b in doc["boards"]],
            "photos": used, "counts": counts, "notes": notes, "synthetic": False, "mode": "diff", "reference": doc["reference"], "registration": reg}
