"""Voice range confirmation (an utterance not understood in a number test asks "Is the reading ... on <board>?") and the
next-test accept flow. Host only, silent (conftest: tts log, fake recorder)."""
import time

import pytest

from polybench.engine import Engine
from polybench.protocol import load
from polybench.store import Journal
from polybench import tiers
from polybench.report import build_model, to_markdown

from conftest import make_session
from helpers import DEMO
from test_controller import cell, vals, wait


@pytest.fixture(autouse=True)
def commands(nlu):
    nlu.script.update({"accept": {"intent": "accept"}, "next test": {"intent": "next_test"}, "reject": {"intent": "reject"}})


def ask(vc, test="U01", board="S01"):
    vc.set_context("unpowered", test, board)
    e = vc.handle_text("mumble mumble")
    assert e["outcome"] == "question" and vc.question
    return e


def test_question_text_uses_the_limit_and_board(vc):
    assert ask(vc)["reply"] == "Is the reading at least 10 kilo ohms on S1?"
    assert ask(vc, "U02", "S02")["reply"] == "Is the reading at least 2 kilo ohms on S2?"


def test_yes_current_board_records_range_not_a_number(vc):
    ask(vc)
    e = vc.handle_text("yes")
    assert e["outcome"] == "proposed" and e["reply"].startswith("S1 in range, no reading.") and "Say next test to accept, or reject." in e["reply"]
    p = list(cell(vc, "S01")["proposals"].values())[0]
    assert p["value"] == "IN RANGE" and p["origin"] == "operator_confirmed" and p["verdict"] == "pass"
    vc.handle_text("accept")
    c = cell(vc, "S01")
    assert c["status"] == "accepted" and c["value"] == "IN RANGE" and c["origin"] == "operator_confirmed" and c["verdict"] == "pass"
    ev = [x for x in vc.eng.journal.events if x["type"] == "accept"][-1]["payload"]
    assert ev["via"] == "voice_range" and ev["question"] == "Is the reading at least 10 kilo ohms on S1?" and ev["value"] == "IN RANGE"
    snap = vc.eng.state.snapshot()["cells"]["S01|U01"]
    assert snap["origin"] == "operator_confirmed" and snap["status"] == "accepted"


def test_range_value_is_not_a_number_anywhere_else(vc):
    ask(vc)
    vc.handle_text("yes")
    vc.handle_text("accept")
    assert tiers._num(cell(vc, "S01")["value"]) is None
    t = vc.eng.state.test_index["U01"][2]
    r = tiers.similarity(t, {"S01": "IN RANGE", "S02": "20", "S03": "21", "S04": "22"})
    assert r is None or "S01" not in r["flagged"]
    assert "U01" not in tiers.similarity_all(vc.eng.state) or "S01" not in tiers.similarity_all(vc.eng.state)["U01"]["flagged"]
    md = to_markdown(build_model(vc.eng.state))
    assert "confirmed in range, no reading" in md and "Confirmed in range, no reading" in md
    with pytest.raises(Exception):
        vc.eng.entry("S02", "U01", "IN RANGE")                      # never typed in


def test_yes_all_boards_covers_every_empty_applicable_board(vc, nlu):
    nlu.script["s2"] = vals(("S02", "20", "kOhm"))
    vc.handle_text("s2")                                              # S02 already holds a proposal: not empty
    ask(vc)
    e = vc.handle_text("yes all boards")
    assert cell(vc, "S01")["proposals"] and not cell(vc, "S03")["proposals"]          # S03 is gated
    assert list(cell(vc, "S02")["proposals"].values())[0]["value"] == "20"
    assert e["reply"].startswith("S1 in range")
    ask(vc, "U02", "S01")
    vc.handle_text("yes for all")
    assert [b for b in ("S01", "S02", "S03") if cell(vc, b, "U02")["proposals"]] == ["S01", "S02"]
    assert "2 boards in range, no reading" in vc.eng.state.voice[-1]["reply"]
    assert vc.handle_text("next test")["reply"].startswith("U03")
    assert cell(vc, "S01", "U02")["status"] == cell(vc, "S02", "U02")["status"] == "accepted"      # multi-board next accepts all


