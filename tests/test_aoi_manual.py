"""Operator-found issues (aoi_manual_issue), peers in the review state, the labels file and the board-view image route. Host only."""
import json
from pathlib import Path

import pytest
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from fastapi.testclient import TestClient
from test_aoi_polybench import eng, item, proto_dir, run_with  # noqa: F401  (fixtures)

from polybench.aoi import board_view as BV
from polybench.aoi import ls_export, train_labels
from polybench.engine import CmdError, Engine
from polybench.errata import collect, to_markdown as errata_md
from polybench.report import build_model, to_markdown
from polybench.store import Journal

PART = lambda x0, y0, x1, y1: {"outline": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "box": [x0, y0, x1, y1],
                               "pads": [[x0, y0, x0 + 0.4, y1], [x1 - 0.4, y0, x1, y1]]}


def run_folder(tmp_path, boards=("S01", "S02", "S03")):
    """A run folder with a board view (30 x 20 mm at 10 px/mm), as board_view.write leaves it."""
    d = tmp_path / "runs" / "0001"
    (d / BV.DIR).mkdir(parents=True)
    rng = np.random.default_rng(1)
    for b in boards:
        cv2.imwrite(str(d / BV.DIR / f"{b}.jpg"), rng.integers(60, 255, (200, 300, 3), dtype=np.uint8))
    bv = {"schema": BV.SCHEMA, "box_mm": [0, 0, 30, 20], "ppmm": 10, "size_px": [300, 200], "boards": {b: f"{BV.DIR}/{b}.jpg" for b in boards},
          "parts": {"IC1": PART(8, 8, 14, 14), "C1": PART(2, 2, 3.5, 3), "C2": PART(20, 2, 21.5, 3)}}
    (d / BV.DIR / BV.JSON_NAME).write_text(json.dumps(bv))
    doc = {"schema": "polystage.aoi_result/0", "report_ref": "", "generated": "2026-10-16T10:00:00+01:00", "tool": "t",
           "boards": [{"board": b, "photo": "p", "results": [{"id": "AOI-BRIDGE-IC1-1-2", "status": "review", "value": "v", "confidence": .6, "note": "n", "evidence": []}]}
                      for b in boards]}
    (d / "aoi_result.json").write_text(json.dumps(doc))
    return d


def start_run(eng, d):
    items = [dict(item(b, "AOI-BRIDGE-IC1-1-2", "BRIDGE"), evidence=[]) for b in ("S01", "S02", "S03")]
    eng.aoi_run_done("S01", {"run": 1, "result": str(d / "aoi_result.json"), "dir": str(d), "items": items, "boards": [],
                             "photos": [{"board": b, "side": "top", "name": f"{b}.jpg"} for b in ("S01", "S02", "S03")], "counts": {}})


def test_manual_issue_is_confirmed_fails_the_visual_test_and_is_journalled(eng, tmp_path):
    start_run(eng, run_folder(tmp_path))
    r = eng.aoi_manual_issue("S03", [9, 9, 11, 11], "bridge", ["IC1"], "seen under the loupe")
    assert r["failed"] == ["V1"]
    key = r["key"]
    c = eng.state.cell("S03", "V1")
    assert (c["status"], c["verdict"], c["source"]["kind"], c["source"]["operator"]) == ("accepted", "fail", "aoi_review", "pat")
    assert eng.state.aoi_decisions[key]["decision"] == "confirmed" and eng.state.aoi_decision_for(key)["decision"] == "confirmed"
    ev = [e for e in eng.journal.events if e["type"] == "aoi_manual_issue"][0]
    p = ev["payload"]
    assert ev["operator"] == "pat" and p["board"] == "S03" and p["box_mm"] == [9.0, 9.0, 11.0, 11.0]
    assert (p["kind"], p["designators"], p["note"]) == ("bridge", ["IC1"], "seen under the loupe")
    snap = eng.state.snapshot()["aoi"]
    assert snap["counts"] == {"total": 4, "decided": 1, "confirmed": 1, "rejected": 0, "unsure": 0} and snap["manual"] == 1
    assert snap["per_board"]["S03"]["total"] == 2
    assert [i for i in snap["items"] if i["manual"]][0]["designator"] == "IC1"


