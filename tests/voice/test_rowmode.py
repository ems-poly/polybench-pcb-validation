# Ported from the voice proof of concept. Changes for Polybench: imports.
"""Batch (row) mode: one test across the five boards, then the next test. No hardware, no LLM unless a test stubs it."""
import pytest

from polybench.voice import extract, llm, rowmode
from polybench.voice.session import ACCEPTED, PENDING
from poc_engine import Engine
import eval_extract as ev


@pytest.fixture
def s(tmp_path):
    return ev.make_session(str(tmp_path), mode="row")


@pytest.fixture
def eng(s):
    return Engine(s, "simulator", no_stt=True, use_llm=False, clips=[], cue_sound=False)


def say(eng, text):
    return eng.handle_text(text, "typed")


def row_id(s):
    return s.script.rows[s.ctx.row]["id"]


def cells(s, rid):
    out = {}
    for p in s.proposals.values():
        if p["id"] == rid and p["state"] in (PENDING, ACCEPTED) and str(p["board"])[:1] == "B" and str(p["board"])[1:].isdigit():
            out[p["board"]] = p["value"]
    return out


def go(s, rid):
    s.goto_row(next(i for i, r in enumerate(s.script.rows) if r["id"] == rid))


def test_starts_on_the_first_board_of_the_first_row(s):
    assert s.mode == "row" and s.ctx.board == "B1"


def test_bare_values_fill_b1_to_b5_in_order_then_move_to_the_next_row(s, eng):
    go(s, "C05")
    for v, nxt in [("0.62", "B2"), ("0.63", "B3"), ("0.64", "B4"), ("0.61", "B5")]:
        r = say(eng, v + " volts")
        assert s.ctx.board == nxt and row_id(s) == "C05", r
    say(eng, "0.62 volts")
    assert row_id(s) == "C06" and s.ctx.board == "B1"          # row complete: next test, first board
    assert [cells(s, "C05")[f"B{i}"] for i in range(1, 6)] == ["0.62", "0.63", "0.64", "0.61", "0.62"]


def test_value_without_unit_uses_the_row_unit(s, eng):
    go(s, "C05")
    say(eng, "3.29")
    assert cells(s, "C05")["B1"] == "3.29" and s.ctx.board == "B2"


def test_explicit_boards_in_one_utterance_deterministic_no_llm(s, eng):
    go(s, "C05")
    r = say(eng, "B1 3.29, B2 3.30, B3 3.31")
    assert r["ok"] and r["route"] == "grammar"
    assert cells(s, "C05") == {"B1": "3.29", "B2": "3.30", "B3": "3.31"}
    assert s.ctx.board == "B4" and row_id(s) == "C05"
    t = eng.traces[-1]
    assert t["grammar"]["fields"]["pairs"] == ["B1 = 3.29", "B2 = 3.30", "B3 = 3.31"]
    assert t["row"]["next_board"] == "B4" and t["row"]["board_spoken"] is True


def test_implicit_board_trace(s, eng):
    go(s, "C05")
    say(eng, "B1 0.6")
    say(eng, "0.7 volts")
    tr = eng.traces[-1]["row"]
    assert tr["board_spoken"] is False and tr["touched"] == ["B2"] and tr["next_board"] == "B3"
    assert tr["before"].startswith("B1=pending B2=- ")
    assert eng.traces[-1]["targets"][0]["implicit"] is True


def test_correction_of_an_earlier_board_does_not_move_the_next_board(s, eng):
    go(s, "C05")
    say(eng, "B1 0.6 volts, B2 0.61 volts")
    assert s.ctx.board == "B3"
    say(eng, "B1 0.65 volts")
    assert s.ctx.board == "B3"
    assert cells(s, "C05")["B1"] == "0.65"


