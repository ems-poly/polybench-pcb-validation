import csv
import json
from pathlib import Path

import pytest

from polybench.plugins._util import ParamError, PluginError
from polybench.plugins.pcan import PcanCapture, PcanQuery, PcanStats

DEC = [{"name": "bridge_uV", "id": "0x3F0", "start": 0, "length": 24, "signed": True, "scale": 0.01, "unit": "uV"}]
SIM = {"rate_hz": 100, "seed": 3, "values": {"bridge_uV": {"mean": 1000.0, "sd": 2.0}}}


def test_capture_sim(make_ctx):
    ctx = make_ctx({"duration_s": 2, "decode": DEC, "sim": SIM,
                    "propose": [{"test": "C1", "signal": "bridge_uV", "stat": "mean"},
                                {"test": "C2", "signal": "bridge_uV", "stat": "rate_hz"},
                                {"test": "C3", "signal": "bridge_uV", "stat": "n"}]})
    PcanCapture().run(ctx)
    got = {p["test_id"]: p for p in ctx.proposals}
    assert float(got["C1"]["value"]) == pytest.approx(1000.0, abs=1.0) and got["C1"]["unit"] == "uV"
    assert float(got["C2"]["value"]) == pytest.approx(100.0, rel=0.01) and got["C2"]["unit"] == "Hz"
    assert 190 <= int(got["C3"]["value"]) <= 210
    rows = list(csv.reader(open(got["C1"]["evidence"], encoding="utf-8")))
    assert rows[0][:5] == ["t_s", "can_id", "extended", "dlc", "data_hex"] and rows[0][5] == "bridge_uV"
    assert rows[1][1] == "0x3F0" and float(rows[-1][5]) > 900


def test_capture_samples_stop(make_ctx):
    ctx = make_ctx({"samples": 50, "decode": DEC, "sim": SIM, "propose": [{"test": "C3", "signal": "bridge_uV", "stat": "n"}]})
    PcanCapture().run(ctx)
    assert ctx.proposals[0]["value"] == "50"


def test_capture_preamble_send_recorded_in_sim(make_ctx):
    ctx = make_ctx({"duration_s": 0.2, "decode": DEC, "sim": SIM, "send": [{"id": "0x600", "data": "01 02"}]})
    PcanCapture().run(ctx)
    assert any("send 0x600" in t for _, t in ctx.logs)


def test_capture_bad_params(make_ctx):
    with pytest.raises(ParamError):
        PcanCapture().run(make_ctx({"duration_s": 5, "decode": DEC, "propose": [{"test": "X", "signal": "nope"}]}))
    with pytest.raises(ParamError):
        PcanCapture().run(make_ctx({"duration_s": 99999, "decode": DEC}))
    with pytest.raises(ParamError):
        PcanCapture().run(make_ctx({"duration_s": 1, "decode": DEC, "propose": [{"test": "X", "signal": "bridge_uV", "stat": "bogus"}]}))


def test_capture_no_frames_is_a_clear_error(make_ctx):
    with pytest.raises(PluginError, match="No CAN frames received"):
        PcanCapture().run(make_ctx({"duration_s": 0.5, "decode": DEC, "sim": {"silent": True}}))


def test_capture_cancel(make_ctx):
    ctx = make_ctx({"duration_s": 5, "decode": DEC, "sim": SIM})
    ctx._cancel = True
    with pytest.raises(PluginError, match="cancelled"):
        PcanCapture().run(ctx)


def test_stats_sim(make_ctx):
    ctx = make_ctx({"duration_s": 10, "signal": "bridge_uV", "decode": DEC, "full_scale": 4e6, "sim": SIM,
                    "map": {"mean": "N1", "std": "N2", "pp": "N3", "noise_free_bits": "N4", "effective_bits": "N5",
                            "n": "N6", "rate_hz": "N7"}})
    PcanStats().run(ctx)
    got = {p["test_id"]: p for p in ctx.proposals}
    assert set(got) == {"N1", "N2", "N3", "N4", "N5", "N6", "N7"}
    # the sim noise is sd 2 uV, quantised to 0.01 uV
    assert float(got["N2"]["value"]) == pytest.approx(2.0, rel=0.1)
    assert float(got["N4"]["value"]) < float(got["N5"]["value"])
    assert got["N4"]["unit"] == "bit" and got["N7"]["unit"] == "Hz" and got["N6"]["unit"] is None
    js = json.load(open(got["N1"]["evidence"], encoding="utf-8"))
    assert js["n"] == int(got["N6"]["value"]) and js["signal"] == "bridge_uV"


