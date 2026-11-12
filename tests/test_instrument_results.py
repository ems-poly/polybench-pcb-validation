"""Instrument readings are results (auto-recorded), table results, opt-out, retest on an existing value, stop and interlock on auto values,
old journals with pending proposals. Engine level, no UI, silent."""
import json

import pytest

from helpers import DEMO, reopen
from polybench.engine import CmdError, evaluate
from polybench.protocol import load
from test_planfmt import make_engine, fill, good_unpowered

P = load(DEMO / "demo.pcbv.yaml")


@pytest.fixture
def engine(engine):
    for b in ("S01", "S02", "S03"):
        engine.override_gate(b, "powered", "these tests start at the powered step")
    return engine


def acts(e, type_):
    return [x for x in e.journal.events if x["type"] == type_]


def test_action_reading_is_recorded_without_accept_whatever_the_verdict(engine):
    for tid, v, verdict in [("P04", "10", "pass"), ("P03", "3.3", "pass"), ("P02", "5.4", "fail"), ("P01", "30", "pass")]:
        r = engine.add_proposal("S01", tid, v, plugin="demo.measure", run=1, simulate=True)
        assert r["recorded"] == "recorded"
        c = engine.state.cell("S01", tid)
        assert c["status"] == "accepted" and c["verdict"] == verdict and c["auto"] and not c["proposals"]
    c = engine.state.cell("S01", "P02")
    assert c["source"]["kind"] == "action" and c["source"]["input_method"] == "instrument" and c["source"]["run_ref"] == 1
    assert c["origin"] == "simulated" and c["raw"] is None
    ev = acts(engine, "accept")[-1]["payload"]
    assert ev["auto"] is True and ev["proposal_id"] and ev["confirmed"] is False
    assert len(acts(engine, "proposal")) == 4              # the offer stays in the journal beside the result


def test_review_and_record_verdicts_are_recorded_too(engine):
    engine.add_proposal("S01", "P04", "31", plugin="demo.measure", run=1)           # guess limit exceeded
    c = engine.state.cell("S01", "P04")
    assert c["status"] == "accepted" and c["verdict"] == "review" and "outside_guess" in c["flags"]
    a = [x for x in engine.state.anomalies("S01") if x["test_id"] == "P04"][0]
    assert a["kind"] == "review" and "31" in a["reason"] and "outside" in a["reason"] and "guess limit" in a["reason"]


def test_state_rebuild_from_the_journal_reproduces_auto_results(engine, tmp_path):
    engine.add_proposal("S01", "P02", "5.4", plugin="demo.measure", run=3, evidence="ev", raw="sim")
    e2 = reopen(engine, tmp_path / "j.jsonl")
    c = e2.state.cell("S01", "P02")
    assert c["status"] == "accepted" and c["auto"] and c["verdict"] == "fail" and c["evidence"] == "ev" and not c["proposals"]
    assert e2.state.snapshot()["anomalies"][0]["test_id"] == "P02"


def test_record_propose_opts_out(engine):
    r = engine.add_proposal("S01", "P01", "20", plugin="demo.measure", run=1, record="propose")
    assert r["recorded"] == "pending"
    c = engine.state.cell("S01", "P01")
    assert c["status"] == "empty" and len(c["proposals"]) == 1
    engine.accept("S01", "P01", list(c["proposals"].values())[0]["id"])
    assert engine.state.cell("S01", "P01")["status"] == "accepted" and not engine.state.cell("S01", "P01")["auto"]


def test_voice_and_ai_sources_stay_proposals(engine):
    for src in ({"kind": "voice", "component": "voice", "key": "voice"}, {"kind": "assistant", "component": "ai", "key": "ai", "client": "x", "reason": "r"}):
        engine.add_proposal("S01", "P01", "20", source=src)
        c = engine.state.cell("S01", "P01")
        assert c["status"] == "empty" and len(c["proposals"]) == 1
        engine.reject("S01", "P01", list(c["proposals"].values())[0]["id"])


