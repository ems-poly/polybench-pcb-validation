"""Live multimeter service on a DEMO session: proposal into the focused test, stability, latch, auto-accept, spoken read-back, Undo,
voice override, journal (R8, R10, R11). HOST-ONLY: readings are fed straight in; nothing opens hardware, nothing speaks."""
import shutil
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

from helpers import DEMO
from polybench.meter import frames as F
from polybench.meter import reading as R
from polybench.meter.service import MeterService


def make(tmp_path, monkeypatch, auto=False, delay=0.05, undo_s=5.0):
    from polybench.protocol import load
    from polybench.server import Session, create_app
    monkeypatch.delenv("POLYBENCH_AI_PERMISSIONS", raising=False)
    d = tmp_path / "proj"
    shutil.copytree(DEMO, d)
    sess = Session(load(d / "demo.pcbv.yaml"), tmp_path / "data", "m", True, 3, None, operator="Op")
    for b in ("S01", "S02", "S03"):
        sess.engine.override_gate(b, "unpowered", "test starts at unpowered")
        sess.engine.override_gate(b, "powered", "test starts at powered")
    msgs, said = [], []
    svc = MeterService(lambda: sess, msgs.append, simulate=True)
    svc.cfg.update(delay_s=delay, undo_s=undo_s)
    svc.reader.state, svc.reader.model = "connected", "UT161E"           # the reader thread is not started: readings are fed in by hand
    sess._voice = types.SimpleNamespace(blocked=False, meter_blocked=lambda: sess._voice.blocked,
                                        tts=types.SimpleNamespace(say=lambda t, kind=None: said.append(t)))
    c = TestClient(create_app(sess, meter=svc))
    sess.ai_hub.focus = {"step": "unpowered", "board": "S01", "test": "U01", "view": ""}
    if auto:
        svc.auto = True
    return sess, svc, msgs, said, c


def feed(svc, display, mode=0x02, rng=1, flags=(0x30, 0x30, 0x30), n=5):
    for _ in range(n):
        r = R.decode("UT161E", F.parse_measurement(F.build_measurement(mode, rng, display, flags)), time.time())
        svc.handle(r)
    return r


def props(sess, board="S01", test="U01"):
    return list(sess.engine.state.cell(board, test)["proposals"].values())


def focus(sess, board, test):
    sess.ai_hub.focus = {"step": "x", "board": board, "test": test, "view": ""}


def until(fn, t=3.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.01)
    return False


# ---- R8 proposal path
def test_stable_reading_is_proposed_into_the_focused_test_in_its_unit(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, "  4.700", 0x06, 1, n=4)
    assert props(sess) == []                                                   # four readings are not stable yet
    feed(svc, "  4.700", 0x06, 1, n=1)
    p = props(sess)
    assert len(p) == 1 and p[0]["value"] == "4.7" and p[0]["unit"] == "kOhm"        # U01 is a kOhm test, meter shows kOhm
    ev = [e for e in sess.journal.events if e["type"] == "proposal"][-1]["payload"]
    assert ev["source"]["kind"] == "meter" and ev["source"]["key"] == "meter" and ev["origin"] == "simulated"
    assert "UT161E" in ev["evidence"] and "range 1" in ev["evidence"] and "'  4.700'" in ev["evidence"] and ev["raw"] in ev["evidence"]
    assert ev["raw"] == F.build_measurement(0x06, 1, "  4.700").hex()
    assert sess.engine.state.cell("S01", "U01")["status"] != "accepted"            # a proposal, never a result


def test_meter_converts_to_the_focused_tests_unit(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, "  4700.0", 0x06, 0)                                             # 4700.0 Ohm shown on the 220 Ohm... any Ohm range
    assert props(sess)[0]["value"] == "4.7" and props(sess)[0]["unit"] == "kOhm"


def test_the_plugin_never_computes_a_verdict_the_engine_does(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, "  0.500", 0x06, 1)                                               # 0.5 kOhm: below the hard minimum of 10
    p = props(sess)[0]
    assert p["verdict"] == "fail" and svc.proposal["verdict"] == "fail"
    assert svc.public_state()["proposal"]["value"] == "0.5"


