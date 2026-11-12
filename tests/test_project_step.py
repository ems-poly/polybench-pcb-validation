"""Step 1 (Project): ECAD link, document selection, BOM cross-check, skip with a reason; read-only against the bridge."""
import pytest

from helpers import ROOT, reopen
from polybench import project
from polybench.engine import CmdError

DEMO_DES = ["U1", "U2", "U3", "C1", "C2", "C3", "C4", "C5", "C6", "R3", "R4", "R5", "D1", "C7", "J1", "J2"]   # fitted lines of the demo BOM


def test_crosscheck_classes(engine):
    bom = engine.state.bom_lines()
    cc = project.crosscheck(bom, DEMO_DES + ["TP9", "R1"])           # R1 is a DNF part drawn on the board; TP9 is on no BOM line
    assert cc["matched"] == len(DEMO_DES) and cc["missing_in_design"] == [] and cc["missing_in_bom"] == ["TP9"]
    assert cc["dnf"] == ["R1", "R2"] and cc["dnf_in_design"] == ["R1"] and cc["mismatches"] == 1 and not cc["ok"]
    cc2 = project.crosscheck(bom, [d for d in DEMO_DES if d != "C7"])
    assert cc2["missing_in_design"] == ["C7"] and cc2["missing_in_bom"] == []


def test_fingerprint_depends_on_contents_not_order():
    assert project.fingerprint(["A", "B"]) == project.fingerprint(["B", "A"]) != project.fingerprint(["A", "C"])


def test_step_needs_selection_review_and_note_for_mismatch(engine):
    assert engine.state.snapshot()["project"]["state"] == "empty"
    with pytest.raises(CmdError):
        engine.accept_crosscheck("")
    engine.select_project({"document": "demo-board", "editor": "board", "elements": 17, "program": "Fusion"}, DEMO_DES + ["TP9"])
    snap = engine.state.snapshot()
    assert snap["project"]["state"] == "partial" and snap["project"]["crosscheck"]["missing_in_bom"] == ["TP9"]
    with pytest.raises(CmdError):
        engine.accept_crosscheck("")                                   # a mismatch needs words
    engine.accept_crosscheck("TP9 is the probe point on the fixture, not a BOM part")
    p = engine.state.snapshot()["project"]
    assert p["state"] == "done" and p["accepted"]["mismatches"] == 1
    st = engine.state.snapshot()["steps"][0]
    assert st["id"] == "project" and st["settled"] == 3 and st["state"] == "done"


def test_clean_crosscheck_needs_no_note(engine):
    engine.select_project({"document": "d", "editor": "board", "elements": 16}, DEMO_DES)
    engine.accept_crosscheck("")
    assert engine.state.snapshot()["project"]["state"] == "done"


def test_reselecting_resets_the_review(engine):
    engine.select_project({"document": "d", "editor": "board", "elements": 16}, DEMO_DES)
    engine.accept_crosscheck("")
    engine.select_project({"document": "d2", "editor": "board", "elements": 16}, DEMO_DES)
    p = engine.state.snapshot()["project"]
    assert p["state"] == "partial" and p["accepted"] is None and p["select"]["document"] == "d2"


def test_skip_needs_reason_and_can_be_cleared(engine):
    with pytest.raises(CmdError):
        engine.skip_ecad("  ")
    engine.skip_ecad("Fusion not installed on the bench laptop")
    p = engine.state.snapshot()["project"]
    assert p["state"] == "done" and p["ecad_skipped"]["reason"].startswith("Fusion")
    engine.clear_skip_ecad()
    assert engine.state.snapshot()["project"]["state"] == "empty"


def test_project_events_replay_after_restart(engine, tmp_path):
    engine.select_project({"document": "d", "editor": "board", "elements": 16}, DEMO_DES + ["TP9"])
    engine.accept_crosscheck("probe point")
    e2 = reopen(engine, tmp_path / "j.jsonl")
    p = e2.state.snapshot()["project"]
    assert p["state"] == "done" and p["select"]["fingerprint"] == project.fingerprint(DEMO_DES + ["TP9"]) and len(p["history"]) == 2