def test_linter_accepts_record_and_rejects_nonsense(demo_copy):
    f = demo_copy / "function.yaml"
    txt = f.read_text(encoding="utf-8")
    assert "record: propose" in txt
    assert not load(demo_copy / "demo.pcbv.yaml").errors
    f.write_text(txt.replace("record: propose", "record: maybe"), encoding="utf-8")
    errs = [str(e) for e in load(demo_copy / "demo.pcbv.yaml").errors]
    assert any("record must be one of" in e for e in errs)


def test_remeasure_replaces_the_cell_in_the_same_attempt_and_keeps_the_rest(engine):
    """D1: an action measuring a cell that already holds a result replaces THAT cell; no step retest, other tests keep their values."""
    for tid, v in [("P01", "20"), ("P02", "5.0"), ("P03", "3.3"), ("P04", "10")]:
        engine.add_proposal("S01", tid, v, plugin="demo.measure", run=1)
    n_retests = len(acts(engine, "retest_open"))
    r = engine.add_proposal("S01", "P02", "5.1", plugin="demo.measure", run=2)           # a later run measures P02 again
    assert r["recorded"] == "replaced"
    assert engine.state.attempt("S01", "powered") == 1 and len(acts(engine, "retest_open")) == n_retests == 0
    assert engine.state.retests == []
    for tid, v in [("P01", "20"), ("P03", "3.3"), ("P04", "10")]:
        assert engine.state.cell("S01", tid)["status"] == "accepted" and engine.state.cell("S01", tid)["value"] == v
    c = engine.state.cell("S01", "P02")
    assert c["value"] == "5.1" and c["auto"] and [h["value"] for h in c["history"]] == ["5.0"] and c["history"][0]["remeasured"]
    engine.add_proposal("S01", "P02", "5.2", plugin="demo.measure", run=2)               # same run again: still in place
    assert [h["value"] for h in engine.state.cell("S01", "P02")["history"]] == ["5.0", "5.1"]
    assert acts(engine, "edit")[-1]["payload"]["remeasure"] is True
    # the history is in the snapshot the UI shows


def test_remeasure_evaluates_stop_and_verdict_on_the_newest_value(engine):
    engine.add_proposal("S01", "P02", "5.4", plugin="demo.measure", run=1)               # fail
    assert engine.state.cell("S01", "P02")["verdict"] == "fail"
    engine.add_proposal("S01", "P02", "5.0", plugin="demo.measure", run=2)
    c = engine.state.cell("S01", "P02")
    assert c["verdict"] == "pass" and c["history"][0]["verdict"] == "fail"
    assert not [a for a in engine.state.anomalies("S01") if a["test_id"] == "P02"]       # anomalies follow the newest value
    engine.add_proposal("S01", "P01", "150", plugin="demo.measure", run=2)               # stop_if above 100 on the new value
    assert engine.state.stopped("S01")


def test_old_journal_with_step_retests_rebuilds_identically(engine, tmp_path):
    engine.add_proposal("S01", "P01", "20", plugin="demo.measure", run=1)
    engine.add_proposal("S01", "P02", "5.0", plugin="demo.measure", run=1)
    engine.retest("S01", "powered", "rework")                                              # an explicit, journalled step retest
    engine.add_proposal("S01", "P01", "21", plugin="demo.measure", run=2)
    # a journal written by the previous build: an auto retest_open (plugin, run_ref) followed by the value in attempt 2
    engine.add_proposal("S02", "P01", "20", plugin="demo.measure", run=1)
    engine._emit("retest_open", {"board": "S02", "step": "powered", "attempt": 2, "cause": "re-measured by demo.measure run 2",
                                 "fix": "", "reason": "re-measured by demo.measure run 2", "auto": True, "plugin": "demo.measure", "run_ref": 2})
    engine._emit("accept", {"board": "S02", "test_id": "P01", "attempt": 2, "value": "21", "unit": "mA", "verdict": "pass", "flags": [],
                            "reason": "", "source": {"kind": "action", "component": "demo.measure", "run_ref": 2, "key": "demo.measure"},
                            "origin": "measured", "evidence": None, "raw": None, "confirmed": False, "auto": True})
    before = engine.state.snapshot()
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert e2.state.attempt("S01", "powered") == 2 and len(e2.state.retests) == 2
    assert e2.state.cell("S01", "P01")["value"] == "21" and e2.state.cell("S01", "P02")["status"] == "empty"
    assert e2.state.cells[("S01", "P01", 1)]["value"] == "20"
    e3 = e2
    assert e3.state.attempt("S02", "powered") == 2 and e3.state.cells[("S02", "P01", 1)]["value"] == "20"
    assert e3.state.cell("S02", "P01")["value"] == "21" and e3.state.retests[-1]["auto"]
    assert e3.state.snapshot()["cells"] == before["cells"]


