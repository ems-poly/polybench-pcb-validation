"""Polybench side of the AOI port: journal events, decisions to results, photo routes, report, derivation. Host only."""
import json
from pathlib import Path

import pytest

from polybench import extras

needs_aoi = pytest.mark.skipif(not extras.check("aoi"), reason="needs the aoi extra (opencv, pillow, numpy)")
from fastapi.testclient import TestClient

from polybench.aoi import link
from polybench.engine import CmdError, Engine
from polybench.protocol import load
from polybench.store import Journal

PROTO = """\
schema: pcbv.protocol/2
design: {name: T-1, revision: "1"}
batch: {default_boards: 3, label_prefix: S}
include: [aoi/aoi.yaml]
steps:
  - id: visual
    title: Visual
    sections:
      - id: ic
        title: ICs
        tests:
          - {id: V1, title: "IC1 no solder bridges between the pins", kind: yn, highlight: [IC1]}
          - {id: V2, title: "IC1 sits flat and level", kind: yn, highlight: [IC1]}
          - {id: V3, title: "Left zone complete (C1, C2 none missing)", kind: yn, highlight: [C1, C2]}
          - {id: V4, title: "Whole board clean", kind: yn}
bom:
  - {line: 1, designators: [IC1], mpn: X, package: QFN, dnf: false, side: top, barcode: [], polarity: none, order: 1}
"""


def part(n, x, y, w=1.0, h=0.6):
    poly = [[x - w / 2, y - h / 2], [x + w / 2, y - h / 2], [x + w / 2, y + h / 2], [x - w / 2, y + h / 2]]
    return {"name": n, "value": "1u", "x": x, "y": y, "angle": 0, "side": "top", "fitted": True, "package": "P", "polygon": poly,
            "inspection_polygon": poly}


@pytest.fixture
def proto_dir(tmp_path):
    d = tmp_path / "proto"
    (d / "aoi").mkdir(parents=True)
    (d / "t.pcbv.yaml").write_text(PROTO, encoding="utf-8")
    (d / "aoi" / "aoi.yaml").write_text("aoi:\n  protocol: aoi_protocol.json\n  layout: layout.json\n", encoding="utf-8")
    layout = {"board": "T-1", "unit": "mm", "parts": [part("IC1", 5, 5, 3, 3), part("C1", 2, 2), part("C2", 9, 2)], "holes": [], "outline": []}
    (d / "aoi" / "layout.json").write_text(json.dumps(layout), encoding="utf-8")
    (d / "aoi" / "aoi_protocol.json").write_text(json.dumps({
        "schema": "polystage.aoi_protocol/0", "protocol": "t", "report_ref": "", "layout": {"design": "T-1", "cam_zip": "x"},
        "boards": {"S01": {}, "S02": {}, "S03": {}}, "classes": {"large": {"ceiling": "review", "designators": []},
        "medium": {"ceiling": "review", "designators": []}, "small": {"ceiling": "review", "designators": "*"}},
        "thresholds": {"present_max_exposure": .15, "absent_min_exposure": .6},
        "rows": [{"id": "V1", "check": "joint_review", "ceiling": "review", "designators": ["IC1"], "requires": ["top"], "limits": "x"}]}), encoding="utf-8")
    return d / "t.pcbv.yaml"


@pytest.fixture
def eng(tmp_path, proto_dir):
    e = Engine(load(proto_dir), Journal(tmp_path / "j.jsonl"), "pat")
    e.create_batch("t", ["S01", "S02", "S03"], "S", True, "T-1")
    return e


def run_with(eng, items):
    eng.aoi_run_done("S01", {"run": 1, "result": "/x/aoi_result.json", "dir": "/x", "items": items, "boards": [], "photos": [], "counts": {}})


def item(board, rid, kind, status="review"):
    return {"key": f"{board}|{rid}", "board": board, "id": rid, "kind": kind, "status": status, "value": "v", "note": "n", "confidence": .6,
            "evidence": ["/x/e.jpg"]}


def test_filename_to_board_and_side():
    labels = ["S01", "S02", "S03", "S10"]
    assert link.board_from_name("IMG_5990-B3.HEIC", labels) == "S03"
    assert link.board_from_name("x_S10_top.jpg", labels) == "S10"
    assert link.board_from_name("IMG_1.jpg", labels) is None
    assert link.board_from_name("B1.jpg", ["B1", "B2"], "B") == "B1"
    assert link.side_from_name("S03_bottom.jpg") == "bottom" and link.side_from_name("S03.jpg") == "top"