def test_report_lists_project_and_does_not_count_it_as_a_test_step(engine):
    from polybench.report import build_model, to_markdown
    engine.select_project({"document": "demo-board", "editor": "board", "elements": 17, "program": "Fusion", "version": "MCP x"}, DEMO_DES + ["TP9"])
    engine.accept_crosscheck("TP9 is a probe point")
    md = to_markdown(build_model(engine.state, engine.journal_hash()))
    assert "demo-board" in md and "BOM cross-check" in md and "TP9 is a probe point" in md


# ------------------------------------------------------------------------------------------------------ server, read-only bridge
def _session(tmp_path, drop=()):
    from polybench.protocol import load
    from polybench.server import Session
    s = Session(load(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"), tmp_path, "pj", True, 3, None)
    s.bridge.configure(DEMO_DES + ["R1", "R2"], extras=("TP9",), drop=drop, name="demo-board")
    return s


def test_server_probe_select_accept_and_highlight_gate(tmp_path):
    from fastapi.testclient import TestClient
    from polybench.server import create_app
    sess = _session(tmp_path)
    with TestClient(create_app(sess)) as c:
        pr = c.get("/api/project/probe").json()
        assert pr["status"]["connected"] and pr["documents"]["open"][0]["name"] == "demo-board" and pr["crosscheck"]["missing_in_bom"] == ["TP9"]
        assert pr["expected_elements"] == 18 and pr["design_total"] == 19
        assert c.post("/api/cmd/project_select", json={"fingerprint": "stale"}).status_code == 409        # the document changed under us
        r = c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]})
        assert r.status_code == 200 and r.json()["crosscheck"]["mismatches"] == 1
        assert c.post("/api/cmd/crosscheck_accept", json={"note": ""}).status_code == 422
        assert c.post("/api/cmd/crosscheck_accept", json={"note": "probe point"}).status_code == 200
        s = c.get("/api/state").json()
        assert s["project"]["state"] == "done" and s["steps"][0]["id"] == "project"
        assert c.post("/api/cmd/ecad_skip", json={"reason": ""}).status_code == 422
        assert c.post("/api/cmd/ecad_skip", json={"reason": "no Fusion today"}).status_code == 200
        r = c.post("/api/cmd/highlight", json={"designators": ["U1"]}).json()
        assert r["ecad"]["ok"] is False and r["ecad"]["disabled"]                                      # highlight is off, nothing sent
        c.post("/api/cmd/ecad_skip_clear", json={})
        assert c.post("/api/cmd/highlight", json={"designators": ["U1"]}).json()["ecad"]["highlighted"] == ["U1"]


def test_highlight_reports_designators_missing_from_the_document(tmp_path):
    from fastapi.testclient import TestClient
    from polybench.server import create_app
    sess = _session(tmp_path, drop=("C7",))
    with TestClient(create_app(sess)) as c:
        r = c.post("/api/cmd/highlight", json={"designators": ["C7", "U1"]}).json()["ecad"]
        assert r["missing"] == ["C7"] and r["highlighted"] == ["U1"]


def test_select_refuses_when_nothing_to_read(tmp_path):
    from fastapi.testclient import TestClient
    from polybench.server import create_app
    sess = _session(tmp_path)
    sess.bridge.known = []
    with TestClient(create_app(sess)) as c:
        r = c.post("/api/cmd/project_select", json={})
        assert r.status_code == 409 and "no parts" in r.json()["error"]


# ------------------------------------------------------------------------------------------------------ the real bridge, fake client
def test_fusion_bridge_lists_documents_read_only():
    from polybench.plugins.fusion import FakeClient, FusionBridge
    cl = FakeClient({"R1": "", "C1": ""}, doc={"name": "DEMO-1", "isModified": True})
    b = FusionBridge(client=cl, simulate=False)
    d = b.documents("DEMO-1")
    assert d["ok"] and d["open"][0]["active"] and d["open"][0]["modified"] and d["candidates"][0]["folder"] == "F"
    assert cl.commands == [] and cl.ended == 0                         # nothing sent to the program
    st = b.status()
    assert st["connected"] and st["elements"] == 2 and st["document"] == "DEMO-1" and "MCP" in st["version"]


