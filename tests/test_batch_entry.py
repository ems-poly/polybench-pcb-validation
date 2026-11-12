"""Typed batch entry: one value applied to every applicable board that is still empty for a test."""
import pytest

from helpers import reopen
from polybench.engine import CmdError
from test_planfmt import make_engine


def open_gates(e):
    for b in ("S01", "S02", "S03"):
        e.override_gate(b, "unpowered", "test")


def events(e, ty="entry"):
    return [x for x in e.journal.events if x["type"] == ty]


def test_all_clean_goes_to_every_empty_board_via_batch(engine):
    open_gates(engine)
    r = engine.entry_batch("U01", "20")
    assert r["recorded"] == ["S01", "S02", "S03"] and r["have"] == [] and r["verdict"] == "pass"
    for b in ("S01", "S02", "S03"):
        c = engine.state.cell(b, "U01")
        assert c["status"] == "accepted" and c["value"] == "20" and c["verdict"] == "pass"
    ev = events(engine)
    assert len(ev) == 3 and all(x["payload"]["via"] == "batch" for x in ev)
    assert [x["payload"]["board"] for x in ev] == ["S01", "S02", "S03"]


def test_single_entry_carries_no_via(engine):
    open_gates(engine)
    engine.entry("S01", "U01", "20")
    assert "via" not in events(engine)[0]["payload"]


def test_one_fail_blocks_all_until_confirmed(engine):
    open_gates(engine)
    with pytest.raises(CmdError) as x:
        engine.entry_batch("U01", "5")
    ex = x.value.extra
    assert ex["needs_confirm_batch"] and ex["verdict"] == "fail"
    assert [b["board"] for b in ex["boards"]] == ["S01", "S02", "S03"] and all(b["verdict"] == "fail" for b in ex["boards"])
    assert events(engine) == []
    assert all(engine.state.cell(b, "U01")["status"] == "empty" for b in ("S01", "S02", "S03"))
    r = engine.entry_batch("U01", "5", confirm=True)
    assert r["recorded"] == ["S01", "S02", "S03"]
    assert all(engine.state.cell(b, "U01")["verdict"] == "fail" for b in ("S01", "S02", "S03"))


def test_review_verdict_also_needs_confirmation(engine):
    open_gates(engine)
    with pytest.raises(CmdError) as x:
        engine.entry_batch("U02", "1")
    assert x.value.extra["verdict"] == "review"
    assert events(engine) == []


def test_stop_rule_needs_confirmation_and_stops_every_board(engine):
    open_gates(engine)
    with pytest.raises(CmdError) as x:
        engine.entry_batch("U01", "0.5")
    assert x.value.extra["needs_confirm_batch"] and x.value.extra["stop"]
    assert not engine.state.stopped("S01")
    r = engine.entry_batch("U01", "0.5", confirm=True)
    assert len(r["stops"]) == 3 and all(engine.state.stopped(b) for b in ("S01", "S02", "S03"))


def test_invalid_value_records_nothing(engine):
    open_gates(engine)
    with pytest.raises(CmdError) as x:
        engine.entry_batch("U01", "abc")
    assert x.value.status == 422 and events(engine) == []


def test_boards_with_a_value_are_skipped_and_kept(engine):
    open_gates(engine)
    engine.entry("S03", "U01", "30")
    r = engine.entry_batch("U01", "20")
    assert r["recorded"] == ["S01", "S02"] and r["have"] == ["S03"]
    assert engine.state.cell("S03", "U01")["value"] == "30"
    assert [x["payload"].get("via") for x in events(engine)] == [None, "batch", "batch"]


def test_skipped_cell_counts_as_having_a_value(engine):
    open_gates(engine)
    engine.skip("S02", "U01", "no access")
    r = engine.entry_batch("U01", "20")
    assert r["recorded"] == ["S01", "S03"] and r["have"] == ["S02"]


def test_nothing_empty_is_an_error(engine):
    open_gates(engine)
    engine.entry_batch("U01", "20")
    with pytest.raises(CmdError) as x:
        engine.entry_batch("U01", "21")
    assert x.value.status == 409


def test_gated_boards_are_left_out_not_failed(engine):
    engine.override_gate("S01", "unpowered", "test")
    r = engine.entry_batch("U01", "20")
    assert r["recorded"] == ["S01"] and r["blocked"] == ["S02", "S03"]


def test_only_the_current_attempt_counts(engine):
    open_gates(engine)
    engine.entry_batch("U01", "20")
    engine.retest("S02", "unpowered", "reflowed")
    r = engine.entry_batch("U01", "22")
    assert r["recorded"] == ["S02"] and r["have"] == ["S01", "S03"]
    assert engine.state.cell("S02", "U01")["value"] == "22"
    assert engine.state.cell("S01", "U01")["value"] == "20"


def test_tier2_boards_not_sent_are_skipped(tmp_path):
    eng, _ = make_engine(tmp_path)
    eng.tier2_assign("S01", "unpowered", "all", "test", "operator")
    eng.tier2_assign("S03", "unpowered", "all", "test", "operator")
    r = eng.entry_batch("U20", "bridge under U1")
    assert r["recorded"] == ["S01", "S03"] and r["na"] == ["S02", "S04"]
    assert eng.state.cell("S02", "U20")["status"] == "empty"
    # a Tier 2 test nobody was sent to has no target
    (tmp_path / "b").mkdir()
    eng2, _ = make_engine(tmp_path / "b")
    with pytest.raises(CmdError):
        eng2.entry_batch("U20", "x")


def test_replay_gives_the_same_state(engine, tmp_path):
    open_gates(engine)
    engine.entry("S02", "U02", "3")
    engine.entry_batch("U01", "20")
    engine.entry_batch("U02", "3")
    snap = {k: (v["value"], v["verdict"], v["status"]) for k, v in engine.state.cells.items()}
    e2 = reopen(engine, engine.journal.path)
    assert {k: (v["value"], v["verdict"], v["status"]) for k, v in e2.state.cells.items()} == snap
    assert len(events(e2)) == len(events(engine)) == 6
    # the batch rows still read as batch after replay
    assert [x["payload"].get("via") for x in events(e2)].count("batch") == 5