def test_candidate_designators():
    assert link.designators_of("AOI-LIFT-C4") == ["C4"]
    assert link.designators_of("AOI-BRIDGE-IC4-6-7") == ["IC4"]
    assert link.designators_of("AOI-BRIDGE-C1.2-C2.1") == ["C1", "C2"]
    assert link.designators_of("AOI-POL-SYS-D3") == ["D3"]


def test_candidates_map_to_the_visual_tests_by_designator_and_kind(eng):
    steps = eng.proto.data["steps"]
    assert link.tests_for(steps, "BRIDGE", ["IC1"]) == ["V1"]
    assert link.tests_for(steps, "LIFT", ["IC1"]) == ["V2"]
    assert link.tests_for(steps, "MISSING", ["C1"]) == ["V3"]
    assert link.tests_for(steps, "ANOM", ["IC1"]) == ["V1", "V2"]      # no kind words: every test that names the part
    assert link.tests_for(steps, "BRIDGE", ["U9"]) == []


def test_aoi_section_is_injected_into_the_visual_step(eng):
    sec = eng.proto.step("visual")["sections"][0]
    assert sec["id"] == "aoi" and [a["plugin"] for a in sec["actions"]] == ["aoi.run", "aoi.ls_export"]
    assert eng.state.snapshot()["aoi"]["counts"]["total"] == 0


def test_confirmed_candidate_fails_the_test_and_clearing_removes_it(eng):
    run_with(eng, [item("S01", "AOI-BRIDGE-IC1-1-2", "BRIDGE")])
    r = eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "confirmed", "seen")
    assert r["failed"] == ["V1"]
    c = eng.state.cell("S01", "V1")
    assert (c["status"], c["verdict"], c["source"]["kind"], c["source"]["operator"]) == ("accepted", "fail", "aoi_review", "pat")
    assert json.loads(c["raw"])["value"] == "v"                           # the AOI value is kept beside the decision
    assert eng.state.gate(eng.proto.step("visual"), "S01")["ok"]          # visual has no gate itself
    assert eng.state.step_board(eng.proto.step("visual"), "S01")["fails"] == 1
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "unsure")                     # unsure: nothing stays recorded on the test
    assert eng.state.cell("S01", "V1")["status"] == "empty"


def test_two_confirmed_issues_on_one_test_keep_it_failed_until_both_clear(eng):
    run_with(eng, [item("S01", "AOI-BRIDGE-IC1-1-2", "BRIDGE"), item("S01", "AOI-BRIDGE-IC1-2-3", "BRIDGE")])
    for k in ("S01|AOI-BRIDGE-IC1-1-2", "S01|AOI-BRIDGE-IC1-2-3"):
        eng.aoi_decide(k, "confirmed")
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "rejected")
    assert eng.state.cell("S01", "V1")["verdict"] == "fail"
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-2-3", "rejected")
    c = eng.state.cell("S01", "V1")
    assert c["status"] == "empty" and [p["value"] for p in c["proposals"].values()] == ["yes"]   # a PASS proposal, not a PASS


def test_dismissed_candidates_propose_pass_only_for_tests_they_touch_and_never_accept(eng):
    run_with(eng, [item("S01", "AOI-MISSING-C1", "MISSING"), item("S02", "AOI-MISSING-C1", "MISSING")])
    eng.aoi_decide("S01|AOI-MISSING-C1", "rejected")
    assert [t for t in ("V1", "V2", "V3", "V4") if eng.state.cell("S01", t)["proposals"]] == ["V3"]
    assert eng.state.cell("S01", "V3")["status"] == "empty"
    assert not eng.state.cell("S02", "V3")["proposals"]


def test_polarity_and_lift_are_never_proposed_as_a_pass(eng):
    run_with(eng, [item("S01", "AOI-POL-IC1", "POL"), item("S01", "AOI-LIFT-IC1", "LIFT")])
    eng.aoi_decide("S01|AOI-POL-IC1", "rejected")
    eng.aoi_decide("S01|AOI-LIFT-IC1", "rejected")
    assert not any(eng.state.cell("S01", t)["proposals"] for t in ("V1", "V2"))


