# Ported from the voice proof of concept. Changes for Polybench: imports; the PoC Flask-server accept routes in the first LLM test dropped.
"""LLM extraction: the validator on the model's list of cell updates, the pending/accept flow, the fast path, the eval harness.
No Ollama needed except for the PRG_LLM=1 test at the end."""
import json
import os

import pytest

from polybench.voice import extract, llm
from poc_engine import Engine
import eval_extract as ev


@pytest.fixture
def smoke(tmp_path):
    return ev.make_session(str(tmp_path))                  # DEMO smoke-test sheet with a Notes column added


@pytest.fixture
def smoke_nonotes(tmp_path):
    return ev.make_session(str(tmp_path), notes=False)    # the sheet as it is in the vault


def U(id, boards, kind="none", **kw):
    return {"id": id, "boards": boards, "kind": kind, "number": kw.get("number", ""), "unit": kw.get("unit", ""),
            "unit_heard": kw.get("heard", ""), "text": kw.get("text", ""), "verdict": kw.get("verdict", "none"), "note": kw.get("note", "")}


def run(s, said, updates, command="none", row=None, board=None):
    if row:
        s.ctx.row = next(i for i, r in enumerate(s.script.rows) if r["id"] == row)
    s.ctx.board = board
    return extract.resolve({"command": command, "updates": updates}, said, s.script, s.ctx, s.effective, s.recent_numbers())


def vals(res):
    return {f"{c['id']} {c['board'] or c['column']}": c["value"] for c in res["cells"]}


# ---------------------------------------------------------------- multi-cell schema
def test_all_apart_from_b3_mixed_values_note_and_computed_fail(smoke):
    said = "C05 measured on diode. All boards open load apart from B3 showed one volt. Traced a short on the power IC of the back."
    res = run(smoke, said, [U("C05", [1, 2, 4, 5], "ol"), U("C05", [3], "number", number="1", unit="V", note="short on the power IC at the back")])
    assert vals(res) == {"C05 B1": "OL", "C05 B2": "OL", "C05 B4": "OL", "C05 B5": "OL", "C05 B3": "1", "C05 Pass/fail": "F3",
                         "C05 Notes": "B3: short on the power IC at the back"}
    assert not res["rejected"]


def test_range_and_list_of_boards(smoke):
    res = run(smoke, "V07a OK for B1 to B4", [U("V07a", [1, 2, 3, 4], "ok")])
    assert sorted(vals(res)) == [f"V07a B{i}" for i in range(1, 5)]
    res = run(smoke, "V07a OK on boards two and five", [U("V07a", [2, 5], "ok")])
    assert sorted(vals(res)) == ["V07a B2", "V07a B5"]


def test_verdict_pass_applies_to_pass_fail_cell(smoke):
    res = run(smoke, "C06 all boards OL pass", [U("C06", [1, 2, 3, 4, 5], "ol", verdict="pass")])
    assert vals(res)["C06 Pass/fail"] == "P" and len(res["cells"]) == 6


def test_pass_cannot_overrule_a_failing_number(smoke):
    res = run(smoke, "C05 board three one volt, pass", [U("C05", [3], "number", number="1", unit="V", verdict="pass")])
    assert vals(res)["C05 Pass/fail"] == "F3"
    assert any("said pass" in c for c in res["corrections"])


def test_fail_verdict_not_marked_when_numbers_meet_criterion(smoke):
    res = run(smoke, "PV05 board two three point three volts, fail", [U("PV05", [2], "number", number="3.3", unit="V", verdict="fail")])
    assert "PV05 Pass/fail" not in vals(res) and any("meets the criterion" in c for c in res["corrections"])


def test_fail_with_board_numbers_on_a_text_row(smoke):
    res = run(smoke, "V04 board five bridge on pin four, fail", [U("V04", [5], "text", text="bridge on pin 4", verdict="fail")])
    assert vals(res) == {"V04 B5": "bridge on pin 4", "V04 Pass/fail": "F5"}