def test_stats_flat_signal_warns_and_skips_bits(make_ctx):
    ctx = make_ctx({"duration_s": 1, "signal": "bridge_uV", "decode": DEC, "full_scale": 4e6,
                    "sim": {"rate_hz": 100, "values": {"bridge_uV": {"mean": 5.0, "sd": 0.0}}},
                    "map": {"noise_free_bits": "N4", "std": "N2"}})
    PcanStats().run(ctx)
    assert [p["test_id"] for p in ctx.proposals] == ["N2"]
    assert any(lv == "warn" for lv, _ in ctx.logs)


def test_stats_bits_need_full_scale(make_ctx):
    ctx = make_ctx({"duration_s": 1, "signal": "bridge_uV", "decode": DEC, "sim": SIM, "map": {"noise_free_bits": "N4"}})
    PcanStats().run(ctx)
    assert ctx.proposals == [] and any("full_scale" in t for _, t in ctx.logs)


def test_stats_display_scale(make_ctx):
    ctx = make_ctx({"duration_s": 1, "signal": "bridge_uV", "decode": DEC, "sim": SIM, "display_scale": 1e-3,
                    "display_unit": "mV", "map": {"mean": "N1"}})
    PcanStats().run(ctx)
    assert float(ctx.proposals[0]["value"]) == pytest.approx(1.0, abs=0.01) and ctx.proposals[0]["unit"] == "mV"


QDEC = [{"name": "st", "id": "0x7E8", "start": 0, "length": 8}, {"name": "ok", "id": "0x7E8", "start": 8, "length": 1}]


def test_query_sim_reply(make_ctx):
    ctx = make_ctx({"request": {"id": "0x7E0", "data": "01"}, "response": {"id": "0x7E8"}, "decode": QDEC,
                    "sim": {"reply": {"request_id": "0x7E0", "id": "0x7E8", "data": "A5 01"}},
                    "propose": [{"test": "Q1", "signal": "st", "as": "hex"}, {"test": "Q2", "signal": "ok", "as": "yn"},
                                {"test": "Q3", "signal": "st"}]})
    PcanQuery().run(ctx)
    got = {p["test_id"]: p["value"] for p in ctx.proposals}
    assert got == {"Q1": "0xA5", "Q2": "Y", "Q3": "165"}


def test_query_timeout(make_ctx):
    ctx = make_ctx({"request": {"id": "0x7E0", "data": "01"}, "response": {"id": "0x7E8", "prefix": "FF"}, "decode": QDEC,
                    "timeout_s": 0.2, "retries": 1,
                    "sim": {"reply": {"request_id": "0x7E0", "id": "0x7E8", "data": "A5"}}})
    with pytest.raises(PluginError, match="No reply with ID 0x7E8"):
        PcanQuery().run(ctx)


def test_query_requires_response(make_ctx):
    with pytest.raises(ParamError):
        PcanQuery().run(make_ctx({"request": {"id": 1}}))


def test_selftests():
    for cls in (PcanCapture, PcanStats, PcanQuery):
        assert cls().selftest() is True
        assert "released" in cls().safe_state()


def test_dbc_capture(make_ctx, dbc):
    d = dbc
    ctx = make_ctx({"duration_s": 1, "dbc": d, "signals": ["Bridge", "Temp"], "sim": {"rate_hz": 50, "values": {
        "Bridge": {"mean": 12.5, "sd": 0.1}, "Temp": {"mean": 23.0, "sd": 0.0}}},
        "propose": [{"test": "A", "signal": "Bridge", "stat": "mean"}, {"test": "B", "signal": "Temp", "stat": "last"}]})
    PcanCapture().run(ctx)
    got = {p["test_id"]: p for p in ctx.proposals}
    assert float(got["A"]["value"]) == pytest.approx(12.5, abs=0.1) and got["A"]["unit"] == "uV"
    assert float(got["B"]["value"]) == pytest.approx(23.0, abs=0.06) and got["B"]["unit"] == "degC"


