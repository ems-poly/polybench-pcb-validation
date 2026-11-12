"""Synthetic AOI result for `--simulate` batches that have no photos: proves the layout loads, the issue list, the
review page and the journal path work. Every candidate is labelled SIMULATED in its value and its evidence image."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


def _sheet(layout, names, title, size=(420, 320), ppmm=40):
    parts = {p["name"]: p for p in layout["parts"]}
    tiles = []
    for tag in ("this board", "peer", "peer"):
        img = np.full((size[1], size[0], 3), (40, 90, 60), np.uint8)
        for n in names:
            p = parts.get(n)
            if not p:
                continue
            poly = p.get("inspection_polygon") or p.get("polygon")
            cx, cy = p["x"], p["y"]
            pts = np.int32([[(x - cx) * ppmm + size[0] / 2, size[1] / 2 - (y - cy) * ppmm] for x, y in poly])
            cv2.fillPoly(img, [pts], (60, 60, 60) if tag != "this board" else (30, 30, 120))
            cv2.polylines(img, [pts], True, (0, 190, 255), 2)
        cv2.putText(img, f"SIMULATED  {tag}", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(img)
    out = np.concatenate(tiles, axis=1)
    cv2.putText(out, title, (8, size[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def make(layout, protocol, out_dir, log=print):
    out_dir = Path(out_dir)
    parts = [p for p in layout["parts"] if p["side"] == "top" and p.get("fitted", True) and str(p.get("value", "")).upper() != "DNF"
             and p.get("inspection_polygon") is not None]
    ics = [p["name"] for p in parts if p["name"].startswith(("IC", "U"))]
    small = [p["name"] for p in parts if p["name"][0] in "CR"]
    plan = []
    if ics:
        plan.append(("BRIDGE", f"AOI-BRIDGE-{ics[0]}-1-2", [ics[0]], "review", .62, "bridge suspicion between pads 1 and 2"))
        plan.append(("POL", f"AOI-POL-{ics[-1]}", [ics[-1]], "review", .55, "pin 1 index not where the footprint puts it"))
    if small:
        plan.append(("LIFT", f"AOI-LIFT-{small[0]}", [small[0]], "review", .6, "one end looks raised"))
        plan.append(("ANOM", f"AOI-ANOM-{small[-1]}", [small[-1]], "review", .5, "appearance differs from the other boards"))
    boards = list(protocol["boards"])
    doc = {"schema": "polystage.aoi_result/0", "report_ref": protocol.get("report_ref", ""), "tool": "polybench-aoi SIMULATED",
           "generated": datetime.now().astimezone().isoformat(timespec="seconds"), "boards": []}
    for i, b in enumerate(boards):
        results = []
        for j, (kind, rid, names, status, conf, note) in enumerate(plan):
            if (i + j) % 3 != 0 and not (i == 0):
                continue
            path = out_dir / "evidence" / b / f"{rid}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), _sheet(layout, names, f"{b} {rid}"), [cv2.IMWRITE_JPEG_QUALITY, 85])
            results.append({"id": rid, "status": status, "value": f"SIMULATED {kind.lower()} candidate", "evidence": [str(path.resolve())],
                            "confidence": conf, "note": f"SIMULATED: {note}. No photo was analysed."})
        doc["boards"].append({"board": b, "photo": "", "results": results})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "aoi_result.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    log(f"no photos: wrote a SIMULATED result with {sum(len(b['results']) for b in doc['boards'])} candidates")
    return doc