def test_manual_issue_survives_replay_and_a_new_run(eng, tmp_path):
    d = run_folder(tmp_path)
    start_run(eng, d)
    key = eng.aoi_manual_issue("S03", [9, 9, 11, 11], "lifted", ["IC1"], "")["key"]
    eng.journal.close()
    e2 = Engine(eng.proto, Journal(tmp_path / "j.jsonl"), "pat")
    assert key in e2.state.aoi_manual and e2.state.cell("S03", "V2")["verdict"] == "fail"       # lifted -> the flat and level test
    e2.aoi_run_done("S01", {"run": 2, "result": "/x/r2.json", "dir": str(d), "items": [], "boards": [], "photos": [], "counts": {}})
    assert e2.state.aoi_decision_for(key)["decision"] == "confirmed"                           # belongs to the board, not to a run


def test_clearing_the_decision_removes_the_fail_but_keeps_the_issue(eng, tmp_path):
    start_run(eng, run_folder(tmp_path))
    key = eng.aoi_manual_issue("S03", [9, 9, 11, 11], "bridge", ["IC1"])["key"]
    eng.aoi_decide(key, None)
    assert eng.state.cell("S03", "V1")["status"] == "empty" and key in eng.state.aoi_manual
    assert eng.state.snapshot()["aoi"]["counts"]["decided"] == 0


def test_manual_issue_is_validated(eng, tmp_path):
    with pytest.raises(CmdError, match="no AOI run"):
        eng.aoi_manual_issue("S03", [1, 1, 2, 2], "bridge", [])
    start_run(eng, run_folder(tmp_path))
    for args, msg in [(("S09", [1, 1, 2, 2], "bridge", []), "unknown board"), (("S03", [1, 1, 2, 2], "scratch", []), "kind"),
                      (("S03", [2, 2, 1, 1], "bridge", []), "no area"), (("S03", [1, 1, "x", 2], "bridge", []), "four numbers"),
                      (("S03", [40, 40, 50, 50], "bridge", []), "outside"), (("S03", [1, 1, 2, 2], "bridge", ["ZZ9"]), "not a part")]:
        with pytest.raises(CmdError, match=msg):
            eng.aoi_manual_issue(*args)
    assert not eng.state.aoi_manual


def test_labels_hold_every_decision_and_the_miss_and_feed_ls_export(eng, tmp_path):
    d = run_folder(tmp_path)
    start_run(eng, d)
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "confirmed", "yes")
    eng.aoi_decide("S02|AOI-BRIDGE-IC1-1-2", "rejected")
    eng.aoi_decide("S03|AOI-BRIDGE-IC1-1-2", "unsure")
    eng.aoi_manual_issue("S03", [20, 2, 21.5, 3], "damage", ["C2"], "scratched pad")
    path = tmp_path / "aoi" / "labels.jsonl"
    assert path.is_file()
    tasks = [json.loads(line) for line in path.read_text().splitlines()]
    assert [t["data"]["board"] for t in tasks] == ["S01", "S02", "S03"]
    dec = {x["key"]: x for t in tasks for x in t["decisions"]}
    assert {k: v["decision"] for k, v in dec.items() if not v["aoi_missed"]} == {"S01|AOI-BRIDGE-IC1-1-2": "confirmed", "S02|AOI-BRIDGE-IC1-1-2": "rejected", "S03|AOI-BRIDGE-IC1-1-2": "unsure"}
    miss = [v for v in dec.values() if v["aoi_missed"]]
    assert len(miss) == 1 and miss[0]["source"] == "operator" and miss[0]["class"] == "damage" and miss[0]["decision"] == "confirmed" and miss[0]["note"] == "scratched pad"
    # the shape ls_export reads: confirmed candidates and the operator box come back as labels in board mm; rejected and unsure do not
    exp = ls_export.export(tasks)
    cls = sorted((lab["board"], lab["class"]) for lab in exp["labels"])
    assert cls == [("S01", "bridge"), ("S03", "damage")]
    box = next(lab["box_mm"] for lab in exp["labels"] if lab["class"] == "damage")
    assert box == pytest.approx([20, 2, 21.5, 3], abs=0.15)
    assert any("scratched pad" in lab["note"] for lab in exp["labels"])
    assert exp["boards"]["S03"]["reviewed"] is True and exp["boards"]["S01"]["reviewed"] is True
    # AOI candidates the operator did not reject are Label Studio pre-annotations (as ls_project builds them)
    pre = {t["data"]["board"]: [r for r in t["predictions"][0]["result"] if r["type"] == "rectanglelabels"] for t in tasks}
    assert len(pre["S01"]) == 1 and len(pre["S03"]) == 1 and pre["S02"] == []
    assert not list((tmp_path / "aoi").glob(".labels*"))                                       # atomic write leaves no temp file


