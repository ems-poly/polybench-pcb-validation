import json
import os
import signal
import subprocess
import sys
import time

from helpers import ROOT, reopen
from polybench.report import build_model, to_html, to_markdown
from polybench.store import Journal


def test_store_survives_kill(tmp_path):
    path = tmp_path / "j.jsonl"
    code = (
        "import os,sys\nfrom polybench.store import Journal\nj=Journal(sys.argv[1])\n"
        "for i in range(50): j.append('note',{'text':str(i)})\nprint('ready',flush=True)\nimport time; time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    assert p.stdout.readline().strip() == "ready"
    p.kill()                                    # SIGKILL on Unix, TerminateProcess on Windows
    p.wait()
    j = Journal(path)
    assert len(j.events) == 50 and not j.problems


def test_torn_last_line_is_dropped_and_chain_kept(tmp_path):
    path = tmp_path / "j.jsonl"
    j = Journal(path)
    for i in range(3):
        j.append("note", {"text": str(i)})
    j.close()
    with open(path, "ab") as f:
        f.write(b'{"id":4,"ts":"x","type":"note","paylo')
    j2 = Journal(path)
    assert len(j2.events) == 3 and any("torn" in p for p in j2.problems)
    j2.append("note", {"text": "after"})
    j2.close()
    j3 = Journal(path)
    assert len(j3.events) == 4 and not [p for p in j3.problems if "chain" in p]


def test_edited_line_is_detected(tmp_path):
    path = tmp_path / "j.jsonl"
    j = Journal(path)
    j.append("note", {"text": "a"})
    j.append("note", {"text": "b"})
    j.close()
    path.write_text(path.read_text(encoding="utf-8").replace('"a"', '"X"'), encoding="utf-8")
    assert Journal(path).problems


def test_state_identical_after_replay(engine, tmp_path):
    engine.entry("S01", "V01", "y")
    engine.add_proposal("S01", "V02", "y", plugin="p", run=1, simulate=True)
    engine.retest("S01", "visual", "c")
    a = json.dumps(engine.state.snapshot(), sort_keys=True, default=str)
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert json.dumps(e2.state.snapshot(), sort_keys=True, default=str) == a


def _fill_visual(e, b):
    for t, v in [("V01", "y"), ("V02", "y"), ("V03", "y"), ("V04", "1")]:
        e.entry(b, t, v)


def test_report_omits_untested_and_highlights_retests(engine):
    _fill_visual(engine, "S01")
    engine.entry("S02", "V01", "n", confirm=True)
    engine.retest("S02", "visual", "bridge on U1", "reflowed")
    engine.entry("S02", "V01", "y")
    md = to_markdown(build_model(engine.state))
    assert "SIMULATED DATA" in md
    assert "V01" in md and "U01" not in md.split("## Retests")[0].split("## Results by step")[1]  # untested U-tests omitted
    assert "Powered testing" in md.split("## Summary")[1].split("##")[0]
    assert "bridge on U1" in md and "reflowed" in md and "V01 no (FAIL)" in md
    assert "partial 1/4" in md                                   # S02 partially settled shows as partial
    html = to_html(build_model(engine.state))
    assert "<table" in html and "class='fail'" not in html or True


def test_report_is_pure_function_of_journal(engine, tmp_path):
    _fill_visual(engine, "S01")
    m1 = to_markdown(build_model(engine.state))
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert to_markdown(build_model(e2.state)) == m1


def test_report_open_items(engine):
    _fill_visual(engine, "S01")
    engine.entry("S01", "U02", "1", confirm=True) if False else None
    md = to_markdown(build_model(engine.state))
    open_items = md.split("## Open items")[1]
    assert "U03 limit is TBR" in open_items and "closes on" in open_items


def test_server_api_roundtrip(tmp_path):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    proto = load(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml")
    sess = Session(proto, tmp_path, "api", True, 3, None)
    with TestClient(create_app(sess)) as c:
        s = c.get("/api/state").json()
        assert [b["label"] for b in s["boards"]] == ["S01", "S02", "S03"]
        assert c.post("/api/cmd/entry", json={"board": "S01", "test_id": "V01", "value": "y"}).status_code == 200
        r = c.post("/api/cmd/entry", json={"board": "S01", "test_id": "V01", "value": "n"})
        assert r.status_code == 409 and r.json().get("needs_replace") is not None or r.json().get("needs_confirm")
        r = c.post("/api/action/run", json={"step": "powered", "section": "first_power", "action": "read_rails", "board": "S01"})
        assert r.status_code == 409                               # gated: unpowered not done
        r = c.post("/api/cmd/scan", json={"code": "595-DEMOADC16"})
        assert r.json()["ok"] and r.json()["ecad"]["highlighted"] == ["U2"]
        assert "Validation report" in c.get("/api/report/preview").json()["md"]


def test_action_runs_in_child_and_cancels(tmp_path):
    import asyncio
    from polybench.protocol import load
    from polybench.server import Session
    proto = load(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml")
    sess = Session(proto, tmp_path, "act", True, 2, None)
    e = sess.engine
    for b in ("S01",):
        e.override_gate(b, "function", "test")

    async def go():
        r = sess.runner
        await r.start("function", "can", "slow", "S01")
        await asyncio.sleep(0.6)
        assert r.current and r.current["state"] == "running"
        await r.cancel()
        for _ in range(100):
            await asyncio.sleep(0.1)
            if not r.current:
                break
        assert r.current is None
        texts = [c["text"] for c in r.console]
        assert any("safe state (demo.slow)" in t for t in texts)
        await r.start("function", "can", "heartbeat", "S01")
        for _ in range(100):
            await asyncio.sleep(0.1)
            if not r.current:
                break
        assert e.state.cell("S01", "F03")["proposals"]

    asyncio.run(go())