def test_numeric_criteria_per_row(smoke):
    f = lambda rid, said, n, u: vals(run(smoke, said, [U(rid, [2], "number", number=n, unit=u)])).get(f"{rid} Pass/fail")
    assert f("PV12", "PV12 B2 3.4 volts", "3.4", "V") == "F2"
    assert f("PV12", "PV12 B2 3.3 volts", "3.3", "V") is None
    assert f("PI12", "PI12 B2 61 milliamps", "61", "mA") == "F2"          # "below 61"
    assert f("C07", "C07 B2 800 ohms", "800", "ohm") == "F2"              # below 1 kOhm is a defect
    assert f("C08", "C08 B2 zero volts", "0", "V") == "F2"                # 0.00 V is a short
    assert f("C08", "C08 B2 five hundred millivolts", "500", "mV") is None  # 0.3 to 1.3 V, a guess limit: never auto-fail
    assert f("C05", "C05 B2 one volt", "1", "V") == "F2"                  # pass criterion is OL
    assert f("C06", "C06 B2 point nine volts", "0.9", "V") == "F2"        # 0.8 to 1.0 V: D2 reversed
    assert f("C04", "C04 B2 two megaohms", "2", "Mohm") is None


def test_corrected_value_clears_a_stale_fail(smoke):
    key = next(c["key"] for c in smoke.script.cells if c["id"] == "C05" and c["column"] == "B2")
    pf = next(c["key"] for c in smoke.script.cells if c["id"] == "C05" and c["column"] == "Pass/fail")
    smoke._add(key, "0.8", "voice")
    smoke._add(pf, "F2", "voice")
    res = run(smoke, "no, B2 was open load", [U("C05", [2], "ol")], row="C05")
    assert vals(res).get("C05 B2") == "OL" and any("recomputed" in c for c in res["corrections"])


# ---------------------------------------------------------------- unit slips
def test_whisper_unit_slip_is_corrected_against_the_row_and_recorded(smoke):
    res = run(smoke, "C04 around 2 megahertz on all five boards", [U("C04", [1, 2, 3, 4, 5], "number", number="2", unit="MHz", heard="megahertz")])
    assert vals(res)["C04 B1"] == "2000000" and "MΩ" in res["corrections"][0] and "megahertz" in res["corrections"][0]
    # the model got the dimension right but the wrong prefix: the word that was said decides the prefix
    res = run(smoke, "C07 board two three megahertz", [U("C07", [2], "number", number="3", unit="kohm", heard="megahertz")])
    assert vals(res)["C07 B2"] == "3000000"
    res = run(smoke, "PI05 B1 forty two milliohms", [U("PI05", [1], "number", number="42", unit="mohm", heard="milliohms")])
    assert vals(res)["PI05 B1"] == "42" and res["corrections"]


def test_incompatible_unit_is_rejected_not_slipped(smoke):
    res = run(smoke, "C05 board one three ohms", [U("C05", [1], "number", number="3", unit="ohm", heard="ohms")])
    assert not res["ok"] and "not compatible" in res["rejected"][0]["reason"]


# ---------------------------------------------------------------- the validator is the safety net
def test_board_not_mentioned_is_rejected(smoke):
    res = run(smoke, "C06 B1 and B2 open load", [U("C06", [1, 2, 3], "ol")])
    assert sorted(vals(res)) == ["C06 B1", "C06 B2"]
    assert res["rejected"][0]["board"] == "B3" and "not mentioned" in res["rejected"][0]["reason"]


def test_no_board_spoken_uses_current_board_only(smoke):
    res = run(smoke, "open load", [U("C06", [1, 2], "ol")], row="C06", board="B2")
    assert list(vals(res)) == ["C06 B2"] and "not mentioned" in res["rejected"][0]["reason"]
    res = run(smoke, "open load", [U("C06", [], "ol")], row="C06", board="B2")
    assert list(vals(res)) == ["C06 B2"]
    res = run(smoke, "open load", [U("C06", [], "ol")], row="C06", board=None)
    assert not res["ok"] and "which board" in res["rejected"][0]["reason"]


