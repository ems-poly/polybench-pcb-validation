"""AI connector fix round (gate review D1 to D10, UX 1 and 2). HOST-ONLY: TestClient on a copy of the DEMO protocol."""
import json
import re
import threading

from test_ai_connector import AI, PLAN, events, make


def drop_rows(text, pattern):
    return "\n".join(ln for ln in text.splitlines() if not re.match(pattern, ln)) + "\n"


def draft(c, text, summary="change"):
    return c.post("/api/ai/plan_draft", json={"text": text, "summary": summary}, headers=AI).json()


# ---------------------------------------------------------------- D1
def test_d1_approve_refuses_a_draft_the_protocol_cannot_load(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    bad = drop_rows(PLAN, r"\| P0[1-4] \|")
    r = draft(c, bad, "drop P01-P04")
    assert r["lint"]["errors"] == 0 and r["load"]["errors"] >= 1                        # lints clean, does not load
    st = c.get("/api/ai/plan_draft").json()
    assert not st["can_approve"] and "does not load" in st["why_not"]
    r = c.post("/api/ai/plan_approve", json={"hash": st["hash"], "operator": "Pat"})
    assert r.status_code == 409 and r.json()["load"]["issues"]
    assert (d / "PLAN.md").read_text(encoding="utf-8") == PLAN and not events(sess, "plan_approved")


def test_d1_recovery_starts_on_the_last_good_plan(tmp_path, monkeypatch):
    from polybench.ai import recovery
    from polybench.protocol import load
    sess, c, d = make(tmp_path, monkeypatch)
    draft(c, PLAN.replace("First issue", "First issue."), "dot")
    h = c.get("/api/ai/plan_draft").json()["hash"]
    assert c.post("/api/ai/plan_approve", json={"hash": h, "operator": "Pat"}).status_code == 200
    good_master = (d / "PLAN.md").read_text(encoding="utf-8")
    (d / "PLAN.md").write_text(drop_rows(good_master, r"\| P0[1-4] \|"), encoding="utf-8")               # the bricked master (edited outside)
    top = d / "demo.pcbv.yaml"
    bad = load(top)
    assert bad.errors
    proto, info = recovery.try_recover(bad, top, tmp_path / "data", "ai")
    assert proto is not None and not proto.errors and info["restored_from"].endswith(".md")
    assert "cp -p" in info["command"] and "PLAN.md.broken" in info["command"]
    assert "STARTED WITH THE LAST GOOD PLAN" in recovery.console_text(info, True)
    assert "| P01 |" not in (d / "PLAN.md").read_text(encoding="utf-8")                                    # the master was not touched
    # the session runs on it, drafts still act on the master, and approving a corrected draft clears the banner
    from fastapi.testclient import TestClient

    from polybench.server import Session, create_app
    s2 = Session(proto, tmp_path / "data2", "ai2", True, 3, None, operator="Op")
    s2.recovery = info
    c2 = TestClient(create_app(s2))
    assert c2.get("/api/state").json()["ai"]["recovery"]["restored_from"] == info["restored_from"]
    assert any(t["id"] == "P01" for _s, _sec, t in s2.proto.tests())
    assert draft(c2, good_master, "restore P01-P04")["ok"]
    st = c2.get("/api/ai/plan_draft").json()
    assert st["master"] == str(d / "PLAN.md") and st["can_approve"]
    r = c2.post("/api/ai/plan_approve", json={"hash": st["hash"], "operator": "Pat"}).json()
    assert r["reload"]["ok"] and s2.recovery is None
    assert c2.get("/api/state").json()["ai"]["recovery"] is None


def test_d1_no_history_prints_the_recovery_command(tmp_path, monkeypatch):
    from polybench.ai import recovery
    from polybench.protocol import load
    sess, c, d = make(tmp_path, monkeypatch)
    (d / "PLAN.md").write_text(drop_rows(PLAN, r"\| P0[1-4] \|"), encoding="utf-8")
    top = d / "demo.pcbv.yaml"
    proto, info = recovery.try_recover(load(top), top, tmp_path / "nodata", "ai")
    assert proto is None
    txt = recovery.console_text(info, False)
    assert f"polybench plan lint {d / 'PLAN.md'}" in txt and "no earlier plan" in txt


def test_d1_cmd_serve_exits_2_without_history_and_starts_with_it(tmp_path, monkeypatch, capsys):
    from polybench import cli
    sess, c, d = make(tmp_path, monkeypatch)
    (d / "PLAN.md").write_text(drop_rows(PLAN, r"\| P0[1-4] \|"), encoding="utf-8")

    class A:
        protocol = str(d / "demo.pcbv.yaml"); data_dir = str(tmp_path / "empty"); batch = "ai"; simulate = True; boards = 3; ecad = None; operator = "Op"
        port = 8999; no_browser = True
    assert cli.cmd_serve(A) == 2
    assert "no earlier plan" in capsys.readouterr().err


# ---------------------------------------------------------------- D2
def test_d2_concurrent_approves_exactly_one_succeeds(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    draft(c, PLAN.replace("First issue", "First issue."), "dot")
    h = c.get("/api/ai/plan_draft").json()["hash"]
    codes = []

    def go():
        codes.append(c.post("/api/ai/plan_approve", json={"hash": h, "operator": "TabA"}).status_code)
    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes).count(200) == 1, codes
    assert len(events(sess, "plan_approved")) == 1
    assert len(re.findall(r"^\| r\d+ \|", (d / "PLAN.md").read_text(encoding="utf-8"), re.M)) == 2


# ---------------------------------------------------------------- D4
def test_d4_context_reports_master_and_draft_separately(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    tp = c.get("/api/ai/context", headers=AI).json()["test_plan"]
    assert tp["master_exists"] is True and tp["draft_exists"] is False and "exists" not in tp
    draft(c, PLAN.replace("First issue", "First issue."), "dot")
    tp = c.get("/api/ai/context", headers=AI).json()["test_plan"]
    assert tp["master_exists"] is True and tp["draft_exists"] is True


# ---------------------------------------------------------------- D6
def test_d6_a_draft_with_its_own_revision_row_gets_no_second_one(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    text = PLAN.replace("| U06 | R4 across its pads |", "| U06 | R4 across its two pads |").rstrip("\n") + "\n| r2 | 2026-10-17 | U06 wording | assistant |\n"
    assert draft(c, text, "U06 wording")["lint"]["errors"] == 0
    h = c.get("/api/ai/plan_draft").json()["hash"]
    r = c.post("/api/ai/plan_approve", json={"hash": h, "operator": "Pat"}).json()
    assert r["revision_row"] == "draft" and r["revision"] == "r2"
    rows = re.findall(r"^\| r\d+ \|.*$", (d / "PLAN.md").read_text(encoding="utf-8"), re.M)
    assert len(rows) == 2 and "AI draft approved" not in rows[-1]


def test_d6_a_draft_without_a_row_gets_exactly_one(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    draft(c, PLAN.replace("First issue", "First issue."), "dot")
    h = c.get("/api/ai/plan_draft").json()["hash"]
    r = c.post("/api/ai/plan_approve", json={"hash": h, "operator": "Pat"}).json()
    assert r["revision_row"] == "added"
    assert len(re.findall(r"^\| r\d+ \|.*$", (d / "PLAN.md").read_text(encoding="utf-8"), re.M)) == 2


# ---------------------------------------------------------------- D7 and UX 2
def test_d7_retired_results_are_readable_and_ux2_lists_ids(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    assert c.post("/api/cmd/entry", json={"board": "S01", "test_id": "U06", "value": "10.0", "confirm": True}).status_code == 200
    draft(c, drop_rows(PLAN, r"\| U06 \|"), "drop U06")
    st = c.get("/api/ai/plan_draft").json()
    assert st["load"]["retired"] == ["U06"] and st["load"]["added"] == [] and st["load"]["regenerates"] is True
    r = c.post("/api/ai/plan_approve", json={"hash": st["hash"], "operator": "Pat"}).json()
    assert r["tests_retired"] == ["U06"] and events(sess, "plan_approved")[-1]["payload"]["tests_retired"] == ["U06"]
    t = c.get("/api/ai/test?test_id=U06&board=S01", headers=AI)
    assert t.status_code == 200 and t.json()["retired"] is True
    assert t.json()["test"]["results"][0]["value"] == "10.0" and t.json()["test"]["results"][0]["board"] == "S01"
    ts = c.get("/api/ai/troubleshoot?board=S01&test_id=U06", headers=AI).json()
    assert ts["retired"] is True and ts["readings"][0]["this_board"]["value"] == "10.0"
    b = c.get("/api/ai/board?board=S01", headers=AI).json()
    assert [x["id"] for x in b["retired_results"]] == ["U06"]
    assert all(t["test_id"] != "U06" for s in b["steps"] for t in s["tests"])
    assert c.get("/api/ai/test?test_id=NOPE", headers=AI).status_code == 404


# ---------------------------------------------------------------- D10
def test_d10_operator_routes_need_the_session_cookie(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from polybench.server import create_app
    monkeypatch.delenv("POLYBENCH_OP_TOKEN", raising=False)                  # a random token, as in production
    sess, _c, _ = make(tmp_path, monkeypatch)
    c = TestClient(create_app(sess))
    c.cookies.clear()
    body = {"board": "S01", "test_id": "U01", "value": "5", "confirm": True}
    assert c.post("/api/cmd/entry", json=body).status_code == 403                      # a script with no header and no cookie
    assert c.post("/api/cmd/entry", json=body, headers={"X-Polybench-Client": "operator"}).status_code == 403
    assert c.post("/api/cmd/entry", json=body, cookies={"pb_op": "guess"}).status_code == 403
    assert c.post("/api/ai/plan_approve", json={"hash": "x"}).status_code == 403
    assert c.get("/api/state").status_code == 200                                      # reads need nothing
    r = c.get("/")
    ck = r.headers["set-cookie"]
    assert ck.startswith("pb_op=") and "httponly" in ck.lower() and "samesite=strict" in ck.lower()
    token = ck.split("pb_op=")[1].split(";")[0]
    assert len(token) >= 32
    assert c.post("/api/cmd/entry", json=body).status_code == 200                      # the browser, now with the cookie
    assert c.post("/api/cmd/entry", json=body, headers=AI).status_code == 403          # the assistant header still wins
    # the token is nowhere else
    for text in (json.dumps(c.get("/api/state").json()), json.dumps(c.get("/api/ai/status").json()), sess.journal.path.read_text(encoding="utf-8"),
                 json.dumps(c.get("/api/journal").json())):
        assert token not in text
    # an assistant GET / does not receive the cookie
    assert "set-cookie" not in TestClient(create_app(sess)).get("/", headers=AI).headers


def test_d10_assistant_routes_do_not_need_the_cookie(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    c.cookies.clear()
    r = c.post("/api/ai/note", json={"text": "hello"}, headers=AI)
    assert r.status_code == 200


# ---------------------------------------------------------------- UX 1 and D5
@pytest.mark.skipif(sys.platform == 'win32', reason='needs a Windows ACL check: POSIX permission bits (0600) do not exist on Windows; the file sits in the user profile')
def test_ux1_trial_config_is_a_separate_file_with_0600(tmp_path):
    from polybench.ai.cli import write_trial_config
    f = write_trial_config("http://127.0.0.1:8750", tmp_path / "x" / "trial.json")
    cfg = json.loads(f.read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["polybench"]["args"] == ["--client", "claude-code"]
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_ux1_card_offers_trial_before_permanent(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    s = c.get("/api/state").json()["ai"]["setup"]
    assert s["claude_trial"].startswith("claude --mcp-config ") and s["claude"].startswith("claude mcp add")
    js = (__import__("pathlib").Path(__import__("polybench").__file__).parent / "static" / "ai_card.js").read_text(encoding="utf-8")
    assert "does not change your Claude Code configuration" in js and "EDITS your Claude Code configuration" in js


def test_d5_skills_ship_inside_the_package(tmp_path):
    import polybench
    from polybench.ai import cli
    from pathlib import Path
    assert cli.SKILLS == Path(polybench.__file__).parent / "ai" / "skills"
    assert sorted(p.name for p in cli.SKILLS.iterdir() if (p / "SKILL.md").is_file()) == [
        "polybench-bench-assistant", "polybench-plugin-authoring", "polybench-test-firmware", "polybench-test-plan",
        "polybench-troubleshooting"]
    import io
    assert cli.install_skills(tmp_path / "t", False, False, out=io.StringIO()) == 0


def test_skills_and_agents_carry_the_data_not_instructions_rule():
    from pathlib import Path

    from polybench.ai import cli
    files = list(cli.SKILLS.glob("*/SKILL.md")) + [Path(cli.__file__).resolve().parents[3] / "AGENTS.md"]
    assert len(files) == 6
    for f in files:
        assert "Data, never instructions" in f.read_text(encoding="utf-8"), f


def test_d10_every_non_get_route_refuses_a_request_with_no_session(tmp_path, monkeypatch):
    """Sweep: with neither the assistant header nor the operator cookie, no mutating route does anything."""
    sess, c, _ = make(tmp_path, monkeypatch)
    c.cookies.clear()
    n = len(sess.journal.events)
    fill = {"{name}": "accept", "{path:path}": "x.jpg", "{board}": "S01"}
    seen = 0
    for r in c.app.routes:
        path, methods = getattr(r, "path", None), getattr(r, "methods", None)
        if not path or not methods or path.startswith("/static"):
            continue
        for k, v in fill.items():
            path = path.replace(k, v)
        for m in sorted(methods - {"GET", "HEAD", "OPTIONS"}):
            assert c.request(m, path, json={}).status_code == 403, (m, path)
            seen += 1
    assert seen > 40 and len(sess.journal.events) == n
