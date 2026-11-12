"""AI connector: permission guard, read and act routes, plan co-authoring, troubleshooting bundle, MCP server, installer.
HOST-ONLY: FastAPI TestClient on a copy of the DEMO protocol; nothing touches port 8750 or ~/polybench-data."""
import io
import json
import shutil

import pytest

from polybench import extras

needs_aoi = pytest.mark.skipif(not extras.check("aoi"), reason="needs the aoi extra (opencv, pillow, numpy)")
from fastapi.testclient import TestClient

from helpers import DEMO, ROOT

AI = {"X-Polybench-Client": "assistant", "X-Polybench-Assistant": "pytest", "X-Polybench-Session": "t1"}
PLAN = (ROOT / "tests" / "data" / "DEMO_TEST_PLAN.md").read_text()


def make(tmp_path, monkeypatch, mode=None, plan=True, generate=True):
    from polybench.protocol import load
    from polybench.server import Session, create_app
    if mode is None:
        monkeypatch.delenv("POLYBENCH_AI_PERMISSIONS", raising=False)
    else:
        monkeypatch.setenv("POLYBENCH_AI_PERMISSIONS", mode)
    d = tmp_path / "proj"
    shutil.copytree(DEMO, d)
    top = d / "demo.pcbv.yaml"
    if plan:
        (d / "PLAN.md").write_text(PLAN)
        top.write_text(top.read_text() + ("\ntest_plan: {file: PLAN.md, generate: true}\n" if generate else "\ntest_plan: PLAN.md\n"))
    sess = Session(load(top), tmp_path / "data", "ai", True, 3, None, operator="Op")
    for b in ("S01", "S02", "S03"):                  # the demo gates unpowered on visual; the tests start at unpowered
        sess.engine.override_gate(b, "unpowered", "test starts at unpowered")
    return sess, TestClient(create_app(sess)), d


def events(sess, ty):
    return [e for e in sess.journal.events if e["type"] == ty]


# ---------------------------------------------------------------- permission guard
@pytest.mark.parametrize("path,body", [
    ("/api/cmd/accept", {"board": "S01", "test_id": "U01", "proposal_id": 1}),
    ("/api/cmd/entry", {"board": "S01", "test_id": "U01", "value": "5"}),
    ("/api/cmd/override_gate", {"board": "S01", "step": "powered", "reason": "x"}),
    ("/api/cmd/ack_stop", {"board": "S01", "stop_id": 1, "reason": "x"}),
    ("/api/cmd/gate_accept", {"gate": 0, "boards": ["S01"]}),
    ("/api/action/run", {"step": "powered", "section": "x", "action": "read_rails", "board": "S01"}),
    ("/api/action/safe_state", {}),
    ("/api/ai/plan_approve", {"hash": "x"}),
    ("/api/ai/plan_reject", {"reason": "x"}),
    ("/api/ai/answer", {"qid": "x", "answer": "y"}),
    ("/api/ai/focus", {"step": "x"}),
    ("/aoi/review/api/decision", {}),
])
def test_assistant_refused_on_operator_routes(tmp_path, monkeypatch, path, body):
    sess, c, _ = make(tmp_path, monkeypatch)
    n = len(sess.journal.events)
    r = c.post(path, json=body, headers=AI)
    assert r.status_code == 403 and r.json()["refused"] is True
    refused = events(sess, "ai_action")[-1]["payload"]
    assert refused["outcome"] == "refused" and refused["source"] == "assistant" and path in refused["route"]
    assert len(sess.journal.events) == n + 1                       # the refusal and nothing else