# ---- 15 Oct: simulated multi-query, windowed pp, sequence gaps, error frames

REGS = [{"id": "0x7F0", "data": "03 00 00 00 00 00 00 00"}, {"id": "0x7F0", "data": "03 00 01 00 00 00 00 00"}]


def _regq(**kw):
    return {"bitrate": 1000000, "timeout_s": 0.5,
            "queries": [
                {"title": "reg0", "request": REGS[0], "response": {"id": "0x7E0", "prefix": "04 00 00"},
                 "decode": [{"name": "node_id", "id": "0x7E0", "start": 32, "length": 8}],
                 "propose": [{"test": "R0", "signal": "node_id"}]},
                {"title": "reg1", "request": REGS[1], "response": {"id": "0x7E0", "prefix": "04 00 01"},
                 "decode": [{"name": "baud", "id": "0x7E0", "start": 32, "length": 8}],
                 "propose": [{"test": "R1", "signal": "baud"}]},
                {"title": "reg32", "request": {"id": "0x7F0", "data": "03 00 20"}, "response": {"id": "0x7E0", "prefix": "04 00 20 80"},
                 "decode": [{"name": "ser", "id": "0x7E0", "start": 32, "length": 32}],
                 "propose": [{"test": "R32", "signal": "ser", "as": "hex"}]}],
            **kw}


def test_query_sim_decodes_through_decode_map_without_explicit_reply(make_ctx):
    """The 15 Oct bug: no sim.reply meant an all-zero reply that never matched the prefix, so every query timed out."""
    ctx = make_ctx(_regq(sim={"values": {"node_id": 0, "baud": 3, "ser": 0x4D434D31}}))
    PcanQuery().run(ctx)
    assert {p["test_id"]: p["value"] for p in ctx.proposals} == {"R0": "0", "R1": "3", "R32": "0x4D434D31"}


def test_query_single_sim_default_values(make_ctx):
    q = _regq()["queries"][1]
    ctx = make_ctx({"request": q["request"], "response": q["response"], "decode": q["decode"], "propose": q["propose"],
                    "sim": {"values": {"baud": 3}}})
    PcanQuery().run(ctx)
    assert [(p["test_id"], p["value"]) for p in ctx.proposals] == [("R1", "3")]


def test_query_multi_partial_failure_keeps_proposals(make_ctx):
    cfg = _regq(sim={"values": {"node_id": 7, "baud": 3, "ser": 1},
                     "replies": []})
    cfg["queries"][1]["response"] = {"id": "0x7E1", "prefix": "04"}      # nobody answers on 0x7E1... except the sim, which does
    cfg["queries"][1]["decode"][0]["id"] = "0x7E1"
    ctx = make_ctx(cfg)
    PcanQuery().run(ctx)         # the simulator answers every request; all three arrive
    assert len(ctx.proposals) == 3


def test_query_multi_reports_missing_reply(make_ctx):
    cfg = _regq(sim={"values": {"node_id": 5}})
    cfg["queries"][1]["sim"] = cfg["queries"][2]["sim"] = {"no_reply": True}
    ctx = make_ctx(cfg)
    with pytest.raises(PluginError, match="No reply from 2 of 3 queries: reg1, reg32"):
        PcanQuery().run(ctx)
    assert [(p["test_id"], p["value"]) for p in ctx.proposals] == [("R0", "5")]