def test_redo_names_the_action_that_measures_the_test(engine):
    engine.add_proposal("S01", "P02", "5.4", plugin="demo.measure", run=1)
    engine.add_proposal("S01", "P01", "30", plugin="demo.measure", run=1)
    a = {x["test_id"]: x for x in engine.state.anomalies("S01")}
    assert a["P02"]["redo"]["action"] and a["P02"]["redo"]["step"] == "powered" and a["P02"]["redo"]["section"]
    assert engine.redo_action("U01") is None                                               # a typed test: no action


def test_redo_without_an_action_replaces_the_typed_value_in_the_same_attempt(tmp_path):
    eng, _ = make_engine(tmp_path, boards=("S01",))
    fill(eng, "S01", {"U01": 0.5, "U02": 3.0})
    with pytest.raises(CmdError):
        eng.entry("S01", "U01", "2.0")                                                     # a plain re-entry still needs replace
    eng.entry("S01", "U01", "2.0", redo=True)
    assert eng.state.attempt("S01", "unpowered") == 1 and eng.state.retests == []
    c = eng.state.cell("S01", "U01")
    assert c["value"] == 2.0 or str(c["value"]) == "2.0" or c["value"] == "2.0"
    assert [float(h["value"]) for h in c["history"]] == [0.5] and c["history"][0]["remeasured"]
    assert eng.state.cell("S01", "U02")["status"] == "accepted"


def test_interlock_is_not_satisfied_by_a_remeasure_or_a_redo(tmp_path):
    """D3: only a real step retest repeats Tier 1 unpowered."""
    eng, _ = make_engine(tmp_path, boards=("S01", "S02"))
    fill(eng, "S02", {"U02": 3.0, "U03": 0.6, "U04": "Fluke", "U05": 10.0, "U06": 10.0})
    eng.add_proposal("S02", "U01", "0.5", plugin="demo.measure", run=1)                  # out of band, by an action
    pw = eng.proto.step("powered")
    assert not eng.state.gate(pw, "S02")["ok"]
    eng.gate_accept(0, ["S02"])
    eng.entry("S02", "U20", "R3 was 12k, replaced")
    eng.entry("S02", "U21", "y", confirm=True)
    assert "repeat Tier 1 unpowered" in eng.state.gate(pw, "S02")["reasons"][-1]
    eng.add_proposal("S02", "U01", "2.0", plugin="demo.measure", run=2)                  # re-measured in band, same attempt
    assert eng.state.cell("S02", "U01")["verdict"] == "pass" and eng.state.attempt("S02", "unpowered") == 1
    g = eng.state.gate(pw, "S02")
    assert not g["ok"] and "repeat Tier 1 unpowered" in g["reasons"][-1]
    eng.entry("S02", "U01", "2.1", redo=True)                                              # Redo by typing: same
    g = eng.state.gate(pw, "S02")
    assert not g["ok"] and "repeat Tier 1 unpowered" in g["reasons"][-1]
    assert eng.state.attempt("S02", "unpowered") == 1
    eng.retest("S02", "unpowered", "R3 replaced")                                          # the real step retest is what the interlock needs
    assert "repeat of Tier 1 unpowered is not finished" in eng.state.gate(pw, "S02")["reasons"][-1]
    good_unpowered(eng, "S02")
    assert eng.state.gate(pw, "S02")["ok"]


def test_stop_rule_fires_on_an_auto_recorded_value(engine):
    r = engine.add_proposal("S01", "P01", "150", plugin="demo.measure", run=1)       # stop_if above 100
    assert r["recorded"] == "recorded"
    st = engine.state.stopped("S01")
    assert st and st[0]["test_id"] == "P01" and "over-current" in st[0]["text"]
    assert any(a["kind"] == "stop" for a in engine.state.anomalies("S01"))
    with pytest.raises(CmdError):
        engine.entry("S01", "P02", "5.0")                                             # the board is stopped, as after a typed value
    # a later reading while stopped waits as a proposal; nothing is lost
    r = engine.add_proposal("S01", "P02", "5.0", plugin="demo.measure", run=1)
    assert r["recorded"] == "pending" and "stopped" in r["why"]
    assert len(engine.state.cell("S01", "P02")["proposals"]) == 1


