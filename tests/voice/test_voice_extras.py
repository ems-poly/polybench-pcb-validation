"""Voice: step context, mic check, unclear hint, Whisper noise, and AOI issue review by voice (real grammar, no model). Host only."""
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_session, speech
from polybench.server import create_app
from polybench.voice import capture, stt


@pytest.fixture
def rv(tmp_path):
    """A controller on the demo protocol with the real grammar (llm off, replies logged not spoken)."""
    sess = make_session(tmp_path)
    for b in ("S01", "S02"):
        sess.engine.override_gate(b, "unpowered", "test")
    c = sess.voice
    c.set_config({"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
    c.msgs = []
    c._sink = c.msgs.append
    return c


# ---- 1. step context
def test_context_route_carries_step_test_board_and_review_flag(tmp_path):
    with TestClient(create_app(make_session(tmp_path))) as c:
        c.post("/api/voice/context", json={"step": "unpowered", "test_id": "U01", "board": "S02", "aoi_review": True})
        assert c.get("/api/voice/status").json()["context"].items() >= {"step": "unpowered", "test_id": "U01", "board": "S02", "aoi_review": True}.items()
        # typed text carries the page's context, so an older post can never win
        e = c.post("/api/voice/text", json={"text": "which test", "context": {"step": "unpowered", "test_id": "U01", "board": "S01"}}).json()
        assert e["test_id"] == "U01" and e["step"] == "unpowered" and e["board"] == "S01" and e["reply"].startswith("U01")


def test_no_test_reply_names_the_step(rv):
    rv.set_context("unpowered", None, "S01")
    assert rv.handle_text("which test")["reply"] == "No test selected. You are in Unpowered testing."


# ---- 2. mic check
@pytest.mark.parametrize("said", ["test", "testing", "hello", "Hello?", "can you hear me", "Can you hear me?"])
def test_mic_check(rv, said):
    rv.set_context("unpowered", "U01", "S01")
    e = rv.handle_text(said)
    assert e["intent"] == "mic_check" and e["route"] == "grammar"
    assert e["reply"].startswith("I can hear you. You are in Unpowered testing, U01. Try: S one ") and e["outcome"] == "applied"


def test_mic_check_without_a_test(rv):
    rv.set_context("unpowered", None, None)
    assert rv.handle_text("hello")["reply"] == "I can hear you. You are in Unpowered testing, no test selected. Try: which test."


# ---- 3. unclear hint
def test_unclear_gives_two_examples_and_keeps_detail(rv):
    rv.set_context("unpowered", "U04", "S01")                                # yn test: no numeric limit, the hint stays
    e = rv.handle_text("what a lovely day")
    assert e["outcome"] == "unclear" and e["reply"].startswith("Not understood. Try: S one yes") and e["reply"].endswith(", or next.")
    assert e["detail"] and "Not understood" not in e["detail"]               # the transcript row keeps the reason
    rv.set_context("unpowered", None, None)
    assert rv.handle_text("what a lovely day")["reply"] == "Not understood. Try: which test, or next."


# ---- 4. Whisper noise
@pytest.mark.parametrize("text,peak,secs,noise", [
    ("What? What? What? What?", None, None, True), ("Hello. Hello. Hello.", None, None, True), ("yes yes", None, None, False),
    ("What?", 0.1, 0.8, True), ("Thank you.", 0.1, 0.8, True), ("you", 0.1, 3.0, True), ("What?", 0.5, 3.0, False),
    ("S1 0.2 volts", 0.1, 0.8, False), ("S one three three three", None, None, False)])
def test_is_noise(text, peak, secs, noise):
    assert capture.is_noise(text, peak, secs) is noise


def test_noise_transcript_is_dropped_silently(rv, tmp_path):
    wav = tmp_path / "n.wav"
    stt.write_wav(str(wav), speech(1.0))
    wav.with_suffix(".txt").write_text("What? What? What? What? What?")
    rv.set_context("unpowered", "U01", "S01")
    n_spoken = len(rv.tts.log)
    e = rv.feed_wav(str(wav))
    assert e["outcome"] == "dropped" and e["detail"] == "dropped: noise" and e["reply"] == "" and e["intent"] == "unclear"
    assert len(rv.tts.log) == n_spoken and rv.eng.state.voice[-1]["detail"] == "dropped: noise"        # nothing spoken, journalled


# ---- 5. AOI review by voice
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_aoi_manual import run_folder, start_run  # noqa: E402
from test_aoi_polybench import eng, proto_dir  # noqa: E402,F401  (fixtures)


@pytest.fixture
def av(eng, tmp_path):
    from polybench.voice.controller import VoiceController
    d = run_folder(tmp_path)
    start_run(eng, d)
    v = VoiceController(types.SimpleNamespace(engine=eng, dir=tmp_path, loop=None))
    v.set_config({"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
    v.msgs = []
    v._sink = v.msgs.append
    v.set_context("visual", None, "S01", aoi_review=True)
    from polybench.aoi import review
    review.set_cursor(0)
    return v


def _dec(v, key):
    d = v.eng.state.aoi_decision_for(key)
    return d["decision"] if d else None


@pytest.mark.parametrize("said,decision,words", [("defect fixed", "confirmed", "defect fixed"), ("it's fixed", "confirmed", "defect fixed"), ("no fault", "rejected", "not an issue"),
                                                 ("not an issue", "rejected", "not an issue"), ("reject", "rejected", "not an issue"),
                                                 ("fine", "rejected", "not an issue"), ("false alarm", "rejected", "not an issue"),
                                                 ("ignore", "unsure", "ignored")])
def test_aoi_decisions(av, said, decision, words):
    first = av._aoi_items()[0]["key"]
    e = av.handle_text(said)
    assert e["outcome"] == "applied" and _dec(av, first) == decision
    assert e["reply"].startswith("IC1 bridge on S") and f": {words}. Next: IC1 bridge on S" in e["reply"], e["reply"]
    ev = [x for x in av.eng.journal.events if x["type"] == "aoi_decision"][-1]
    assert ev["payload"]["key"] == first and ev["payload"]["decision"] == decision
    assert {"kind": "voice_aoi", "key": av._aoi_items()[1]["key"]} in [m for m in av.msgs if m.get("kind") == "voice_aoi" and not m.get("sync")]


def test_aoi_navigation_and_read(av):
    keys = [i["key"] for i in av._aoi_items()]
    r = av.handle_text("read issue")["reply"]
    assert r.startswith("IC1 bridge on S1.") and "v. n" in r                          # title, then the reason line (value. note)
    assert "2 of 3" in av.handle_text("next issue")["reply"]
    assert "1 of 3" in av.handle_text("previous issue")["reply"]
    av.handle_text("defect fixed")                                                         # decides item 1, moves to item 2
    av.handle_text("previous issue")
    assert av.handle_text("next undecided")["reply"].endswith("undecided.")           # item 1 is the next undecided after item 0 was decided
    from polybench.aoi import review
    assert review.engine_cursor(av.eng) == 1
    assert keys[1] in [m["key"] for m in av.msgs if m.get("kind") == "voice_aoi"]


def test_aoi_commands_only_in_review_tab(av):
    av.set_context("visual", None, "S01", aoi_review=False)
    e = av.handle_text("not an issue")
    assert e["intent"] != "aoi_reject" and _dec(av, av._aoi_items()[0]["key"]) is None


def test_aoi_grace_applies_after_delay_and_cancel_keeps_it_undecided(av):
    av.set_config({"grace_s": 0.2})
    key = av._aoi_items()[0]["key"]
    e = av.handle_text("not an issue")
    assert e["outcome"] == "proposed" and _dec(av, key) is None
    assert av.handle_text("cancel")["reply"] == "Cancelled. The issue stays undecided."
    import time
    time.sleep(0.5)
    assert _dec(av, key) is None
    av.handle_text("not an issue")
    av.tts.stop()
    deadline = time.time() + 3
    while time.time() < deadline and _dec(av, key) is None:
        time.sleep(0.05)
    assert _dec(av, key) == "rejected"


def test_guard_blocks_say_and_open():
    import subprocess
    with pytest.raises(RuntimeError, match="test guard"):
        subprocess.Popen(["say", "hello"])
    with pytest.raises(RuntimeError, match="test guard"):
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.universalaccess"])
