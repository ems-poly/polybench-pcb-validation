import pytest

from helpers import reopen
from polybench.engine import CmdError, evaluate
from polybench.protocol import load
from helpers import DEMO

P = load(DEMO / "demo.pcbv.yaml")


def T(tid):
    return P.test(tid)[2]


def test_hard_fail_guess_review_tbr_record_none():
    assert evaluate(T("U01"), "20")["verdict"] == "pass"
    assert evaluate(T("U01"), "5")["verdict"] == "fail"
    assert evaluate(T("U02"), "1")["verdict"] == "review"
    assert evaluate(T("U02"), "3")["verdict"] == "pass"
    r = evaluate(T("U03"), "0.1")
    assert r["verdict"] == "none" and "outside_tbr" in r["flags"]
    assert evaluate(T("C03"), "99")["verdict"] == "none"
    assert evaluate(T("V01"), "n")["verdict"] == "fail" and evaluate(T("V01"), "Y")["verdict"] == "pass"
    assert evaluate(T("F01"), "10810a05")["verdict"] == "pass" and evaluate(T("F01"), "0x1")["verdict"] == "fail"
    assert not evaluate(T("U01"), "abc")["ok"] and not evaluate(T("V04"), "dirty")["ok"]


def visual_ok(e, board):
    for t, v in [("V01", "y"), ("V02", "y"), ("V03", "y"), ("V04", "1")]:
        e.entry(board, t, v)


def test_fail_needs_its_own_confirm(engine):
    visual_ok(engine, "S01")
    with pytest.raises(CmdError) as x:
        engine.entry("S01", "U01", "5")
    assert x.value.extra["needs_confirm"] == "fail"
    assert engine.state.cell("S01", "U01")["status"] == "empty"
    engine.entry("S01", "U01", "5", confirm=True)
    assert engine.state.cell("S01", "U01")["verdict"] == "fail"


def test_second_value_needs_deliberate_replace(engine):
    engine.entry("S01", "V01", "y")
    with pytest.raises(CmdError) as x:
        engine.entry("S01", "V01", "n", confirm=True)
    assert x.value.extra["needs_replace"]
    engine.entry("S01", "V01", "n", confirm=True, replace=True)
    c = engine.state.cell("S01", "V01")
    assert c["value"] == "no" and c["history"][0]["value"] == "yes"


def test_skip_needs_reason_and_is_not_pass(engine):
    with pytest.raises(CmdError):
        engine.skip("S01", "V01", "  ")
    engine.skip("S01", "V01", "no lamp")
    assert engine.state.cell("S01", "V01")["status"] == "skipped"


def test_gate_blocks_and_override_needs_reason(engine):
    with pytest.raises(CmdError) as x:
        engine.entry("S01", "U01", "20")
    assert "gated" in str(x.value)
    with pytest.raises(CmdError):
        engine.override_gate("S01", "unpowered", "")
    engine.override_gate("S01", "unpowered", "visual done on paper")
    engine.entry("S01", "U01", "20")


def test_gate_requires_no_accepted_fail(engine):
    visual_ok(engine, "S01")
    engine.entry("S01", "V01", "n", confirm=True, replace=True)
    with pytest.raises(CmdError):
        engine.entry("S01", "U01", "20")


def test_stop_rule_blocks_until_acknowledged(engine):
    visual_ok(engine, "S01")
    engine.entry("S01", "U01", "0.5", confirm=True)
    assert engine.state.stopped("S01")
    with pytest.raises(CmdError):
        engine.entry("S01", "U02", "3")
    with pytest.raises(CmdError):
        engine.ack_stop("S01", engine.state.stopped("S01")[0]["id"], "")
    engine.ack_stop("S01", engine.state.stopped("S01")[0]["id"], "reflowed U2")
    engine.entry("S01", "U02", "3")


def test_retest_keeps_attempt_history(engine):
    visual_ok(engine, "S01")
    engine.entry("S01", "V01", "n", confirm=True, replace=True)
    with pytest.raises(CmdError):
        engine.retest("S01", "visual", "")
    engine.retest("S01", "visual", "bridge on U1", "reflow")
    assert engine.state.attempt("S01", "visual") == 2
    assert engine.state.cell("S01", "V01")["status"] == "empty"
    engine.entry("S01", "V01", "y")
    snap = engine.state.snapshot()["cells"]["S01|V01"]
    assert snap["attempt"] == 2 and snap["prev_attempts"][0]["verdict"] == "fail"
    assert engine.state.retests[0]["cause"] == "bridge on U1"


def test_proposals_accept_reject_and_accept_all(engine):
    visual_ok(engine, "S01")
    for t, v in [("U01", "20"), ("U02", "1"), ("U03", "0.5"), ("U04", "y")]:
        engine.entry("S01", t, v, confirm=True)
    engine.override_gate("S01", "powered", "x") if False else None
    for t in ("U05",):
        engine.entry("S01", t, "x")
    pw = ["P01", "P02", "P03", "P04"]
    for t, v in zip(pw, ["20", "5.4", "3.3", "10"]):
        engine.add_proposal("S01", t, v, plugin="demo.measure", run=1, simulate=True, record="propose")
    r = engine.accept_all("S01", "powered")
    assert r["accepted"] == 3 and r["left"] == 1          # the 5.4 V FAIL is left for its own accept
    assert engine.state.cell("S01", "P02")["status"] == "empty"
    prop = list(engine.state.cell("S01", "P02")["proposals"].values())[0]
    with pytest.raises(CmdError):
        engine.accept("S01", "P02", prop["id"])
    engine.accept("S01", "P02", prop["id"], confirm=True)
    assert engine.state.cell("S01", "P02")["verdict"] == "fail"
    engine.add_proposal("S01", "P01", "21", plugin="demo.measure", run=2, simulate=True, record="propose")
    q = list(engine.state.cell("S01", "P01")["proposals"].values())[0]
    engine.reject("S01", "P01", q["id"])
    assert not engine.state.cell("S01", "P01")["proposals"]


def test_pending_proposals_survive_restart(engine, tmp_path):
    engine.add_proposal("S01", "V01", "y", plugin="demo.measure", run=1, simulate=True, record="propose")
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert len(e2.state.cell("S01", "V01")["proposals"]) == 1


def test_scan_matches_barcode_mpn_designator(engine):
    assert engine.find_line("595-DEMOMCU32")[0]["line"] == 1
    assert engine.find_line("demomcu-32")[0]["line"] == 1
    assert engine.find_line("C3")[0]["line"] == 4
    assert engine.find_line("nonsense")[0] is None
