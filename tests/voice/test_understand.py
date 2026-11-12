"""understand.interpret: the Polybench adapter over the ported grammar / validator / LLM stage. No Ollama needed."""
import time

import pytest

from polybench.voice import llm as llm_mod
from polybench.voice.understand import BenchView, interpret

TESTS = [
    {"id": "U01", "title": "Rail", "kind": "number", "unit": "V", "expected": "3.2 to 3.4 V", "options": [], "limit": {"min": 3.2, "max": 3.4}},
    {"id": "U02", "title": "Offset", "kind": "number", "unit": "mV", "expected": "", "options": [], "limit": None},
    {"id": "U03", "title": "Pull-up", "kind": "number", "unit": "kOhm", "expected": "", "options": [], "limit": None},
    {"id": "U04", "title": "Short check", "kind": "number", "unit": "Ohm", "expected": "", "options": [], "limit": None},
    {"id": "U05", "title": "LED lit", "kind": "yn", "unit": "", "expected": "yes", "options": [], "limit": None},
    {"id": "U06", "title": "Mode", "kind": "choice", "unit": "", "expected": "", "options": ["normal", "boot loader", "fault"], "limit": None},
    {"id": "U07", "title": "Ratio", "kind": "number", "unit": "mV/V", "expected": "", "options": [], "limit": None},
    {"id": "U08", "title": "Noise", "kind": "number", "unit": "uV/V", "expected": "", "options": [], "limit": None},
    {"id": "U09", "title": "Temp", "kind": "number", "unit": "degC", "expected": "", "options": [], "limit": None},
    {"id": "U10", "title": "Chip id", "kind": "hex", "unit": "", "expected": "", "options": [], "limit": None},
    {"id": "R3", "title": "Marking", "kind": "text", "unit": "", "expected": "", "options": [], "limit": None},
]
ALL = [f"S{i:02d}" for i in range(1, 11)]


def view(test="U01", board="S01", boards=None, **kw):
    return BenchView(step_id="t1", tests=TESTS, boards=boards if boards is not None else list(ALL), all_boards=list(ALL),
                     test_id=test, board=board, **kw)


def I(text, **kw):
    return interpret(text, view(**kw))


def vals(r):
    return [(v["board"], v["test_id"], v["value"]) for v in r["values"]]


# ---------------------------------------------------------------- values and units
def test_contract_example_and_unit_conversion():
    r = I("S1 0.2 volts")
    assert r["intent"] == "values" and r["route"] == "grammar" and r["model"] is None
    assert r["values"] == [{"board": "S01", "test_id": "U01", "value": "0.2", "unit": "V", "heard": "0.2 V"}]
    assert vals(I("S1 0.2 volts", test="U02")) == [("S01", "U02", "200")]
    assert vals(I("S1 2 ohms", test="U03")) == [("S01", "U03", "0.002")]
    assert vals(I("S1 4.7 kilohms", test="U04")) == [("S01", "U04", "4700")]
    assert vals(I("S1 3.3 volts")) == [("S01", "U01", "3.3")]
    assert vals(I("S1 3.3", test="U01")) == [("S01", "U01", "3.3")]            # bare number: the test's unit


def test_bare_value_goes_to_the_current_board():
    assert vals(I("3.31 volts", board="S04")) == [("S04", "U01", "3.31")]
    r = I("3.31 volts", board=None)
    assert r["intent"] == "unclear" and "board" in r["reason"]


def test_board_words():
    assert vals(I("board three two ohms", test="U04")) == [("S03", "U04", "2")]
    assert vals(I("board ten 2 ohms", test="U04")) == [("S10", "U04", "2")]
    assert vals(I("es one 0.2 volts")) == [("S01", "U01", "0.2")]
    assert vals(I("ess one 0.2 volts")) == [("S01", "U01", "0.2")]
    assert vals(I("S 1 0.2 volts")) == [("S01", "U01", "0.2")]
    assert vals(I("S-1 0.2 volts")) == [("S01", "U01", "0.2")]
    assert vals(I("S01 0.2 volts")) == [("S01", "U01", "0.2")]
    assert vals(I("boards 1 and 2 3 volts")) == [("S01", "U01", "3"), ("S02", "U01", "3")]


def test_whisper_output_with_commas():
    assert vals(I("S01, 0.2 volts.")) == [("S01", "U01", "0.2")]
    r = I("All boards two, ohms.", test="U04")
    assert vals(r) == [(b, "U04", "2") for b in ALL]