def test_a_new_stable_value_replaces_the_meters_previous_pending_proposal(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, "  4.700", 0x06, 1)
    feed(svc, "  OL.  ", 0x06, 6, n=1)                                         # probe lifted
    feed(svc, " 12.300", 0x06, 1)
    p = props(sess)
    assert len(p) == 1 and p[0]["value"] == "12.3"
    assert len([e for e in sess.journal.events if e["type"] == "proposal"]) == 2   # both journalled, one pending


def test_goes_through_the_normal_accept_path(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, " 12.300", 0x06, 1)
    pid = props(sess)[0]["id"]
    sess.engine.accept("S01", "U01", pid)
    c = sess.engine.state.cell("S01", "U01")
    assert c["status"] == "accepted" and c["value"] == "12.3" and c["source"]["kind"] == "meter"
    acc = [e for e in sess.journal.events if e["type"] == "accept"][-1]["payload"]
    assert acc["source"]["kind"] == "meter" and "via" not in acc


# ---- R4/R5/R6 reasons on the card, nothing proposed
@pytest.mark.parametrize("display,mode,rng,flags,needle", [
    ("  OL.  ", 0x06, 6, (0x30,) * 3, "overload"), (" -OL.  ", 0x06, 6, (0x30,) * 3, "-OL"),
    (" 12.300", 0x06, 1, (0x32, 0x30, 0x30), "HOLD"), (" 12.300", 0x06, 1, (0x31, 0x30, 0x30), "REL"),
    (" 12.300", 0x06, 1, (0x34, 0x30, 0x30), "MIN"), (" 12.300", 0x06, 1, (0x38, 0x30, 0x30), "MAX"),
    (" 12.300", 0x06, 1, (0x30, 0x30, 0x32), "P-MIN"), (" 12.300", 0x06, 1, (0x30, 0x30, 0x34), "P-MAX"),
    ("  3.300", 0x00, 1, (0x30,) * 3, "AC"), ("  3.300", 0x19, 1, (0x30,) * 3, "AC"), ("  3.300", 0x09, 0, (0x30,) * 3, "vocabulary"),
])
def test_not_proposed_with_the_reason(tmp_path, monkeypatch, display, mode, rng, flags, needle):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, display, mode, rng, flags, n=8)
    assert props(sess) == [] and needle in svc.public_state()["reason"]


