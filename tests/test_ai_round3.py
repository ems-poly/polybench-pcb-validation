"""AI connector fix round 3 (re-review N1 to N5, card trial, operator name). HOST-ONLY: TestClient and subprocess on a copy of the DEMO protocol."""
import json
import re
import shlex
import subprocess

from test_ai_connector import AI, PLAN, make
from test_ai_fixes import draft, drop_rows


def approve(c, text, summary, operator="Pat"):
    assert draft(c, text, summary)["ok"]
    st = c.get("/api/ai/plan_draft").json()
    return c.post("/api/ai/plan_approve", json={"hash": st["hash"], "operator": operator}), st


# ---------------------------------------------------------------- N1
def test_n1_fallback_is_the_plan_that_is_live_now(tmp_path, monkeypatch):
    from polybench.ai import recovery
    from polybench.protocol import load
    sess, c, d = make(tmp_path, monkeypatch)
    r, _ = approve(c, drop_rows(PLAN, r"\| U01 \|"), "retire U01")
    assert r.status_code == 200 and "U01" in r.json()["tests_retired"]
    live = (d / "PLAN.md").read_text(encoding="utf-8")
    (d / "PLAN.md").write_text(drop_rows(live, r"\| P0[1-4] \|"), encoding="utf-8")                      # hand-broken master
    top = d / "demo.pcbv.yaml"
    proto, info = recovery.try_recover(load(top), top, tmp_path / "data", "ai")
    assert proto is not None
    assert open(info["restored_from"], encoding="utf-8").read() == live                                  # the approved plan, not the one it replaced
    ids = {t["id"] for _s, _sec, t in proto.tests()}
    assert "U01" not in ids and "P01" in ids                                           # U01 stays retired


def test_n1_fallback_command_works_when_the_master_was_deleted(tmp_path, monkeypatch):
    from polybench.ai import recovery
    from polybench.protocol import load
    sess, c, d = make(tmp_path, monkeypatch)
    approve(c, PLAN.replace("First issue", "First issue."), "dot")
    live = (d / "PLAN.md").read_text(encoding="utf-8")
    (d / "PLAN.md").write_text("garbage", encoding="utf-8")
    top = d / "demo.pcbv.yaml"
    _p, info = recovery.try_recover(load(top), top, tmp_path / "data", "ai")
    (d / "PLAN.md").unlink()                                                           # deleted, not just broken
    subprocess.run(info["command"], shell=True, check=True)
    assert (d / "PLAN.md").read_text(encoding="utf-8") == live and not (d / "PLAN.md.broken").exists()
    # and when the file exists, the broken copy is kept
    (d / "PLAN.md").write_text("garbage", encoding="utf-8")
    subprocess.run(info["command"], shell=True, check=True)
    assert (d / "PLAN.md.broken").read_text(encoding="utf-8") == "garbage"


# ---------------------------------------------------------------- N2
def test_n2_lint_command_in_the_message_reports_the_error(tmp_path, monkeypatch, capsys):
    from polybench.ai import recovery
    from polybench.cli import main
    from polybench.protocol import load
    sess, c, d = make(tmp_path, monkeypatch)
    (d / "PLAN.md").write_text(drop_rows(PLAN, r"\| P0[1-4] \|"), encoding="utf-8")                      # lints clean as a plan, does not load
    top = d / "demo.pcbv.yaml"
    proto, info = recovery.try_recover(load(top), top, tmp_path / "nodata", "ai")
    assert proto is None
    argv = shlex.split(info["lint_command"])
    assert argv[:3] == ["polybench", "plan", "lint"] and "--protocol" in argv
    capsys.readouterr()
    rc = main(argv[1:])
    err = capsys.readouterr().err
    assert rc == 2 and "error" in err and "protocol load with this plan: " in err and "0 errors" not in err.split("protocol load with this plan:")[1].splitlines()[0]
    (d / "PLAN.md").write_text(PLAN, encoding="utf-8")                                                    # a good plan still lints clean
    assert main(argv[1:]) == 0


# ---------------------------------------------------------------- N5
def test_n5_fixed_token_warns_and_is_refused_on_8750(monkeypatch):
    from polybench.ai.guard import op_token_notice
    monkeypatch.delenv("POLYBENCH_OP_TOKEN", raising=False)
    monkeypatch.delenv("POLYBENCH_OP_TOKEN_ALLOW", raising=False)
    assert op_token_notice(8750) == ("", False)
    monkeypatch.setenv("POLYBENCH_OP_TOKEN", "x")
    msg, refuse = op_token_notice(9010)
    assert msg.startswith("WARNING") and not refuse
    msg, refuse = op_token_notice(8750)
    assert refuse and "REFUSED" in msg and "WARNING" in msg
    monkeypatch.setenv("POLYBENCH_OP_TOKEN_ALLOW", "1")
    msg, refuse = op_token_notice(8750)
    assert msg.startswith("WARNING") and not refuse


def test_n5_serve_refuses_before_loading_anything(tmp_path, monkeypatch, capsys):
    from polybench.cli import main
    monkeypatch.setenv("POLYBENCH_OP_TOKEN", "x")
    monkeypatch.delenv("POLYBENCH_OP_TOKEN_ALLOW", raising=False)
    rc = main(["--protocol", str(tmp_path / "none.pcbv.yaml"), "--port", "8750", "--data-dir", str(tmp_path), "--no-browser"])
    assert rc == 2 and "REFUSED" in capsys.readouterr().err