@pytest.mark.parametrize("said", ["yes board S2", "only S2", "yes S two", "Yes, S2."])
def test_named_board(vc, said):
    ask(vc)
    vc.handle_text(said)
    assert cell(vc, "S02")["proposals"] and not cell(vc, "S01")["proposals"]


def test_named_board_that_is_gated_is_refused(vc):
    ask(vc)
    e = vc.handle_text("only S3")
    assert e["outcome"] == "rejected" and "gated" in e["reply"] and not cell(vc, "S03")["proposals"]


def test_no_then_value_for_the_named_board(vc, nlu):
    ask(vc)
    e = vc.handle_text("no S2")
    assert e["reply"] == "Say the reading for S2." and e["outcome"] == "question"
    assert not cell(vc, "S02")["proposals"] and not cell(vc, "S01")["proposals"]        # a no records nothing
    nlu.script["four seven"] = lambda v: vals((v.board, "4.7", "kOhm"))
    nlu.script["four seven"] = lambda v: {"intent": "values", "values": [{"board": v.board, "test_id": "U01", "value": "30", "unit": "kOhm", "heard": "30"}]}
    e = vc.handle_text("four seven")
    p = list(cell(vc, "S02")["proposals"].values())[0]
    assert p["value"] == "30" and p["origin"] != "operator_confirmed" and vc.question is None


def test_plain_no_asks_for_the_current_board_and_unclear_asks_again(vc, nlu):
    ask(vc)
    assert vc.handle_text("no")["reply"] == "Say the reading for S1."
    assert "Say the reading for S1" in vc.handle_text("blah blah")["reply"] and vc.question["stage"] == "value"
    assert not cell(vc, "S01")["proposals"]


def test_cancel_records_nothing(vc):
    ask(vc)
    e = vc.handle_text("cancel")
    assert e["reply"] == "Cancelled. Nothing recorded." and vc.question is None and not cell(vc, "S01")["proposals"]
    ask(vc)
    vc.handle_text("no S2")
    vc.handle_text("cancel")
    assert vc.question is None and not cell(vc, "S02")["proposals"]


def test_other_utterance_drops_the_question(vc, nlu):
    ask(vc)
    nlu.script["which"] = {"intent": "which_test"}
    vc.handle_text("which")
    assert vc.question is None
    assert vc.handle_text("yes")["intent"] != "range_yes"


def test_non_number_tests_and_aoi_keep_the_hint(vc):
    for t in ("U04", "U05"):                                         # yn, text
        vc.set_context("unpowered", t, "S01")
        e = vc.handle_text("mumble")
        assert e["outcome"] == "unclear" and e["reply"].startswith("Not understood") and vc.question is None


def test_grace_accepts_range_with_accepted_reply(vc):
    vc.set_config({"grace_s": 0.2})
    ask(vc)
    vc.handle_text("yes")
    assert wait(lambda: cell(vc, "S01")["status"] == "accepted")
    assert vc.eng.state.voice[-1]["reply"] == "Accepted." and vc.ctx["test_id"] == "U01"


def test_replay_from_journal(vc, tmp_path):
    ask(vc)
    vc.handle_text("yes all")
    vc.handle_text("accept")
    again = make_session(tmp_path).voice
    for b in ("S01", "S02"):
        c = again.eng.state.cell(b, "U01")
        assert c["status"] == "accepted" and c["value"] == "IN RANGE" and c["origin"] == "operator_confirmed" and c["verdict"] == "pass"
    assert again._settled("S01", "U01")


def test_record_class_range_gives_no_verdict(tmp_path):
    from polybench.engine import evaluate
    t = {"id": "X", "kind": "number", "unit": "V", "limit": {"class": "tbr", "min": 1}}
    assert evaluate(t, "IN RANGE", range_ok=True)["verdict"] == "none"
    t["limit"] = {"class": "record", "max": 5}
    assert evaluate(t, "IN RANGE", range_ok=True)["verdict"] == "none"
    assert not evaluate({"kind": "number", "limit": {"class": "hard", "equals": 3}}, "IN RANGE", range_ok=True)["ok"]
    assert not evaluate({"kind": "number"}, "IN RANGE", range_ok=True)["ok"]
    assert evaluate({"kind": "number", "limit": {"class": "hard", "min": 1}}, "IN RANGE", range_ok=False)["ok"] is False


