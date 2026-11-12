"""Powered step (PW-2, PW-3, PW-6): the single value box's override and history path, tests derived from an action's analysis, the
Results explorer's data. Engine and server level, simulated, silent. DEMO protocol only."""
import asyncio
import json

import pytest

from helpers import DEMO
from polybench import derive, measurements
from polybench.protocol import load


@pytest.fixture
def engine(engine):
    for b in ("S01", "S02", "S03"):
        engine.override_gate(b, "powered", "these tests start at the powered step")
    return engine


def snap(engine, b, t):
    return engine.state.snapshot()["cells"][f"{b}|{t}"]


# ---- PW-2: override keeps the instrument value in the history, source keyboard
def test_typed_override_of_an_instrument_value_keeps_it_in_history(engine):
    engine.add_proposal("S01", "P01", "22", plugin="demo.measure", run=7, simulate=True)
    c = snap(engine, "S01", "P01")
    assert c["status"] == "accepted" and c["source"]["kind"] == "action" and c["source"]["run_ref"] == 7
    r = engine.entry("S01", "P01", "21.5", replace=True)
    assert r["ok"]
    c = snap(engine, "S01", "P01")
    assert c["value"] == "21.5" and c["source"]["kind"] == "keyboard" and not c["auto"]
    assert len(c["edits"]) == 1 and c["edits"][0]["value"] == "22" and c["edits"][0]["source"]["kind"] == "action"
    assert c["edits"][0]["source"]["run_ref"] == 7           # the UI line "edited (instrument said 22 mA)" reads this
    kinds = [x["type"] for x in engine.journal.events if x["payload"].get("test_id") == "P01"]
    assert kinds[-1] == "edit"                                # a replace, in the same attempt


def test_override_of_a_pending_proposal_then_accept_flow(engine):
    engine.add_proposal("S01", "P01", "22", plugin="demo.measure", run=2, record="propose")
    c = snap(engine, "S01", "P01")
    assert c["status"] == "empty" and c["proposals"][0]["value"] == "22"
    assert engine.entry("S01", "P01", "20")["ok"]            # typed over the proposal: recorded as keyboard, the proposal is not the result
    c = snap(engine, "S01", "P01")
    assert c["status"] == "accepted" and c["source"]["kind"] == "keyboard" and c["value"] == "20"
    engine.add_proposal("S02", "P01", "23", plugin="demo.measure", run=2, record="propose")
    pid = snap(engine, "S02", "P01")["proposals"][0]["id"]
    engine.accept("S02", "P01", pid)                          # Enter on the pre-filled box: the instrument's own proposal is accepted
    c = snap(engine, "S02", "P01")
    assert c["value"] == "23" and c["source"]["kind"] == "action" and not c["edits"]


def test_use_this_value_is_an_override_with_the_earlier_value_in_the_history(engine):
    engine.add_proposal("S01", "P01", "22", plugin="demo.measure", run=1)
    engine.entry("S01", "P01", "30", replace=True)
    engine.entry("S01", "P01", "22", replace=True)            # "Use this value" with the instrument's first reading, then Accept
    c = snap(engine, "S01", "P01")
    assert c["value"] == "22" and c["source"]["kind"] == "keyboard"
    assert [e["value"] for e in c["edits"]] == ["22", "30"] and c["edits"][0]["source"]["kind"] == "action"


# ---- PW-6: derive
def test_derive_values_maps_booleans_and_leaves_missing_data_to_the_operator():
    rules = [{"test": "P06", "from": "analysis.within_limit", "map": {True: "yes", False: "no"}},
             {"test": "P09", "from": "current.final", "unit": "mA"},
             {"test": "P10", "from": "analysis.nothing"},
             {"test": "P11", "from": "mode", "map": {"CV": "yes"}}]
    d, skipped = derive.derive_values(rules, {"within_limit": False, "current": {"final": 4.123456789}, "mode": "CC"})
    got = {x["test"]: x["value"] for x in d}
    assert got == {"P06": "no", "P09": "4.12346"}
    assert len(skipped) == 2 and "P10" in skipped[0] and "P11" in skipped[1]
    assert derive.derive_values([{"test": "P06"}, "x", {"from": "a"}], {"a": 1}) == ([], [])      # malformed rules are dropped (lint reports them)
    assert derive.derive_values([{"test": "P06", "from": "tripped", "map": {"false": "no", "true": "yes"}}], {"tripped": True})[0][0]["value"] == "yes"


def _with_derive(demo_copy, extra_action="", extra_test=""):
    f = demo_copy / "powered.yaml"
    s = f.read_text()
    s = s.replace("values: {P01: [22, 0.15, mA], P02: [5.0, 0.01, V], P03: [3.3, 0.01, V], P04: [14, 0.2, mA]}",
                  "values: {P01: [22, 0.15, mA], P02: [5.0, 0.01, V], P03: [3.3, 0.01, V], P04: [14, 0.2, mA]}\n              analyse: {trip_above: {P01: 40}}")
    s = s.replace("            proposes: [P01, P02, P03, P04]\n", "            proposes: [P01, P02, P03, P04]\n" + extra_action, 1)
    s = s.rstrip("\n") + "\n" + extra_test
    f.write_text(s)
    return load(demo_copy / "demo.pcbv.yaml")


