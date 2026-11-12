"""Guided section walk by voice: derivation, announcements, ready, readback of both event shapes, navigation, hold window, stop, gates.
Simulator only, no microphone, no sound. The controller is the real one (grammar only); actions are real child processes (demo.*)."""
import asyncio
import shutil
from pathlib import Path
import threading
import time

import pytest

from polybench.protocol import load
from polybench.server import Session
from polybench.voice import guide as gd

from helpers import DEMO

FIX = Path(__file__).parent / "fixtures" / "guided_powered.yaml"      # the demo's Powered step with a 3-step "Rails in band" section; %(read)s = plugin of step 2
POWERED = FIX.read_text(encoding="utf-8")


def wait(cond, t=8.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def voice_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYBENCH_VOICE_CONFIG", str(tmp_path / "voice.json"))
    monkeypatch.setenv("POLYBENCH_VOICE_TEST", "1")


def make_session(tmp_path, read="demo.measure", boards=3, extra=""):
    d = tmp_path / "proto"
    shutil.copytree(DEMO, d)
    (d / "powered.yaml").write_text(POWERED % {"read": read} + extra, encoding="utf-8")
    proto = load(d / "demo.pcbv.yaml")
    assert not [i for i in proto.issues if i.severity == "error"], [i.msg for i in proto.issues]
    sess = Session(proto, tmp_path / "data", "v", True, boards, None, "tester")
    for b in ("S01", "S02"):
        sess.engine.override_gate(b, "powered", "test")
    return sess


def start_loop(sess):
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    sess.loop = loop
    return loop, th


@pytest.fixture
def gvc(tmp_path):
    sess = make_session(tmp_path)
    loop, th = start_loop(sess)
    c = sess.voice
    c.set_config({"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
    c.set_context("powered", "P02", "S01")
    c.msgs = []
    c._sink = c.msgs.append
    yield c
    if sess.runner.status():
        asyncio.run_coroutine_threadsafe(sess.runner.cancel(), loop).result(5)
        wait(lambda: sess.runner.status() is None, 10)
    c._gq.put(None)
    loop.call_soon_threadsafe(loop.stop)
    th.join(2)


def say_log(c):
    return list(c.tts.log)


def said(c, needle, t=8.0):
    return wait(lambda: any(needle in m for m in c.tts.log), t)


# ------------------------------------------------------------------------------------------------ pure: walk derivation

def sec(**kw):
    base = {"id": "s", "title": "S", "actions": [], "tests": []}
    base.update(kw)
    return base


def A(i, proposes=(), **kw):
    return {"id": i, "title": i.title(), "plugin": "demo.measure", "params": {}, "proposes": list(proposes), **kw}


def T(i):
    return {"id": i, "title": i, "kind": "number"}


def test_walk_is_actions_in_order_then_manual_tests():
    w = gd.derive_walk(sec(actions=[A("b", ["T1"]), A("a")], tests=[T("T1"), T("T2"), T("T3")]))
    assert [(s["n"], s["kind"], s["id"]) for s in w] == [(1, "action", "b"), (2, "action", "a"), (3, "manual", "T2"), (4, "manual", "T3")]
    assert w[0]["tests"] == ["T1"] and w[2]["tests"] == ["T2"]


def test_advanced_actions_are_left_out_of_the_default_walk():
    w = gd.derive_walk(sec(actions=[A("a"), A("x", advanced=True)]))
    assert [s["id"] for s in w] == ["a"]


def test_guide_key_overrides_order_and_contents():
    s = sec(actions=[A("a", ["T1"]), A("b")], tests=[T("T1"), T("T2")], guide=[{"test": "T2"}, {"action": "b"}, {"action": "a"}, {"action": "ghost"}])
    assert [(x["kind"], x["id"]) for x in gd.derive_walk(s)] == [("manual", "T2"), ("action", "b"), ("action", "a")]


def test_empty_section_has_no_walk():
    assert gd.derive_walk(sec()) == []


def test_linter_accepts_a_valid_guide_and_names_a_bad_one(tmp_path):
    d = tmp_path / "p"
    shutil.copytree(DEMO, d)
    txt = POWERED % {"read": "demo.measure"}
    (d / "powered.yaml").write_text(txt.replace("        tests:\n          - id: P02", "        guide: [{action: power_on}, {test: P02}, {action: read_5v_an}]\n        tests:\n          - id: P02", 1), encoding="utf-8")
    assert not [i for i in load(d / "demo.pcbv.yaml").issues if i.severity == "error"]
    (d / "powered.yaml").write_text(txt.replace("        tests:\n          - id: P02", "        guide: [{action: nope}, {test: P99}, {step: 3}]\n        tests:\n          - id: P02", 1), encoding="utf-8")
    msgs = [i.msg for i in load(d / "demo.pcbv.yaml").issues if i.severity == "error"]
    assert any("unknown action 'nope'" in m for m in msgs) and any("unknown test 'P99'" in m for m in msgs) and any("'step': 3" in m for m in msgs)


# ------------------------------------------------------------------------------------------------ pure: sentences

def test_first_sentence_is_short_and_plain():
    assert gd.first_sentence("Set the supply to 12 V. Switch on.") == "Set the supply to 12 V."
    assert gd.first_sentence("**Put** the [lead](http://x) on the pad") == "Put the lead on the pad."
    long = " ".join(["word"] * 40)
    assert len(gd.first_sentence(long).split()) == gd.DETAIL_WORDS


def test_energise_phrase_names_voltage_and_current_limit():
    assert gd.energise_phrase({"params": {"volts": 12, "ilim_a": 0.1}}) == "This turns on 12 volts, 100 milliamp limit."
    assert gd.energise_phrase({"params": {"volts": 3.3}}) == "This turns on 3.3 volts."
    assert gd.energise_phrase({"params": {}}) == ""


@pytest.mark.parametrize("raw,want", [("Power analyzer not reachable: [Errno 61] Connection refused. It is on a Wi-Fi extender", "Power analyzer not reachable"),
                                      ("simulated hardware failure: programmer not found", "Simulated hardware failure: programmer not found"),
                                      ("RuntimeError: simulated hardware failure: programmer not found", "Simulated hardware failure: programmer not found"),
                                      ("", "The action failed")])
def test_reason_is_the_plain_first_part(raw, want):
    assert gd.reason_phrase(raw) == want


def test_reading_phrases():
    t = {"title": "5V rail (TP3)", "kind": "number", "unit": "V", "limit": {"class": "hard", "min": 4.94, "max": 5.15}}
    assert gd.reading_phrase(t, {"value": "5.0237", "unit": "V", "verdict": "pass", "flags": []}, lambda u: {"V": "volts"}.get(u, u)) == "5V rail: 5.02 volts, pass."
    assert gd.reading_phrase(t, {"value": "4.5", "unit": "V", "verdict": "fail", "flags": []}, lambda u: {"V": "volts"}.get(u, u)) == "5V rail: 4.5 volts, fail, below 4.94."
    assert gd.reading_phrase(t, {"value": "5.4", "unit": "V", "verdict": "fail", "flags": []}).endswith("fail, above 5.15.")
    assert gd.reading_phrase(t, {"value": "5.2", "unit": "V", "verdict": "review", "flags": ["outside_guess"]}).endswith("review, outside the guess band.")
    assert gd.reading_phrase(t, {"value": "5.0", "unit": "V", "verdict": "none", "flags": []}).endswith("recorded.")


@pytest.mark.parametrize("said,cmd", [("guide me", "start"), ("Guide rails in band", "start"), ("start section", "start"), ("begin section rails", "start"),
                                      ("walk me through rails in band", "start"), ("next section", "next_section")])
def test_start_phrases_always_parse(said, cmd):
    assert gd.parse_guide(gd.__dict__["tokens"](said), False)["cmd"] == cmd


@pytest.mark.parametrize("said,cmd", [("ready", "ready"), ("Ready.", "ready"), ("go", "ready"), ("go ahead", "ready"), ("I'm ready", "ready"),
                                      ("next step", "next"), ("next", "next"), ("previous step", "prev"), ("back", "prev"), ("repeat", "repeat"),
                                      ("redo", "redo"), ("redo step", "redo"), ("redo step 1", "redo"), ("skip step", "skip"), ("end guide", "end"),
                                      ("stop guide", "end"), ("which step", "where"), ("step two", "goto"), ("go to step 3", "goto")])
def test_walk_phrases_parse_only_while_a_walk_is_open(said, cmd):
    toks = gd.__dict__["tokens"](said)
    assert gd.parse_guide(toks, True)["cmd"] == cmd
    assert gd.parse_guide(toks, False) is None


@pytest.mark.parametrize("said", ["S1 0.2 volts", "next test", "skip", "accept", "ready to go home", "redo it all", "board S2"])
def test_other_speech_is_not_a_guide_command(said):
    assert gd.parse_guide(gd.__dict__["tokens"](said), True) is None


# ------------------------------------------------------------------------------------------------ the walk by voice

def test_guide_me_announces_step_one_with_the_intro(gvc):
    e = gvc.handle_text("guide me")
    assert e["outcome"] == "applied"
    assert e["reply"] == "Rails in band, board S1, three steps. Step 1 of 3: Turn on 12 V. This turns on 12 volts, 100 milliamp limit. Say ready."
    assert len(e["reply"].split()) <= 28
    assert gvc.status()["guide"]["n"] == 1 and gvc.status()["guide"]["total"] == 3
    assert gvc.ctx["test_id"] is None or gvc.ctx["step"] == "powered"


def test_guide_by_title_words_and_section_button(gvc):
    e = gvc.handle_text("guide bench checks")
    assert e["reply"].startswith("Bench checks, board S1, two steps. Step 1 of 2: Arm the scope.")
    gvc.handle_text("end guide")
    e = gvc.guide_start("rails_in_band")
    assert e["reply"].startswith("Rails in band, board S1, three steps.")
    gvc.handle_text("end guide")
    e = gvc.handle_text("guide banana")
    assert e["outcome"] == "rejected" and e["reply"].startswith("No section called banana. Say guide Rails in band")


def test_ready_energises_without_a_second_confirm_and_is_the_confirmation(gvc):
    gvc.handle_text("guide me")
    e = gvc.handle_text("ready")
    assert e["outcome"] == "applied" and e["reply"] == "Step 1: running Turn on 12 V." and "confirm" not in e["reply"].lower()
    assert gvc.status()["bench"]["confirm"] is None
    assert said(gvc, "Hold window 60 seconds") and said(gvc, "Say next step")
    starts = [x for x in gvc.eng.journal.events if x["type"] == "action_start"]
    assert [s["payload"]["action"] for s in starts] == ["power_on"] and starts[0]["payload"]["board"] == "S01"
    assert gvc.status()["guide"]["steps"][0]["state"] == "done"


def test_outside_the_guide_start_test_still_asks_for_confirm(gvc):
    e = gvc.handle_text("start test")
    assert e["outcome"] in ("question", "rejected")
    if e["outcome"] == "question":
        assert "Say confirm" in e["reply"]


def test_three_steps_walk_reads_each_reading_back(gvc):
    gvc.handle_text("guide me")
    gvc.handle_text("ready")
    assert said(gvc, "Say next step")
    e = gvc.handle_text("next step")
    assert e["reply"] == "Step 2 of 3: Read 5V. Probe 5V at TP3. Say ready."
    gvc.handle_text("ready")
    assert said(gvc, "5V rail: 5.0")
    assert wait(lambda: gvc.status()["guide"]["steps"][1]["state"] == "done")
    spoken = [m for m in gvc.tts.log if m.startswith("5V rail")][-1]
    assert spoken.endswith(", pass. Say next step.") and "volts" in spoken
    # an instrument reading is recorded at once (auto accept); there is nothing for "next step" to take
    c = gvc.eng.state.cell("S01", "P02")
    assert c["status"] == "accepted" and c["auto"] is True and c["verdict"] == "pass"
    n_acc = sum(ev["type"] == "accept" for ev in gvc.eng.journal.events)
    e = gvc.handle_text("next step")
    assert e["outcome"] == "applied" and e["reply"].startswith("Step 3 of 3: Read 5V_AN.")
    assert gvc.eng.state.cell("S01", "P02")["status"] == "accepted"
    assert sum(ev["type"] == "accept" for ev in gvc.eng.journal.events) == n_acc        # next step emitted no accept for it
    gvc.handle_text("ready")
    assert said(gvc, "5V_AN rail:")
    e = gvc.handle_text("next step")
    assert e["reply"].startswith("Section done. 2 pass.") and "next section" in e["reply"]
    assert gvc.status()["guide"]["done"] is True


def test_review_and_fail_readbacks_and_summary(gvc):
    gvc.handle_text("board S2")
    gvc.handle_text("guide me")
    gvc.handle_text("skip step")
    gvc.handle_text("ready")
    assert said(gvc, "5V rail: 4.5 volts, fail, below 4.85")
    # a failing instrument reading is recorded at once (it shows in the anomalies strip); it does not block the walk and counts as to review
    c = gvc.eng.state.cell("S02", "P02")
    assert c["status"] == "accepted" and c["verdict"] == "fail" and c["auto"] is True
    n_acc = sum(ev["type"] == "accept" for ev in gvc.eng.journal.events)
    e = gvc.handle_text("next step")
    assert e["outcome"] == "applied" and sum(ev["type"] == "accept" for ev in gvc.eng.journal.events) == n_acc
    gvc.handle_text("ready")
    assert said(gvc, "5V_AN rail: 5.2 volts, review, outside the guess band")
    e = gvc.handle_text("next step")
    assert e["reply"].startswith("Section done. 0 pass, 2 to review.")


def feed(c, board, tid, value, shape, run=99):
    """Both event shapes of an instrument reading: a proposal (before instrument-results) and an auto accept (after it)."""
    from polybench.engine import evaluate
    t = c.eng.state.test_index[tid][2]
    ev = evaluate(t, value)
    a = c.eng.state.attempt(board, "powered")
    src = {"kind": "action", "component": "demo.measure", "run_ref": run, "key": "demo.measure", "operator": "tester"}
    pl = {"board": board, "test_id": tid, "attempt": a, "value": ev["value"], "unit": t.get("unit"), "verdict": ev["verdict"], "flags": ev["flags"],
          "reason": ev["reason"], "source": src, "origin": "simulated", "evidence": None, "raw": None}
    if shape == "proposal":
        c.eng._emit("proposal", pl)
    else:
        c.eng._emit("proposal", pl)
        c.eng._emit("accept", {**pl, "auto": True, "confirmed": False})        # instrument-results: proposal, then the auto accept of it


@pytest.mark.parametrize("shape", ["proposal", "auto_accept"])
def test_reading_is_spoken_once_for_either_event_shape(gvc, shape):
    gvc.handle_text("guide me")
    gvc.handle_text("skip step")
    g = gvc._g
    g["states"][1], g["run"] = "running", 99
    feed(gvc, "S01", "P02", "5.0237", shape)
    gvc.eng._emit("action_end", {"run": 99, "state": "ok", "text": ""})
    assert said(gvc, "5V rail: 5.02 volts, pass. Say next step.")
    assert sum("5V rail" in m for m in gvc.tts.log) == 1


def test_accept_events_of_the_operator_are_not_read_back_as_readings(gvc):
    gvc.handle_text("guide me")
    gvc.handle_text("skip step")
    gvc._g["states"][1], gvc._g["run"] = "running", 99
    gvc.eng.entry("S01", "P02", "5.0")            # a keyboard entry: not an instrument reading
    gvc.eng._emit("action_end", {"run": 99, "state": "ok", "text": ""})
    assert said(gvc, "Say next step")
    assert not any("5V rail:" in m for m in gvc.tts.log)


def test_failed_action_says_the_plain_reason_and_stays_on_the_step(gvc):
    gvc.handle_text("guide bench checks")
    gvc.handle_text("ready")
    assert said(gvc, "Simulated hardware failure: programmer not found. Say redo")
    g = gvc.status()["guide"]
    assert g["n"] == 1 and g["step"]["state"] == "failed"
    assert gvc.handle_text("next step")["reply"] == "Step 1 is not done. Say ready, or skip step."
    assert gvc.handle_text("redo")["outcome"] == "applied"
    assert wait(lambda: sum("Simulated hardware failure" in m for m in gvc.tts.log) == 2)


def test_manual_step_value_is_read_back_in_guide_words(gvc):
    gvc.handle_text("guide bench checks")
    gvc.handle_text("skip step")
    assert gvc.tts.log[-1] == "Step 2 of 2: Measure Ripple on 5V. Probe plus on TP3. Say the value."
    assert gvc.ctx["test_id"] == "M01"
    e = gvc.handle_text("12 millivolts")
    assert e["reply"] == "Ripple on 5V: 12 millivolts, pass. Say next step.", e["reply"]
    assert gvc.ctx["board"] == "S01"
    e = gvc.handle_text("next step")
    assert e["reply"].startswith("Section done. 1 pass") and gvc.eng.state.cell("S01", "M01")["status"] == "accepted"


def test_manual_fail_blocks_next_step_until_decided(gvc):
    gvc.handle_text("guide bench checks")
    gvc.handle_text("skip step")
    e = gvc.handle_text("80 millivolts")
    assert "fail, above 50" in e["reply"] and e["reply"].endswith("Say accept fail, or reject.")
    assert gvc.handle_text("next step")["reply"] == "Pending fail: say accept fail or reject first."


def test_next_without_ready_is_refused_and_skip_moves_on(gvc):
    gvc.handle_text("guide me")
    e = gvc.handle_text("next step")
    assert e["outcome"] == "rejected" and e["reply"] == "Step 1 is not done. Say ready, or skip step."
    e = gvc.handle_text("skip step")
    assert e["reply"].startswith("Step 2 of 3: Read 5V.")


def test_previous_repeat_goto_and_where(gvc):
    gvc.handle_text("guide me")
    gvc.handle_text("skip step")
    assert gvc.handle_text("previous step")["reply"].startswith("Step 1 of 3: Turn on 12 V.")
    assert gvc.handle_text("previous step")["reply"] == "This is the first step."
    assert gvc.handle_text("repeat")["reply"].startswith("Step 1 of 3")
    assert gvc.handle_text("step three")["reply"].startswith("Step 3 of 3: Read 5V_AN.")
    assert gvc.handle_text("which step")["reply"].startswith("Step 3 of 3")
    assert gvc.handle_text("step nine")["outcome"] == "rejected"


def test_end_guide_and_commands_without_a_walk(gvc):
    assert gvc.handle_text("ready")["intent"] != "guide"                       # no walk: "ready" is not a guide command
    gvc.handle_text("guide me")
    assert gvc.handle_text("end guide")["reply"] == "Guide ended."
    assert gvc.status()["guide"] is None
    # "next" keeps its older meaning once the walk is closed
    assert gvc.handle_text("next test")["intent"] == "next_test"


def test_next_section_after_the_last_step(gvc):
    gvc.handle_text("guide me")
    for _ in range(3):
        gvc.handle_text("skip step")
    assert gvc.status()["guide"]["done"]
    assert gvc.handle_text("next section")["reply"].startswith("Bench checks, board S1, two steps.")
    for _ in range(2):
        gvc.handle_text("skip step")
    assert gvc.handle_text("next section")["reply"].startswith("Long capture, board S1, one step.")
    gvc.handle_text("skip step")
    e = gvc.handle_text("next section")
    assert e["outcome"] == "rejected" and "No more sections" in e["reply"]


# ------------------------------------------------------------------------------------------------ hold window

def test_hold_window_warns_at_15_s_and_offers_redo_when_it_lapses(gvc):
    clock = {"t": 1000.0}
    gvc._gnow = lambda: clock["t"]
    gvc._g_autotick = False
    gvc.handle_text("guide me")
    gvc.handle_text("ready")
    assert said(gvc, "Hold window 60 seconds.")
    assert gvc.status()["guide"]["hold"] == {"left_s": 60.0, "window_s": 60}
    clock["t"] += 44
    gvc.guide_tick()
    assert not any("left on the hold" in m for m in gvc.tts.log)
    clock["t"] += 2
    gvc.guide_tick()
    assert gvc.tts.log[-1] == "15 seconds left on the hold."
    gvc.guide_tick()
    assert sum("left on the hold" in m for m in gvc.tts.log) == 1
    clock["t"] += 20
    gvc.guide_tick()
    assert gvc.tts.log[-1] == "The hold lapsed and the output is off. Say redo step 1."
    assert gvc.status()["guide"]["hold"] is None and gvc.status()["guide"]["steps"][0]["state"] == "lapsed"
    e = gvc.handle_text("redo step 1")
    assert e["outcome"] == "applied" and e["reply"] == "Step 1: running Turn on 12 V."
    assert said(gvc, "Hold window 60 seconds.")


def test_hold_comes_from_the_hold_param_or_the_profile_watchdog():
    assert gd.hold_s({"params": {"hold_s": 45}}) == 45
    assert gd.hold_s({"params": {}}) is None
    assert gd.hold_s({"params": {"leave": "on", "profile": "does-not-exist"}}) is None


# ------------------------------------------------------------------------------------------------ stop, gates, other

def test_stop_mid_step_aborts_at_once_and_pauses_the_walk(tmp_path):
    sess = make_session(tmp_path, read="demo.slow")
    loop, th = start_loop(sess)
    c = sess.voice
    c.set_config({"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
    c.set_context("powered", "P02", "S01")
    c._sink = lambda m: None
    try:
        c.handle_text("guide me")
        c.handle_text("skip step")
        c.handle_text("ready")
        assert wait(lambda: sess.runner.status() is not None)
        e = c.handle_text("stop")
        assert e["reply"] == "Stopping." and e["bench"]["cancel_ms"] < 500
        assert said(c, "Stopped. Guide paused on step 2. Say ready to run it again.", 15)
        assert c.status()["guide"]["step"]["state"] == "paused" and sess.runner.status() is None
        assert c.handle_text("ready")["reply"] == "Step 2: running Read 5V."          # resumes by running the step again
        assert wait(lambda: sess.runner.status() is not None)
    finally:
        if sess.runner.status():
            asyncio.run_coroutine_threadsafe(sess.runner.cancel(), loop).result(5)
            wait(lambda: sess.runner.status() is None, 10)
        c._gq.put(None)
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)


def test_gated_board_is_refused_with_the_reason_and_no_walk_opens(gvc):
    gvc.handle_text("board S3")
    e = gvc.handle_text("guide me")
    assert e["outcome"] == "rejected" and e["reply"].startswith("Refused: step is gated") and gvc.status()["guide"] is None


def test_gate_closing_during_the_walk_refuses_ready(gvc):
    gvc.handle_text("guide me")
    gvc.eng._emit("stop", {"board": "S01", "step": "unpowered", "test_id": "U01", "attempt": 1, "text": "shorted", "value": "0.5"})
    e = gvc.handle_text("ready")
    assert e["outcome"] == "rejected" and "S1 is stopped" in e["reply"] and not [x for x in gvc.eng.journal.events if x["type"] == "action_start"]
    assert gvc.status()["guide"]["step"]["state"] == "pending"


def test_one_action_at_a_time(tmp_path):
    sess = make_session(tmp_path, read="demo.slow")
    loop, th = start_loop(sess)
    c = sess.voice
    c.set_config({"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
    c.set_context("powered", "P02", "S01")
    c._sink = lambda m: None
    try:
        c.handle_text("guide me")
        c.handle_text("skip step")
        c.handle_text("ready")
        assert wait(lambda: sess.runner.status() is not None)
        for said_ in ("ready", "redo", "skip step", "next step", "previous step"):
            e = c.handle_text(said_)
            assert e["outcome"] == "rejected" and "still running" in e["reply"], said_
        assert len([x for x in sess.engine.journal.events if x["type"] == "action_start"]) == 1
    finally:
        if sess.runner.status():
            asyncio.run_coroutine_threadsafe(sess.runner.cancel(), loop).result(5)
            wait(lambda: sess.runner.status() is None, 10)
        c._gq.put(None)
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)


def test_a_board_change_restarts_the_walk_on_the_new_board(gvc):
    gvc.handle_text("guide me")
    gvc.handle_text("skip step")
    e = gvc.handle_text("board S2")
    assert "board S2" in e["reply"] and "Step 1 of 3" in e["reply"] and gvc.status()["guide"]["board"] == "S02"


def test_guide_without_a_board_or_step(gvc):
    gvc.set_context("powered", "P02", None)
    assert gvc.handle_text("guide me")["reply"] == "Which board? Say board S1."
    gvc.set_context(None, None, "S01")
    assert gvc.handle_text("guide me")["reply"].startswith("No step is open")


def test_journal_entries_carry_the_guide_position(gvc):
    gvc.handle_text("guide me")
    ents = [x["payload"] for x in gvc.eng.journal.events if x["type"] == "voice"]
    assert ents[-1]["guide"] == {"section": "rails_in_band", "n": 1, "total": 3}


# ------------------------------------------------------------------------------------------------ after instrument-results

def readings_spoken(c, name):
    return sum(m.startswith(name) or (" " + name) in m for m in c.tts.log)


def test_redo_reruns_the_action_and_replaces_the_value_in_the_same_attempt(gvc):
    gvc.handle_text("guide me")
    gvc.handle_text("skip step")
    gvc.handle_text("ready")
    assert said(gvc, "5V rail: 5.0") and wait(lambda: gvc.status()["guide"]["steps"][1]["state"] == "done")
    eng = gvc.eng
    cell = eng.state.cell("S01", "P02")
    first, att = cell["value"], eng.state.attempt("S01", "powered")
    assert cell["status"] == "accepted" and not cell["history"]
    types = [e["type"] for e in eng.journal.events]
    e = gvc.handle_text("redo")
    assert e["outcome"] == "applied" and e["reply"].startswith("Step 2: running Read 5V")
    assert wait(lambda: sum(m.startswith("5V rail") for m in gvc.tts.log) == 2)
    assert wait(lambda: gvc.status()["guide"]["steps"][1]["state"] == "done")
    cell = eng.state.cell("S01", "P02")
    assert cell["status"] == "accepted" and len(cell["history"]) == 1 and cell["history"][0]["value"] == first      # value replaced, old one kept
    assert eng.state.attempt("S01", "powered") == att                                                              # same attempt
    new = [e["type"] for e in eng.journal.events][len(types):]
    assert "retest" not in new and "edit" in new                                                                   # the per-test Redo path, not a step retest
    assert sum(m.startswith("5V rail") for m in gvc.tts.log) == 2                                                  # each run read back once


def test_a_table_result_is_read_back_as_rows_not_as_a_dump(tmp_path):
    extra = """      - id: load_check
        title: Load check
        instructions: "Sweep the load."
        actions:
          - id: sweep
            title: Sweep the 5 V rail
            plugin: demo.sweep
            params: {test: P05, loads: [0, 10, 20, 40, 80], v0: 5.0, droop: 0.0006, force_droop: {"S02": 0.004}}
            proposes: [P05]
        tests:
          - id: P05
            title: 5V rail against load
            kind: table
            columns:
              - {id: load_ma, title: Load, unit: mA}
              - {id: rail_v, title: Rail, unit: V}
            limits:
              rail_v: {class: hard, min: 4.85, max: 5.15, source: {type: datasheet, doc: "DEMO DS", page: 3}}
            inputs: [action]
"""
    sess = make_session(tmp_path, extra=extra)
    loop, th = start_loop(sess)
    c = sess.voice
    c.set_config({"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
    c.set_context("powered", "P05", "S02")
    try:
        c.handle_text("guide load check")
        assert c.handle_text("ready")["outcome"] == "applied"
        assert said(c, "5V rail against load: 5 rows") and wait(lambda: c.status()["guide"]["step"]["state"] == "done")
        assert sum("against load" in m for m in c.tts.log) == 1 and not any("{" in m or "columns" in m for m in c.tts.log)
        cell = sess.engine.state.cell("S02", "P05")
        assert cell["status"] == "accepted" and cell["auto"] is True
        spoken = [m for m in c.tts.log if m.startswith("5V rail against load")][-1]
        assert ("fail" in spoken and "out of band" in spoken) == (cell["verdict"] == "fail")
    finally:
        if sess.runner.status():
            asyncio.run_coroutine_threadsafe(sess.runner.cancel(), loop).result(5)
            wait(lambda: sess.runner.status() is None, 10)
        c._gq.put(None)
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)


def test_a_voice_value_on_a_manual_step_is_still_a_proposal_until_next_step(gvc):
    gvc.handle_text("guide bench checks")
    gvc.handle_text("skip step")
    gvc.handle_text("12 millivolts")
    c = gvc.eng.state.cell("S01", "M01")
    assert c["status"] == "empty" and c["proposals"]                       # the spoken value is only a proposal (source voice)
    gvc.handle_text("next step")                                           # read back first (above), then accepted on next step
    assert gvc.eng.state.cell("S01", "M01")["status"] == "accepted"