# ---- next test accepts the pending clean value
def test_next_test_accepts_clean_and_reads_only_the_next_test(vc, nlu):
    nlu.script["v"] = vals(("S01", "20", "kOhm"))
    nlu.script["next test"] = {"intent": "next_test"}
    vc.handle_text("v")
    e = vc.handle_text("next test")
    assert cell(vc, "S01")["status"] == "accepted" and vc.ctx["test_id"] == "U02"
    assert e["reply"].startswith("U02,") and "ccept" not in e["reply"]


def test_next_test_accepts_all_pending_boards(vc, nlu):
    nlu.script["all"] = vals(("S01", "20", "kOhm"), ("S02", "21", "kOhm"))
    nlu.script["next test"] = {"intent": "next_test"}
    vc.handle_text("all")
    vc.handle_text("next test")
    assert cell(vc, "S01")["status"] == cell(vc, "S02")["status"] == "accepted"


def test_fail_blocks_next_test(vc, nlu):
    nlu.script["bad"] = vals(("S01", "5", "kOhm"))
    nlu.script["next test"] = {"intent": "next_test"}
    nlu.script["accept fail"] = {"intent": "accept_fail"}
    e = vc.handle_text("bad")
    assert e["reply"].startswith("S1 5 kilo ohms is outside the limit: say accept fail, or reject.")
    n = len(vc.eng.journal.events)
    e = vc.handle_text("next test")
    assert e["reply"] == "Pending fail: say accept fail or reject first." and vc.ctx["test_id"] == "U01"
    assert [x["type"] for x in vc.eng.journal.events[n:]] == ["voice"] and cell(vc, "S01")["status"] == "empty"
    vc.handle_text("accept fail")
    assert cell(vc, "S01")["status"] == "accepted"


def test_fail_with_a_clean_one_records_nothing_on_next_test(vc, nlu):
    nlu.script["two"] = vals(("S01", "20", "kOhm"), ("S02", "5", "kOhm"))
    nlu.script["next test"] = {"intent": "next_test"}
    vc.handle_text("two")
    assert vc.handle_text("next test")["reply"] == "Pending fail: say accept fail or reject first."
    assert cell(vc, "S01")["status"] == "empty" and vc.ctx["test_id"] == "U01"


def test_reject_and_cancel_discard_and_stay(vc, nlu):
    nlu.script["v"] = vals(("S01", "20", "kOhm"))
    nlu.script["reject"] = {"intent": "reject"}
    nlu.script["cancel"] = {"intent": "cancel"}
    for word in ("reject", "cancel"):
        vc.set_context("unpowered", "U01", "S01")
        vc.handle_text("v")
        assert vc.handle_text(word)["reply"] == "Rejected."
        assert not cell(vc, "S01")["proposals"] and cell(vc, "S01")["status"] == "empty" and vc.ctx["test_id"] == "U01"


def test_accept_stays_and_timer_says_accepted(vc, nlu):
    nlu.script["v"] = vals(("S01", "20", "kOhm"))
    nlu.script["accept"] = {"intent": "accept"}
    vc.handle_text("v")
    assert vc.handle_text("accept")["reply"] == "Accepted." and vc.ctx["test_id"] == "U01"
    vc.set_config({"grace_s": 0.2})
    vc.set_context("unpowered", "U02", "S01")
    nlu.script["w"] = {"intent": "values", "values": [{"board": "S01", "test_id": "U02", "value": "9", "unit": "kOhm", "heard": "9"}]}
    vc.handle_text("w")
    assert wait(lambda: cell(vc, "S01", "U02")["status"] == "accepted")
    assert vc.eng.state.voice[-1]["reply"] == "Accepted." and vc.ctx["test_id"] == "U02"