def test_errata_and_report_carry_the_aoi_miss(eng, tmp_path):
    start_run(eng, run_folder(tmp_path))
    eng.aoi_manual_issue("S03", [9, 9, 11, 11], "bridge", ["IC1"], "between pins 1 and 2")
    rows = collect(eng.state)
    row = next(r for r in rows if r["kind"] == "aoi")
    assert row["designators"] == ["IC1"] and "AOI missed" in row["observation"] and "between pins 1 and 2" in row["observation"]
    assert "of which 1 AOI missed" in errata_md(eng.state, rows, "B", "h" * 16, "now")
    md = to_markdown(build_model(eng.state, "h"))
    assert "AOI MISSED, bridge" in md and "Operator-found (AOI missed)" in md


# ---- the review page state and routes

@pytest.fixture
def served(eng, tmp_path, proto_dir):
    from polybench.server import Session, create_app
    from polybench.protocol import load
    sess = Session(load(proto_dir), tmp_path / "data", "B", False, None, None, "pat")
    d = run_folder(sess.dir)
    start_run(sess.engine, d)
    with TestClient(create_app(sess)) as c:
        yield sess, c, d


def test_board_view_image_is_served_from_the_run_folder(served):
    """Regression: the viewer asked /file/boardview/S01.jpg, which resolves in the batch folder; the image lives in the run folder."""
    sess, c, d = served
    r = c.get("/aoi/review/bvimg/S01")
    assert r.status_code == 200 and r.content == (d / "boardview" / "S01.jpg").read_bytes()
    assert c.get("/aoi/review/file/boardview/S01.jpg").status_code == 404          # the old URL: not found
    assert c.get("/aoi/review/bvimg/S09").status_code == 404
    assert c.get("/aoi/review/bvimg/..%2Fx").status_code in (404, 422)
    assert c.get("/aoi/review/api/boardview").json()["run"] == 1


def test_manual_route_and_review_state_with_peers(served):
    sess, c, d = served
    r = c.post("/aoi/review/api/manual", json={"board": "S02", "box_mm": [9, 9, 11, 11], "kind": "polarity", "designators": ["IC1"], "note": "n"})
    assert r.status_code == 200 and r.json()["key"].startswith("S02|MANUAL-")
    assert c.post("/aoi/review/api/manual", json={"board": "S02", "box_mm": [9, 9, 9, 11], "kind": "bridge"}).status_code == 422
    st = c.get("/aoi/review/api/state").json()
    man = [i for i in st["items"] if i.get("manual")]
    assert len(man) == 1 and man[0]["decision"] == "confirmed" and man[0]["board"] == "S02" and man[0]["image"]
    assert c.get(man[0]["image"]).status_code == 200                                    # the crop sheet of the operator-found issue
    assert st["progress"]["total"] == 4 and st["progress"]["confirmed"] == 1
    s01 = next(i for i in st["items"] if i["key"] == "S01|AOI-BRIDGE-IC1-1-2")
    peer = {p["key"]: p for p in s01["peers"]}
    assert man[0]["key"] in peer and peer[man[0]["key"]]["manual"] is True and peer[man[0]["key"]]["id"].startswith("MANUAL-")   # S01 sees the S02 issue
    assert {p["board"] for p in s01["peers"]} == {"S02", "S03"}
    assert (sess.dir / "aoi" / "labels.jsonl").is_file()
    exp = c.post("/aoi/review/api/export").json()
    assert exp["summary"]["aoi_missed"] == 1 and exp["paths"]["labels"].endswith("labels.jsonl")


def test_peer_far_from_a_candidate_is_not_flagged(served):
    sess, c, d = served
    c.post("/aoi/review/api/manual", json={"board": "S02", "box_mm": [25, 15, 27, 17], "kind": "other", "designators": []})
    st = c.get("/aoi/review/api/state").json()
    s01 = next(i for i in st["items"] if i["key"] == "S01|AOI-BRIDGE-IC1-1-2")
    assert not any(p["key"].startswith("S02|MANUAL-") for p in s01["peers"])        # 10 mm away: not in this crop's region
