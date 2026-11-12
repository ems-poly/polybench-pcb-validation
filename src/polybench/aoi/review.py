"""The PoC review page served inside Polybench. Same page, same JSON API; decisions go to the Polybench journal
(operator as source) instead of a decisions file, and the page is loaded under /aoi/review/ in the centre pane."""
from __future__ import annotations

import json
import os
from pathlib import Path

from polybench.aoi import board_view as BV
from polybench.aoi import review_merge as M

PAGE = Path(__file__).with_name("review_page.html")


def page_html(base: str) -> str:
    return PAGE.read_text().replace("__TOKEN__", "pb").replace("__BASE__", base)


def state(engine, batch_dir: Path, base: str) -> dict:
    run = engine.state.aoi_current_run()
    if not run:
        return {"tool": "", "generated": "", "report_ref": "", "items": [], "cursor": 0,
                "progress": {"total": 0, "decided": 0, "undecided": 0, "confirmed": 0, "rejected": 0, "unsure": 0, "stale_keys": []},
                "decisions_file": "journal", "same_run": True, "boards": [], "boardview": False}
    result = Path(run["result"])
    document = json.loads(result.read_text())
    items = M.review_items(document)
    meta = {i["key"]: i.get("diff") for i in run.get("items", []) if i.get("diff")}
    for it in items:
        if it["key"] in meta:
            it["diff"] = meta[it["key"]]
    folder = Path(run["dir"])
    bv = BV.load(folder)
    order = [b["board"] for b in document["boards"]]
    for m in engine.state.aoi_manual.values():          # operator-found issues join the list after the candidates
        items.append({**{k: m[k] for k in ("key", "board", "id", "kind", "status", "value", "note", "confidence", "manual", "manual_kind", "designators", "box_mm")},
                      "evidence": [], "operator": m["operator"]})
    M.attach_peers(items)
    for item in items:
        if item.get("manual"):
            item["designator"] = (item["designators"] or [""])[0]
    decisions = {i['key']: d for i in items if (d := engine.state.aoi_decision_for(i['key'], run))}
    for item in items:
        item["tiles"] = [item["board"]] + [b for b in order if b != item["board"] and (not bv or b in bv["boards"])]    # boards with a photo: the sheet's tiles
        if item.get("manual"):
            marker = {"kind": item["kind"], "designator": item["designator"], "label": "operator-found " + item["manual_kind"] + (" at " + ", ".join(item["designators"]) if item["designators"] else ""),
                      "box": item["box_mm"]} if bv else None
        else:
            marker = BV.marker_for_id(item["id"], bv) if bv else None
        item["marker"] = marker if marker and bv and item["board"] in bv["boards"] else None
    _attach_region_peers(items)
    for item in items:
        d = decisions.get(item["key"], {})
        item["decision"], item["operator_note"] = d.get("decision"), d.get("note", "")
        item["decided_by"] = d.get("operator", "")
        item["issue_board"] = d.get("issue_board")
        item["reworked"] = item["key"] in engine.state.aoi_reworked
        item["tests"] = engine.aoi_links(item)
        item["image"] = _manual_sheet(item, folder, bv, batch_dir, base) if item.get("manual") else _url(item["evidence"], batch_dir, base)
        del item["evidence"]
    data = {"decisions": decisions, "cursor": engine_cursor(engine)}
    return {"tool": document["tool"], "generated": document["generated"], "report_ref": "", "items": items,
            "cursor": data["cursor"], "progress": M.progress(items, data), "decisions_file": "the Polybench journal",
            "same_run": True, "boards": order, "boardview": bool(bv)}


REGION_MARGIN = 1.0     # mm around the marker: about what the evidence crop shows


def _overlap(a, b, m=REGION_MARGIN):
    return a[0] - m < b[2] + m and b[0] - m < a[2] + m and a[1] - m < b[3] + m and b[1] - m < a[3] + m


def _attach_region_peers(items):
    """Extends `peers` (same designator on another board) with every other candidate or operator-found issue on another board whose
    marker lies in this crop's region, and gives each peer its id. A board with a peer gets a warning triangle on its preview."""
    for item in items:
        seen = {p["key"]: p for p in item["peers"]}
        for o in items:
            if o["board"] == item["board"] or o["key"] in seen:
                continue
            same = bool(item["designator"]) and o["designator"] == item["designator"]
            if same or (item["marker"] and o["marker"] and _overlap(item["marker"]["box"], o["marker"]["box"])):
                seen[o["key"]] = {"key": o["key"], "board": o["board"], "kind": o["kind"]}
        by_key = {o["key"]: o for o in items}
        item["peers"] = [{**p, "id": by_key[p["key"]]["id"], "designator": by_key[p["key"]]["designator"], "manual": bool(by_key[p["key"]].get("manual"))}
                         for p in seen.values()]


def _manual_sheet(item, run_folder, bv, batch_dir, base):
    """Evidence sheet of an operator-found issue: the crop on every board, written once beside the run."""
    if not bv:
        return None
    out = Path(run_folder) / "manual" / (item["id"] + ".jpg")
    if not out.is_file():
        import cv2
        sheet = BV.crop_sheet(run_folder, bv, item["box_mm"], item["tiles"], item["board"], f'{item["board"]} {item["id"]}  operator-found {item["manual_kind"]}')
        if sheet is None:
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return _url([str(out)], batch_dir, base)


_cursor = {"i": 0}


def engine_cursor(engine):
    return _cursor["i"]


def set_cursor(i):
    _cursor["i"] = max(0, int(i))


def _url(evidence, batch_dir: Path, base: str):
    for e in evidence:
        try:
            rel = Path(e).resolve().relative_to(Path(batch_dir).resolve())
        except ValueError:
            continue
        return base + "/file/" + str(rel).replace(os.sep, "/")
    return None