def test_a_rerun_starts_undecided_unless_the_evidence_is_identical(eng):
    a, b = item("S01", "AOI-BRIDGE-IC1-1-2", "BRIDGE"), item("S01", "AOI-LIFT-C1", "LIFT")
    a["evidence_sha"], b["evidence_sha"] = "same", "old"
    run_with(eng, [a, b])
    eng.aoi_decide(a["key"], "confirmed")
    eng.aoi_decide(b["key"], "rejected")
    a2, b2 = dict(a, evidence_sha="same"), dict(b, evidence_sha="new")
    eng.aoi_run_done("S01", {"run": 2, "result": "/x/r2.json", "dir": "/x", "items": [a2, b2], "boards": [], "photos": [], "counts": {}})
    snap = eng.state.snapshot()["aoi"]
    assert snap["counts"] == {"total": 2, "decided": 1, "confirmed": 1, "rejected": 0, "unsure": 0}
    assert eng.state.aoi_decision_for(b["key"]) is None and eng.state.aoi_decision_for(a["key"])["decision"] == "confirmed"


def test_lift_is_review_only_and_polarity_says_look_by_eye(eng):
    from polybench.aoi import runner
    items = runner.harden([dict(item("S01", "AOI-LIFT-C1", "LIFT"), status="fail"), item("S01", "AOI-POL-IC1", "POL")])
    assert items[0]["status"] == "review" and "review only" in items[0]["note"]
    assert "look at the part by eye" in items[1]["note"]


@needs_aoi
def test_a_non_red_mask_photo_is_warned_about(tmp_path):
    import cv2, numpy as np
    from polybench.aoi import runner
    green = np.zeros((400, 400, 3), np.uint8)
    green[..., 1] = 140
    red = np.zeros((400, 400, 3), np.uint8)
    red[..., 2] = 170
    cv2.imwrite(str(tmp_path / "g.jpg"), green)
    cv2.imwrite(str(tmp_path / "r.jpg"), red)
    assert runner.mask_colour_check(tmp_path / "g.jpg")["ok"] is False
    assert runner.mask_colour_check(tmp_path / "r.jpg")["ok"] is True


def test_unknown_candidate_and_bad_decision_are_refused(eng):
    run_with(eng, [item("S01", "AOI-LIFT-C1", "LIFT")])
    with pytest.raises(CmdError):
        eng.aoi_decide("S01|nope", "confirmed")
    with pytest.raises(CmdError):
        eng.aoi_decide("S01|AOI-LIFT-C1", "maybe")


def test_journal_replay_restores_photos_runs_and_decisions(tmp_path, eng):
    eng.aoi_add_photo("S01", "top", "a-S01.jpg", "aoi/photos/x_a-S01.jpg", "abc", 3, "filename")
    eng.aoi_add_photo(None, "top", "b.jpg", "aoi/photos/y_b.jpg", "def", 3, "unassigned")
    eng.aoi_drop("aoi/photos/y_b.jpg")
    run_with(eng, [item("S01", "AOI-BRIDGE-IC1-1-2", "BRIDGE")])
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "confirmed")
    eng.journal.close()
    e2 = Engine(eng.proto, Journal(tmp_path / "j.jsonl"), "pat")
    assert list(e2.state.aoi_photos) == ["aoi/photos/x_a-S01.jpg"]
    assert e2.state.aoi_decisions["S01|AOI-BRIDGE-IC1-1-2"]["decision"] == "confirmed"
    assert e2.state.cell("S01", "V1")["verdict"] == "fail"
    assert e2.state.snapshot()["aoi"]["counts"]["confirmed"] == 1


def test_a_journal_without_aoi_events_replays_unchanged(engine):
    snap = engine.state.snapshot()
    assert snap["aoi"] is None and engine.state.aoi_photos == {}


def make_client(tmp_path, proto_dir, simulate=True):
    from polybench.server import Session, create_app
    sess = Session(load(proto_dir), tmp_path / "data", "B", simulate, None, None, "pat")
    return sess, TestClient(create_app(sess))


