"""The Visual step is the AOI review (16 Oct): its state per board, the Unpowered gate, Reworked, unsure, replay, and the
protocol-accepted badge. Host only."""
import pytest
from fastapi.testclient import TestClient
from test_aoi_polybench import eng, item, proto_dir  # noqa: F401  (fixtures)

from polybench.engine import CmdError, Engine
from polybench.protocol import load
from polybench.store import Journal

NEXT = {"id": "unpowered", "kind": "unpowered", "gate": {"requires": ["visual"]}}


def photo(eng, board="S01"):
    eng.aoi_add_photo(board, "top", f"{board}.jpg", f"aoi/photos/{board}.jpg", "sha", 1, "filename")


def run(eng, items, photos=("S01",), boards=()):
    eng.aoi_run_done("S01", {"run": 1, "result": "/x/r.json", "dir": "/x", "items": items, "boards": list(boards),
                             "photos": [{"board": b, "side": "top", "name": f"{b}.jpg"} for b in photos], "counts": {}})


def vis(eng, b="S01"):
    return eng.state.step_board(eng.proto.step("visual"), b)


def reasons(eng, b="S01"):
    return eng.state.gate(NEXT, b)["reasons"]


def two(eng):
    return [item("S01", "AOI-BRIDGE-IC1-1-2", "BRIDGE"), item("S01", "AOI-LIFT-C1", "LIFT")]


def test_no_photo_is_empty_and_blocks(eng):
    assert vis(eng)["state"] == "empty"
    assert reasons(eng) == ["no AOI photo for S01"]


def test_photo_without_a_run_covering_it_is_empty(eng):
    photo(eng)
    assert reasons(eng) == ["no AOI run covers S01"]
    photo(eng, "S02")
    run(eng, [], photos=("S02",))
    assert vis(eng, "S01")["state"] == "empty" and reasons(eng, "S01") == ["no AOI run covers S01"]
    assert vis(eng, "S02")["state"] == "done"          # a run with nothing to decide on a covered board is complete


def test_partial_counts_are_decided_over_total(eng):
    photo(eng)
    run(eng, two(eng))
    assert (vis(eng)["state"], vis(eng)["settled"], vis(eng)["applicable"]) == ("partial", 0, 2)
    eng.aoi_decide("S01|AOI-LIFT-C1", "rejected")
    sb = vis(eng)
    assert (sb["state"], sb["settled"], sb["applicable"]) == ("partial", 1, 2)
    assert reasons(eng) == ["S01 has 1 AOI item not decided"]
    snap = eng.state.snapshot()
    assert snap["steps"][[s["id"] for s in snap["steps"]].index("visual")]["per_board"]["S01"]["settled"] == 1


def test_done_when_every_item_is_decided_and_gate_opens(eng):
    photo(eng)
    run(eng, two(eng))
    eng.aoi_decide("S01|AOI-LIFT-C1", "rejected")
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "rejected")
    assert vis(eng)["state"] == "done" and eng.state.gate(NEXT, "S01")["ok"]


def test_confirmed_issue_is_attention_and_blocks_until_reworked(eng):
    photo(eng)
    run(eng, two(eng))
    eng.aoi_decide("S01|AOI-LIFT-C1", "rejected")
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "confirmed")
    assert vis(eng)["state"] == "attention" and vis(eng)["fails"] == 1
    assert reasons(eng) == ["S01 has 1 confirmed AOI issue"]
    assert not eng.state.gate(NEXT, "S01")["ok"]
    # the other boards are unaffected by this board's issue
    assert vis(eng, "S02")["state"] == "empty"


def test_plural_reason_and_override(eng):
    photo(eng)
    run(eng, two(eng))
    for k in ("S01|AOI-LIFT-C1", "S01|AOI-BRIDGE-IC1-1-2"):
        eng.aoi_decide(k, "confirmed")
    assert reasons(eng) == ["S01 has 2 confirmed AOI issues"]
    eng.override_gate("S01", "unpowered", "accepted by Pat")
    assert eng.state.gate(NEXT, "S01")["ok"]


def test_reworked_clears_the_block_and_a_new_decision_reopens_it(eng):
    photo(eng)
    run(eng, two(eng))
    eng.aoi_decide("S01|AOI-LIFT-C1", "rejected")
    k = "S01|AOI-BRIDGE-IC1-1-2"
    with pytest.raises(CmdError):
        eng.aoi_rework("S01|AOI-LIFT-C1", "x")          # only a confirmed issue can be reworked
    eng.aoi_decide(k, "confirmed")
    eng.aoi_rework(k, "reflowed IC1")
    assert vis(eng)["state"] == "done" and eng.state.gate(NEXT, "S01")["ok"]
    assert eng.state.aoi_reworked[k]["note"] == "reflowed IC1"
    snap = eng.state.snapshot()["aoi"]
    assert snap["per_board"]["S01"]["reworked"] == 1 and [i for i in snap["items"] if i["key"] == k][0]["reworked"] is True
    eng.aoi_decide(k, "unsure")                          # decided again: the mark goes with the old decision
    assert k not in eng.state.aoi_reworked
    eng.aoi_decide(k, "confirmed")
    assert vis(eng)["state"] == "attention"
    eng.aoi_rework(k)
    eng.aoi_rework(k, on=False)                          # taken back
    assert vis(eng)["state"] == "attention"


