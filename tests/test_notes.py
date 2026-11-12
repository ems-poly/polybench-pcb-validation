"""Operator notes: linked context, append-only edit chain, replay of old journals, errata candidate export."""
import pytest

from helpers import reopen
from polybench import errata
from polybench.engine import CmdError
from polybench.report import build_model, to_markdown


def test_note_links_its_context_and_derives_lines(engine):
    r = engine.note("R3 is 0603 for no reason", category="design issue", suggested_fix="use 0402", step="assembly",
                    boards=["S01", "S02"], designators="r3, R4", line=5, scan="603-RC0402FR-0710KL")
    n = engine.state.notes[-1]
    assert r["id"] == n["id"] and n["category"] == "design issue" and n["suggested_fix"] == "use 0402"
    assert n["designators"] == ["R3", "R4"] and n["boards"] == ["S01", "S02"] and n["board"] == "S01"
    assert n["lines"] == [5] and n["step"] == "assembly" and n["operator"] == "tester" and n["scan"].startswith("603-")


def test_suggested_fix_only_for_design_issue_and_default_category(engine):
    engine.note("hot", board="S01", suggested_fix="ignored")
    n = engine.state.notes[-1]
    assert n["category"] == "observation" and n["suggested_fix"] == ""


def test_test_id_gives_the_step(engine):
    tid = next(iter(engine.state.test_index))
    engine.note("odd", board="S02", test_id=tid)
    assert engine.state.notes[-1]["step"] == engine.state.test_index[tid][0]["id"]


@pytest.mark.parametrize("kw", [dict(text=" "), dict(text="x", category="rant"), dict(text="x", boards=["S99"]), dict(text="x", test_id="NOPE"),
                                dict(text="x", designators="ZZ9"), dict(text="x", line=999), dict(text="x", step="nope"), dict(text="x", aoi_key="a|b")])
def test_bad_notes_are_refused_and_not_journaled(engine, kw):
    n0 = len(engine.journal.events)
    with pytest.raises(CmdError):
        engine.note(**kw)
    assert len(engine.journal.events) == n0


def test_edit_is_a_new_event_never_a_rewrite(engine, tmp_path):
    first = engine.note("R3 too big", category="observation", designators="R3")["id"]
    raw0 = (tmp_path / "j.jsonl").read_bytes()
    e = engine.note_edit(first, "R3 is 0603 for no reason", category="design issue", suggested_fix="0402", designators="R3")
    raw1 = (tmp_path / "j.jsonl").read_bytes()
    assert raw1.startswith(raw0) and e["ref"] == first and e["id"] > first          # the first line is untouched
    ev = engine.journal.events[-1]
    assert ev["type"] == "note_edit" and ev["payload"]["ref"] == first
    n = engine.state.note_by_id[first]
    assert n["text"] == "R3 is 0603 for no reason" and n["category"] == "design issue" and n["edits"] == [e["id"]]
    assert [h["text"] for h in n["history"]] == ["R3 too big"] and len(engine.state.notes) == 1
    engine.note_edit(first, "third", designators="R3")
    assert [h["text"] for h in engine.state.note_by_id[first]["history"]] == ["R3 too big", "R3 is 0603 for no reason"]
    with pytest.raises(CmdError):
        engine.note_edit(9999, "x")


def test_replay_keeps_edit_chain(engine, tmp_path):
    a = engine.note("one", board="S01")["id"]
    engine.note_edit(a, "two", board="S01", category="process")
    e2 = reopen(engine, tmp_path / "j.jsonl")
    n = e2.state.note_by_id[a]
    assert n["text"] == "two" and n["category"] == "process" and len(n["history"]) == 1
    snap = e2.state.snapshot()
    assert snap["notes_v"] == 2 and snap["notes"][0]["n_versions"] == 2 and "history" not in snap["notes"][0]