def test_overwrite_of_a_filled_board_asks_for_a_second_accept(s, eng):
    go(s, "C05")
    say(eng, "B1 0.6 volts")
    s.decide_focus(ACCEPTED, "pedal")
    say(eng, "B1 0.65 volts")
    p = next(p for p in s.proposals.values() if p["state"] == PENDING and p["board"] == "B1")
    assert p["needs_confirm"] and p["replaces"] == "0.6"
    assert s.decide_focus(ACCEPTED, "pedal") == 0 and p["state"] == PENDING      # first accept only arms it
    assert "accept again" in s.messages[-1]["text"] or "already holds" in s.messages[-1]["text"]
    assert s.accept_all("voice", "ui", include_flagged=True) == 0                 # accept-all never replaces
    assert s.decide_focus(ACCEPTED, "pedal") == 1 and p["state"] == ACCEPTED


def test_same_value_again_does_not_ask(s, eng):
    go(s, "C05")
    say(eng, "B1 0.6 volts")
    say(eng, "B1 0.6 volts")
    p = [p for p in s.proposals.values() if p["state"] == PENDING and p["board"] == "B1"][0]
    assert not p.get("needs_confirm")


def test_skip_board_with_reason_marks_it_and_moves_on(s, eng):
    go(s, "C05")
    say(eng, "B1 0.6 volts, B2 0.61 volts")
    r = say(eng, "skip B3 reworking")
    assert r["ok"] and s.ctx.board == "B4"
    assert s.skipped_boards[(s.ctx.row, "B3")] == "reworking"
    note = [p for p in s.proposals.values() if p["cell"].endswith("Notes") and p["state"] == PENDING]
    assert note and note[0]["value"] == "B3: skipped, reworking"
    st = {x["board"]: x["state"] for x in s.row_strip()["slots"]}
    assert st["B3"] == "skipped"
    say(eng, "0.7 volts, ")        # B4
    say(eng, "0.7 volts")          # B5
    assert row_id(s) == "C06"      # skipped board counts as settled; the row completed


def test_bare_skip_skips_the_current_board_and_next_test_moves_the_row(s, eng):
    go(s, "C05")
    say(eng, "skip")
    assert s.ctx.board == "B2" and row_id(s) == "C05"
    say(eng, "next test")
    assert row_id(s) == "C06" and s.ctx.board == "B1"


def test_pass_after_the_last_board_still_belongs_to_the_completed_row(s, eng):
    go(s, "C05")
    say(eng, "B1 0.6 volts, B2 0.6 volts, B3 0.6 volts, B4 0.6 volts")
    say(eng, "B5 0.6 volts")
    assert row_id(s) == "C06"
    say(eng, "note probe slipped")
    notes = [p for p in s.proposals.values() if p["cell"].endswith("Notes") and p["state"] == PENDING]
    assert notes and notes[0]["id"] == "C05" and row_id(s) == "C06"


def test_board_mode_keeps_the_old_behaviour(s, eng):
    s.set_mode("board")
    go(s, "C05")
    say(eng, "board 3")
    say(eng, "0.6 volts")
    assert s.ctx.board == "B3" and cells(s, "C05") == {"B3": "0.6"}


def test_everything_arrives_pending(s, eng):
    go(s, "C05")
    say(eng, "B1 0.6 volts, B2 0.61 volts")
    assert all(p["state"] == PENDING for p in s.proposals.values())
    assert s.accepted_edits() == []


def test_undo_restores_the_cursor_and_skips(s, eng):
    go(s, "C05")
    say(eng, "skip B1 not fitted")
    assert (s.ctx.row, "B1") in s.skipped_boards
    s.undo()
    assert not s.skipped_boards and s.ctx.board == "B1"


# ---- extraction path (the LLM is stubbed with its JSON) -------------------------------------------------
def U(id, boards, kind="none", **kw):
    return {"id": id, "boards": boards, "kind": kind, "number": kw.get("number", ""), "unit": kw.get("unit", ""),
            "unit_heard": kw.get("heard", ""), "text": "", "verdict": kw.get("verdict", "none"), "note": kw.get("note", "")}


