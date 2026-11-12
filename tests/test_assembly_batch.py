"""Batch build (one component on all boards), line status, assembly log, label parsing, old-journal compatibility."""
import json

import pytest

from helpers import reopen
from polybench import labels
from polybench.engine import CmdError


def test_place_batch_marks_all_ticked_boards_only(engine):
    engine.place_batch(4, ["S01", "S02"], via="button")          # C1..C6 on two of three boards
    st = engine.state
    assert st.placed["S01"] >= {"C1", "C6"} and st.placed["S02"] >= {"C1", "C6"} and not st.placed["S03"]
    ls = st.line_status(next(b for b in st.bom_lines() if b["line"] == 4))
    assert ls["state"] == "partial" and ls["done"] == ["S01", "S02"] and ls["missing"] == ["S03"] and ls["per_board"] == 6
    engine.place_batch(4, ["S03"])
    assert st.line_status(next(b for b in st.bom_lines() if b["line"] == 4))["state"] == "done"


def test_batch_is_one_journal_event_and_replays(engine, tmp_path):
    n0 = len(engine.journal.events)
    engine.place_batch(5, ["S01", "S02", "S03"], via="scan", code="603-RC0402FR-0710KL")
    assert len(engine.journal.events) == n0 + 1
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert e2.state.placed["S03"] >= {"R3", "R4", "R5"}
    log = e2.state.alog[-1]
    assert log["kind"] == "place" and log["line"] == 5 and log["via"] == "scan" and log["operator"] == "tester" and log["boards"] == ["S01", "S02", "S03"]


def test_per_board_exception_after_batch(engine):
    engine.place_batch(4, ["S01", "S02", "S03"])
    engine.set_placed("S02", ["C3"], False)                      # C3 missing on S02 only
    ls = engine.state.line_status(next(b for b in engine.state.bom_lines() if b["line"] == 4))
    assert ls["state"] == "partial" and ls["partial"] == ["S02"] and ls["done"] == ["S01", "S03"]


def test_dnf_line_cannot_be_placed_and_is_never_todo(engine):
    with pytest.raises(CmdError):
        engine.place_batch(8, ["S01"])
    dnf = next(b for b in engine.state.bom_lines() if b["line"] == 8)
    assert engine.state.line_status(dnf)["state"] == "dnf"


def test_place_batch_rejects_unknown_line_board_and_empty(engine):
    for args in ((99, ["S01"]), (4, ["S09"]), (4, [])):
        with pytest.raises(CmdError):
            engine.place_batch(*args)


def test_step_progress_follows_batch_placement(engine):
    for ln in engine.state.bom_lines():
        if not ln.get("dnf"):
            engine.place_batch(ln["line"], ["S01", "S02", "S03"])
    snap = engine.state.snapshot()
    a = next(s for s in snap["steps"] if s["kind"] == "assembly")
    assert all(a["per_board"][b]["state"] == "done" for b in ("S01", "S02", "S03")) and a["state"] == "done"
    assert snap["asm"]["8"]["state"] == "dnf" and snap["asm"]["4"]["per_board"] == 6


def test_old_journal_events_still_read(engine, tmp_path):
    """A journal written before this change (placed + scan with the old payload) replays without error and fills the log."""
    engine.set_placed("S01", ["U1"], True)
    engine._emit("scan", {"code": "595-DEMOMCU32", "line": 1, "matched_by": "barcode"})   # no via, no format: the old payload
    e2 = reopen(engine, tmp_path / "j.jsonl")
    kinds = [r["kind"] for r in e2.state.alog]
    assert kinds == ["place", "scan"] and e2.state.alog[0]["via"] == "per board" and e2.state.alog[1]["via"] == "typed"
    assert e2.state.snapshot()["alog"]


def test_scan_records_how_it_was_read_and_matches_any_code_on_the_label(engine):
    r = engine.scan("", via="camera", codes=["1234567", "DEMO1 R3,4,5", "603-RC0402FR-0710KL", "US"], fmt="Code128")
    assert r["ok"] and r["line"] == 5 and r["code"] == "603-RC0402FR-0710KL"
    ev = engine.journal.events[-1]["payload"]
    assert ev["via"] == "camera" and ev["format"] == "Code128" and ev["codes"][0] == "1234567"
    engine.scan("garbage", via="camera")
    assert engine.state.alog[-1]["kind"] == "scan_miss"