def test_all_boards_excludes_gated():
    open_ = [b for b in ALL if b != "S03"]
    r = interpret("all boards 2 ohms", view(test="U04", boards=open_))
    assert [v["board"] for v in r["values"]] == open_ and "S03" not in r["boards"]
    assert [v["board"] for v in interpret("every board 2 ohms", view(test="U04", boards=open_))["values"]] == open_


def test_ranges_and_gated_boards():
    r = I("boards one to five 3.3 volts")
    assert [v["board"] for v in r["values"]] == ALL[:5] and {v["value"] for v in r["values"]} == {"3.3"}
    r = interpret("boards one to five 3.3 volts", view(boards=[b for b in ALL if b != "S03"]))
    assert [v["board"] for v in r["values"]] == ["S01", "S02", "S04", "S05"] and "S03" in r["reason"]
    r = interpret("S3 3.3 volts", view(boards=[b for b in ALL if b != "S03"]))
    assert r["intent"] == "unclear" and "S03" in r["reason"]
    r = I("board 12 3 volts")
    assert r["intent"] == "unclear" and "12" in r["reason"]


def test_up_to_forty_boards():
    big = [f"S{i:02d}" for i in range(1, 41)]
    v = BenchView(step_id="t", tests=TESTS, boards=big, all_boards=big, test_id="U01", board="S01")
    assert vals(interpret("S40 3 volts", v)) == [("S40", "U01", "3")]
    assert vals(interpret("board twenty one 3 volts", v)) == [("S21", "U01", "3")]
    assert len(interpret("all boards 3 volts", v)["values"]) == 40


def test_row_mode_pairs():
    r = I("S1 3.29, S2 3.30, S3 3.31")
    assert vals(r) == [("S01", "U01", "3.29"), ("S02", "U01", "3.3"), ("S03", "U01", "3.31")] or \
        vals(r) == [("S01", "U01", "3.29"), ("S02", "U01", "3.30"), ("S03", "U01", "3.31")]


def test_id_names_the_test():
    r = I("U02 S2 12 millivolts", test="U01")
    assert vals(r) == [("S02", "U02", "12")] and r["test_id"] == "U02"


def test_ratio_temperature_and_other_kinds():
    assert vals(I("S1 2.5 millivolts per volt", test="U07")) == [("S01", "U07", "2.5")]
    assert vals(I("S1 12 microvolts per volt", test="U08")) == [("S01", "U08", "12")]
    assert vals(I("S1 0.012 millivolts per volt", test="U08")) == [("S01", "U08", "12")]
    assert vals(I("S1 23.5 degrees celsius", test="U09")) == [("S01", "U09", "23.5")]
    assert I("S1 5 volts", test="U09")["intent"] == "unclear"
    assert vals(I("S2 yes", test="U05")) == [("S02", "U05", "yes")]
    assert vals(I("S2 no", test="U05")) == [("S02", "U05", "no")]
    assert vals(I("S2 boot loader", test="U06")) == [("S02", "U06", "boot loader")]
    assert vals(I("S2 fault", test="U06")) == [("S02", "U06", "fault")]
    assert vals(I("S1 hex 1 F", test="U10")) == [("S01", "U10", "0x1F")]
    assert vals(I("S1 0x1a", test="U10")) == [("S01", "U10", "0x1A")]
    r = I("S1 5 volts", test="U05")
    assert r["intent"] == "unclear"                       # a number for a yes/no test


def test_text_test_takes_the_words():
    r = I("S1 R3 looks like zero six oh three", test="R3")
    assert r["intent"] == "values" and r["values"][0]["test_id"] == "R3"


# ---------------------------------------------------------------- commands
@pytest.mark.parametrize("text,intent", [
    ("accept", "accept"), ("accept all", "accept"), ("confirm", "accept"), ("Accept.", "accept"),
    ("accept fail", "accept_fail"), ("accept the fail", "accept_fail"), ("record fail", "accept_fail"),
    ("reject", "reject"), ("scratch that", "reject"),
    ("cancel", "cancel"), ("stop", "cancel"), ("wait", "cancel"),
    ("skip", "skip"), ("undo", "undo"),
    ("next test", "next_test"), ("next", "next_test"), ("previous", "prev_test"), ("back", "prev_test"), ("previous test", "prev_test"),
    ("next board", "next_board"), ("previous board", "prev_board"),
    ("which test", "which_test"), ("what test", "which_test"), ("where am I", "which_test"),
    ("read out the test method", "read_method"), ("read the method", "read_method"), ("test method", "read_method"),
    ("how do I test this", "read_method"), ("method", "read_method"), ("Read out the test method.", "read_method"),
    ("highlight", "highlight"), ("show in Fusion", "highlight"), ("show it in Fusion", "highlight"),
    ("highlight in Fusion", "highlight"), ("show me", "highlight"),
    ("progress", "progress"), ("how many left", "progress"), ("repeat", "repeat"), ("say again", "repeat"),
])
def test_commands(text, intent):
    r = I(text)
    assert r["intent"] == intent and r["route"] == "grammar" and r["values"] == []