def test_old_journal_with_legacy_note_replays(engine, tmp_path):
    tid = next(iter(engine.state.test_index))
    engine.journal.append("note", {"board": "S02", "test_id": tid, "text": "legacy note"}, "tester")      # the pre-16 Oct payload
    engine.journal.append("note_edit", {"ref": 424242, "text": "orphan edit"}, "tester")                  # references a note we do not hold
    e2 = reopen(engine, tmp_path / "j.jsonl")
    n = e2.state.notes[-1]
    assert n["text"] == "legacy note" and n["category"] == "observation" and n["boards"] == ["S02"] and n["test_id"] == tid
    assert n["step"] == e2.state.test_index[tid][0]["id"] and n["designators"] == []
    e2.state.snapshot()
    to_markdown(build_model(e2.state))


def test_report_groups_notes_by_step(engine):
    engine.note("pad is tight", category="design issue", designators="R3", step="assembly", boards=["S01"])
    engine.note("what is the limit?", category="question", board="S02")
    md = to_markdown(build_model(engine.state))
    assert "## Notes and comments" in md and "pad is tight" in md and "design issue" in md
    assert "Question (note" in md.split("## Open items")[1]


def test_errata_has_design_issues_fails_with_notes_and_confirmed_aoi(engine):
    engine.note("R3 is 0603 for no reason", category="design issue", suggested_fix="use 0402", step="assembly", boards=["S01"],
                designators="R3", line=5)
    engine.note("just observing", category="observation", designators="R4")
    tid = next(t for t, (s, sec, tt) in engine.state.test_index.items() if tt.get("kind") == "yn")
    engine.entry("S02", tid, "no", True)
    assert engine.state.cell("S02", tid)["verdict"] == "fail"
    engine.note("probe slipped", category="process", board="S02", test_id=tid)
    engine.note("U-turn", category="design issue", board="S02", test_id=tid, suggested_fix="move the pad")
    rows = errata.collect(engine.state)
    assert sum(r["kind"] == "note" for r in rows) == 2 and f"F-{tid}" in [r["id"] for r in rows]
    fr = next(r for r in rows if r["kind"] == "fail")
    assert "probe slipped" in fr["observation"] and "U-turn" not in fr["observation"]       # the design-issue note is its own row
    assert not any("just observing" in r["observation"] for r in rows)
    md = errata.to_markdown(engine.state, rows, "t", engine.journal_hash(), "now")
    assert "| ID | Origin | Observation | Suggested fix | Evidence | Status |" in md.splitlines()
    r3 = next(l for l in md.splitlines() if l.startswith("| N"))
    assert "designators: R3" in r3 and "board: S01" in r3 and "use 0402" in r3 and r3.rstrip().endswith("| candidate |")
    assert "not merged into any programme register" in md


def test_errata_empty_and_pipe_escape(engine):
    md = errata.to_markdown(engine.state, errata.collect(engine.state), "t", engine.journal_hash(), "now")
    assert "**Candidates:** 0" in md and "| none |" in md
    engine.note("a | b\nc", category="design issue")
    md = errata.to_markdown(engine.state, errata.collect(engine.state), "t", engine.journal_hash(), "now")
    assert "a \\| b c" in md


def test_errata_includes_confirmed_aoi_defects(engine):
    run = {"run": 1, "result": "x", "dir": "x", "boards": ["S01"], "photos": [], "log": "", "counts": {}, "notes": [], "synthetic": True,
           "items": [{"key": "S01|AOI-LIFT-R3", "board": "S01", "id": "AOI-LIFT-R3", "kind": "LIFT", "status": "candidate", "value": "lifted end",
                      "confidence": 0.8, "evidence": ["aoi/crops/r3.png"], "evidence_sha": "abc"}]}
    engine.aoi_run_done("S01", run)
    engine.aoi_decide("S01|AOI-LIFT-R3", "confirmed", "tombstone")
    engine.note("pad too small", category="design issue", aoi_key="S01|AOI-LIFT-R3", designators="R3", boards=["S01"])
    rows = errata.collect(engine.state)
    a = next(r for r in rows if r["kind"] == "aoi")
    assert a["designators"] == ["R3"] and "tombstone" in a["observation"] and "aoi/crops/r3.png" in a["evidence"]
    assert any(r["kind"] == "note" and "AOI S01|AOI-LIFT-R3" in r["evidence"] for r in rows)