def test_unsure_warns_but_does_not_block(eng):
    photo(eng)
    run(eng, two(eng))
    eng.aoi_decide("S01|AOI-LIFT-C1", "unsure")
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "rejected")
    sb = vis(eng)
    assert sb["state"] == "done" and sb["reviews"] == 1 and sb["aoi"]["unsure"] == 1
    g = eng.state.gate(NEXT, "S01")
    assert g["ok"] and g["warnings"] == ["S01 has 1 AOI item marked ignored"]
    from polybench.report import build_model
    items = [b for b in build_model(eng.state, "h")["blocks"] if b[0] == "ul"]
    assert any("WARNING ignored" in x for b in items for x in b[1])


def test_manual_issue_counts_as_an_item_and_blocks(eng, tmp_path):
    from test_aoi_manual import run_folder, start_run
    photo(eng, "S03")
    start_run(eng, run_folder(tmp_path))
    eng.aoi_decide("S03|AOI-BRIDGE-IC1-1-2", "rejected")
    assert vis(eng, "S03")["state"] == "done"
    eng.aoi_manual_issue("S03", [9, 9, 11, 11], "bridge", ["IC1"], "loupe")
    assert vis(eng, "S03")["state"] == "attention" and reasons(eng, "S03") == ["S03 has 1 confirmed AOI issue"]


def test_replay_from_the_journal_gives_the_same_state(eng, tmp_path):
    photo(eng)
    run(eng, two(eng))
    eng.aoi_decide("S01|AOI-LIFT-C1", "unsure")
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "confirmed")
    eng.aoi_rework("S01|AOI-BRIDGE-IC1-1-2", "fixed")
    again = Engine(eng.proto, Journal(eng.journal.path), "pat")
    assert again.state.aoi_board_status("S01") == eng.state.aoi_board_status("S01")
    assert again.state.aoi_board_status("S01")["state"] == "done" and again.state.aoi_reworked["S01|AOI-BRIDGE-IC1-1-2"]["note"] == "fixed"
    assert again.state.gate(NEXT, "S01") == eng.state.gate(NEXT, "S01")


def test_protocol_accepted_replays_and_clears_the_badge(proto_dir, tmp_path):
    from polybench.server import Session, create_app
    sess = Session(load(proto_dir), tmp_path / "data", "B", False, None, None, "pat")
    st = sess.engine.state
    st.meta["protocol_hash"] = "oldoldoldold"                # the batch started on another protocol
    assert sess.snapshot()["meta"]["protocol_changed"]
    r = sess.engine.protocol_accept(sess.proto.hash, "oldoldoldold", " tests changed ")
    assert r["ok"] and not sess.snapshot()["meta"]["protocol_changed"]
    # replay: the journal alone restores the accepted hash
    again = Engine(sess.proto, Journal(sess.engine.journal.path), "pat")
    assert again.state.accepted_protocol == sess.proto.hash
    assert again.state.protocol_accepts[-1]["reason"] == "tests changed" and again.state.protocol_accepts[-1]["old_hash"] == "oldoldoldold"
    # a later change of the file shows the badge again, against the accepted hash
    sess.proto.hash = "newnewnew"
    assert sess.snapshot()["meta"]["protocol_changed"] and sess.snapshot()["meta"]["protocol_baseline"] == again.state.accepted_protocol
    with TestClient(create_app(sess)) as c:
        assert c.post("/api/cmd/protocol_accept", json={"hash": "stale"}).status_code == 409
        assert c.post("/api/cmd/protocol_accept", json={"hash": "newnewnew", "reason": "ok"}).status_code == 200
    assert not sess.snapshot()["meta"]["protocol_changed"]


def test_defect_fixed_is_confirmed_plus_reworked_in_one_call_and_does_not_block(eng):
    photo(eng)
    run(eng, two(eng))
    k = "S01|AOI-BRIDGE-IC1-1-2"
    eng.aoi_decide("S01|AOI-LIFT-C1", "rejected")
    r = eng.aoi_decide(k, "fixed", "reflowed")
    assert r["ok"]
    types = [e["type"] for e in eng.journal.events][-2:]
    assert types == ["aoi_decision", "aoi_reworked"]
    assert eng.state.aoi_decisions[k]["decision"] == "confirmed" and eng.state.aoi_reworked[k]["note"] == "reflowed"
    assert vis(eng)["state"] == "done" and eng.state.gate(NEXT, "S01")["ok"]       # Unpowered opens with no extra Finish
    assert [i for i in eng.state.snapshot()["aoi"]["items"] if i["key"] == k][0]["reworked"] is True
    again = Engine(eng.proto, Journal(eng.journal.path), "pat")                    # replay
    assert again.state.aoi_reworked[k] and again.state.gate(NEXT, "S01")["ok"]


def test_ignore_is_unsure_and_does_not_block(eng):
    photo(eng)
    run(eng, two(eng))
    eng.aoi_decide("S01|AOI-LIFT-C1", "unsure")
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "fixed")
    g = eng.state.gate(NEXT, "S01")
    assert g["ok"] and g["warnings"] == ["S01 has 1 AOI item marked ignored"]


def test_legacy_confirmed_without_rework_still_blocks_and_can_be_reworked(eng):
    photo(eng)
    run(eng, two(eng))
    eng.aoi_decide("S01|AOI-LIFT-C1", "rejected")
    eng.aoi_decide("S01|AOI-BRIDGE-IC1-1-2", "confirmed")       # an old journal event
    assert vis(eng)["state"] == "attention"
    eng.aoi_rework("S01|AOI-BRIDGE-IC1-1-2")
    assert vis(eng)["state"] == "done"


def test_a_new_decision_after_fixed_starts_the_item_afresh(eng):
    photo(eng)
    run(eng, two(eng))
    k = "S01|AOI-LIFT-C1"
    eng.aoi_decide(k, "fixed")
    eng.aoi_decide(k, "rejected")
    assert k not in eng.state.aoi_reworked
