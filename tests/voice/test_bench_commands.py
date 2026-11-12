"""Hands-free bench commands: board select, start / stop / confirm of an action. Simulator only, no microphone, no sound.

The grammar tables are pure. The controller tests run the real language layer (grammar only, the model is off), the real action
runner and a real child process (demo.slow, a plugin that runs until it is cancelled) on a background event loop."""
import asyncio
import json
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from polybench.protocol import load
from polybench.server import Session, create_app
from polybench.voice import benchcmd as bc, stt

from helpers import DEMO
from conftest import speech


def wait(cond, t=8.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------------------------------------ pure grammar

@pytest.mark.parametrize("text,n", [
    ("board S1", 1), ("Board S1.", 1), ("board one", 1), ("board S01", 1), ("go to board 3", 3), ("board s one", 1), ("Board S 2", 2),
    ("select board two", 2), ("board 3", 3), ("please go to board S02", 2), ("switch to board five", 5), ("board S9", 9),
])
def test_board_phrases(text, n):
    assert bc.parse_board(bc.tokens(text)) == ("board", n)


@pytest.mark.parametrize("text", ["S1", "boards 1 and 2", "all boards", "board zebra", "next board", "start test", "board", "the board is hot"])
def test_not_a_board_command(text):
    assert bc.parse_board(bc.tokens(text)) is None


@pytest.mark.parametrize("text,words", [
    ("start test", []), ("run test", []), ("run it", []), ("start", []), ("Start the test.", []), ("start the test please", []),
    ("begin test", []), ("run", []), ("Run First power", ["first", "power"]), ("start measure", ["measure"]),
    ("run the long capture test", ["long", "capture"]),
])
def test_start_phrases(text, words):
    assert bc.parse_start(bc.tokens(text)) == words


@pytest.mark.parametrize("text", ["S1 0.2 volts", "next test", "stop", "board S1", "restart", "test", "accept"])
def test_not_a_start(text):
    assert bc.parse_start(bc.tokens(text)) is None


@pytest.mark.parametrize("text", ["stop", "Stop.", "stop stop", "stop test", "stop the test", "stop it now", "abort", "Abort!", "halt",
                                  "cancel test", "cancel the action", "stop the test please", "stop stop stop"])
def test_stop_phrases_abort_a_running_action(text):
    assert bc.stop_kind(bc.tokens(text), True) == "abort"


@pytest.mark.parametrize("text", ["cancel", "stopped", "next test", "board S2", "accept", "start test", "S1 0.2 volts"])
def test_not_a_stop_while_running(text):
    assert bc.stop_kind(bc.tokens(text), True) is None


def test_stop_with_nothing_running():
    k = lambda t: bc.stop_kind(bc.tokens(t), False)
    assert k("stop") == "bare" and k("stop stop") == "bare"            # the older cancel-the-proposal meaning stays
    assert k("stop test") == "idle" and k("abort") == "idle" and k("cancel test") == "idle"
    assert k("cancel") is None and k("next test") is None


@pytest.mark.parametrize("text,ok", [("confirm", True), ("Confirm.", True), ("yes confirm", True), ("yes, confirm", True), ("confirmed", True),
                                     ("please confirm", True), ("yes", False), ("confirm fail", False), ("accept", False), ("no", False),
                                     ("confirm it all", False)])
def test_confirm_phrases(text, ok):
    assert bc.is_confirm(bc.tokens(text)) is ok


# ------------------------------------------------------------------------------------------------ fixtures

@pytest.fixture(autouse=True)
def voice_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYBENCH_VOICE_CONFIG", str(tmp_path / "voice.json"))
    monkeypatch.setenv("POLYBENCH_VOICE_TEST", "1")


def bench_session(tmp_path, boards=3, one_action=True):
    """The demo protocol with its First power action replaced by demo.slow (runs until cancelled)."""
    d = tmp_path / "proto"
    shutil.copytree(DEMO, d)
    p = d / "powered.yaml"
    txt = p.read_text().replace("plugin: demo.measure", "plugin: demo.slow")
    if one_action:    # the demo's Powered step has a second action (load sweep); most tests want the single-action case
        a, b = txt.index("          - id: load_sweep"), txt.index("        tests:")
        txt = txt[:a] + txt[b:]
    p.write_text(txt)
    sess = Session(load(d / "demo.pcbv.yaml"), tmp_path / "data", "v", True, boards, None, "tester")
    for b in ("S01", "S02"):
        sess.engine.override_gate(b, "powered", "test")
        sess.engine.override_gate(b, "function", "test")
    return sess


@pytest.fixture(params=[True])
def bvc(tmp_path, request):
    """A real voice controller (grammar only) with the runner on a background loop, cursor on powered / P01 / S01.
    param False keeps the demo's second action (the load sweep), so "start test" is ambiguous."""
    sess = bench_session(tmp_path, one_action=request.param)
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    sess.loop = loop
    c = sess.voice
    c.set_config({"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
    c.set_context("powered", "P01", "S01")
    c.msgs = []
    c._sink = c.msgs.append
    yield c
    if sess.runner.status():
        asyncio.run_coroutine_threadsafe(sess.runner.cancel(), loop).result(5)
        wait(lambda: sess.runner.status() is None, 10)
    loop.call_soon_threadsafe(loop.stop)
    th.join(2)


def running(c):
    return c.sess.runner.status()


def journal_voice(c):
    return [e for e in c.eng.journal.events if e["type"] == "voice"]


def start_and_confirm(c, board="S2"):
    c.handle_text(f"board {board}")
    assert "Say confirm" in c.handle_text("start test")["reply"]
    e = c.handle_text("confirm")
    assert wait(lambda: running(c) is not None)
    return e


# ------------------------------------------------------------------------------------------------ board

def test_board_select_normalises_to_the_batch_label(bvc):
    for said, label in (("board S2", "S02"), ("board one", "S01"), ("board S03", "S03"), ("go to board 2", "S02"), ("board s one", "S01")):
        e = bvc.handle_text(said)
        assert e["outcome"] == "applied" and bvc.ctx["board"] == label, said
        assert e["reply"] == f"Board S{int(label[1:])}." and e["intent"] == "bench_board"
    assert {"kind": "voice_nav", "step": "powered", "test_id": "P01", "board": "S01"} in bvc.msgs


def test_unknown_board_is_refused_and_nothing_changes(bvc):
    bvc.handle_text("board S2")
    e = bvc.handle_text("board S9")
    assert e["outcome"] == "rejected" and e["reply"] == "No board S9." and bvc.ctx["board"] == "S02"


# ------------------------------------------------------------------------------------------------ start and confirm

def test_start_reads_back_and_waits_for_confirm(bvc):
    bvc.handle_text("board S2")
    e = bvc.handle_text("start test")
    assert e["reply"] == "Run Read rails and current on S2. Say confirm." and e["outcome"] == "question"
    assert running(bvc) is None
    b = bvc.status()["bench"]
    assert b["confirm"]["board"] == "S02" and 0 < b["confirm"]["deadline_s"] <= 10
    e = bvc.handle_text("confirm")
    assert e["outcome"] == "applied" and e["reply"] == "Running Read rails and current on S2."
    assert wait(lambda: running(bvc) is not None)
    cur = running(bvc)
    assert cur["board"] == "S02" and cur["action"] == "read_rails" and cur["plugin"] == "demo.slow"
    assert bvc.status()["bench"]["confirm"] is None and bvc.status()["bench"]["running"]["board"] == "S02"
    assert any(a["type"] == "action_start" and a["payload"]["board"] == "S02" for a in bvc.eng.journal.events)


@pytest.mark.parametrize("bvc", [False], indirect=True)
def test_two_actions_in_the_step_make_a_bare_start_ask_which(bvc):
    e = bvc.handle_text("start test")
    assert e["reply"].startswith("More than one action here.") and "Sweep the 5 V rail" in e["reply"]
    assert running(bvc) is None and bvc.status()["bench"]["confirm"] is None
    e = bvc.handle_text("run sweep")
    assert e["outcome"] == "question" and "Sweep the 5 V rail against load" in e["reply"] and e["reply"].endswith("Say confirm.")


def test_yes_confirm_and_variants_start_it(bvc):
    bvc.handle_text("start test")
    assert bvc.handle_text("yes confirm")["outcome"] == "applied"
    assert wait(lambda: running(bvc) is not None)


def test_wrong_word_cancels_the_request(bvc):
    bvc.handle_text("start test")
    e = bvc.handle_text("yes")
    assert e["reply"].startswith("Start cancelled.") and running(bvc) is None and bvc.status()["bench"]["confirm"] is None
    # a later "confirm" does not resurrect it: it keeps its older meaning, accept
    assert bvc.handle_text("confirm")["reply"] == "Nothing to accept."
    assert running(bvc) is None


def test_other_command_cancels_the_request_and_still_runs(bvc):
    bvc.handle_text("start test")
    e = bvc.handle_text("board S2")
    assert e["reply"] == "Start cancelled. Board S2." and bvc.ctx["board"] == "S02" and running(bvc) is None


def test_confirm_times_out(bvc):
    bvc.cfg["confirm_timeout_s"] = 0.2
    bvc.handle_text("start test")
    assert wait(lambda: bvc.status()["bench"]["confirm"] is None)
    assert wait(lambda: any(e["payload"]["source"] == "timer" for e in journal_voice(bvc)))
    t = [e for e in journal_voice(bvc) if e["payload"]["source"] == "timer"][-1]["payload"]
    assert t["reply"] == "No confirmation. Nothing started." and t["bench"]["confirm"] == "timeout"
    e = bvc.handle_text("confirm")
    assert "timed out" in e["reply"] and running(bvc) is None and bvc.eng.state.cell("S01", "P01")["status"] == "empty"


def test_start_while_one_runs_is_refused(bvc):
    start_and_confirm(bvc, "S1")
    e = bvc.handle_text("start test")
    assert e["outcome"] == "rejected" and e["reply"] == "Read rails and current is still running. Say stop to abort."


def test_start_on_a_gated_board_is_refused_with_the_reason(bvc):
    bvc.handle_text("board S3")
    e = bvc.handle_text("start test")
    assert e["outcome"] == "rejected" and e["reply"].startswith("Refused: step is gated") and "S3" in e["reply"] and running(bvc) is None
    assert bvc.status()["bench"]["confirm"] is None


def test_power_interlock_hold_is_refused_and_spoken(bvc, monkeypatch):
    from polybench import engine as eng_mod
    monkeypatch.setattr(eng_mod.tiers, "hold_reason", lambda st, b: "S03 has an out-of-band reading in unpowered; repeat and settle it" if b == "S03" else None)
    bvc.handle_text("board S3")
    e = bvc.handle_text("start test")
    assert e["outcome"] == "rejected" and "power interlock: S3 has an out-of-band reading" in e["reply"] and running(bvc) is None


def test_stopped_board_is_refused(bvc):
    bvc.eng._emit("stop", {"board": "S02", "step": "unpowered", "test_id": "U01", "attempt": 1, "text": "shorted", "value": "0.5"})
    bvc.handle_text("board S2")
    e = bvc.handle_text("start test")
    assert e["outcome"] == "rejected" and "S2 is stopped" in e["reply"]


def test_no_board_and_no_step(bvc):
    bvc.set_context("powered", "P01", None)
    assert bvc.handle_text("start test")["reply"] == "Which board? Say board S1."
    bvc.set_context(None, None, "S01")
    assert bvc.handle_text("start test")["reply"].startswith("No step is open")


def test_several_actions_name_the_options_and_run_by_title_words(bvc):
    bvc.set_context("function", "F03", "S01")
    e = bvc.handle_text("start test")
    assert e["outcome"] == "rejected" and e["reply"] == "More than one action here. Say run Read heartbeat over CAN or run Long capture."
    e = bvc.handle_text("run long capture")
    assert e["reply"] == "Run Long capture on S1. Say confirm."
    bvc.handle_text("confirm")
    assert wait(lambda: running(bvc) is not None) and running(bvc)["action"] == "slow"
    e = bvc.handle_text("run nothing here")
    assert "is still running" in e["reply"]


def test_unknown_action_name_lists_options(bvc):
    bvc.set_context("function", "F03", "S01")
    e = bvc.handle_text("run banana")
    assert e["reply"].startswith("No action called banana. Say run Flash bring-up image or run")


def test_the_unrun_action_is_the_default(bvc):
    bvc.set_context("function", "F03", "S01")
    bvc.eng.state.actions.append({"run": 90, "board": "S01", "step": "function", "action": "heartbeat", "state": "ok"})
    e = bvc.handle_text("start test")
    assert e["reply"] == "Run Long capture on S1. Say confirm."


def test_readback_names_the_volts_and_the_operator_prompt(bvc):
    a = bvc.eng.proto.step("powered")["sections"][0]["actions"][0]
    a["params"] = {"volts": 12, "prompt": "Is the supply set to 12 volts?"}
    e = bvc.handle_text("start test")
    assert e["reply"] == "Run Read rails and current on S1, 12 volts. Is the supply set to 12 volts? Say confirm."
    bvc.handle_text("confirm")
    assert wait(lambda: running(bvc) is not None)
    assert any(x["type"] == "action_confirm" for x in bvc.eng.journal.events)       # the runner's own prompt record


def test_confirmation_can_be_switched_off_in_simulate_mode_only(bvc):
    bvc.set_config({"confirm_simulated": False})
    e = bvc.handle_text("start test")
    assert e["reply"] == "Running Read rails and current on S1." and wait(lambda: running(bvc) is not None)
    bvc.handle_text("stop")
    assert wait(lambda: running(bvc) is None, 10)
    bvc.sess.simulate = False                                         # a real batch: confirmation is not optional
    assert bvc.handle_text("start test")["reply"].endswith("Say confirm.")
    bvc.sess.simulate = True
    bvc.handle_text("stop test")                                      # drops the request
    a = bvc.eng.proto.step("powered")["sections"][0]["actions"][0]
    a["params"] = {"prompt": "Supply connected?"}                    # an action with an operator prompt always asks
    assert bvc.handle_text("start test")["reply"].endswith("Say confirm.")


# ------------------------------------------------------------------------------------------------ stop

def test_stop_aborts_the_run_and_the_safe_state_runs(bvc):
    start_and_confirm(bvc, "S1")
    e = bvc.handle_text("stop test")
    assert e["outcome"] == "applied" and e["reply"] == "Stopping." and e["intent"] == "bench_stop"
    assert wait(lambda: running(bvc) is None, 15)
    evs = bvc.eng.journal.events
    end = [x for x in evs if x["type"] == "action_end"][-1]["payload"]
    assert end["state"] == "cancelled"
    safe = [x for x in evs if x["type"] == "safe_state"][-1]["payload"]
    assert "outputs off" in safe["text"] and safe["plugin"] == "demo.slow"      # all outputs off, read back
    assert wait(lambda: any(e["payload"]["reply"] == "Stopped." for e in journal_voice(bvc)))


def test_stop_needs_no_confirmation_and_one_word_is_enough(bvc):
    start_and_confirm(bvc, "S1")
    e = bvc.handle_text("stop")
    assert e["reply"] == "Stopping." and e["bench"]["cmd"] == "stop" and e["bench"]["cancel_ms"] < 1000
    bvc.handle_text("stop stop")                                      # noisy repeat: harmless
    assert wait(lambda: running(bvc) is None, 15)


def test_stop_is_not_blocked_by_the_utterance_lock_or_a_slow_model(bvc):
    start_and_confirm(bvc, "S1")
    held = threading.Event()

    def hog():                                  # an earlier utterance stuck in the model call
        with bvc._serial:
            held.set()
            time.sleep(1.5)
    th = threading.Thread(target=hog)
    th.start()
    held.wait(2)
    t = time.perf_counter()
    e = bvc.handle_text("stop")
    assert time.perf_counter() - t < 1.0 and e["reply"] == "Stopping."
    th.join()


def test_no_model_call_while_an_action_runs(bvc, monkeypatch):
    start_and_confirm(bvc, "S1")
    bvc.set_config({"llm_enabled": True})
    seen = {}
    from polybench.voice import controller as cm
    real = cm.understand.interpret
    monkeypatch.setattr(cm.understand, "interpret", lambda text, view, llm=None: seen.setdefault("llm", llm) and real(text, view, llm))
    monkeypatch.setattr(bvc, "_llm_status", lambda: {"ok": True, "model": "x"})
    bvc.handle_text("which test")
    assert seen["llm"]["enabled"] is False


def test_stop_with_nothing_running(bvc):
    assert bvc.handle_text("stop test")["reply"] == "Nothing is running."
    assert bvc.handle_text("abort")["reply"] == "Nothing is running."
    assert bvc.handle_text("stop")["reply"] == "Nothing to cancel."     # the older meaning of a bare stop


def test_stop_cancels_a_pending_start_request(bvc):
    bvc.handle_text("start test")
    e = bvc.handle_text("stop")
    assert e["reply"] == "Cancelled. Nothing started." and bvc.status()["bench"]["confirm"] is None and running(bvc) is None


def test_stop_while_a_request_is_pending_and_an_action_runs(bvc):
    start_and_confirm(bvc, "S1")
    bvc.handle_text("start test")                       # refused (busy): no request is made
    assert bvc.status()["bench"]["confirm"] is None
    assert bvc.handle_text("stop")["reply"] == "Stopping."


def test_partial_transcript_stops_at_once(bvc):
    assert bvc.handle_partial("stop") is False          # nothing running
    start_and_confirm(bvc, "S1")
    assert bvc.handle_partial("board S2") is False and running(bvc)["state"] == "running"
    assert bvc.handle_partial("sto") is False           # a half word is not a stop
    assert bvc.handle_partial("stop") is True
    assert wait(lambda: running(bvc) is None, 15)
    e = bvc.handle_text("stop")                         # the final transcript of the same word: not answered again
    assert e["reply"] == ""


def test_the_voice_loop_keeps_listening_while_an_action_runs(bvc, tmp_path):
    """A recorded clip through the voice-activity detector and the speech-to-text stand-in (sidecar text) while demo.slow runs."""
    start_and_confirm(bvc, "S1")
    w = tmp_path / "stop.wav"
    stt.write_wav(str(w), speech(1.0))
    w.with_suffix(".txt").write_text("stop test")
    e = bvc.feed_wav(str(w), "vad")
    assert e["source"] == "clip" and e["reply"] == "Stopping."
    assert wait(lambda: running(bvc) is None, 15)


def test_stop_latency_is_small_in_the_simulator(bvc):
    lat = []
    for _ in range(3):
        start_and_confirm(bvc, "S1")
        lat.append(bvc.handle_text("stop")["bench"]["cancel_ms"])
        assert wait(lambda: running(bvc) is None, 15)
    print("stop latency ms (text arrived -> cancel written):", lat)
    assert max(lat) < 250


# ------------------------------------------------------------------------------------------------ journal

def test_every_command_is_journalled_with_source_and_transcript(bvc):
    bvc.cfg["confirm_timeout_s"] = 0.2
    bvc.handle_text("start test")                                         # no answer: times out
    assert wait(lambda: bvc.status()["bench"]["confirm"] is None)
    assert wait(lambda: any(e["payload"]["source"] == "timer" for e in journal_voice(bvc)))
    bvc.cfg["confirm_timeout_s"] = 10
    for t in ("board S2", "board S9", "start test", "confirm"):
        bvc.handle_text(t, source="handsfree")
    assert wait(lambda: running(bvc) is not None)
    bvc.handle_text("start test")                                         # refused: busy
    bvc.handle_text("stop")
    assert wait(lambda: running(bvc) is None, 15)
    ev = [e["payload"] for e in journal_voice(bvc)]
    by = [(p["transcript"], p["intent"], p["outcome"], p["source"]) for p in ev]
    assert ("board S2", "bench_board", "applied", "handsfree") in by
    assert ("board S9", "bench_board", "rejected", "handsfree") in by
    assert ("start test", "bench_start", "question", "handsfree") in by
    assert ("confirm", "bench_confirm", "applied", "handsfree") in by
    assert ("start test", "bench_start", "rejected", "typed") in by       # busy
    assert ("stop", "bench_stop", "applied", "typed") in by
    assert any(p["source"] == "timer" and p["intent"] == "bench_timeout" for p in ev)
    assert all(p["reply"] is not None and "source" in p and "transcript" in p for p in ev)
    assert [p["bench"]["cmd"] for p in ev if p["intent"] == "bench_confirm"] == ["confirm"]


# ------------------------------------------------------------------------------------------------ end to end (the text path)

def test_end_to_end_board_start_confirm_stop_next_test(tmp_path):
    sess = bench_session(tmp_path)
    with TestClient(create_app(sess)) as c:
        c.post("/api/voice/config", json={"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
        assert c.post("/api/voice/context", json={"step": "powered", "test_id": "P01", "board": "S01"}).json() == {"ok": True}
        say = lambda t: c.post("/api/voice/text", json={"text": t}).json()
        e1 = say("Board S2")
        assert e1["reply"] == "Board S2." and c.get("/api/voice/status").json()["context"]["board"] == "S02"
        e2 = say("start test")
        assert e2["reply"] == "Run Read rails and current on S2. Say confirm."
        st = c.get("/api/voice/status").json()["bench"]
        assert st["confirm"]["board"] == "S02" and st["running"] is None
        e3 = say("confirm")
        assert e3["reply"] == "Running Read rails and current on S2."
        assert wait(lambda: c.get("/api/state").json()["action"] is not None)
        assert c.get("/api/state").json()["action"]["board"] == "S02"
        assert c.get("/api/voice/status").json()["bench"]["running"]["title"] == "Read rails and current"
        e4 = say("stop test")
        assert e4["reply"] == "Stopping."
        assert wait(lambda: c.get("/api/state").json()["action"] is None, 15)
        e5 = say("next test")
        assert e5["intent"] == "next_test" and e5["reply"].startswith("P02,")
        assert c.get("/api/voice/status").json()["context"]["test_id"] == "P02"
        assert c.get("/api/voice/status").json()["bench"]["last"]["text"] == "stop test"
        v = [x for x in c.get("/api/state").json()["voice"] if x["intent"] != "bench_stopped"]
        assert [x["intent"] for x in v[:5]] == ["bench_board", "bench_start", "bench_confirm", "bench_stop", "next_test"]
        assert all(x["source"] == "typed" for x in v)
    types = [e["type"] for e in sess.engine.journal.events]
    assert types.count("action_start") == 1 and "safe_state" in types
    assert not any(e["type"] == "accepted" for e in sess.engine.journal.events)        # a command never accepts a value
    json.dumps(sess.engine.journal.events)


def test_navigation_phrases_still_work_through_the_real_grammar(bvc):
    assert bvc.handle_text("next test")["reply"].startswith("P02,") and bvc.ctx["test_id"] == "P02"
    assert bvc.handle_text("previous test")["reply"].startswith("P01,") and bvc.ctx["test_id"] == "P01"
    assert bvc.handle_text("next board")["intent"] == "next_board"
    assert bvc.handle_text("repeat")["reply"] == bvc.last_reply
