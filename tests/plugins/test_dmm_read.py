"""Action `dmm.read` (R15): stable reading through the same decode, table and rule as the live card; one owner of the HID handle.
HOST-ONLY: simulated meter; the real cable is never opened."""
import asyncio
import json
import shutil
import types

import pytest

from pb_plugin_helpers import run_child
from polybench.plugin_api import Cancelled  # noqa: F401  (import check)
from polybench.plugins._util import ParamError, PluginError
from polybench.plugins.dmm import DmmRead


def test_sim_proposes_the_stable_value_in_the_requested_unit(make_ctx):
    ctx = make_ctx({"map": {"value": "T9"}, "unit": "mV", "timeout_s": 5, "sim": {"scene": "dc_v"}})
    DmmRead().run(ctx)
    (p,) = ctx.proposals
    assert p["test_id"] == "T9" and p["unit"] == "mV" and p["value"] in ("5006", "5007", "5008")
    assert "UT161E" in p["evidence"] and p["raw"].startswith("abcd10")


def test_sim_ohm_scene_converts_kohm_to_ohm(make_ctx):
    ctx = make_ctx({"map": {"value": "T9"}, "unit": "Ohm", "timeout_s": 5, "sim": {"scene": "ohm"}})
    DmmRead().run(ctx)
    assert float(ctx.proposals[0]["value"]) == pytest.approx(4700, abs=1)


def test_key_without_a_mapping_is_not_proposed(make_ctx):
    ctx = make_ctx({"timeout_s": 5})
    DmmRead().run(ctx)
    assert ctx.proposals == []


def test_overload_hold_and_ac_never_give_a_value_and_time_out_with_the_reason(make_ctx):
    for scene, needle in (("ohm_ol", "overload"), ("hold", "HOLD"), ("ac_v", "AC")):
        with pytest.raises(PluginError, match=needle):
            DmmRead().run(make_ctx({"map": {"value": "T9"}, "timeout_s": 0.4, "sim": {"scene": scene}}))


def test_mismatch_fails_and_names_both_quantities(make_ctx):
    with pytest.raises(PluginError, match="resistance.*voltage"):
        DmmRead().run(make_ctx({"map": {"value": "T9"}, "unit": "V", "timeout_s": 5, "sim": {"scene": "ohm"}}))


@pytest.mark.parametrize("params", [{"timeout_s": 0}, {"timeout_s": "x"}, {"n": 1}, {"pct": 50}, {"unit": "furlong"}])
def test_bad_params_are_plain_errors(make_ctx, params):
    with pytest.raises(PluginError):
        DmmRead().run(make_ctx(params))


def test_cancel_stops_the_wait(make_ctx):
    ctx = make_ctx({"map": {"value": "T9"}, "timeout_s": 30, "sim": {"scene": "hold"}})
    ctx.cancelled = lambda: True
    DmmRead().run(ctx)
    assert ctx.proposals == []


def test_safe_state_and_selftest_need_no_hardware():
    a = DmmRead()
    assert "holds nothing" in a.safe_state() and a.selftest() is True


def test_child_process_path(tmp_path):
    msgs, err, rc = run_child("dmm.read", {"map": {"value": "P2"}, "unit": "mV", "timeout_s": 5}, tmp_path)
    assert rc == 0, err
    assert [m["t"] for m in msgs][-1] == "done" and any(m["t"] == "proposal" and m["test_id"] == "P2" for m in msgs)
    msgs, err, rc = run_child("dmm.read", {}, tmp_path, safe_state=True)
    assert rc == 0 and msgs[-1]["t"] == "done"


def test_real_path_without_a_cable_is_a_plain_message(make_ctx, monkeypatch):
    from polybench.meter import transport
    monkeypatch.setattr(transport, "_hid", lambda: types.SimpleNamespace(device=lambda: types.SimpleNamespace(
        open=lambda v, p: (_ for _ in ()).throw(OSError("open failed")))))
    with pytest.raises(PluginError, match="CH9329"):
        DmmRead().run(make_ctx({"map": {"value": "T9"}}, simulate=False))


def test_server_pauses_the_live_reader_while_a_real_run_holds_the_cable(tmp_path, monkeypatch):
    """One owner of the HID handle: the runner pauses the live meter before the child starts and resumes it when the run ends."""
    from helpers import DEMO
    from polybench.protocol import load
    from polybench.server import Session
    d = tmp_path / "proj"
    shutil.copytree(DEMO, d)
    f = d / "unpowered.yaml"
    f.write_text(f.read_text().replace("        tests:\n          - id: U01", "        actions:\n          - {id: dmm, title: Meter, plugin: dmm.read, "
                 "params: {unit: furlong, map: {value: U01}}, proposes: [U01]}\n        tests:\n          - id: U01", 1))
    sess = Session(load(d / "demo.pcbv.yaml"), tmp_path / "data", "dmm", True, 2, None)
    sess.engine.override_gate("S01", "unpowered", "test")
    calls = []
    sess.runner.meter = types.SimpleNamespace(pause=lambda: calls.append("pause") or True, resume=lambda: calls.append("resume"))
    sess.runner.simulate = False             # a "real" run: the bad unit makes the child fail on its params before it can open anything

    async def go():
        await sess.runner.start("unpowered", "rails", "dmm", "S01")
        for _ in range(200):
            await asyncio.sleep(0.1)
            if sess.runner.current is None:
                break
    asyncio.run(go())
    assert calls == ["pause", "resume"]
    end = [e for e in sess.journal.events if e["type"] == "action_end"][-1]["payload"]
    assert end["state"] == "error" and "furlong" in end["text"]


def test_a_simulated_run_never_touches_the_live_reader(tmp_path):
    from helpers import DEMO
    from polybench.protocol import load
    from polybench.server import Session
    d = tmp_path / "proj"
    shutil.copytree(DEMO, d)
    f = d / "unpowered.yaml"
    f.write_text(f.read_text().replace("        tests:\n          - id: U01", "        actions:\n          - {id: dmm, title: Meter, plugin: dmm.read, "
                 "params: {unit: kOhm, map: {value: U01}, sim: {scene: ohm}}, proposes: [U01]}\n        tests:\n          - id: U01", 1))
    sess = Session(load(d / "demo.pcbv.yaml"), tmp_path / "data", "dmm", True, 2, None)
    sess.engine.override_gate("S01", "unpowered", "test")
    calls = []
    sess.runner.meter = types.SimpleNamespace(pause=lambda: calls.append("pause") or True, resume=lambda: calls.append("resume"))

    async def go():
        await sess.runner.start("unpowered", "rails", "dmm", "S01")
        for _ in range(200):
            await asyncio.sleep(0.1)
            if sess.runner.current is None:
                break
    asyncio.run(go())
    assert calls == []
    c = sess.engine.state.cell("S01", "U01")
    assert c["status"] == "accepted" and abs(float(c["value"]) - 4.7) < 0.01 and c["source"]["kind"] == "action"