def test_invented_number_is_rejected(smoke):
    res = run(smoke, "C07 board two high", [U("C07", [2], "number", number="5", unit="kohm")])
    assert not res["ok"] and "not in the transcript" in res["rejected"][0]["reason"]
    res = run(smoke, "C07 board two four point seven k", [U("C07", [2], "number", number="4.7", unit="kohm")])
    assert vals(res) == {"C07 B2": "4700"}


def test_pre_converted_number_is_still_licensed(smoke):
    res = run(smoke, "PV05 board one 3300 millivolts", [U("PV05", [1], "number", number="3.3", unit="V")])
    assert vals(res) == {"PV05 B1": "3.3"}


def test_ol_must_have_been_said(smoke):
    res = run(smoke, "C06 board two high", [U("C06", [2], "ol")])
    assert not res["ok"] and "OL was not said" in res["rejected"][0]["reason"]


def test_unknown_id_nonexistent_board_and_garbage(smoke):
    res = run(smoke, "see ninety nine B1 3", [U("C99", [1], "number", number="3")])
    assert not res["ok"] and "C99" in res["rejected"][0]["reason"]
    res = run(smoke, "C06 board nine open load", [U("C06", [9], "ol")])
    assert not res["ok"] and "does not exist" in res["rejected"][0]["reason"]
    res = run(smoke, "C07 board one twelve", [U("C07", [1], "number", number="twelve")])
    assert not res["ok"] and "plain decimal" in res["rejected"][0]["reason"]
    res = run(smoke, "C07 board one 3", [U("C07", [1], "number", number="3", unit="parsec")])
    assert not res["ok"] and "not recognised" in res["rejected"][0]["reason"]
    assert not run(smoke, "uh hello", [], command="none")["ok"]


def test_insane_value_is_rejected(smoke):
    res = run(smoke, "PV05 board one 500 volts", [U("PV05", [1], "number", number="500", unit="V")])
    assert not res["ok"] and "not a sane value" in res["rejected"][0]["reason"]


def test_verdict_words_must_have_been_said(smoke):
    res = run(smoke, "V04 OK on all boards", [U("V04", [1, 2, 3, 4, 5], "ok", verdict="pass")])
    assert "V04 Pass/fail" not in vals(res) and any("no pass word" in c for c in res["corrections"])
    res = run(smoke, "C07 board two five kilohms", [U("C07", [2], "number", number="5", unit="kohm", verdict="fail")])
    assert "C07 Pass/fail" not in vals(res)


def test_same_as_before_takes_numbers_from_the_row(smoke):
    key = next(c["key"] for c in smoke.script.cells if c["id"] == "C07" and c["column"] == "B1")
    smoke._add(key, "2200000", "voice")
    res = run(smoke, "same on the rest", [U("C07", [2, 3, 4, 5], "number", number="2.2", unit="Mohm")], row="C07")
    assert len(vals(res)) == 4 and set(vals(res).values()) == {"2200000"}
    res = run(smoke, "C07 on the rest", [U("C07", [2, 3, 4, 5], "number", number="2.2", unit="Mohm")], row="C07")
    assert not res["ok"]            # without 'same', a number the technician did not say is refused


def test_notes_need_a_notes_column_and_restating_notes_are_dropped(smoke, smoke_nonotes):
    res = run(smoke_nonotes, "C05 board three traced a short", [U("C05", [3], "none", note="traced a short on the IC")])
    assert not res["ok"] and res["unplaced_notes"][0]["reason"].endswith("has no Notes column")
    res = run(smoke, "AOI for all boards have passed and cleared", [U("AOI_clear", [1, 2, 3, 4, 5], "yes", note="passed and cleared")])
    assert "AOI_clear Notes" not in vals(res)
    res = run(smoke, "V04 board five bridge on pin four", [U("V04", [5], "text", text="bridge on pin 4", note="bridge on pin 4")])
    assert "V04 Notes" not in vals(res)