def test_customer_part_number_never_matches(engine):
    """D7: a customer P/N such as 'DEMO1 R3,4' must not be matched, not even through a designator it contains."""
    r = engine.scan("DEMO1 R3,4,5", via="camera")
    assert not r["ok"]


def test_partial_mpn_only_when_nothing_exact(engine):
    r = engine.scan("", via="camera", codes=["DEMOMCU", "DEMOCAN-8"])
    assert r["line"] == 3 and r["matched_by"] == "mpn"          # the exact one wins over the earlier partial


def test_iso15434_datamatrix_uses_1P_ignores_customer_pn_reports_qty():
    gs, rs, eot = "\x1d", "\x1e", "\x04"
    dm = f"[)>{rs}06{gs}K4500123456{gs}14K001{gs}PEVT1 R4,5{gs}1PGRM21BZ71E106KE15K{gs}Q60{gs}11K98765{gs}4LCN{gs}1VMurata{rs}{eot}"
    info = labels.parse_label(dm)
    assert info["envelope"] and info["candidates"] == ["GRM21BZ71E106KE15K"] and info["qty"] == 60
    assert "P" not in info["fields"] and info["fields"]["1P"] == "GRM21BZ71E106KE15K" and info["fields"]["K"] == "4500123456"
    # the D2 label: DataMatrix without an MPN gives nothing to match on, and no error
    d2 = f"[)>{rs}06{gs}K1{gs}14K1{gs}PEVT1 R4,5{gs}Q10{rs}{eot}"
    assert labels.parse_label(d2)["candidates"] == [] and labels.parse_label(d2)["qty"] == 10


def test_plain_codes_are_kept_and_normalised():
    assert labels.parse_label("  603-RC0402FR-0710KL ")["candidates"] == ["603-RC0402FR-0710KL"]
    assert labels.parse_label("")["candidates"] == []
    assert labels.norm(" a   b ") == "A B"


def test_server_batch_command_and_log_in_snapshot(tmp_path):
    from fastapi.testclient import TestClient
    from helpers import ROOT
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"), tmp_path, "b", True, 3, None)
    with TestClient(create_app(sess)) as c:
        r = c.post("/api/cmd/scan", json={"code": "", "codes": ["x", "595-DEMOADC16"], "via": "camera", "format": "Code128"}).json()
        assert r["ok"] and r["line"] == 2 and r["ecad"]["highlighted"] == ["U2"]
        r = c.post("/api/cmd/place_batch", json={"line": 2, "boards": ["S01", "S03"], "via": "scan", "code": "595-DEMOADC16"})
        assert r.status_code == 200
        s = c.get("/api/state").json()
        assert s["asm"]["2"]["state"] == "partial" and s["asm"]["2"]["missing"] == ["S02"]
        assert [x["kind"] for x in s["alog"]] == ["scan", "place"]
        assert c.post("/api/cmd/place_batch", json={"line": 2, "boards": []}).status_code == 422


def test_select_line_does_not_trust_a_stale_board_gate(tmp_path):
    """Regression (flaky `KeyError: 'highlighted'`): ensure_board() returns when board_ctx() sees the auto-picked board, but
    ctl.board_ok is refreshed later by a worker callback. A select_line in that gap used to be blocked with 'choosing…'
    and the server answered {'error': ...}. The gate must be recomputed from the same state ensure_board() looked at."""
    from fastapi.testclient import TestClient
    from helpers import ROOT
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"), tmp_path, "b", True, 3, None)
    with TestClient(create_app(sess)) as c:
        assert c.post("/api/cmd/select_line", json={"line": 2}).json()["ecad"]["highlighted"] == ["U2"]
        svc = sess.scanner
        svc.ctl.board_ok, svc.ctl.board_block_reason = False, "choosing…"      # the stale flag the callback has not refreshed yet
        r = c.post("/api/cmd/select_line", json={"line": 2}).json()
        assert r["ecad"].get("highlighted") == ["U2"], r