def test_n5_e2e_scripts_do_not_teach_the_variable():
    import pathlib
    root = pathlib.Path(__file__).parent / "e2e"
    for f in root.glob("*.py"):
        if f.name != "opauth.py":
            assert "POLYBENCH_OP_TOKEN" not in f.read_text(encoding="utf-8"), f.name
    assert "tests only" in (root / "opauth.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- N3
def test_n3_fresh_cookie_after_a_server_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from polybench.server import create_app
    monkeypatch.delenv("POLYBENCH_OP_TOKEN", raising=False)
    sess, _c, _ = make(tmp_path, monkeypatch)
    tab = TestClient(create_app(sess))
    tab.cookies.clear()
    tab.get("/")                                                   # the open tab got its cookie from the first server
    body = {"board": "S01", "test_id": "U01", "value": "5", "confirm": True}
    assert tab.post("/api/cmd/entry", json=body).status_code == 200
    sess2, _c2, _ = make(tmp_path / "again", monkeypatch)            # the restart: same tab, a new server with a new random token
    app2 = create_app(sess2)
    tab.app = app2
    tab._transport.app = app2
    r = tab.post("/api/cmd/entry", json=body)
    assert r.status_code == 403 and "operator session" in r.json()["error"]
    s = tab.get("/api/session")                                    # what the page does when its event stream reconnects
    assert s.status_code == 200 and "pb_op=" in s.headers["set-cookie"]
    assert tab.post("/api/cmd/entry", json=body).status_code == 200
    a = tab.get("/api/session", headers=AI)                         # the assistant never gets it
    assert "set-cookie" not in a.headers


# ---------------------------------------------------------------- N4
def test_n4_draft_may_not_delete_revision_rows(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    r = draft(c, drop_rows(PLAN, r"\| r1 \|"), "tidy")
    assert r["lint"]["errors"] >= 1 and any("append-only" in i["msg"] for i in r["lint"]["issues"])
    st = c.get("/api/ai/plan_draft").json()
    assert not st["can_approve"]
    assert c.post("/api/ai/plan_approve", json={"hash": st["hash"], "operator": "E"}).status_code == 409
    assert re.search(r"^\| r1 \|", (d / "PLAN.md").read_text(encoding="utf-8"), re.M)


def test_n4_new_row_must_be_labelled_rn(tmp_path, monkeypatch):
    sess, c, _ = make(tmp_path, monkeypatch)
    r = draft(c, PLAN.rstrip("\n") + "\n| v2 | 2026-10-17 | change | assistant |\n", "x")
    assert any("labelled rN" in i["msg"] for i in r["lint"]["issues"]) and r["lint"]["errors"] >= 1
    r = draft(c, PLAN.rstrip("\n") + "\n| r2x | 2026-10-17 | change | assistant |\n", "x")
    assert r["lint"]["errors"] >= 1


def test_n4_at_most_one_new_row_and_exactly_one_after_approval(tmp_path, monkeypatch):
    from polybench.ai.plan_draft import revision_rows
    sess, c, d = make(tmp_path, monkeypatch)
    two = PLAN.rstrip("\n") + "\n| r2 | 2026-10-17 | a | assistant |\n| r3 | 2026-10-17 | b | assistant |\n"
    r = draft(c, two, "two rows")
    assert r["lint"]["errors"] >= 1 and any("adds 2 revision rows" in i["msg"] for i in r["lint"]["issues"])
    n0 = len(revision_rows(PLAN))
    ok, _ = approve(c, PLAN.replace("First issue", "First issue."), "no row of its own")
    assert ok.status_code == 200 and len(revision_rows((d / "PLAN.md").read_text(encoding="utf-8"))) == n0 + 1
    one = (d / "PLAN.md").read_text(encoding="utf-8").rstrip("\n") + "\n| r3 | 2026-10-17 | own | assistant |\n"
    ok, _ = approve(c, one, "own row")
    assert ok.status_code == 200 and len(revision_rows((d / "PLAN.md").read_text(encoding="utf-8"))) == n0 + 2


# ---------------------------------------------------------------- operator name and card
def test_empty_operator_name_is_refused_with_422(tmp_path, monkeypatch):
    sess, c, d = make(tmp_path, monkeypatch)
    assert draft(c, PLAN.replace("First issue", "First issue."), "dot")["ok"]
    h = c.get("/api/ai/plan_draft").json()["hash"]
    for body in ({"hash": h}, {"hash": h, "operator": ""}, {"hash": h, "operator": "   "}):
        r = c.post("/api/ai/plan_approve", json=body)
        assert r.status_code == 422 and "operator name required" in r.json()["error"]
    assert (d / "PLAN.md").read_text(encoding="utf-8") == PLAN
    assert c.post("/api/ai/plan_approve", json={"hash": h, "operator": "Pat"}).status_code == 200


def test_card_says_the_trial_is_interactive_and_gives_the_p_form(tmp_path, monkeypatch):
    import pathlib

    import polybench
    sess, c, _ = make(tmp_path, monkeypatch)
    s = c.get("/api/state").json()["ai"]["setup"]
    assert "--allowedTools mcp__polybench" in s["claude_trial_p"] and s["claude_trial_p"].startswith("claude -p --mcp-config ")
    js = (pathlib.Path(polybench.__file__).parent / "static" / "ai_card.js").read_text(encoding="utf-8")
    assert "interactive" in js and "claude_trial_p" in js
    from polybench.ai.cli import setup_text
    assert "non-interactive" in setup_text("http://127.0.0.1:8750")