@needs_aoi
def test_upload_assign_drop_and_review_routes(tmp_path, proto_dir):
    sess, c = make_client(tmp_path, proto_dir)
    r = c.post("/api/aoi/photos?name=IMG_1-S02.jpg", content=b"jpgbytes").json()
    assert r["board"] == "S02" and r["side"] == "top"
    assert c.post("/api/aoi/photos?name=IMG_1-S02.jpg", content=b"jpgbytes").json()["duplicate"]       # same bytes: not stored twice
    r = c.post("/api/aoi/photos?name=mystery.png", content=b"other").json()
    assert r["board"] is None
    assert c.post("/api/aoi/photos?name=notes.txt", content=b"x").status_code == 422
    assert c.post("/api/aoi/photos?name=z.jpg&board=S03&side=bottom", content=b"zz").json()["board"] == "S03"
    state = c.get("/api/state").json()["aoi"]
    assert sorted((p["board"], p["side"]) for p in state["photos"] if p["board"]) == [("S02", "top"), ("S03", "bottom")]
    f = next(p["file"] for p in state["photos"] if p["board"] is None)
    assert c.post("/api/aoi/assign", json={"file": f, "board": "S01"}).status_code == 200
    assert c.post("/api/aoi/drop", json={"file": f}).status_code == 200
    assert all(p["file"] != f for p in c.get("/api/state").json()["aoi"]["photos"])
    assert "__BASE__" not in c.get("/aoi/review/").text and c.get("/aoi/review/api/state").json()["items"] == []
    assert c.get("/aoi/photo/../../journal.jsonl").status_code in (403, 404)


@needs_aoi
def test_simulated_run_without_photos_gives_a_labelled_result_and_the_review_flow_works(tmp_path, proto_dir):
    from polybench.aoi import runner
    cfg = load(proto_dir).data["aoi"]
    (tmp_path / "journal.jsonl").write_text("", encoding="utf-8")
    out = runner.run(tmp_path, tmp_path / "run", cfg, simulate=True)
    assert out["synthetic"] and out["items"] and all("SIMULATED" in i["value"] for i in out["items"])
    with pytest.raises(RuntimeError, match="no photos"):
        runner.run(tmp_path, tmp_path / "run2", cfg, simulate=False)


def test_report_has_the_aoi_section(eng):
    from polybench.report import build_model, to_markdown
    run_with(eng, [item("S01", "AOI-BRIDGE-IC1-1-2", "BRIDGE"), item("S02", "AOI-LIFT-C1", "LIFT")])
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "confirmed", "seen")
    md = to_markdown(build_model(eng.state, "h"))
    assert "Automated optical inspection (AOI)" in md and "confirmed by pat" in md and "S02 AOI AOI-LIFT-C1: not reviewed" in md


def _journal_photo_events(sess):
    return [json.loads(l) for l in (sess.dir / "journal.jsonl").read_text(encoding="utf-8").splitlines() if '"aoi_photo' in l]


@needs_aoi
def test_upload_with_an_explicit_board_wins_over_the_file_name_and_is_journalled_as_a_tile(tmp_path, proto_dir):
    sess, c = make_client(tmp_path, proto_dir)
    r = c.post("/api/aoi/photos?name=IMG_1-S02.jpg&board=S03", content=b"one").json()      # name says S02, the tile says S03
    assert r["board"] == "S03" and r["assigned_by"] == "tile"
    r2 = c.post("/api/aoi/photos?name=mystery.png&board=S01", content=b"two").json()         # no board in the name: the tile still assigns
    assert r2["board"] == "S01"
    r3 = c.post("/api/aoi/photos?name=IMG_9-S02.jpg", content=b"three").json()               # general area: by file name
    assert r3["board"] == "S02" and r3["assigned_by"] == "filename"
    r4 = c.post("/api/aoi/photos?name=mystery2.png", content=b"four").json()
    assert r4["board"] is None and r4["assigned_by"] == "unassigned"
    assert c.post("/api/aoi/photos?name=a.jpg&board=S99", content=b"x").status_code == 422
    ev = [e["payload"] for e in _journal_photo_events(sess) if e["type"] == "aoi_photo"]
    assert [(p["board"], p["assigned_by"]) for p in ev] == [("S03", "tile"), ("S01", "tile"), ("S02", "filename"), (None, "unassigned")]
    assert all("source" in p and "sha256" in p for p in ev)                                    # existing fields stay
    assert (sess.dir / r["file"]).read_bytes() == b"one"