def test_llm_unlabelled_values_go_to_successive_boards(s):
    go(s, "C05")
    res = extract.resolve({"command": "none", "updates": [U("C05", [], "number", number="0.6", unit="V"), U("C05", [], "number", number="0.61", unit="V"),
                                                           U("C05", [], "number", number="0.62", unit="V")]},
                          "zero point six, zero point six one, zero point six two", s.script, s.ctx, s.effective)
    assert [(c["board"], c["value"]) for c in res["cells"] if c["column"].startswith("B")] == [("B1", "0.6"), ("B2", "0.61"), ("B3", "0.62")]
    assert all(c["implicit"] for c in res["cells"] if c["column"].startswith("B"))


def test_llm_all_ol_except_b3_one_volt(s, eng, monkeypatch):
    go(s, "C05")
    raw = {"command": "none", "updates": [U("C05", [1, 2, 4, 5], "ol"), U("C05", [3], "number", number="1", unit="V", heard="volt")]}
    eng.use_llm = True
    monkeypatch.setattr(llm, "extract", lambda *a, **k: raw)
    r = say(eng, "all OL except B3 one volt")
    assert r["ok"]
    assert cells(s, "C05") == {"B1": "OL", "B2": "OL", "B4": "OL", "B5": "OL", "B3": "1"}
    assert row_id(s) == "C06"


def test_llm_same_for_b4_and_b5(s, eng, monkeypatch):
    go(s, "C05")
    say(eng, "B1 0.6 volts, B2 0.6 volts, B3 0.6 volts")
    eng.use_llm = True
    monkeypatch.setattr(llm, "extract", lambda *a, **k: {"command": "none", "updates": [U("C05", [4, 5], "number", number="0.6", unit="V")]})
    say(eng, "same for B4 and B5")
    assert cells(s, "C05") == {f"B{i}": "0.6" for i in range(1, 6)}


def test_llm_skip_kind_with_values(s, eng, monkeypatch):
    go(s, "C05")
    raw = {"command": "none", "updates": [U("C05", [1], "number", number="0.6", unit="V"), U("C05", [2], "skip", note="reworking"),
                                          U("C05", [3], "number", number="0.62", unit="V")]}
    eng.use_llm = True
    monkeypatch.setattr(llm, "extract", lambda *a, **k: raw)
    r = say(eng, "B1 zero point six volts skip B2 reworking B3 zero point six two volts")
    assert r["ok"] and (s.ctx.row, "B2") in s.skipped_boards and s.ctx.board == "B4"
    assert cells(s, "C05") == {"B1": "0.6", "B3": "0.62"}


def test_next_slot_pure():
    order = ["B1", "B2", "B3", "B4", "B5"]
    done = {"B1", "B2", "B4"}
    f = lambda b: b in done
    assert rowmode.next_slot(order, f, None) == "B3"
    assert rowmode.next_slot(order, f, "B3") == "B5"
    assert rowmode.next_slot(order, f, "B5") == "B3"            # wraps
    assert rowmode.next_slot(order, lambda b: True, "B1") is None


def test_split_pairs():
    from polybench.voice import grammar
    idx = grammar.IdIndex(["C04"])
    assert rowmode.split_pairs("C04 B1 3.29, B2 3.30", idx) == ["C04 B1 3.29", "C04 B2 3.30"]
    assert rowmode.split_pairs("boards 1 and 2 3.3", idx) is None
    assert rowmode.split_pairs("B1 3.29", idx) is None


def test_skipping_a_later_board_leaves_the_cursor_on_the_first_open_board(s, eng):
    go(s, "C05")
    say(eng, "skip B3 reworking")
    assert s.ctx.board == "B1" and s.skipped_boards[(s.ctx.row, "B3")] == "reworking"
    say(eng, "0.6 volts")
    say(eng, "0.6 volts")
    assert s.ctx.board == "B4"          # B3 is settled, so B2 is followed by B4