def test_power_interlock_acts_on_an_auto_recorded_unpowered_value(tmp_path):
    eng, _ = make_engine(tmp_path, boards=("S01", "S02"))
    fill(eng, "S01", {"U01": 2.0, "U02": 3.0, "U03": 0.6, "U04": "Fluke", "U05": 10.0, "U06": 10.0})
    fill(eng, "S02", {"U02": 3.0, "U03": 0.6, "U04": "Fluke", "U05": 10.0, "U06": 10.0})
    eng.add_proposal("S02", "U01", "0.5", plugin="demo.measure", run=1)                 # out of band, recorded by an action
    pw = eng.proto.step("powered")
    assert eng.state.gate(pw, "S01")["ok"]
    g = eng.state.gate(pw, "S02")
    assert not g["ok"] and "power interlock" in g["reasons"][0]


def test_old_journal_with_pending_action_proposals_still_loads_and_accepts(engine, tmp_path):
    """A journal written before this change holds proposals from actions that never became results."""
    engine.add_proposal("S01", "P04", "12", plugin="n6705b.first_power", run=1, record="propose")
    e2 = reopen(engine, tmp_path / "j.jsonl")
    c = e2.state.cell("S01", "P04")
    assert c["status"] == "empty" and len(c["proposals"]) == 1
    e2.accept("S01", "P04", list(c["proposals"].values())[0]["id"])
    assert e2.state.cell("S01", "P04")["status"] == "accepted"
    assert not e2.state.cell("S01", "P04")["proposals"]


# ---- table results

def tbl(rows, cols=("load_ma", "rail_v")):
    return {"columns": list(cols), "rows": rows}


def test_table_row_verdicts_and_worst_row(engine):
    t = P.test("P05")[2]
    ev = evaluate(t, tbl([[0, 5.0], [10, 4.96], [40, 4.84], [80, 5.3]]))
    assert ev["ok"] and ev["verdict"] == "fail"
    assert [r["verdict"] for r in ev["row_verdicts"]] == ["pass", "pass", "fail", "fail"]
    assert ev["row_verdicts"][2]["out"] == ["rail_v"]
    assert "2 of 4 rows out of band" in ev["reason"] and "row 3" in ev["reason"]
    ok = evaluate(t, tbl([[0, 5.0], [10, 4.96]]))
    assert ok["verdict"] == "pass" and ok["reason"] == "" and ok["row_verdicts"][0]["out"] == []


def test_table_guess_limit_gives_review_and_record_gives_none():
    t = {"id": "T", "kind": "table", "columns": [{"id": "a"}, {"id": "b"}], "limits": {
        "a": {"class": "guess", "max": 1, "source": {"type": "estimate"}, "closes_on": "x"}, "b": {"class": "record"}}}
    ev = evaluate(t, tbl([[0.5, 9], [2, 9]], ("a", "b")))
    assert ev["verdict"] == "review" and "outside_guess" in ev["flags"]
    assert evaluate({**t, "limits": {"b": {"class": "record"}}}, tbl([[5, 1]], ("a", "b")))["verdict"] == "none"


def test_table_shape_errors(engine):
    t = P.test("P05")[2]
    assert not evaluate(t, tbl([[1]]))["ok"]                                  # row has one cell, two columns
    assert not evaluate(t, tbl([[1, "x"]]))["ok"]                              # limited column must be a number
    assert not evaluate(t, {"columns": ["nope"], "rows": [[1]]})["ok"]
    assert not evaluate(t, "20")["ok"]                                         # a table cannot be typed
    assert evaluate(t, json.dumps(tbl([[0, 5.0]])))["ok"]                      # JSON text is accepted