def test_assistant_refused_on_camera_and_voice(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    assert c.get("/api/scanner/state", headers=AI).status_code == 403
    assert c.get("/api/voice/status", headers=AI).status_code == 403
    assert c.get("/api/state", headers=AI).status_code == 200


def test_forbidden_accept_leaves_the_proposal_for_the_operator(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    r = c.post("/api/ai/propose", json={"board": "S01", "test_id": "U01", "value": "4.7", "reason": "operator read it aloud"}, headers=AI)
    assert r.status_code == 200, r.text
    pid = r.json()["proposal_id"]
    assert c.post("/api/cmd/accept", json={"board": "S01", "test_id": "U01", "proposal_id": pid}, headers=AI).status_code == 403
    cell = c.get("/api/state").json()["cells"]["S01|U01"]
    assert cell["status"] == "empty" and cell["proposals"][0]["source"]["kind"] == "assistant"
    assert c.post("/api/cmd/accept", json={"board": "S01", "test_id": "U01", "proposal_id": pid}).status_code == 200    # operator
    cell = c.get("/api/state").json()["cells"]["S01|U01"]
    assert cell["status"] == "accepted" and cell["value"] == "4.7" and cell["source"]["kind"] == "assistant"


def test_propose_needs_a_reason_and_a_valid_value(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    assert c.post("/api/ai/propose", json={"board": "S01", "test_id": "U01", "value": "4.7"}, headers=AI).status_code == 422
    assert c.post("/api/ai/propose", json={"board": "S01", "test_id": "U01", "value": "abc", "reason": "x"}, headers=AI).status_code == 422
    assert c.post("/api/ai/propose", json={"board": "S99", "test_id": "U01", "value": "1", "reason": "x"}, headers=AI).status_code == 404
    assert [e["payload"]["outcome"] for e in events(sess, "ai_action")] == ["error", "error", "error"]
    assert not events(sess, "proposal")


def test_act_routes_need_the_identity(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    assert c.post("/api/ai/propose", json={"board": "S01", "test_id": "U01", "value": "1", "reason": "x"}).status_code == 400


def test_mode_read_and_off(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch, mode="read")
    assert c.get("/api/ai/context", headers=AI).status_code == 200
    r = c.post("/api/ai/note", json={"text": "x"}, headers=AI)
    assert r.status_code == 403 and "read-only" in r.json()["error"]
    sess2, c2, _ = make(tmp_path / "b", monkeypatch, mode="off")
    assert c2.get("/api/ai/context", headers=AI).status_code == 403
    assert c2.get("/api/state").json()["ai"]["mode"] == "off"
    sess3, c3, _ = make(tmp_path / "c", monkeypatch, mode="admin")          # unknown value: off, never more
    assert c3.get("/api/state").json()["ai"]["mode"] == "off"


# ---------------------------------------------------------------- read tools
def test_read_tools(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    ctx = c.get("/api/ai/context", headers=AI).json()
    assert ctx["simulated"] and ctx["permission_mode"] == "propose" and [b["label"] for b in ctx["boards"]] == ["S01", "S02", "S03"]
    assert ctx["test_plan"]["configured"] and ctx["test_plan"]["master_name"] == "PLAN.md"
    assert c.get("/api/ai/focus", headers=AI).json()["focus"] is None
    c.post("/api/ai/focus", json={"step": "unpowered", "board": "S02", "test": "U02"})                # the UI reports focus
    f = c.get("/api/ai/focus", headers=AI).json()
    assert f["focus"]["board"] == "S02" and f["test"]["limit"]["min"] == 1.0 and f["test"]["source"] and f["cell"]["status"] == "empty"
    for b, v in (("S01", "5"), ("S02", "5.2"), ("S03", "0.6")):
        assert c.post("/api/cmd/entry", json={"board": b, "test_id": "U01", "value": v, "confirm": True}).status_code == 200
    t = c.get("/api/ai/test?test_id=U01&board=S03", headers=AI).json()
    assert t["batch"]["median"] == 5.0 and t["cell"]["verdict"] == "review"
    bd = c.get("/api/ai/board?board=S03", headers=AI).json()
    un = next(s for s in bd["steps"] if s["step"] == "unpowered")
    assert un["tests"][0]["test_id"] == "U01" and un["tests"][0]["value"] == "0.6"
    j = c.get("/api/ai/journal?since=0&limit=3", headers=AI).json()
    assert len(j["events"]) == 3 and j["head"] == sess.journal.events[-1]["id"]
    j2 = c.get(f"/api/ai/journal?since={j['head']}", headers=AI).json()
    assert j2["events"] == []
    e = c.get("/api/ai/ecad?designators=C3", headers=AI).json()
    assert e["parts"] and all("C3" in p["designators"] for p in e["parts"]) and "3V3" in e["nets_in_tests"]
    p = c.get("/api/ai/plan", headers=AI).json()
    assert p["text"].startswith("---") and p["report"]["lint"]["errors"] == 0 and p["draft"]["draft_exists"] is False


def test_troubleshoot_bundle(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    for b, v in (("S01", "5"), ("S02", "5.2"), ("S03", "0.6")):
        c.post("/api/cmd/entry", json={"board": b, "test_id": "U01", "value": v, "confirm": True})
    t = c.get("/api/ai/troubleshoot?board=S03", headers=AI).json()
    assert [r["test"]["id"] for r in t["readings"]] == ["U01"]
    r = t["readings"][0]
    assert r["this_board"]["verdict"] == "review" and r["batch"]["median"] == 5.0
    assert {"C2", "U1", "J1"} <= set(t["parts"]) and "5V" in t["nets"]
    assert any("AOI" in m for m in t["missing"]) and "Hypothesis" in t["rules"]
    t2 = c.get("/api/ai/troubleshoot?board=S01&test_id=U03", headers=AI).json()
    assert any("no reading" in m for m in t2["missing"]) and any(p["line"] for p in t2["polarised"])


def test_aoi_view_without_aoi_block(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    a = c.get("/api/ai/aoi", headers=AI).json()
    assert a["configured"] in (False, True)


# ---------------------------------------------------------------- act tools
def test_note_highlight_ask(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    r = c.post("/api/ai/note", json={"text": "C3 looks dry", "board": "S01", "designators": ["C3"]}, headers=AI)
    assert r.status_code == 200
    n = events(sess, "note")[-1]["payload"]
    assert n["source"] == "assistant" and n["designators"] == ["C3"]
    h = c.post("/api/ai/highlight", json={"designators": ["U2"]}, headers=AI).json()
    assert h["ok"] and h["ecad"]["highlighted"] == ["U2"]
    q = c.post("/api/ai/ask", json={"text": "Measure TP1 to GND?", "options": ["done", "later"], "board": "S01"}, headers=AI).json()
    s = c.get("/api/state").json()["ai"]
    assert [x["qid"] for x in s["questions"]] == [q["qid"]] and s["connected"] is True and s["client"] == "pytest"
    assert c.post("/api/ai/answer", json={"qid": q["qid"], "answer": "done, 4.9 kOhm"}).status_code == 200
    qs = c.get("/api/ai/questions", headers=AI).json()["questions"]
    assert qs[0]["answer"] == "done, 4.9 kOhm" and not qs[0]["open"]
    log = c.get("/api/state").json()["ai"]["actions"]
    assert [x["type"] for x in log[:3]] == ["ai_answer", "ai_action", "ai_question"]
    assert all(e["payload"]["source"] == "assistant" for e in events(sess, "ai_action"))


def test_card_state_setup_and_privacy_inputs(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    s = c.get("/api/state").json()["ai"]
    assert s["connected"] is False and s["mode"] == "propose"
    assert "claude mcp add polybench" in s["setup"]["claude"] and "polybench-mcp" in s["setup"]["claude"]
    assert "[mcp_servers.polybench]" in s["setup"]["codex"] and s["setup"]["skills"].endswith("polybench ai install-skills")


# ---------------------------------------------------------------- plan co-authoring
def test_plan_draft_approve(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    old = (d / "PLAN.md").read_text()
    new = old.replace("| U06 | R4 across its pads |", "| U06 | R4 across its two pads |")
    r = c.post("/api/ai/plan_draft", json={"text": new, "summary": "U06 wording"}, headers=AI).json()
    assert r["ok"] and r["lint"]["errors"] == 0 and "+| U06 | R4 across its two pads" in r["diff"]
    assert (d / "PLAN.draft.md").read_text() == new and (d / "PLAN.md").read_text() == old           # master untouched
    st = c.get("/api/ai/plan_draft").json()
    assert st["can_approve"] and not st["stale"]
    assert c.post("/api/ai/plan_approve", json={"hash": st["hash"]}, headers=AI).status_code == 403
    assert c.post("/api/ai/plan_approve", json={"hash": "0" * 16, "operator": "Op"}).status_code == 409
    r = c.post("/api/ai/plan_approve", json={"hash": st["hash"], "operator": "Pat"})
    assert r.status_code == 200, r.text
    out = (d / "PLAN.md").read_text()
    assert "R4 across its two pads" in out and "| r2 | " in out and "| U06 wording | AI draft approved by Pat |" in out
    assert not (d / "PLAN.draft.md").exists()
    ap = events(sess, "plan_approved")[-1]["payload"]
    from polybench.planfmt import file_hash
    assert ap["operator"] == "Pat" and ap["new_hash"] == file_hash(out) and ap["old_hash"] == file_hash(old) and ap["revision"] == "r2"
    assert (sess.dir / "ai" / "plan_history").glob("PLAN_*.md")
    assert r.json()["reload"]["ok"] and events(sess, "plan_loaded")[-1]["payload"]["hash"] == file_hash(out)
    assert sess.engine.state.test_index["U06"][2]["title"] == "R4 across its two pads"


def test_plan_draft_with_lint_error_cannot_be_approved(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    bad = PLAN.replace("| U05 | R3 across its pads | R3 | R3 | — | R3 |", "| U05 | R3 across its pads | R3 | R3 | — | R99 |")
    r = c.post("/api/ai/plan_draft", json={"text": bad, "summary": "typo"}, headers=AI).json()
    assert r["lint"]["errors"] >= 1 and any("R99" in i["msg"] for i in r["lint"]["issues"])
    st = c.get("/api/ai/plan_draft").json()
    assert not st["can_approve"] and "lint error" in st["why_not"]
    assert c.post("/api/ai/plan_approve", json={"hash": st["hash"], "operator": "Op"}).status_code == 409
    assert (d / "PLAN.md").read_text() == PLAN


def test_plan_draft_stale_and_edited_outside(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    c.post("/api/ai/plan_draft", json={"text": PLAN.replace("First issue", "First issue."), "summary": "dot"}, headers=AI)
    (d / "PLAN.md").write_text(PLAN + "\n")                                                    # master changed under the draft
    st = c.get("/api/ai/plan_draft").json()
    assert st["stale"] and not st["can_approve"]
    (d / "PLAN.md").write_text(PLAN)
    (d / "PLAN.draft.md").write_text(PLAN + "\nedited by hand\n")
    st = c.get("/api/ai/plan_draft").json()
    assert not st["journaled"] and not st["can_approve"]


def test_plan_draft_reject(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    c.post("/api/ai/plan_draft", json={"text": PLAN, "summary": "same"}, headers=AI)
    assert c.post("/api/ai/plan_reject", json={"reason": ""}).status_code == 422
    r = c.post("/api/ai/plan_reject", json={"reason": "not now", "operator": "Op"}).json()
    assert r["ok"] and not (d / "PLAN.draft.md").exists() and (sess.dir / "ai" / "rejected").is_dir()
    assert events(sess, "plan_draft_rejected")[-1]["payload"]["reason"] == "not now"
    assert c.get("/api/ai/plan_draft").json()["draft_exists"] is False


def test_plan_draft_needs_summary_and_a_plan(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    assert c.post("/api/ai/plan_draft", json={"text": PLAN, "summary": ""}, headers=AI).status_code == 422
    sess2, c2, _ = make(tmp_path / "np", monkeypatch, plan=False)
    assert c2.post("/api/ai/plan_draft", json={"text": PLAN, "summary": "x"}, headers=AI).status_code == 409


def test_revision_row_added_when_section_missing():
    from polybench.ai.plan_draft import add_revision_row
    t, rev = add_revision_row("# Plan\n\ntext\n", "first | change", "Op", "2026-10-17")
    assert rev == "r1" and t.endswith("| r1 | 2026-10-17 | first \\| change | AI draft approved by Op |\n") and "## Revision history" in t
    t2, rev2 = add_revision_row(t, "second", "Op", "2026-10-18")
    assert rev2 == "r2" and t2.count("| r") == 2


# ---------------------------------------------------------------- MCP server
class _TCApp:
    """The MCP App with its HTTP replaced by the TestClient (same headers, same routes)."""

    def __init__(self, client):
        from polybench.ai.mcp_server import App
        self._a = App("http://127.0.0.1:8750", "pytest-mcp")
        self.headers = self._a.headers
        self.c = client

    def request(self, method, path, body=None, raw=False):
        r = self.c.request(method, path, json=body, headers=self.headers)
        if raw:
            return r.status_code, r.content, r.headers.get("content-type", "")
        return r.status_code, r.json()


def _rpc(app, *msgs):
    from polybench.ai.mcp_server import serve
    out = io.StringIO()
    serve(app, io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out, heartbeat=False)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_mcp_initialize_list_and_call(tmp_path, monkeypatch):
    from polybench.ai.mcp_server import TOOLS
    sess, c, _ = make(tmp_path, monkeypatch)
    app = _TCApp(c)
    res = _rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
               {"jsonrpc": "2.0", "method": "notifications/initialized"},
               {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
               {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_state", "arguments": {}}},
               {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "propose_value", "arguments":
                   {"board": "S01", "test_id": "U01", "value": "4.9", "reason": "operator said 4.9"}}},
               {"jsonrpc": "2.0", "id": 5, "method": "nope"})
    assert [r["id"] for r in res] == [1, 2, 3, 4, 5]
    assert res[0]["result"]["serverInfo"]["name"] == "polybench"
    names = {t["name"] for t in res[1]["result"]["tools"]}
    assert names == set(TOOLS) and not names & {"accept", "accept_result", "approve_plan", "override_gate", "run_action"}
    assert json.loads(res[2]["result"]["content"][0]["text"])["batch"] == "ai"
    assert res[3]["result"]["isError"] is False
    assert res[4]["error"]["code"] == -32601
    p = events(sess, "proposal")[-1]["payload"]
    assert p["source"]["client"] == "pytest-mcp" and p["source"]["kind"] == "assistant"


def test_mcp_schemas_are_valid():
    from polybench.ai.mcp_server import TOOLS
    for name, t in TOOLS.items():
        s = t["inputSchema"]
        assert s["type"] == "object" and set(s["required"]) <= set(s["properties"]), name
        assert all("required" not in v for v in s["properties"].values()), name


def test_mcp_refuses_non_loopback(capsys):
    from polybench.ai.mcp_server import _loopback, main
    assert _loopback("http://127.0.0.1:8750") and _loopback("http://localhost:1") and _loopback("http://[::1]:8750")
    assert not _loopback("http://203.0.113.5:8750") and not _loopback("http://example.com")
    assert main(["--url", "http://203.0.113.5:8750"]) == 2
    assert "loopback" in capsys.readouterr().err


# ---------------------------------------------------------------- installer
def test_install_skills_copies_and_links(tmp_path):
    from polybench.ai.cli import SKILLS, install_skills
    out = io.StringIO()
    assert install_skills(tmp_path / "s", False, False, out) == 0
    names = sorted(p.name for p in (tmp_path / "s").iterdir())
    assert names == sorted(p.name for p in SKILLS.iterdir() if (p / "SKILL.md").is_file()) and len(names) >= 4
    assert install_skills(tmp_path / "s", False, False, out) == 0 and "skip" in out.getvalue()
    assert install_skills(tmp_path / "l", True, False, io.StringIO()) == 0
    assert all((tmp_path / "l" / n).is_symlink() for n in names)


def test_skills_are_demo_only():
    import re
    files = list((ROOT / "ai" / "skills").rglob("*.md")) + [ROOT / "AGENTS.md"] + list((ROOT / "docs").glob("AI_CONNECTOR_*.md"))
    for f in files:
        t = f.read_text()
        assert not re.search("|".join(["\\bstra" + "in", "mcmu" + "rtry", "EV" + "T1", "/Us" + "ers/"]), t, re.I), f
        if f.name == "SKILL.md":
            assert t.startswith("---\nname: ") and "\ndescription: " in t.split("---")[1], f


# ---------------------------------------------------------------- AOI through the assistant (synthetic run, simulated)
from test_aoi_polybench import proto_dir  # noqa: E402,F401  (fixture: a small AOI-configured protocol)


@needs_aoi
def test_aoi_findings_photos_and_thumbnails(tmp_path, proto_dir, monkeypatch):  # noqa: F811
    from polybench.aoi import runner
    from polybench.protocol import load
    from polybench.server import Session, create_app
    monkeypatch.delenv("POLYBENCH_AI_PERMISSIONS", raising=False)
    sess = Session(load(proto_dir), tmp_path / "data", "B", True, None, None, "pat")
    c = TestClient(create_app(sess))
    out = runner.run(sess.dir, sess.dir / "runs" / "0001_aoi", load(proto_dir).data["aoi"], simulate=True, log=lambda *_: None)     # synthetic: no photo yet
    import io
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (8, 8), (40, 120, 40)).save(buf, "JPEG")   # a real JPEG: the photo import now refuses undecodable bytes (EXIF strip, DIFF-06)
    assert c.post("/api/aoi/photos?name=IMG_1-S01.jpg", content=buf.getvalue()).json()["board"] == "S01"
    sess.engine.aoi_run_done("S01", {"run": 1, **out})
    a = c.get("/api/ai/aoi?board=S01", headers=AI).json()
    assert a["configured"] and a["photos"][0]["board"] == "S01" and a["findings"]
    f = next(x for x in a["findings"] if x.get("evidence_urls"))
    assert f["decision"] is None and f["evidence"][0].endswith((".png", ".jpg"))
    assert c.get(f["evidence_urls"][0], headers=AI).status_code == 200
    res = _rpc(_TCApp(c), {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "get_aoi", "arguments": {"board": "S01", "thumbnails": True, "max_images": 2}}})
    kinds = [x["type"] for x in res[0]["result"]["content"]]
    assert kinds[0] == "text" and "image" in kinds
    t = c.get("/api/ai/troubleshoot?board=S01", headers=AI).json()
    assert t["aoi_all_findings_on_board"] and not any("AOI" in m for m in t["missing"])