@needs_aoi
def test_a_select_change_is_journalled_as_select(tmp_path, proto_dir):
    sess, c = make_client(tmp_path, proto_dir)
    f = c.post("/api/aoi/photos?name=m.png", content=b"m").json()["file"]
    assert c.post("/api/aoi/assign", json={"file": f, "board": "S01"}).status_code == 200
    last = [e["payload"] for e in _journal_photo_events(sess) if e["type"] == "aoi_photo"][-1]
    assert last["board"] == "S01" and last["assigned_by"] == "select"


@needs_aoi
def test_reassigning_a_photo_moves_it_journals_a_move_and_keeps_the_file(tmp_path, proto_dir):
    sess, c = make_client(tmp_path, proto_dir)
    r = c.post("/api/aoi/photos?name=a.jpg&board=S01", content=b"aaa").json()
    f = r["file"]
    m = c.post("/api/aoi/move", json={"file": f, "board": "S02"}).json()
    assert m == {"ok": True, "moved": True, "from": "S01", "to": "S02"}
    assert c.post("/api/aoi/move", json={"file": f, "board": "S02"}).json()["moved"] is False       # same board: nothing journalled
    assert c.post("/api/aoi/move", json={"file": f, "board": "S99"}).status_code in (404, 422)
    assert c.post("/api/aoi/move", json={"file": "aoi/photos/nope.jpg", "board": "S01"}).status_code == 404
    ph = [p for p in c.get("/api/state").json()["aoi"]["photos"] if p["file"] == f]
    assert len(ph) == 1 and ph[0]["board"] == "S02"
    assert (sess.dir / f).read_bytes() == b"aaa"                                                     # never deleted
    mv = [e["payload"] for e in _journal_photo_events(sess) if e["type"] == "aoi_photo_move"]
    assert mv == [{"file": f, "from": "S01", "to": "S02", "name": "a.jpg", "assigned_by": "tile"}]
    # the same bytes dropped on another tile reassigns instead of duplicating
    d = c.post("/api/aoi/photos?name=a.jpg&board=S03", content=b"aaa").json()
    assert d["duplicate"] and d["board"] == "S03" and d["moved_from"] == "S02"
    from polybench.aoi.runner import photos_from_journal
    assert photos_from_journal(sess.dir)[f]["board"] == "S03"                                        # the run's own replay agrees


def test_journal_replay_applies_moves_and_old_journals_without_assigned_by_still_replay(tmp_path, eng):
    eng.aoi_add_photo("S01", "top", "a.jpg", "aoi/photos/x_a.jpg", "abc", 3, "filename")            # old call shape: no assigned_by
    eng.aoi_move("aoi/photos/x_a.jpg", "S03")
    eng.journal.close()
    e2 = Engine(eng.proto, Journal(tmp_path / "j.jsonl"), "pat")
    assert e2.state.aoi_photos["aoi/photos/x_a.jpg"]["board"] == "S03"
    # a journal line from before this change (no assigned_by) replays as it was
    jp = tmp_path / "j.jsonl"
    lines = [json.loads(l) for l in jp.read_text(encoding="utf-8").splitlines()]
    for e in lines:
        if e["type"] == "aoi_photo":
            e["payload"].pop("assigned_by")
    jp.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    e3 = Engine(eng.proto, Journal(jp), "pat")
    assert e3.state.aoi_photos["aoi/photos/x_a.jpg"]["board"] == "S03" and "assigned_by" not in e3.state.aoi_photos["aoi/photos/x_a.jpg"]