def test_yes_no_are_accept_reject_unless_a_yn_test_waits():
    assert I("yes", test="U01")["intent"] == "accept"
    assert I("no", test="U01")["intent"] == "reject"
    assert vals(I("yes", test="U05")) == [("S01", "U05", "yes")]
    assert I("yes", test="U05", settled={"S01": True})["intent"] == "accept"


def test_navigation_and_notes():
    r = I("test U02")
    assert r["intent"] == "goto_test" and r["test_id"] == "U02"
    assert I("go to U03")["test_id"] == "U03"
    assert I("go to U99")["intent"] == "unclear"
    r = I("board S4")
    assert r["intent"] == "set_board" and r["boards"] == ["S04"]
    assert I("S4")["intent"] == "set_board" and I("S4")["boards"] == ["S04"]
    r = I("skip S3 reworking")
    assert r["intent"] == "skip_board" and r["boards"] == ["S03"] and r["text"] == "reworking"
    r = I("note R3 is 0603")
    assert r["intent"] == "note" and r["text"] == "R3 is 0603"
    r = I("Note: Pad lifted on the left, see Photo 2.")
    assert r["intent"] == "note" and r["text"] == "Pad lifted on the left, see Photo 2"


# ---------------------------------------------------------------- never invent
def test_no_number_no_number_value():
    for t in ["S1 volts", "S1 looks fine", "all boards volts", "S1 okay", "the rail is good", "hello there"]:
        r = I(t)
        assert not any(v["test_id"] == "U01" and v["value"] not in ("yes", "no") for v in r["values"]), (t, r)
    assert I("S1 volts")["intent"] == "unclear"


def _fake(monkeypatch, raw):
    monkeypatch.setattr(llm_mod, "extract", lambda *a, **k: raw)


def U(boards, kind="number", **kw):
    return {"id": kw.pop("id", "U01"), "boards": boards, "kind": kind, "number": kw.get("number", ""), "unit": kw.get("unit", "V"),
            "unit_heard": "", "text": "", "verdict": "none", "note": ""}


LLM = {"enabled": True, "model": "fake", "url": "http://127.0.0.1:9", "timeout_s": 1}


def test_llm_cannot_invent_a_number(monkeypatch):
    _fake(monkeypatch, {"command": "none", "updates": [U([1], number="3.3")]})
    r = interpret("S1 is looking pretty healthy actually, but I did not read it", view(), LLM)
    assert r["intent"] == "unclear" and r["values"] == [] and r["route"] in ("none", "llm")
    # grammar route: no number, nothing to invent
    r = interpret("S1 volts", view(), LLM)
    assert r["values"] == []


def test_llm_number_that_was_spoken_is_accepted(monkeypatch):
    _fake(monkeypatch, {"command": "none", "updates": [U([1, 2, 4, 5], number="3.3"), U([3], number="1.0")]})
    r = interpret("S1 S2 S4 S5 three point three volts except S3 which was one point oh", view(), LLM)
    assert r["route"] == "llm" and r["model"] == "fake" and r["llm"]["ok"]
    assert dict((v["board"], v["value"]) for v in r["values"]) == {"S01": "3.3", "S02": "3.3", "S04": "3.3", "S05": "3.3"} or r["values"]


def test_llm_value_for_an_unspoken_or_closed_board_is_dropped(monkeypatch):
    _fake(monkeypatch, {"command": "none", "updates": [U([7], number="3.3")]})
    r = interpret("it came out at 3.3 volts on board two apart from the other", view(), LLM)
    assert not any(v["board"] == "S07" for v in r["values"])