def test_commands_and_command_with_updates(smoke):
    assert run(smoke, "skip this one", [], command="skip")["command"] == "skip"
    assert run(smoke, "previous", [], command="previous")["command"] == "back"
    res = run(smoke, "C06 B1 OL next", [U("C06", [1], "ol")], command="next")
    assert res["command"] is None and vals(res) == {"C06 B1": "OL"}


# ---------------------------------------------------------------- engine: pending proposals, trace, accept per utterance
@pytest.fixture
def eng(smoke):
    e = Engine(smoke, "simulator", "x", False, "x", [], False, no_stt=True)
    e.use_llm = True
    return e


def test_llm_utterance_arrives_pending_and_flagged_with_full_trace(smoke, eng, monkeypatch):
    seen = {}
    raw = {"command": "none", "updates": [U("C05", [1, 2, 4, 5], "ol"), U("C05", [3], "number", number="1", unit="V", note="short on the power IC")]}

    def fake(text, script, state, model):
        seen.update(text=text, state=state)
        return raw
    monkeypatch.setattr(llm, "extract", fake)
    said = "C05 measured on diode. All boards open load apart from B3 showed one volt. Traced a short on the power IC of the back."
    eng.handle_text(said, source="voice")
    assert seen["state"]["row_id"] == smoke.script.rows[smoke.ctx.row]["id"] or smoke.ctx.row is not None
    ps = [p for p in smoke.proposals.values()]
    assert {p["board"]: p["value"] for p in ps} == {"B1": "OL", "B2": "OL", "B4": "OL", "B5": "OL", "B3": "1", "Pass/fail": "F3", "Notes": "B3: short on the power IC"}
    assert all(p["state"] == "pending" and "interpreted by the LLM" in p["flag"] for p in ps)
    t = list(eng.traces)[-1]
    assert t["parser"] == "llm" and t["llm"]["raw"] == raw and t["llm"]["validation"]["cells"] and "sheet rows" in t["llm"]["prompt"]
    assert len(t["targets"]) == 7 and t["utt"] == 1
    # (PoC: the accept-all / per-cell / per-utterance HTTP routes of the PoC Flask server follow here; not ported, Polybench's engine owns accept)


def test_fast_path_does_not_call_the_llm_for_a_simple_utterance(smoke, eng, monkeypatch):
    monkeypatch.setattr(llm, "extract", lambda *a, **k: pytest.fail("LLM called for a simple utterance"))
    eng.handle_text("board two C07 four point seven k ohms", source="typed")
    t = list(eng.traces)[-1]
    assert t["parser"] == "grammar" and t["llm"] is None and t["targets"][0]["value"] == "4700"


def test_fast_path_also_computes_a_failing_pass_fail(smoke, eng, monkeypatch):
    monkeypatch.setattr(llm, "extract", lambda *a, **k: pytest.fail("LLM called"))
    eng.handle_text("board five PI05 one hundred and twelve milliamps", source="typed")
    ps = {p["board"]: p for p in smoke.proposals.values()}
    assert ps["B5"]["value"] == "112" and ps["Pass/fail"]["value"] == "F5" and "computed from Expected" in ps["Pass/fail"]["flag"]


def test_grammar_ok_but_validator_refuses_goes_to_the_llm(smoke, eng, monkeypatch):
    called = []
    monkeypatch.setattr(llm, "extract", lambda *a, **k: (called.append(1), {"command": "none", "updates": [U("C04", [2], "number", number="2", unit="Mohm", heard="megahertz")]})[1])
    eng.handle_text("board two C04 two megahertz", source="typed")
    assert called and list(smoke.proposals.values())[0]["value"] == "2000000"


