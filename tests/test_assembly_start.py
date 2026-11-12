"""Assembly aid start page, UIR-20: Assemble, skip (boards already built) and reverse are journal events; the state rebuilds from them,
no placement is lost, and nothing builds the scanner (camera, Fusion client) before Assemble. HOST-ONLY. DEMO protocol, simulated."""
import pytest

from helpers import reopen
from polybench.engine import CmdError


def test_fresh_batch_starts_on_the_start_page(engine):
    """R1 (state side): no placements and no choice yet: the mode is 'start'."""
    assert engine.state.assembly_mode() == "start"
    assert engine.state.snapshot()["assembly"] == {"mode": "start", "declared": "start", "since": None}


def test_skip_and_unskip_are_journaled_and_rebuild(engine, tmp_path):
    """R2: both are events (append-only); a rebuild from the journal gives the same state."""
    n0 = len(engine.journal.events)
    engine.assembly_skip()
    engine.assembly_unskip()
    ev = engine.journal.events[n0:]
    assert [e["type"] for e in ev] == ["assembly_skip", "assembly_unskip"]
    assert engine.state.assembly_mode() == "assembling"
    engine.assembly_skip()
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert e2.state.assembly_mode() == "skipped"
    assert [r["kind"] for r in e2.state.alog] == ["skip", "unskip", "skip"]          # the Journal's Assembly filter shows them
    e2.assembly_unskip()
    e3 = reopen(e2, tmp_path / "j.jsonl")
    assert e3.state.assembly_mode() == "assembling" and len(e3.journal.events) == n0 + 4


def test_reversing_the_skip_loses_no_placement(engine, tmp_path):
    engine.assembly_skip()
    engine.assembly_unskip()
    engine.place_batch(4, ["S01", "S02"])
    engine.assembly_begin()
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert e2.state.placed["S01"] >= {"C1", "C6"} and e2.state.assembly_mode() == "assembling"


def test_skip_is_refused_once_something_is_placed(engine):
    engine.place_batch(4, ["S01"])
    n0 = len(engine.journal.events)
    with pytest.raises(CmdError) as e:
        engine.assembly_skip()
    assert e.value.status == 409 and len(engine.journal.events) == n0 and engine.state.placed["S01"]


def test_a_journal_from_before_the_start_page_is_assembling(engine, tmp_path):
    """Placements or scans without any start-page event (every older journal): assembling, not the start page."""
    engine.place_batch(5, ["S01"])
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert e2.state.assembly["declared"] == "start" and e2.state.assembly_mode() == "assembling"


def test_begin_unskip_guards(engine):
    with pytest.raises(CmdError):
        engine.assembly_unskip()                                   # not skipped
    engine.assembly_begin()
    n = len(engine.journal.events)
    engine.assembly_begin()                                        # pressing it again journals nothing
    assert len(engine.journal.events) == n
    engine.assembly_skip()
    with pytest.raises(CmdError):
        engine.assembly_begin()                                    # a skipped batch is reversed with unskip, not begin


def test_scanner_is_not_built_before_assemble(tmp_path, monkeypatch):
    """R3: spy on Session.get_scanner. The state, the journal, the rank list, skip, reverse and Assemble itself never build it."""
    from pp_helpers import client, new_project_session
    sess = new_project_session(tmp_path / "proj")
    calls = []
    real = sess.get_scanner
    monkeypatch.setattr(sess, "get_scanner", lambda *a, **k: calls.append(1) or real(*a, **k))
    c = client(sess)
    c.cookies.set("pb_op", __import__("os").environ.get("POLYBENCH_OP_TOKEN", ""))
    assert c.get("/api/state").json()["assembly"]["mode"] == "start"
    c.get("/api/journal"), c.get("/api/assembly/rank")
    for name, mode in (("assembly_skip", "skipped"), ("assembly_unskip", "assembling")):
        r = c.post(f"/api/cmd/{name}", json={})
        assert r.status_code == 200, r.text
        assert c.get("/api/state").json()["assembly"]["mode"] == mode
    assert calls == [] and sess.scanner is None
    # and the first thing that does need it builds it: the page's scanner poll after Assemble
    c.post("/api/cmd/assembly_begin", json={})
    assert calls == [] and sess.scanner is None
    c.get("/api/scanner/state")
    assert calls == [1] and sess.scanner is not None