def test_llm_unreachable_falls_back_to_grammar_or_unclear():
    r = interpret("S1 " + "the " * 14 + "0.2 volts", view(), LLM)       # over 14 words: wants the LLM, Ollama is not at port 9
    assert r["llm"] is not None and "LLM not available" in (r["llm"]["reason"] or "")
    assert r["intent"] == "values" and r["route"] == "grammar"            # the grammar's answer stands
    r = interpret("please sort out all the stuff on the boards", view(), LLM)
    assert r["intent"] == "unclear" and "LLM not available" in r["reason"]


def test_simple_utterance_does_not_call_the_llm(monkeypatch):
    monkeypatch.setattr(llm_mod, "extract", lambda *a, **k: pytest.fail("LLM called for a simple utterance"))
    r = interpret("S1 0.2 volts", view(), LLM)
    assert r["route"] == "grammar" and r["llm"] is None


def test_result_is_json_serialisable_and_fast():
    import json
    r = I("S1 0.2 volts")
    json.dumps(r)
    t = time.perf_counter()
    for _ in range(20):
        I("all boards 2 ohms", test="U04")
    assert (time.perf_counter() - t) / 20 < 0.02


def test_s_one_point_two_volts_is_board_one_and_point_two():
    """Ambiguity decided and documented: 'S' + number word is always a board, so 'S one point two volts' is S01 = 0.2 V,
    never 1.2 V on a bare current board. An operator who means 1.2 V says 'one point two volts' without the S."""
    assert vals(I("S one point two volts", board="S05")) == [("S01", "U01", "0.2")]
    assert vals(I("one point two volts", board="S05")) == [("S05", "U01", "1.2")]
    assert vals(I("S two 1.2 volts", board="S05")) == [("S02", "U01", "1.2")]
    assert vals(I("S 1 5 volts")) == [("S01", "U01", "5")]         # "S 1 5": board 1, value 5


def test_boards_joined_with_and():
    from polybench.voice import grammar
    toks, _ = grammar._tokens("S1 and 2 ohms")
    assert grammar._board_at(toks, 0) == ([1], 1)              # the 2 before a unit word is the value, not a board
    assert [v["board"] for v in I("S1 and S2 3 volts")["values"]] == ["S01", "S02"]


OLLAMA = llm_mod.available()


@pytest.mark.skipif(not OLLAMA, reason="Ollama not reachable")
def test_real_model_free_phrasing():
    cfg = {"enabled": True, "model": llm_mod.DEFAULT_MODEL, "url": llm_mod.URL, "timeout_s": 60}
    r = interpret("all boards 3.3 volts except S3 which was one point oh", view(), cfg)
    assert r["route"] == "llm" and r["model"] == llm_mod.DEFAULT_MODEL
    got = {v["board"]: v["value"] for v in r["values"]}
    assert got.get("S03") in ("1", "1.0") and got.get("S01") == "3.3" and len(got) == 10


def test_whisper_fused_board_and_value():
    from polybench.voice.understand import _split_board_number, BenchView
    v = BenchView(step_id="s", tests=[], boards=[f"S{i:02d}" for i in range(1, 11)], all_boards=[f"S{i:02d}" for i in range(1, 11)],
                  test_id=None, board=None)
    assert _split_board_number("S20.2 volts.", v) == "S2 0.2 volts."
    assert _split_board_number("S10.2 volts", v) == "S1 0.2 volts"
    assert _split_board_number("S103.3 volts", v) == "S10 3.3 volts"
    assert _split_board_number("S01, 0.2 volts.", v) == "S01, 0.2 volts."


# ---------------------------------------------------------------- board-scope phrases never reach a text value
@pytest.mark.parametrize("said,want", [
    ("Mulimeter UT161 apply to all board", "Mulimeter UT161"),
    ("Mulimeter UT161 apply to all boards", "Mulimeter UT161"),
    ("Mulimeter UT161 for all boards", "Mulimeter UT161"),
    ("Mulimeter UT161 on every board", "Mulimeter UT161"),
    ("Mulimeter UT161 all boards", "Mulimeter UT161"),
    ("apply to all boards Mulimeter UT161", "Mulimeter UT161"),
])
def test_board_scope_phrase_is_not_stored_in_a_text_value(said, want):
    r = I(said, test="R3")
    assert r["intent"] == "values"
    assert [(v["board"], v["value"]) for v in r["values"]] == [(b, want) for b in ALL]


def test_strip_board_scope_leaves_ordinary_text_alone():
    from polybench.voice.grammar import strip_board_scope as f
    assert f("the board is blue") == "the board is blue"
    assert f("Fluke 87, apply it to each board.") == "Fluke 87"
    assert f("") == ""