def test_complex_utterance_goes_to_the_llm_even_if_grammar_parses_a_part(smoke, eng, monkeypatch):
    called = []
    monkeypatch.setattr(llm, "extract", lambda *a, **k: (called.append(1), {"command": "none", "updates": []})[1])
    eng.handle_text("board two C07 four k ohms except board three", source="typed")
    assert called
    t = list(eng.traces)[-1]
    assert not t["validator"]["ok"] and t["validator"]["verdict"].startswith("not understood")


def test_llm_down_falls_back_to_the_grammar_answer(smoke, eng, monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(llm, "extract", boom)
    eng.handle_text("board two C04 two megahertz", source="typed")      # grammar parses, binder refuses the unit, LLM is down
    t = list(eng.traces)[-1]
    assert not t["validator"]["ok"] and "not compatible" in t["validator"]["verdict"] and "unavailable" in t["llm"]["reason"]


def test_audit_log_keeps_the_extraction(smoke, eng, monkeypatch):
    monkeypatch.setattr(llm, "extract", lambda *a, **k: {"command": "none", "updates": [U("C06", [1, 2], "ol")]})
    eng.handle_text("C06 B1 and B2 open load, and also whatever", source="voice")
    recs = [json.loads(l) for l in open(smoke.audit_path, encoding="utf-8")]
    u = [r for r in recs if r["event"] == "utterance"][-1]
    assert u["route"] == "llm-extract" and len(u["extraction"]["cells"]) == 2 and u["llm_raw"]["updates"][0]["id"] == "C06"


# ---------------------------------------------------------------- eval harness
def test_eval_harness_scores_cells(monkeypatch):
    monkeypatch.setattr(llm, "extract", lambda *a, **k: {"command": "none", "updates": [U("C06", [1, 2], "ol")]})
    ok = ev.run_case({"id": "t", "said": "C06 B1 and B2 open load, whatever else", "expect": {"C06 B1": "OL", "C06 B2": "OL"}}, "x")
    assert ok["exact"] and ok["parser"] == "llm"
    bad = ev.run_case({"id": "t", "said": "C06 B1 and B2 open load, whatever else", "expect": {"C06 B1": "OL", "C06 B3": "OL", "C06 B2": {"re": "^O"}}}, "x")
    assert not bad["exact"] and bad["hits"] == {"C06 B1": True, "C06 B3": False, "C06 B2": True}
    extra = ev.run_case({"id": "t", "said": "C06 B1 and B2 open load, whatever else", "expect": {"C06 B1": "OL"}}, "x")
    assert not extra["exact"] and extra["extra"] == {"C06 B2": "OL"}


def test_eval_case_files_are_wellformed():
    for f in ("extract_cases.json", "extract_cases_heldout.json"):
        cases = json.load(open(os.path.join(os.path.dirname(__file__), "eval", f), encoding="utf-8"))
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids)) and all(c["said"] and "expect" in c for c in cases)


@pytest.mark.skipif(not os.environ.get("PRG_LLM"), reason="needs Ollama with gemma4:26b; set PRG_LLM=1")
def test_sentences_with_gemma4():
    cases = [c for c in json.load(open(os.path.join(os.path.dirname(__file__), "eval", "extract_cases.json"), encoding="utf-8"))]
    for c in cases:
        r = ev.run_case(c, llm.DEFAULT_MODEL)
        assert r["exact"], (c["id"], r["got"], r["expected"])


def test_ok_on_a_yn_row_means_yes(smoke):
    res = run(smoke, "AOI for all boards have passed and cleared", [U("AOI_clear", [1, 2, 3, 4, 5], "ok", text="passed and cleared", verdict="pass")])
    assert vals(res) == {**{f"AOI_clear B{i}": "Y" for i in range(1, 6)}, "AOI_clear Pass/fail": "P"}
    res = run(smoke, "V04 board one yes", [U("V04", [1], "yes")])
    assert vals(res) == {"V04 B1": "OK"}