def test_fusion_bridge_unreachable_is_a_status_not_an_exception():
    from polybench.plugins.fusion import FakeClient, FusionBridge
    b = FusionBridge(client=FakeClient(fail="cannot reach Fusion MCP at http://127.0.0.1:27182/mcp: refused"), simulate=False)
    assert b.status()["connected"] is False
    d = b.documents("x")
    assert d["ok"] is False and "cannot reach" in d["message"]


# ------------------------------------------------------------------------------------------------------ GET /api/project (feeds the scanner)
def test_api_project_accessor_before_and_after_selection(tmp_path):
    from fastapi.testclient import TestClient
    from polybench.server import create_app
    sess = _session(tmp_path)
    with TestClient(create_app(sess)) as c:
        p = c.get("/api/project").json()
        assert set(p) >= {"ecad_connected", "document_name", "document_id", "board", "bom_source", "bom_lines", "crosscheck"}
        assert p["ecad_connected"] is True and p["document_name"] is None and p["board"] is None and p["crosscheck"] is None
        assert p["bom_source"].endswith("demo/assembly.yaml") or p["bom_source"].endswith(".yaml")
        assert len(p["bom_lines"]) == p["bom_line_count"] > 0 and p["state"] == "empty"
        fp = c.get("/api/project/probe").json()["fingerprint"]
        c.post("/api/cmd/project_select", json={"fingerprint": fp})
        p = c.get("/api/project").json()
        assert p["document_name"] == "demo-board" and p["document_id"] == "sim-A" and p["board"]["elements"] == 19
        assert p["crosscheck"]["missing_in_bom"] == ["TP9"] and p["state"] == "partial" and not p["reviewed"]
        assert sess.engine.state.project["select"]["document_id"] == "sim-A"
        c.post("/api/cmd/ecad_skip", json={"reason": "no Fusion today"})
        p = c.get("/api/project").json()
        assert p["ecad_connected"] is False and p["board"] is None and p["crosscheck"] is None and p["ecad_skipped"]


def test_bom_source_names_file_and_counts(tmp_path):
    src = _session(tmp_path).bom_source()
    assert src["name"].endswith(".yaml") and src["file_lines"] > src["lines"] > 0 and src["first_line"] > 0
    assert src["fitted"] + src["dnf"] == src["designators"]


def test_fusion_document_list_marks_open_candidates_by_id_and_status_has_id():
    from polybench.plugins.fusion import FakeClient, FusionBridge
    cl = FakeClient({"R1": ""}, doc={"name": "T", "id": "urn:A", "isModified": False})
    cl.search_documents = lambda name: [{"id": "urn:A", "name": "T"}, {"id": "urn:Z", "name": "T"}]
    cl.open_documents = lambda: [{**cl.doc, "isActive": True}]
    b = FusionBridge(client=cl, simulate=False)
    d = b.documents("T")
    assert [c["open"] for c in d["candidates"]] == [True, False] and b.status()["document_id"] == "urn:A"


def test_project_info_is_what_the_scanner_glue_reads(tmp_path):
    """sync_project(sess, svc) calls sess.project_info(); the real service reads exactly these keys."""
    from fastapi.testclient import TestClient
    from polybench.assembly_scanner.polybench_link import sync_project
    from polybench.server import create_app
    sess = _session(tmp_path)
    got = []

    class Svc:
        def apply_project(self, info, loader):
            got.append(info)
            return True
    with TestClient(create_app(sess)) as c:
        fp = c.get("/api/project/probe").json()["fingerprint"]
        c.post("/api/cmd/project_select", json={"fingerprint": fp})
        assert sync_project(sess, Svc()) is True
        info = got[0]
        assert {"ecad_connected", "document_name", "document_id", "board", "bom_source", "bom_lines", "crosscheck"} <= set(info)
        assert info["document_id"] == "sim-A" and info["bom_lines"] == sess.engine.state.bom_lines()
        c.post("/api/cmd/ecad_skip", json={"reason": "no Fusion today"})
        assert sess.project_info() is None                             # skipped: the scanner keeps its own defaults


def test_skip_ecad_disables_select_line_without_building_the_scanner(tmp_path):
    import asyncio
    sess = _session(tmp_path)
    sess.engine.skip_ecad("no Fusion today")
    res = asyncio.run(sess.select_line(1))
    assert res["disabled"] and sess.scanner is None