def test_stats_window_pp_and_bits(make_ctx):
    sim = {"rate_hz": 600, "seed": 4, "values": {"bridge_uV": {"mean": 0.0, "sd": 10.0}}}
    ctx = make_ctx({"samples": 1800, "signal": "bridge_uV", "decode": DEC, "sim": sim, "full_scale": 1e6, "window": 450,
                    "map": {"pp": "P", "pp_window": "PW", "pp_window_max": "PX", "noise_free_bits_window": "BW"}})
    PcanStats().run(ctx)
    got = {p["test_id"]: float(p["value"]) for p in ctx.proposals}
    assert got["PW"] < got["P"] and got["PW"] <= got["PX"] <= got["P"] + 1e-9
    assert 5 * 10 < got["PW"] < 8 * 10                # ~6.1 sigma for 450 gaussian samples
    import math
    assert got["BW"] == pytest.approx(math.log2(1e6 / got["PW"]), abs=0.01)


def test_stats_window_needs_param_and_samples(make_ctx):
    ctx = make_ctx({"samples": 100, "signal": "bridge_uV", "decode": DEC, "sim": SIM, "window": 450, "map": {"pp_window": "PW", "mean": "M"}})
    PcanStats().run(ctx)
    assert [p["test_id"] for p in ctx.proposals] == ["M"] and any("pp_window not available" in t for _, t in ctx.logs)


def test_stats_sequence_gaps_from_counter_signal(make_ctx):
    dec = [{"name": "x", "id": 0x100, "start": 0, "length": 16, "signed": True},
           {"name": "seq", "id": 0x100, "start": 16, "length": 8}]
    sim = {"rate_hz": 100, "values": {"x": {"mean": 5, "sd": 1}, "seq": {"counter": 256, "start": 250}}, "drop": [4, 5, 20]}
    ctx = make_ctx({"samples": 50, "signal": "x", "decode": dec, "sim": sim, "counter": {"signal": "seq", "modulus": 256},
                    "map": {"seq_gaps": "G", "seq_repeats": "D"}})
    PcanStats().run(ctx)
    got = {p["test_id"]: p["value"] for p in ctx.proposals}
    assert got == {"G": "3", "D": "0"}                 # frames 4, 5 and 20 lost, across the counter wrap at 256


def test_capture_seq_gaps_stat_needs_modulus(make_ctx):
    dec = [{"name": "cnt", "id": 0x7D1, "start": 0, "length": 32}]
    base = {"duration_s": 5, "decode": dec, "sim": {"rate_hz": 1, "values": {"cnt": {"counter": 1 << 32}}}}
    ctx = make_ctx({**base, "propose": [{"test": "G", "signal": "cnt", "stat": "seq_gaps", "modulus": 1 << 32},
                                       {"test": "N", "signal": "cnt", "stat": "n"}]})
    PcanCapture().run(ctx)
    assert {p["test_id"]: p["value"] for p in ctx.proposals} == {"G": "0", "N": "6"}     # t = 0 .. 5 s inclusive
    with pytest.raises(ParamError, match="modulus"):
        PcanCapture().run(make_ctx({**base, "propose": [{"test": "G", "signal": "cnt", "stat": "seq_gaps"}]}))


def test_error_frames_stat(make_ctx):
    ctx = make_ctx({"samples": 20, "signal": "bridge_uV", "decode": DEC,
                    "sim": {**SIM, "error_frames": 2}, "map": {"error_frames": "E"}})
    PcanStats().run(ctx)
    assert [(p["test_id"], p["value"]) for p in ctx.proposals] == [("E", "2")]


def test_sim_message_rates_are_per_message(make_ctx):
    dec = [{"name": "a", "id": 0x10, "start": 0, "length": 8}, {"name": "b", "id": 0x20, "start": 0, "length": 8}]
    ctx = make_ctx({"duration_s": 10, "decode": dec, "sim": {"rate_hz": 2, "rates": {"b": 1}},
                    "propose": [{"test": "RA", "signal": "a", "stat": "rate_hz"}, {"test": "RB", "signal": "b", "stat": "rate_hz"}]})
    PcanCapture().run(ctx)
    got = {p["test_id"]: float(p["value"]) for p in ctx.proposals}
    assert got["RA"] == pytest.approx(2.0, abs=0.01) and got["RB"] == pytest.approx(1.0, abs=0.01)