def test_table_recorded_by_action_survives_reopen_and_is_in_the_report(engine, tmp_path):
    from polybench import report
    engine.add_proposal("S01", "P05", tbl([[0, 5.0], [80, 4.7]]), plugin="demo.sweep", run=1)
    c = engine.state.cell("S01", "P05")
    assert c["status"] == "accepted" and c["verdict"] == "fail" and c["row_verdicts"][1]["verdict"] == "fail"
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert e2.state.cell("S01", "P05")["value"]["rows"] == [[0.0, 5.0], [80.0, 4.7]]
    md = report.to_markdown(report.build_model(e2.state))
    assert "5V rail against load" in md and "4.7" in md and "FAIL" in md and "table, 2 rows" in md


def test_table_stop_if_on_a_column(tmp_path, demo_copy):
    f = demo_copy / "powered.yaml"
    f.write_text(f.read_text(encoding="utf-8").replace("            plot: {x: load_ma, y: rail_v}", "            plot: {x: load_ma, y: rail_v}\n            stop_if: {column: rail_v, below: 4.0, text: \"rail collapsed\"}"), encoding="utf-8")
    p = load(demo_copy / "demo.pcbv.yaml")
    assert not p.errors, [str(e) for e in p.errors]
    from polybench.engine import Engine
    from polybench.store import Journal
    e = Engine(p, Journal(tmp_path / "j.jsonl"), "t")
    e.create_batch("t", ["S01"], "S", True, "D")
    e.override_gate("S01", "powered", "x")
    e.add_proposal("S01", "P05", tbl([[0, 5.0], [80, 3.5]]), plugin="demo.sweep", run=1)
    assert e.state.stopped("S01")[0]["text"] == "rail collapsed"


def test_table_lint(demo_copy):
    f = demo_copy / "powered.yaml"
    base = f.read_text(encoding="utf-8")
    for bad, msg in [("limits:\n              rail_v:", "limits:\n              nope:"), ("plot: {x: load_ma, y: rail_v}", "plot: {x: load_ma, y: zzz}")]:
        f.write_text(base.replace(bad, msg), encoding="utf-8")
        assert load(demo_copy / "demo.pcbv.yaml").errors, msg
    f.write_text(base, encoding="utf-8")
    assert not load(demo_copy / "demo.pcbv.yaml").errors


def test_propose_table_helper_builds_the_wire_value(tmp_path):
    from polybench.testing import RecordingContext
    ctx = RecordingContext(workdir=tmp_path)
    ctx.propose_table("P05", [[0, 5.0], [10, 4.9]], ["load_ma", "rail_v"])
    ctx.propose_table("P05", [{"load_ma": 1, "rail_v": 5.0}])
    (tmp_path / "s.csv").write_text("load_ma,rail_v\n0,5.0\n10,4.9\n", encoding="utf-8")
    ctx.propose_table("P05", csv="s.csv")
    v = [p["value"] for p in ctx.proposals]
    assert v[0] == {"rows": [[0, 5.0], [10, 4.9]], "columns": ["load_ma", "rail_v"]}
    assert v[1]["columns"] == ["load_ma", "rail_v"] and v[1]["rows"] == [[1, 5.0]]
    assert v[2] == {"rows": [[0.0, 5.0], [10.0, 4.9]], "columns": ["load_ma", "rail_v"], "attachment": "s.csv"}


def test_record_pending_backfills_an_old_journal_once(engine, tmp_path):
    engine.add_proposal("S01", "P04", "12", plugin="demo.measure", run=1, record="propose")      # what an old journal holds
    engine.add_proposal("S01", "P05", {"columns": ["load_ma", "rail_v"], "rows": [[0, 5.0]]}, plugin="demo.sweep", run=1, record="propose")
    engine.add_proposal("S01", "P03", "3.3", plugin="demo.measure", run=1, record="propose")
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert len(e2.pending_instrument("S01")) == 3 and e2.state.snapshot()["pending_instrument"] == {"S01": 3}
    r = e2.record_pending("S01")
    assert r["recorded"] == 3 and r["left"] == 0
    assert e2.state.cell("S01", "P04")["status"] == "accepted" and e2.state.cell("S01", "P04")["auto"]
    assert e2.record_pending("S01")["recorded"] == 0
    assert acts(e2, "accept")[-1]["payload"]["via"] == "backfill"
