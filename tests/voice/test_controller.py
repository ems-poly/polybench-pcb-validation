import json
import types
import time

import pytest
from fastapi.testclient import TestClient

from polybench.engine import Engine
from polybench.server import create_app
from polybench.store import Journal
from polybench.voice import config as vcfg
from polybench.voice import stt

from conftest import make_session, speech


def vals(*rows):
    """rows: (board, value, unit) -> a values interpretation for test U01 (or the cursor test)."""
    return {"intent": "values", "values": [{"board": b, "test_id": "U01", "value": v, "unit": u, "heard": f"{v} {u}"} for b, v, u in rows]}


def cell(vc, b, t="U01"):
    return vc.eng.state.cell(b, t)


def wait(cond, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_value_becomes_voice_proposal_and_row_mode_moves_on(vc, nlu):
    nlu.script["s1 twenty"] = vals(("S01", "20", "kOhm"))
    e = vc.handle_text("s1 twenty")
    p = list(cell(vc, "S01")["proposals"].values())
    assert len(p) == 1 and p[0]["source"]["kind"] == "voice" and p[0]["source"]["utterance"] == e["utt"]
    assert p[0]["verdict"] == "pass" and p[0]["origin"] == "simulated"
    assert e["outcome"] == "proposed" and e["reply"].startswith("S1 20 kilo ohms.") and e["reply"].endswith("Say next test to accept, or reject.")
    assert "Next S2" in e["reply"] and vc.ctx["board"] == "S02"
    assert {"kind": "voice_nav", "step": "unpowered", "test_id": "U01", "board": "S02"} in vc.msgs
    assert any(m["kind"] == "voice" and m["entry"]["utt"] == e["utt"] for m in vc.msgs) and {"kind": "state"} in vc.msgs


def test_all_boards_skips_gated_and_stopped_boards(vc, nlu):
    def allb(view):
        assert view.boards == ["S01", "S02"]                      # S03 is gated
        return vals(*[(b, "20", "kOhm") for b in view.boards])
    nlu.script["all twenty"] = allb
    vc.handle_text("all twenty")
    assert cell(vc, "S01")["proposals"] and cell(vc, "S02")["proposals"] and not cell(vc, "S03")["proposals"]
    # a stop on S02 takes it out of "all boards" and an explicit value for it is refused
    vc.eng._emit("stop", {"board": "S02", "step": "unpowered", "test_id": "U01", "attempt": 1, "text": "shorted", "value": "0.5"})
    nlu.script["s2"] = vals(("S02", "30", "kOhm"))
    nlu.script["s3"] = vals(("S03", "30", "kOhm"))
    assert vc.handle_text("s2")["outcome"] == "rejected" and "stopped" in vc.handle_text("s2")["reply"]
    e = vc.handle_text("s3")
    assert e["outcome"] == "rejected" and "gated" in e["reply"] and not cell(vc, "S03")["proposals"]
    vc.handle_text("all twenty")
    assert nlu.views[-1].boards == ["S01"]


def test_accept_takes_clean_fail_needs_accept_fail(vc, nlu):
    nlu.script["two values"] = vals(("S01", "20", "kOhm"), ("S02", "5", "kOhm"))
    nlu.script["accept"] = {"intent": "accept"}
    nlu.script["accept fail"] = {"intent": "accept_fail"}
    e = vc.handle_text("two values")
    assert "1 pass, 1 fail on S2" in e["reply"] and e["reply"].endswith("Say accept, accept fail, or reject.")
    a = vc.handle_text("accept")
    assert cell(vc, "S01")["status"] == "accepted" and cell(vc, "S02")["status"] == "empty"
    assert "Accepted." in a["reply"] and "held" in a["reply"] and "accept fail" in a["reply"]
    assert vc.handle_text("accept")["reply"].startswith("S2") or "held" in vc.handle_text("accept")["reply"]
    assert cell(vc, "S02")["status"] == "empty"                   # plain accept never takes a FAIL
    f = vc.handle_text("accept fail")
    c = cell(vc, "S02")
    assert c["status"] == "accepted" and c["accepted_fail"] and c["verdict"] == "fail" and c["source"]["kind"] == "voice"
    assert "Accepted fail on S2" in f["reply"]


def test_single_fail_is_read_back_with_the_limit(vc, nlu):
    nlu.script["bad"] = vals(("S01", "5", "kOhm"))
    e = vc.handle_text("bad")
    assert e["reply"].startswith("S1 5 kilo ohms is outside the limit: say accept fail, or reject.")


def test_guess_limit_never_fails_and_is_held(vc, nlu):
    nlu.script["u2"] = {"intent": "values", "values": [{"board": "S01", "test_id": "U03", "value": "0.5", "unit": "kOhm", "heard": "x"}]}
    nlu.script["accept"] = {"intent": "accept"}
    e = vc.handle_text("u2")
    pr = list(cell(vc, "S01", "U03")["proposals"].values())[0]
    assert pr["verdict"] == "none" and "outside_tbr" in pr["flags"] and "TBR" in e["reply"]
    vc.set_config({"grace_s": 0.1})
    vc.handle_text("accept")
    assert cell(vc, "S01", "U03")["status"] == "empty"            # flagged: only "accept fail" takes it


def test_grace_accepts_clean_pass_not_fail_and_cancel_stops_it(vc, nlu):
    vc.set_config({"grace_s": 0.2})
    nlu.script["ok"] = vals(("S01", "20", "kOhm"))
    nlu.script["bad"] = vals(("S02", "5", "kOhm"))
    nlu.script["cancel"] = {"intent": "cancel"}
    vc.handle_text("ok")
    assert vc.status()["pending"]["utt"] and cell(vc, "S01")["status"] == "empty"
    # the timer thread accepts the cell, then appends its journal entry (source "grace"): wait for the entry, not just the accept
    assert wait(lambda: cell(vc, "S01")["status"] == "accepted" and vc.eng.state.voice[-1]["source"] == "grace")
    assert cell(vc, "S01")["source"]["kind"] == "voice" and vc.pending is None
    vc.handle_text("bad")
    assert vc.pending is None
    time.sleep(0.5)
    assert cell(vc, "S02")["status"] == "empty"
    # cancel by voice and by Esc
    vc.set_context("unpowered", "U02", "S01")
    nlu.script["ok2"] = {"intent": "values", "values": [{"board": "S01", "test_id": "U02", "value": "9", "unit": "kOhm", "heard": "9"}]}
    vc.handle_text("ok2")
    assert vc.pending
    assert vc.handle_text("cancel")["reply"] == "Rejected."
    time.sleep(0.5)
    assert cell(vc, "S01", "U02")["status"] == "empty" and not list(cell(vc, "S01", "U02")["proposals"].values())
    vc.handle_text("ok2")
    assert vc.cancel_pending()["pending"] is None
    time.sleep(0.4)
    assert cell(vc, "S01", "U02")["status"] == "empty"


def test_reject_and_undo(vc, nlu):
    nlu.script["v"] = vals(("S01", "20", "kOhm"))
    nlu.script["reject"] = {"intent": "reject"}
    vc.handle_text("v")
    assert vc.handle_text("reject")["reply"] == "Rejected."
    assert not cell(vc, "S01")["proposals"]
    assert vc.handle_text("reject")["reply"] == "Nothing to reject."


def test_skip_needs_reason_and_bare_skip_records_nothing(vc, nlu):
    nlu.script["skip s1"] = {"intent": "skip_board", "boards": ["S01"], "text": ""}
    nlu.script["skip s1 dnf"] = {"intent": "skip_board", "boards": ["S01"], "text": "connector not fitted"}
    nlu.script["skip"] = {"intent": "skip"}
    e = vc.handle_text("skip s1")
    assert "reason" in e["reply"] and cell(vc, "S01")["status"] == "empty"
    e = vc.handle_text("skip s1 dnf")
    assert cell(vc, "S01")["status"] == "skipped" and cell(vc, "S01")["skip_reason"] == "connector not fitted"
    assert vc.ctx["board"] == "S02" and "Next S2" in e["reply"]
    n = len(vc.eng.journal.events)
    vc.handle_text("skip")
    assert [x["type"] for x in vc.eng.journal.events[n:]] == ["voice"] and vc.ctx["test_id"] == "U02"   # no other open board: next test, nothing recorded


def test_navigation_broadcasts(vc, nlu):
    nlu.script["next test"] = {"intent": "next_test"}
    nlu.script["go u04"] = {"intent": "goto_test", "test_id": "U04"}
    nlu.script["previous test"] = {"intent": "prev_test"}
    nlu.script["board two"] = {"intent": "set_board", "boards": ["S02"]}
    nlu.script["next board"] = {"intent": "next_board"}
    vc.handle_text("next test")
    assert vc.ctx["test_id"] == "U02"
    assert {"kind": "voice_nav", "step": "unpowered", "test_id": "U02", "board": "S01"} in vc.msgs
    vc.handle_text("go u04")
    assert vc.ctx["test_id"] == "U04"
    vc.handle_text("previous test")
    assert vc.ctx["test_id"] == "U03"
    vc.handle_text("board two")
    assert {"kind": "voice_nav", "step": "unpowered", "test_id": "U03", "board": "S02"} in vc.msgs[-4:]
    vc.handle_text("next board")
    assert vc.ctx["board"] == "S01"
    vc.set_context("unpowered", "U05", "S01")
    nlu.script["end"] = {"intent": "next_test"}
    assert vc.handle_text("end")["reply"] == "Last test in this step."


def test_note_is_linked_to_step_board_test_and_designators(vc, nlu):
    d = vc.eng.state.bom_designators()[0]
    nlu.script["n"] = {"intent": "note", "text": f"{d.lower()} looks tilted", "boards": ["S01"]}
    e = vc.handle_text("n")
    n = vc.eng.state.notes[-1]
    assert n["step"] == "unpowered" and n["board"] == "S01" and n["test_id"] == "U01" and d in n["designators"]
    assert e["outcome"] == "applied" and d in e["reply"]


def test_questions_progress_repeat(vc, nlu):
    nlu.script["which"] = {"intent": "which_test"}
    nlu.script["progress"] = {"intent": "progress"}
    nlu.script["repeat"] = {"intent": "repeat"}
    r = vc.handle_text("which")["reply"]
    assert r == "U01, VIN to GND resistance. Probe VIN at TP1, GND at TP2. Expected at least 10 kOhm."
    assert vc.handle_text("progress")["reply"] == "U01: 0 of 3 boards done, 3 left, next S1."
    assert vc.handle_text("repeat")["reply"].startswith("U01: 0 of 3")
    assert vc.handle_text("repeat")["reply"].startswith("U01: 0 of 3")


def test_unclear_and_no_nlu(vc, nlu):
    e = vc.handle_text("mumble")
    assert e["outcome"] == "question" and e["intent"] == "unclear" and e["reply"] == "Is the reading at least 10 kilo ohms on S1?"
    vc.set_context("unpowered", "U05", "S01")               # a text test has no numeric limit: the hint stays
    e = vc.handle_text("mumble")
    assert e["outcome"] == "unclear" and "Not understood" in e["reply"]
    assert vc.handle_text("")["reply"] == "Nothing heard."


def test_voice_events_journalled_and_replayed_after_restart(vc, nlu, tmp_path):
    nlu.script["v"] = vals(("S01", "20", "kOhm"))
    e = vc.handle_text("v")
    ev = vc.eng.journal.events[-1]
    assert ev["type"] == "voice" and ev["payload"]["reply"] == e["reply"] and ev["payload"]["values"][0]["proposal_id"]
    assert vc.eng.state.voice[-1]["utt"] == 1 and vc.eng.state.snapshot()["voice"][-1]["transcript"] == "v"
    again = make_session(tmp_path).voice
    assert [x["utt"] for x in again.eng.state.voice] == [1] and again._utt == 1
    assert list(again.eng.state.cell("S01", "U01")["proposals"].values())[0]["source"]["kind"] == "voice"


def test_old_journal_without_voice_events_replays(tmp_path):
    from polybench.protocol import load
    from helpers import DEMO
    path = tmp_path / "old.jsonl"
    eng = Engine(load(DEMO / "demo.pcbv.yaml"), Journal(path), "t")
    eng.create_batch("old", ["S01"], "S", True, "DEMO-1")
    eng.add_proposal("S01", "U01", "20", plugin="demo.measure")
    eng.journal.close()
    again = Engine(load(DEMO / "demo.pcbv.yaml"), Journal(path), "t")
    assert again.state.voice == [] and again.state.snapshot()["voice"] == []
    assert list(again.state.cell("S01", "U01")["proposals"].values())[0]["source"]["kind"] == "action"


def test_voice_keeps_last_300(vc):
    for i in range(305):
        vc.eng._emit("voice", {"utt": i + 1})
    assert len(vc.eng.state.voice) == 300 and vc.eng.state.voice[0]["utt"] == 6


def test_feed_wav_with_sidecar_both_paths(vc, nlu, tmp_path):
    from polybench.voice import stt as stt_
    nlu.script["s1 twenty kilo ohms"] = vals(("S01", "20", "kOhm"))
    w = tmp_path / "c.wav"
    stt_.write_wav(str(w), speech(1.2))
    w.with_suffix(".txt").write_text("s1 twenty kilo ohms\n")
    e = vc.feed_wav(str(w), "ptt")
    assert e["source"] == "clip" and e["transcript"] == "s1 twenty kilo ohms" and e["outcome"] == "proposed" and e["stt_ms"] is not None
    e2 = vc.feed_wav(str(w), "vad")
    assert e2["transcript"] == "s1 twenty kilo ohms" and e2["audio"]
    silent = tmp_path / "s.wav"
    stt_.write_wav(str(silent), speech(1.0, peak=0.001))
    d = vc.feed_wav(str(silent), "ptt")
    assert d["outcome"] == "unclear" and "no speech" in d["detail"]
    bare = tmp_path / "b.wav"
    stt_.write_wav(str(bare), speech(1.0))
    assert "no sidecar" in vc.feed_wav(str(bare), "ptt")["detail"]


def test_config_round_trip_and_validation(vc, tmp_path):
    p = vcfg.path()
    assert p == tmp_path / "voice.json"
    vc.set_config({"grace_s": "2.5", "tts_rate": 220, "row_mode": False, "input_device": ""})
    saved = json.loads(p.read_text())
    assert saved["grace_s"] == 2.5 and saved["tts_rate"] == 220 and saved["row_mode"] is False and saved["llm_model"] == "qwen3:8b"
    assert vcfg.load()["grace_s"] == 2.5 and vc.status()["config"]["tts_rate"] == 220
    with pytest.raises(ValueError):
        vc.set_config({"nonsense": 1})
    with pytest.raises(ValueError):
        vc.set_config({"stt_backend": "cloud"})
    p.unlink()
    assert vcfg.load() == vcfg.DEFAULTS


def test_status_reports_missing_stt_and_llm_with_install_lines(vc, monkeypatch):
    from polybench.voice import capture
    real = capture.importlib.util.find_spec
    monkeypatch.setattr(capture.importlib.util, "find_spec", lambda n, *a: None if n in ("mlx_whisper", "sounddevice") else real(n, *a))
    vc.set_config({"stt_backend": "whisper", "llm_enabled": True, "ollama_url": "http://127.0.0.1:9"})
    s = vc.status()
    assert s["stt"]["ok"] is False and "[voice]" in s["stt"]["install"] and "mlx-whisper" in s["stt"]["detail"]
    assert s["mic"]["ok"] is False and "[voice]" in s["mic"]["install"]
    assert s["llm"]["ok"] is False and "ollama pull qwen3:8b" in s["llm"]["install"]
    assert s["tts"]["ok"] and s["enabled"] is False and s["listening"] is False and s["pending"] is None
    # typed text still works with no STT or mic
    assert vc.handle_text("anything")["intent"] == "unclear"


def test_stt_model_not_cached_is_not_downloaded_silently(vc, monkeypatch):
    from polybench.voice import capture
    monkeypatch.setattr(capture.importlib.util, "find_spec", lambda n, *a: object())
    monkeypatch.setattr(capture, "model_cached", lambda m: False)
    vc.set_config({"stt_backend": "whisper"})
    s = vc.status()["stt"]
    assert not s["ok"] and "not downloaded" in s["detail"] and "HF_HUB_OFFLINE=0" in s["install"]


def test_routes(tmp_path, nlu):
    sess = make_session(tmp_path)
    nlu.script["s1 twenty"] = vals(("S01", "20", "kOhm"))
    with TestClient(create_app(sess)) as c:
        assert c.post("/api/voice/config", json={"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False}).json()["config"]["tts_backend"] == "log"
        assert c.post("/api/voice/config", json={"bogus": 1}).status_code == 422
        for b in ("S01",):
            sess.engine.override_gate(b, "unpowered", "t")
        assert c.post("/api/voice/context", json={"step": "unpowered", "test_id": "U01", "board": "S01"}).json() == {"ok": True}
        e = c.post("/api/voice/text", json={"text": "s1 twenty"}).json()
        assert e["outcome"] == "proposed"
        assert c.get("/api/voice/status").json()["context"]["board"] == "S01"
        assert c.post("/api/voice/visibility", json={"visible": False}).json()["listening"] is False
        assert c.post("/api/voice/handsfree", json={"on": True}).json()["handsfree"] is True
        assert c.post("/api/voice/cancel").json()["pending"] is None
        assert c.post("/api/voice/ptt", json={"action": "beat"}).status_code == 200
        assert c.post("/api/voice/ptt", json={"action": "x"}).status_code == 422
        assert "models" in c.get("/api/voice/models").json() and "voices" in c.get("/api/voice/voices").json()
        assert c.get("/api/state").json()["voice"][-1]["utt"] == 1
        w = tmp_path / "c.wav"
        stt.write_wav(str(w), speech(1.0))
        w.with_suffix(".txt").write_text("s1 twenty")
        assert c.post("/api/voice/clip", json={"path": str(w), "via": "ptt"}).json()["source"] == "clip"


def test_clip_route_is_closed_outside_test_mode(tmp_path, monkeypatch):
    from polybench.protocol import load
    from polybench.server import Session
    from helpers import DEMO
    monkeypatch.delenv("POLYBENCH_VOICE_TEST")
    sess = Session(load(DEMO / "demo.pcbv.yaml"), tmp_path / "d", "r", False, 2, None)
    with TestClient(create_app(sess)) as c:
        assert c.post("/api/voice/clip", json={"path": "x.wav"}).status_code == 403


def test_read_method_reads_instructions_probes_expected_and_stop_rule(vc, nlu):
    nlu.script["method"] = {"intent": "read_method"}
    r = vc.handle_text("method")["reply"]
    assert r == ("U01, VIN to GND resistance. Meter on resistance. Red lead on the first net, black on GND. Board unpowered. "
                 "Probe VIN at TP1, GND at TP2. Expected at least 10 kOhm. Stop below 1 kOhm: VIN looks shorted: do not power this board.")
    t = vc.eng.state.test_index["U01"][2]
    t["probe"] = [{"point": "TP1", "pole": "+"}, {"point": "TP2", "pole": "-"}]          # new plan format
    assert "Probe plus on TP1, probe minus on TP2." in vc.handle_text("method")["reply"]
    assert vc.handle_text("method")["outcome"] == "applied"


def test_highlight_uses_highlight_test_when_present_else_session_highlight(vc, nlu):
    nlu.script["show"] = {"intent": "highlight"}
    calls = []

    async def fake(des):
        calls.append(list(des))
        return {"highlighted": list(des), "missing": []}
    vc.sess.highlight = fake
    vc.sess.highlight_test = None            # the real Session has highlight_test now; this case is a session without it
    assert vc.handle_text("show")["outcome"] == "applied"
    assert calls and calls[0][-2:] == ["TP1", "TP2"]
    vc.sess.highlight_test = lambda t: {"highlighted": ["R3", "TP1"], "missing": []}
    assert vc.handle_text("show")["reply"] == "Highlighted R3, TP1 in Fusion."
    real_bridge = vc.sess.bridge
    vc.sess.bridge = type("KicadStub", (), {"name": "kicad"})()      # KC-43: the reply names the active ECAD program
    assert vc.handle_text("show")["reply"] == "Highlighted R3, TP1 in KiCad."
    vc.sess.bridge = real_bridge

    async def off(des):
        return {"ok": False, "disabled": True, "highlighted": [], "missing": [], "message": "ECAD is off: nothing was sent"}
    vc.sess.highlight_test = None
    vc.sess.highlight = off
    e = vc.handle_text("show")
    assert e["outcome"] == "rejected" and e["reply"].startswith("ECAD is off")


def test_probe_text_both_formats():
    from polybench.voice.controller import _probe_text
    assert _probe_text({"probe": [{"point": "TP1", "pole": "+"}, {"point": "TP2", "pole": "-"}]}) == "Probe plus on TP1, probe minus on TP2."
    assert _probe_text({"probe": [{"net": "VIN", "point": "TP1"}]}) == "Probe VIN at TP1."
    assert _probe_text({}) == ""


def test_default_model_falls_back_until_pulled(vc, monkeypatch):
    """qwen3:8b is the default; while it is not pulled the best installed model runs and settings say how to pull it."""
    det = [{"name": "gemma4:26b", "size": "25.8B", "bytes": 17}, {"name": "tiny:1b", "size": "1B", "bytes": 1}]
    monkeypatch.setattr(vc, "models", lambda fresh=False: {"ok": True, "models": [d["name"] for d in det], "details": det,
                                                          "default": "qwen3:8b", "detail": ""})
    vc.set_config({"llm_enabled": True})
    s = vc._llm_status()
    assert s["ok"] and s["fallback"] and s["model"] == "gemma4:26b" and s["install"] == "ollama pull qwen3:8b"
    assert "Default model qwen3:8b not installed: run `ollama pull qwen3:8b`" in s["detail"]
    det.append({"name": "qwen3:8b", "size": "8.2B", "bytes": 5})
    s = vc._llm_status()
    assert s["ok"] and not s["fallback"] and s["model"] == "qwen3:8b"
    assert vc.privacy_line({"ok": True, "model": "mlx-community/whisper-large-v3-turbo"}, s) == \
        "Voice: local only: STT mlx-community/whisper-large-v3-turbo (cached), LLM qwen3:8b at 127.0.0.1"


# ---- reply voice: settings link, test button, voice in use
def test_open_speech_settings_route(tmp_path, nlu, monkeypatch):
    from polybench.voice import tts as ttsmod
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(ttsmod.subprocess, "run", fake_run)
    with TestClient(create_app(make_session(tmp_path))) as c:
        r = c.post("/api/voice/open-speech-settings", json={"url": "file:///etc/passwd"}, params={"url": "http://evil"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert calls == [["open", ttsmod.SPOKEN_CONTENT_URLS[0]]]              # fixed URL, request data ignored
        assert c.get("/api/voice/open-speech-settings").status_code == 405


def test_open_speech_settings_falls_back(monkeypatch):
    from polybench.voice import tts as ttsmod
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd[1])
        return types.SimpleNamespace(returncode=1 if len(seen) < 3 else 0)
    monkeypatch.setattr(ttsmod.subprocess, "run", fake_run)
    out = ttsmod.open_speech_settings()
    assert out["ok"] and seen == list(ttsmod.SPOKEN_CONTENT_URLS) and out["url"].endswith("universalaccess")
    monkeypatch.setattr(ttsmod.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1))
    assert ttsmod.open_speech_settings() == {"ok": False, "url": None}


def _tts(monkeypatch, voices, **cfg):
    from polybench.voice import tts as ttsmod
    c = {"tts_enabled": True, "tts_backend": "say", "tts_voice": "", "tts_rate": 200, **cfg}
    t = ttsmod.Tts(lambda: c)
    monkeypatch.setattr(t, "available", lambda: True)
    monkeypatch.setattr(ttsmod.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout="".join(f"{n}   en_GB    # Hello\n" for n in voices), returncode=0))
    spoken = []
    monkeypatch.setattr(ttsmod.subprocess, "Popen", lambda cmd, **k: spoken.append(cmd) or types.SimpleNamespace(poll=lambda: 0))
    return t, c, spoken


def test_voice_order_default_and_status(monkeypatch):
    t, c, spoken = _tts(monkeypatch, ["Daniel", "Fred", "Zoe (Premium)", "Ava (Enhanced)"])
    assert t.voices()[:2] == ["Zoe (Premium)", "Ava (Enhanced)"]
    assert t.effective_voice() == "Zoe (Premium)" and t.status()["label"] == "Zoe (Premium)" and t.status()["warn"] == ""
    t.say("hello")
    assert spoken[-1][3:5] == ["-v", "Zoe (Premium)"]
    c["tts_voice"] = "Fred"
    assert t.status()["label"] == "Fred"


def test_only_basic_voices_warns(monkeypatch):
    t, c, spoken = _tts(monkeypatch, ["Daniel", "Fred"])
    assert t.effective_voice() == "" and t.status()["label"] == "System voice"
    assert t.status()["warn"] == "Only basic voices are installed"
    t.say("hello")
    assert "-v" not in spoken[-1]


def test_test_voice_speaks_and_respects_mute(monkeypatch):
    t, c, spoken = _tts(monkeypatch, ["Daniel", "Zoe (Premium)"])
    assert t.test()["spoken"] and spoken and spoken[-1][-1].startswith("Polybench voice test")
    assert t.test("Daniel")["voice"] == "Daniel" and spoken[-1][4] == "Daniel"
    assert t.test("Not A Voice")["voice"] == "Zoe (Premium)"                  # unknown names are not passed to say
    n = len(spoken)
    t.muted = True
    r = t.test()
    assert r == {"spoken": False, "muted": True, "voice": "Zoe (Premium)"} and len(spoken) == n
    t.muted = False
    c["tts_enabled"] = False
    assert t.test()["muted"] is True and len(spoken) == n


# ---- static caching: no stale script mixed with a new one
def test_static_no_cache_and_versioned_links(tmp_path, nlu):
    import re
    with TestClient(create_app(make_session(tmp_path))) as c:
        idx = c.get("/")
        assert idx.status_code == 200 and idx.headers["cache-control"] == "no-store"
        links = re.findall(r'(?:src|href)="(/static/[^"]+\.(?:js|css)[^"]*)"', idx.text)
        assert any(l.startswith("/static/app.js") for l in links) and any(l.endswith(".css") or ".css?" in l for l in links)
        for l in links:
            assert re.search(r"\?v=[0-9a-f]{10}$", l), l                      # every script and stylesheet link carries a version
            r = c.get(l)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", l
        assert c.get("/static/app.js").headers["cache-control"] == "no-cache"


def test_ack_switch_silences_accept_but_not_the_value_readback(vc, nlu):
    """UIR-13: Voice acknowledgements off: 'accept' is not spoken, the read-back of a value still is; the reply stays in the transcript."""
    nlu.script["s1 twenty"] = vals(("S01", "20", "kOhm"))
    nlu.script["accept"] = {"intent": "accept"}
    vc.set_config({"tts_ack": False})
    e = vc.handle_text("s1 twenty")
    a = vc.handle_text("accept")
    assert "Accepted." in a["reply"]
    assert any(s.startswith("S1 20 kilo ohms") for s in vc.tts.spoken) and not any(s.startswith("Accepted") for s in vc.tts.spoken)
    vc.set_config({"tts_ack": True})
    nlu.script["s2 twenty"] = vals(("S02", "20", "kOhm"))
    vc.handle_text("s2 twenty")
    vc.handle_text("accept")
    assert any(s.startswith("Accepted") for s in vc.tts.spoken)