def test_quantity_mismatch_proposes_nothing_and_says_so(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    focus(sess, "S01", "P02")                                                    # P02 wants V; the meter is on resistance
    feed(svc, "  4.700", 0x06, 1, n=8)
    assert props(sess, "S01", "P02") == []
    why = svc.public_state()["reason"]
    assert "resistance" in why and "voltage" in why


def test_no_focus_or_a_non_number_test_gets_nothing(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    sess.ai_hub.focus = {}
    feed(svc, "  4.700", 0x06, 1, n=6)
    assert "no test in focus" in svc.public_state()["reason"]
    focus(sess, "S01", "U04")                                                    # a yes/no test
    svc.latch = None
    feed(svc, "  4.700", 0x06, 1, n=6)
    assert props(sess, "S01", "U04") == [] and "yn" in svc.public_state()["reason"]


def test_mode_or_range_change_restarts_the_window(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, "  4.700", 0x06, 1, n=4)
    feed(svc, " 47.000", 0x06, 2, n=1)
    assert props(sess) == [] and svc.win.progress()[0] == 1


def test_an_unstable_reading_is_not_proposed(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    for v in ("  4.700", "  4.900", "  4.700", "  5.100", "  4.700", "  5.300"):
        feed(svc, v, 0x06, 1, n=1)
    assert props(sess) == [] and "waiting for a stable" in svc.public_state()["reason"]


# ---- latch: one proposal per settled reading
def test_a_settled_reading_is_proposed_once_then_waits_for_the_probe_to_move(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, "  4.700", 0x06, 1)
    pid = props(sess)[0]["id"]
    sess.engine.reject("S01", "U01", pid, "test")
    feed(svc, "  4.700", 0x06, 1, n=20)                                         # same reading, still on the same point
    assert props(sess) == []
    focus(sess, "S01", "U02")
    feed(svc, "  4.700", 0x06, 1, n=20)                                         # focus moved, probe has not: nothing for the next test
    assert props(sess, "S01", "U02") == []
    feed(svc, "  OL.  ", 0x06, 6, n=1)                                          # probe lifted
    feed(svc, " 22.100", 0x06, 1)
    assert props(sess, "S01", "U02")[0]["value"] == "22.1"


def test_a_clearly_different_stable_value_unlatches(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, "  4.700", 0x06, 1)
    focus(sess, "S01", "U02")
    feed(svc, "  9.100", 0x06, 1)
    assert props(sess, "S01", "U02")[0]["value"] == "9.1"


# ---- R10 auto-accept
def test_auto_accept_is_off_by_default(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    assert svc.auto is False and svc.public_state()["auto"]["on"] is False
    feed(svc, " 12.300", 0x06, 1)
    time.sleep(0.2)
    assert sess.engine.state.cell("S01", "U01")["status"] != "accepted"


def test_auto_accept_waits_the_delay_then_accepts_speaks_advances_and_offers_undo(tmp_path, monkeypatch):
    sess, svc, msgs, said, _ = make(tmp_path, monkeypatch, auto=True, delay=0.15)
    feed(svc, " 12.300", 0x06, 1)
    assert sess.engine.state.cell("S01", "U01")["status"] != "accepted"             # not before the delay
    assert svc.public_state()["auto"]["due_in_s"] is not None
    assert until(lambda: sess.engine.state.cell("S01", "U01")["status"] == "accepted")
    acc = [e for e in sess.journal.events if e["type"] == "accept"][-1]["payload"]
    assert acc["via"] == "meter-auto" and acc["value"] == "12.3" and acc["source"]["kind"] == "meter" and acc["source"]["operator"] == "Op"
    assert said == ["twelve point three kilo ohms, accepted"]
    assert {"kind": "meter_advance", "board": "S01", "test_id": "U01"} in msgs
    st = svc.public_state()
    assert st["undo"]["test_id"] == "U01" and st["undo"]["left_s"] > 4


def test_auto_accept_is_cancelled_if_the_reading_moves_before_the_delay(tmp_path, monkeypatch):
    sess, svc, msgs, said, _ = make(tmp_path, monkeypatch, auto=True, delay=0.3)
    feed(svc, " 12.300", 0x06, 1)
    feed(svc, " 30.000", 0x06, 1, n=1)
    time.sleep(0.45)
    assert sess.engine.state.cell("S01", "U01")["status"] != "accepted" and said == []


def test_auto_accept_never_confirms_a_fail(tmp_path, monkeypatch):
    sess, svc, msgs, said, _ = make(tmp_path, monkeypatch, auto=True, delay=0.05)
    feed(svc, "  0.500", 0x06, 1)                                                  # below the hard limit: FAIL
    time.sleep(0.3)
    assert sess.engine.state.cell("S01", "U01")["status"] != "accepted" and said == []
    assert "FAIL" in svc.public_state()["reason"] and len(props(sess)) == 1        # left for the operator


def test_toggle_off_cancels_a_pending_auto_accept(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch, auto=True, delay=0.3)
    feed(svc, " 12.300", 0x06, 1)
    svc.set_auto(False)
    time.sleep(0.45)
    assert sess.engine.state.cell("S01", "U01")["status"] != "accepted"


def test_manual_accept_before_the_delay_leaves_nothing_for_auto(tmp_path, monkeypatch):
    sess, svc, msgs, said, _ = make(tmp_path, monkeypatch, auto=True, delay=0.2)
    feed(svc, " 12.300", 0x06, 1)
    sess.engine.accept("S01", "U01", props(sess)[0]["id"])
    time.sleep(0.4)
    assert said == [] and len([e for e in sess.journal.events if e["type"] == "accept"]) == 1
    assert svc.undo_info is None


def test_undo_reverts_through_a_journalled_event_and_is_not_proposed_again(tmp_path, monkeypatch):
    sess, svc, msgs, said, c = make(tmp_path, monkeypatch, auto=True, delay=0.05)
    feed(svc, " 12.300", 0x06, 1)
    assert until(lambda: sess.engine.state.cell("S01", "U01")["status"] == "accepted")
    n = len(sess.journal.events)
    r = c.post("/api/meter/undo")
    assert r.status_code == 200 and r.json()["test_id"] == "U01"
    cell = sess.engine.state.cell("S01", "U01")
    assert cell["status"] != "accepted" and cell["value"] is None
    ev = sess.journal.events[n:]
    assert [e["type"] for e in ev] == ["undo"] and ev[0]["payload"]["via"] == "meter-undo" and ev[0]["payload"]["value"] == "12.3"
    assert any(e["type"] == "accept" for e in sess.journal.events)                    # the accept stays in the journal
    assert {"kind": "meter_focus", "board": "S01", "test_id": "U01"} in msgs
    focus(sess, "S01", "U01")
    feed(svc, " 12.300", 0x06, 1, n=15)
    time.sleep(0.2)
    assert props(sess) == [] and cell["status"] != "accepted"
    assert c.post("/api/meter/undo").status_code == 409                               # nothing left to undo


def test_undo_survives_a_replay_of_the_journal(tmp_path, monkeypatch):
    from polybench.engine import Engine
    from polybench.store import Journal
    sess, svc, *_ = make(tmp_path, monkeypatch, auto=True, delay=0.05)
    feed(svc, " 12.300", 0x06, 1)
    assert until(lambda: sess.engine.state.cell("S01", "U01")["status"] == "accepted")
    svc.undo()
    sess.engine.journal.close()
    e2 = Engine(sess.engine.proto, Journal(sess.journal.path), "Op")
    assert e2.state.cell("S01", "U01")["status"] != "accepted"


def test_undo_window_expires(tmp_path, monkeypatch):
    sess, svc, msgs, said, c = make(tmp_path, monkeypatch, auto=True, delay=0.02, undo_s=1.0)
    feed(svc, " 12.300", 0x06, 1)
    assert until(lambda: sess.engine.state.cell("S01", "U01")["status"] == "accepted")
    svc.undo_info["until"] = time.monotonic() - 0.1
    assert c.post("/api/meter/undo").status_code == 409
    assert sess.engine.state.cell("S01", "U01")["status"] == "accepted"


def test_undo_refused_when_the_value_changed_or_a_stop_tripped(tmp_path, monkeypatch):
    sess, svc, msgs, said, c = make(tmp_path, monkeypatch, auto=True, delay=0.02)
    feed(svc, " 12.300", 0x06, 1)
    assert until(lambda: sess.engine.state.cell("S01", "U01")["status"] == "accepted")
    sess.engine.entry("S01", "U01", "13", replace=True)                              # the operator edited it meanwhile
    r = c.post("/api/meter/undo")
    assert r.status_code == 409 and sess.engine.state.cell("S01", "U01")["value"] == "13"


def test_stop_rule_trip_is_reported_and_has_no_undo(tmp_path, monkeypatch):
    sess, svc, msgs, said, c = make(tmp_path, monkeypatch, auto=True, delay=0.02)
    focus(sess, "S01", "U02")
    feed(svc, "  2.500", 0x06, 1)
    focus(sess, "S02", "U01")
    svc.latch = None
    # U01 has stop_if below 1 kOhm; 0.5 is also a FAIL so auto never takes it: take it by hand and check undo is refused by the engine
    feed(svc, "  0.500", 0x06, 1)
    pid = props(sess, "S02", "U01")[0]["id"]
    sess.engine.accept("S02", "U01", pid, confirm=True)
    with pytest.raises(Exception):
        sess.engine.undo_accept("S02", "U01", sess.engine.state.cell("S02", "U01")["event"])


# ---- R11 voice overrides the meter
def test_voice_busy_blocks_proposals_and_auto_accept(tmp_path, monkeypatch):
    sess, svc, msgs, said, _ = make(tmp_path, monkeypatch, auto=True, delay=0.1)
    sess._voice.blocked = True
    feed(svc, " 12.300", 0x06, 1, n=8)
    assert props(sess) == [] and "voice" in svc.public_state()["reason"]
    sess._voice.blocked = False
    feed(svc, " 12.300", 0x06, 1, n=1)
    assert len(props(sess)) == 1
    sess._voice.blocked = True                                                        # PTT pressed during the auto-accept delay
    time.sleep(0.25)
    assert sess.engine.state.cell("S01", "U01")["status"] != "accepted" and said == []
    assert "voice" in svc.note


def test_controller_state_defines_blocked():
    from polybench.voice.controller import VoiceController as V
    def cap(rec=False, busy=False, seg=False):
        return types.SimpleNamespace(capture=types.SimpleNamespace(recording=rec, busy=busy, seg=types.SimpleNamespace(active=seg)))
    assert V.meter_blocked(cap()) is False
    assert V.meter_blocked(cap(rec=True)) is True                                     # PTT held
    assert V.meter_blocked(cap(busy=True)) is True                                    # an utterance is being handled
    assert V.meter_blocked(cap(seg=True)) is True                                     # the operator is speaking


# ---- connection loss
def test_lost_connection_clears_the_window_and_proposes_nothing(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, " 12.300", 0x06, 1, n=4)
    svc.reader.state = "lost"
    svc._on_conn()
    assert svc.win.progress()[0] == 0 and svc.public_state()["reading"] is None
    feed(svc, " 12.300", 0x06, 1, n=1)
    assert props(sess) == []


# ---- routes
def test_routes_toggle_config_and_state(tmp_path, monkeypatch):
    sess, svc, msgs, said, c = make(tmp_path, monkeypatch)
    assert c.get("/api/meter").json()["auto"]["on"] is False
    assert c.post("/api/meter/auto", json={"on": True}).json()["auto"]["on"] is True
    assert c.post("/api/meter/config", json={"n": 3, "pct": 0.5, "delay_s": 2, "undo_s": 8}).json()["cfg"]["n"] == 3
    assert c.post("/api/meter/config", json={"n": 1}).status_code == 422
    assert c.post("/api/meter/config", json={"delay_s": "x"}).status_code == 422
    assert c.post("/api/meter/sim", json={"scene": "ohm_ol"}).status_code == 200
    assert c.post("/api/meter/sim", json={"scene": "nope"}).status_code == 422


def test_without_a_meter_the_app_still_runs(tmp_path, monkeypatch):
    from polybench.protocol import load
    from polybench.server import Session, create_app
    d = tmp_path / "p"
    shutil.copytree(DEMO, d)
    sess = Session(load(d / "demo.pcbv.yaml"), tmp_path / "data", "m", True, 2, None, operator="Op")
    c = TestClient(create_app(sess))
    assert c.get("/api/meter").json() == {"enabled": False}
    assert c.post("/api/meter/auto", json={"on": True}).status_code == 409
    assert c.get("/api/state").status_code == 200


def test_assistant_cannot_toggle_auto_accept_or_undo(tmp_path, monkeypatch):
    sess, svc, msgs, said, c = make(tmp_path, monkeypatch)
    ai = {"X-Polybench-Client": "assistant", "X-Polybench-Assistant": "pytest", "X-Polybench-Session": "t1"}
    for path, body in (("/api/meter/auto", {"on": True}), ("/api/meter/undo", {}), ("/api/meter/config", {"n": 3}), ("/api/meter/sim", {"scene": "dc_v"})):
        r = c.post(path, json=body, headers=ai)
        assert r.status_code == 403, path
    assert svc.auto is False


def test_a_meter_proposal_shows_in_the_assistants_read_view_as_a_meter_source(tmp_path, monkeypatch):
    sess, svc, *_ = make(tmp_path, monkeypatch)
    feed(svc, " 12.300", 0x06, 1)
    p = props(sess)[0]
    assert p["source"]["kind"] == "meter"