DERIVE = "            derive:\n              - {test: P06, from: analysis.within_limit, map: {true: \"yes\", false: \"no\"}}\n"
P06 = "          - {id: P06, title: \"Current stayed under the limit all the way up\", kind: yn, limit: {class: record}, inputs: [keyboard, action]}\n"


def test_derive_rule_joins_proposes_and_lints(demo_copy):
    p = _with_derive(demo_copy, DERIVE, P06)
    assert not p.errors, [e.msg for e in p.errors]
    act = next(a for s in p.data["steps"] if s["id"] == "powered" for sec in s["sections"] for a in sec.get("actions", []) if a["id"] == "read_rails")
    assert "P06" in act["proposes"]


def test_derive_rule_errors(demo_copy):
    p = _with_derive(demo_copy, "            derive:\n              - {test: P06}\n              - {test: PZZ, from: analysis.tripped}\n", P06)
    msgs = " ".join(e.msg for e in p.errors)
    assert "needs test and from" in msgs and "unknown test 'PZZ'" in msgs


def test_lint_warns_on_a_measured_question_that_nothing_derives(demo_copy):
    q = "          - {id: P07, title: \"Supply current stayed under the limit, no protection trip\", kind: yn, limit: {class: record}}\n" \
        "          - {id: P08, title: \"Nothing smells hot\", kind: yn, limit: {class: record}}\n"
    f = demo_copy / "powered.yaml"
    f.write_text(f.read_text().rstrip("\n") + "\n" + q)
    p = load(demo_copy / "demo.pcbv.yaml")
    w = [x.msg for x in p.warnings]
    # the demo's actions are demo.*: the heuristic watches instrument plugins (n6705b., pcan., meter.), so no warning here
    assert not any("P07" in m for m in w)
    for a in [x for s in p.data["steps"] if s["id"] == "powered" for sec in s["sections"] for x in sec.get("actions", [])]:
        a["plugin"] = "n6705b.first_power"
    from polybench import protocol
    sec = next(sec for s in p.data["steps"] if s["id"] == "powered" for sec in s["sections"] if sec["id"] == "first_power")
    got = []
    protocol._lint_derivable(None, sec, [t for t in sec["tests"] if t["id"] in ("P07", "P08")], lambda n, k, m, sev="error": got.append((n["id"], m)))
    assert [g[0] for g in got] == ["P07"] and "derive" in got[0][1]      # smell and heat stay questions


# ---- the runner stores the analysis, derives, and the explorer reads it back (PW-3, PW-6)
def test_action_analysis_derives_a_test_and_feeds_the_explorer(tmp_path, demo_copy):
    from polybench.server import Session
    proto = _with_derive(demo_copy, DERIVE, P06)
    sess = Session(proto, tmp_path, "pw", True, 3, None)
    e = sess.engine
    for b in ("S01", "S02"):
        e.override_gate(b, "powered", "test")

    async def go(board):
        await sess.runner.start("powered", "first_power", "read_rails", board)
        for _ in range(150):
            await asyncio.sleep(0.1)
            if not sess.runner.current:
                break
        assert sess.runner.current is None

    asyncio.run(go("S01"))
    c = e.state.cell("S01", "P06")
    assert c["status"] == "accepted" and c["value"] == "yes" and c["source"]["kind"] == "action" and "within_limit" in c["raw"]
    run_dir = next((tmp_path / "pw" / "runs").glob("0001_S01_*"))
    m = json.loads((run_dir / "measurements.json").read_text())
    assert m["analysis"]["within_limit"] is True and any(s["test"] == "P01" for s in m["samples"]) and m["derived"][0]["test"] == "P06"
    asyncio.run(go("S02"))             # S02's P04 is forced to 31 mA here, but the trip rule watches P01 (22 +- 15 %, below 40): still no trip
    out = measurements.collect(e, tmp_path / "pw", "S01", "powered")
    kinds = {r["kind"] for r in out["rows"]}
    assert {"sample", "derived", "analysis", "proposal"} <= kinds
    drow = next(r for r in out["rows"] if r["kind"] == "derived")
    assert drow["test"] == "P06" and drow["value"] == "yes" and "analysis.within_limit" in drow["evidence"]
    s01 = next(r for r in out["rows"] if r["kind"] == "sample" and r["test"] == "P01")
    assert s01["unit"] == "mA" and s01["run"] == 1 and s01["ts"]
    assert all(r["run"] == 1 for r in out["rows"]), "rows of S02's run must not appear on S01"


def test_explorer_endpoint_is_read_only_and_scoped(tmp_path, demo_copy):
    from fastapi.testclient import TestClient
    from polybench.server import Session, create_app
    proto = load(DEMO / "demo.pcbv.yaml")
    sess = Session(proto, tmp_path, "ex", True, 2, None)
    with TestClient(create_app(sess)) as c:
        r = c.get("/api/measurements", params={"board": "S01", "step": "powered"})
        assert r.status_code == 200 and r.json()["rows"] == []
        assert c.get("/api/measurements", params={"board": "S01", "step": "nope"}).json()["rows"] == []
    assert sess.engine.state.cell("S01", "P01")["status"] == "empty"