@needs_aoi
def test_remove_moves_the_file_to_removed_journals_it_and_the_run_replay_skips_it(tmp_path, proto_dir):
    sess, c = make_client(tmp_path, proto_dir)
    f = c.post("/api/aoi/photos?name=a.jpg&board=S01", content=b"aaa").json()["file"]
    g = c.post("/api/aoi/photos?name=b.jpg&board=S01", content=b"bbb").json()["file"]
    assert c.post("/api/aoi/remove", json={"file": "aoi/photos/nope.jpg"}).status_code == 404
    r = c.post("/api/aoi/remove", json={"file": f, "reason": " blurred "})
    assert r.status_code == 200
    assert not (sess.dir / f).exists()
    kept = list((sess.dir / "aoi" / "removed").iterdir())
    assert len(kept) == 1 and kept[0].read_bytes() == b"aaa"                                         # moved, never deleted
    ev = [e["payload"] for e in _journal_photo_events(sess) if e["type"] == "aoi_photo_removed"]
    assert ev == [{"file": f, "name": "a.jpg", "board": "S01", "side": "top", "sha256": ev[0]["sha256"], "reason": "blurred",
                   "moved_to": "aoi/removed/" + kept[0].name}]
    assert [p["file"] for p in c.get("/api/state").json()["aoi"]["photos"]] == [g]
    from polybench.aoi.runner import photos_from_journal
    assert list(photos_from_journal(sess.dir)) == [g]                                                 # the next run does not see it
    assert c.post("/api/aoi/remove", json={"file": f}).status_code == 404                           # already removed
    # the same bytes uploaded again are a new photo (the old copy is in removed/), and a second removal of a same-named file does not overwrite
    f2 = c.post("/api/aoi/photos?name=a.jpg&board=S02", content=b"aaa").json()["file"]
    assert f2 == f and c.post("/api/aoi/remove", json={"file": f2}).status_code == 200
    assert len(list((sess.dir / "aoi" / "removed").iterdir())) == 2


@needs_aoi
def test_used_names_the_sharpest_photo_per_board_and_side(tmp_path, proto_dir):
    import cv2
    import numpy as np
    sess, c = make_client(tmp_path, proto_dir)

    def jpg(sharp):
        im = np.full((300, 400, 3), (30, 20, 150), np.uint8)
        if sharp:
            im[::2, ::2] = 255
        return cv2.imencode(".jpg", im)[1].tobytes()
    flat = c.post("/api/aoi/photos?name=flat.jpg&board=S01", content=jpg(False)).json()["file"]
    sharp = c.post("/api/aoi/photos?name=sharp.jpg&board=S01", content=jpg(True)).json()["file"]
    only = c.post("/api/aoi/photos?name=only.jpg&board=S02", content=jpg(True)[:-2] + b"\x00\x00").json()["file"]
    used = c.get("/api/aoi/used").json()["used"]
    assert sharp in used and flat not in used and only in used


def test_diff_mode_decision_names_the_board_gate_follows_it_and_report_says_experimental(eng):
    """Diff mode (experimental): Y needs issue_board; the open issue blocks the named board; journal fields; report label."""
    from polybench.report import build_model, to_markdown
    it = {**item("S02", "AOI-DIFF-1", "DIFF"), "diff": {"mode": "diff", "reference": "S01", "boards": ["S02", "S01"], "box_ref_px": [1, 2, 3, 4], "suggestion": None}}
    eng.aoi_add_photo("S01", "top", "a.jpg", "aoi/photos/a.jpg", "a", 1, "chosen")
    eng.aoi_add_photo("S02", "top", "b.jpg", "aoi/photos/b.jpg", "b", 1, "chosen")
    eng.aoi_run_done("S01", {"run": 1, "result": "/x/aoi_result.json", "dir": "/x", "items": [it], "boards": ["S01", "S02"], "photos": [], "counts": {},
                             "mode": "diff", "reference": "S01"})
    with pytest.raises(CmdError, match="which board"):
        eng.aoi_decide("S02|AOI-DIFF-1", "confirmed", "")
    with pytest.raises(CmdError, match="which board"):
        eng.aoi_decide("S02|AOI-DIFF-1", "confirmed", "", "S03")
    eng.aoi_decide("S02|AOI-DIFF-1", "confirmed", "", "S01")                      # the reference is the board with the issue
    assert eng.state.aoi_board_status("S01")["open_confirmed"] == 1 and eng.state.aoi_board_status("S02")["open_confirmed"] == 0
    ev = [json.loads(l) for l in Path(eng.journal.path).read_text(encoding="utf-8").splitlines()]
    d = next(e for e in ev if e["type"] == "aoi_decision")["payload"]
    assert d["mode"] == "diff" and d["issue_board"] == "S01"
    assert any(e["type"] == "aoi_run" and e["payload"].get("mode") == "diff" for e in ev)
    md = to_markdown(build_model(eng.state, "h"))
    assert "EXPERIMENTAL" in md and "differs from the reference board S01" in md
