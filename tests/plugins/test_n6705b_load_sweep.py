import csv
from pathlib import Path

import pytest

import polybench.plugins.n6705b as n67
from polybench.plugins._util import ParamError, PluginError
from polybench.plugins.n6705b import N6705bFirstPower, N6705bLoadSweep, N6705bMeasure

pytest.importorskip("polystage_bench", reason="the separate polystage-bench library is not installed")

PROFILE = """name = "rail_sweep"
[channel.1]
max_volts = 14.0
max_current_a = 1.0
ovp_max_v = 15.0
allow_sink = false
mode = "PS1Q"
ocp_delay_s = 0.02
watchdog_s = 60
{src_extra}
[channel.2]
max_volts = 5.5
max_current_a = 1.0
ovp_max_v = 6.0
allow_sink = true
max_sink_a = 0.5
mode = "CCL"
watchdog_s = 60
{load_extra}
"""
MAP = {"rail_v_noload": "R0", "rail_v_fullload": "R1", "rail_v_min": "R2", "rail_v_max": "R3", "regulation_mv": "R4",
       "input_ma_fullload": "R5", "efficiency_pct_fullload": "R6", "points_table": "T1"}


@pytest.fixture
def profile(tmp_path):
    f = tmp_path / "rail_sweep.toml"
    f.write_text(PROFILE.format(src_extra="", load_extra="voltmeter = true"))
    return f


@pytest.fixture
def frames(monkeypatch):
    made = []
    real = n67.open_frame

    def spy(p, simulate, profile):
        fr = real(p, simulate, profile)
        made.append(fr)
        return fr
    monkeypatch.setattr(n67, "open_frame", spy)
    return made


def outs(fr):
    return [fr.st[c]["out"] for c in fr.MODELS]


def base(profile, **kw):
    p = {"profile": str(profile), "volts": 12.0, "points_ma": [0, 50, 100, 200], "dwell_s": 0.2, "samples": 3,
         "settle_s": 0.5, "guard_v": 3.6, "map": MAP}
    p.update(kw)
    return p


def load_on_commands(fr):
    """OUTP ON of the load channel after it was switched to CCL (the voltmeter-mode ON before that is not a load)."""
    i = next((k for k, c in enumerate(fr.log) if c.startswith("EMUL CCL") and "(@2)" in c), None)
    return [] if i is None else [c for c in fr.log[i:] if c.startswith("OUTP ON,(@2)")]


def test_happy_path(make_ctx, frames, profile):
    ctx = make_ctx(base(profile, sim_rail_v=3.3, sim_rail_ohm=0.1, sim_eta=0.9, sim_quiescent_ma=2))
    N6705bLoadSweep().run(ctx)
    got = {p["test_id"]: p for p in ctx.proposals}
    assert float(got["R0"]["value"]) == pytest.approx(3.3, abs=0.01)
    assert float(got["R1"]["value"]) == pytest.approx(3.3 - 0.1 * 0.2, abs=0.01)
    assert float(got["R2"]["value"]) == pytest.approx(float(got["R1"]["value"]), abs=1e-6)
    assert float(got["R3"]["value"]) == pytest.approx(3.3, abs=0.01)
    assert float(got["R4"]["value"]) == pytest.approx(20.0, abs=1.0) and got["R4"]["unit"] == "mV"
    assert got["R6"]["unit"] == "%" and 80 < float(got["R6"]["value"]) < 90        # 90 % converter less quiescent loss
    assert float(got["R5"]["value"]) > 0 and got["R5"]["unit"] == "mA"
    tbl = got["T1"]["value"]
    assert tbl["columns"] == ["load_ma", "rail_v", "input_v", "input_ma", "efficiency_pct"]
    assert len(tbl["rows"]) == 4 and tbl["rows"][0][4] is None
    assert outs(frames[0]) == [0, 0, 0]
    assert any("outputs off" in t for _, t in ctx.logs)
    csv_path = Path(got["R0"]["evidence"])
    rows = list(csv.reader(csv_path.open()))
    assert len(rows) == 5 and rows[1][-1] == "" and rows[2][-1] != "" and csv_path.with_suffix(".json").exists()


def test_refuse_over_profile_sink(make_ctx, frames, profile):
    with pytest.raises(PluginError, match="above the profile load limit"):
        N6705bLoadSweep().run(make_ctx(base(profile, points_ma=[0, 100, 600])))
    assert not frames[0].energised_ever and outs(frames[0]) == [0, 0, 0]


def test_refuse_over_profile_volts(make_ctx, frames, profile):
    with pytest.raises(PluginError, match="above the profile limit"):
        N6705bLoadSweep().run(make_ctx(base(profile, volts=15.0)))
    assert not frames[0].energised_ever


def test_refuse_guard_above_load_voltage_limit(make_ctx, frames, profile):
    with pytest.raises(PluginError, match="guard_v"):
        N6705bLoadSweep().run(make_ctx(base(profile, guard_v=9.0)))
    assert not frames[0].energised_ever


def test_refuse_plan_over_60s(make_ctx, frames, profile):
    with pytest.raises(PluginError, match="max_total_s"):
        N6705bLoadSweep().run(make_ctx(base(profile, dwell_s=5.0, points_ma=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])))
    assert not frames[0].energised_ever
    with pytest.raises(ParamError, match="max_total_s"):
        N6705bLoadSweep().run(make_ctx(base(profile, max_total_s=120)))


def test_bad_points(make_ctx, profile):
    for pts in ([10, 20], [0, 20, 10], [0], "x"):
        with pytest.raises(ParamError):
            N6705bLoadSweep().run(make_ctx(base(profile, points_ma=pts)))


def test_guard_exceeded_load_never_energised(make_ctx, frames, profile):
    with pytest.raises(PluginError, match="above guard_v"):
        N6705bLoadSweep().run(make_ctx(base(profile, sim_rail_v=4.4)))
    fr = frames[0]
    assert fr.energised_ever and outs(fr) == [0, 0, 0]
    assert not load_on_commands(fr) and fr.st[2]["out"] == 0


def test_abort_on_stop_below_v(make_ctx, frames, profile):
    ctx = make_ctx(base(profile, sim_collapse_ma=100, stop_below_v=1.0))
    with pytest.raises(PluginError, match="below stop_below_v"):
        N6705bLoadSweep().run(ctx)
    assert outs(frames[0]) == [0, 0, 0] and not ctx.proposals
    assert any("outputs off" in t for _, t in ctx.logs) and any("incomplete" in t for _, t in ctx.logs)


def test_cancel_mid_sweep(make_ctx, frames, profile):
    ctx = make_ctx(base(profile))
    def cancelled():                 # cancel once the load is on (independent of how often the action polls)
        return bool(frames) and bool(load_on_commands(frames[0]))
    ctx.cancelled = cancelled
    with pytest.raises(PluginError, match="cancelled"):
        N6705bLoadSweep().run(ctx)
    fr = frames[0]
    assert load_on_commands(fr) and outs(fr) == [0, 0, 0]


def test_protection_trip_all_off(make_ctx, frames, profile, monkeypatch):
    real = n67.open_frame

    def tripping(p, sim, prof):
        fr = real(p, sim, prof)
        frames.append(fr)
        orig = fr.check_trips

        def trips(report=True):
            return ["ch2 OVP"] if fr.st[2]["out"] else orig(report)
        fr.check_trips = trips
        return fr
    monkeypatch.setattr(n67, "open_frame", tripping)
    with pytest.raises(Exception, match="protection tripped"):      # the library's ProtectionTrip, outputs already off
        N6705bLoadSweep().run(make_ctx(base(profile)))
    assert outs(frames[-1]) == [0, 0, 0]


def test_link_failure_all_off(make_ctx, frames, profile, monkeypatch):
    real = n67.open_frame

    def flaky(p, sim, prof):
        fr = real(p, sim, prof)
        frames.append(fr)
        orig = fr.adjust_output
        state = {"n": 0}

        def adj(*a, **k):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("link glitch")
            return orig(*a, **k)
        fr.adjust_output = adj
        return fr
    monkeypatch.setattr(n67, "open_frame", flaky)
    with pytest.raises(RuntimeError, match="link glitch"):
        N6705bLoadSweep().run(make_ctx(base(profile, points_ma=[0, 50, 100])))
    assert outs(frames[-1]) == [0, 0, 0]


def test_profile_required_on_real_runs(make_ctx):
    with pytest.raises(ParamError, match="profile"):
        N6705bLoadSweep().run(make_ctx({"volts": 5, "points_ma": [0, 10]}, simulate=False))


def _force_6v(monkeypatch):
    real = n67.open_frame

    def opened(p, simulate, profile):
        fr = real(p, simulate, profile)
        for c in fr.channels.values():
            c["vmax"], c["imax"] = 6.12, 3.06
        return fr
    monkeypatch.setattr(n67, "open_frame", opened)


def test_output_range_respected_with_frame_on_6v(make_ctx, tmp_path, monkeypatch):
    _force_6v(monkeypatch)
    f = tmp_path / "r20.toml"
    f.write_text(PROFILE.format(src_extra="output_range_v = 20", load_extra="voltmeter = true"))
    fr0 = n67.open_frame({}, True, n67.load_profile({"profile": str(f)}, True, True))
    if fr0.channels[1].get("model") != "N6781A":
        pytest.skip("simulated ch1 is not an N6781A")
    ctx = make_ctx(base(f, volts=12.0, ilim_a=0.1))
    try:
        N6705bLoadSweep().run(ctx)
    except PluginError as e:
        assert "above the profile limit" not in str(e), e
    with pytest.raises(PluginError, match="above the profile limit 14"):
        N6705bLoadSweep().run(make_ctx(base(f, volts=15.0)))


# ---- n6705b.measure output=off -----------------------------------------------------------------------------------

def _powered(frames, monkeypatch):
    real = n67.open_frame

    def powered(p, sim, prof):
        fr = real(p, sim, prof)
        fr.set_output(1, 3.3, 0.1, 4.0)
        frames.append(fr)
        return fr
    monkeypatch.setattr(n67, "open_frame", powered)


def test_measure_off_zero_reading_is_refused_as_invalid(make_ctx, frames):
    ctx = make_ctx({"channel": 2, "output": "off", "samples": 2, "map": {"voltage": "P1"}})
    with pytest.raises(PluginError, match="OFF channels read 0 V on this frame: use output: voltmeter"):
        N6705bMeasure().run(ctx)
    assert not frames[0].energised_ever and outs(frames[0]) == [0, 0, 0] and not ctx.proposals


def test_measure_off_nonzero_reading_is_proposed_with_a_warning(make_ctx, frames, monkeypatch):
    _powered(frames, monkeypatch)
    frames_off = frames
    real = n67._read_samples
    monkeypatch.setattr(n67, "_read_samples", lambda fr, ch, n, ctx: [{"v": 1.8, "i": 0.0, "output": 0, "regulation": "OFF", "range": {}}] * n)
    ctx = make_ctx({"channel": 2, "output": "off", "samples": 2, "map": {"voltage": "P1"}})
    N6705bMeasure().run(ctx)
    assert float(ctx.proposals[0]["value"]) == pytest.approx(1.8) and any(lv == "warn" for lv, _ in ctx.logs)
    assert frames_off[0].st[2]["out"] == 0


def test_measure_off_refuses_a_channel_that_is_on(make_ctx, frames, monkeypatch):
    _powered(frames, monkeypatch)
    with pytest.raises(PluginError, match="is ON"):
        N6705bMeasure().run(make_ctx({"channel": 1, "output": "off"}))
    assert frames[0].st[1]["out"] == 1


def test_measure_voltmeter_reads_the_rail(make_ctx, frames, profile, monkeypatch):
    _powered(frames, monkeypatch)
    ctx = make_ctx({"profile": str(profile), "channel": 2, "output": "voltmeter", "samples": 3,
                    "map": {"voltage": "P1", "current": "P2"}})
    N6705bMeasure().run(ctx)
    fr = frames[0]
    assert float(ctx.proposals[0]["value"]) == pytest.approx(3.3, abs=0.01) and len(ctx.proposals) == 1
    assert any("EMUL VMET" in c for c in fr.log) and fr.st[2]["out"] == 0 and fr.st[1]["out"] == 1
    assert any("no current reading" in t for _, t in ctx.logs)


def test_measure_voltmeter_simulated_click_through(make_ctx, frames, profile):
    ctx = make_ctx({"profile": str(profile), "channel": 2, "output": "voltmeter", "map": {"voltage": "P1"}, "sim_rail_v": 5.0})
    N6705bMeasure().run(ctx)
    assert float(ctx.proposals[0]["value"]) == pytest.approx(5.0, abs=0.01)


def test_measure_voltmeter_needs_the_profile_permission(make_ctx, frames, tmp_path):
    f = tmp_path / "nov.toml"
    f.write_text(PROFILE.format(src_extra="", load_extra=""))
    with pytest.raises(Exception, match="does not allow voltmeter mode"):
        N6705bMeasure().run(make_ctx({"profile": str(f), "channel": 2, "output": "voltmeter"}))
    assert not any("EMUL VMET" in c for c in frames[0].log)


def test_measure_voltmeter_profile_required_on_real_runs(make_ctx):
    with pytest.raises(ParamError, match="profile"):
        N6705bMeasure().run(make_ctx({"channel": 2, "output": "voltmeter"}, simulate=False))


# ---- voltmeter mode in load_sweep ----------------------------------------------------------------------------------

def test_sweep_zero_point_and_regulation_are_real(make_ctx, frames, profile):
    ctx = make_ctx(base(profile, sim_rail_v=3.3, sim_rail_ohm=0.1))
    N6705bLoadSweep().run(ctx)
    fr = frames[0]
    got = {p["test_id"]: p for p in ctx.proposals}
    assert float(got["R0"]["value"]) == pytest.approx(3.3, abs=0.01) and float(got["R4"]["value"]) == pytest.approx(20.0, abs=1.0)
    emul = [c for c in fr.log if c.startswith("EMUL ") and "(@2)" in c]
    assert emul[0].startswith("EMUL VMET") and emul[1].startswith("EMUL CCL")        # voltmeter first, then the load
    on2 = [i for i, c in enumerate(fr.log) if c.startswith("OUTP ON,(@2)")]
    assert on2[0] < fr.log.index(emul[1]) and on2[-1] > fr.log.index(emul[1])        # the voltmeter ON, then the CCL ON


def test_sweep_guard_is_effective_with_the_off_channel_reading_zero(make_ctx, frames, profile):
    assert N6705bLoadSweep  # an OFF load channel reads 0 V on this frame; the guard must still see 4.4 V
    with pytest.raises(PluginError, match="above guard_v"):
        N6705bLoadSweep().run(make_ctx(base(profile, sim_rail_v=4.4)))
    assert not [c for c in frames[0].log if c.startswith("EMUL CCL")] and frames[0].st[2]["out"] == 0 and outs(frames[0]) == [0, 0, 0]


def test_sweep_refused_without_voltmeter_permission_nothing_energised(make_ctx, frames, tmp_path):
    f = tmp_path / "nov.toml"
    f.write_text(PROFILE.format(src_extra="", load_extra=""))
    with pytest.raises(PluginError, match="voltmeter = true"):
        N6705bLoadSweep().run(make_ctx(base(f)))
    assert not frames[0].energised_ever


# ---- latched protection (watchdog) ------------------------------------------------------------------------------

def _latch(monkeypatch, frames, trip=2048, outputs_on=False):
    real = n67.open_frame

    def opened(p, sim, prof):
        fr = real(p, sim, prof)
        frames.append(fr)
        for c in fr.MODELS:
            fr.forced_trip[c] = trip
        if outputs_on:
            fr.st[3]["out"] = 1
        return fr
    monkeypatch.setattr(n67, "open_frame", opened)


def test_first_power_clears_a_watchdog_only_latch(make_ctx, frames, profile, monkeypatch):
    _latch(monkeypatch, frames)
    ctx = make_ctx({"profile": str(profile), "volts": 5.0, "channel": 1})
    N6705bFirstPower().run(ctx)
    assert any("the previous hold window expired (watchdog); cleared" in t for _, t in ctx.logs)
    assert frames[0].forced_trip == {} and outs(frames[0]) == [0, 0, 0]


def test_first_power_refuses_any_other_latch(make_ctx, frames, profile, monkeypatch):
    _latch(monkeypatch, frames, trip=1 | 2048)
    with pytest.raises(PluginError, match=r"OV .*front panel after checking the board"):
        N6705bFirstPower().run(make_ctx({"profile": str(profile), "volts": 5.0, "channel": 1}))
    assert not frames[0].energised_ever and frames[0].forced_trip


def test_first_power_does_not_clear_while_an_output_is_on(make_ctx, frames, profile, monkeypatch):
    _latch(monkeypatch, frames, outputs_on=True)
    with pytest.raises(PluginError, match="latched"):
        N6705bFirstPower().run(make_ctx({"profile": str(profile), "volts": 5.0, "channel": 1}))


def test_load_sweep_refuses_a_latch_and_never_clears(make_ctx, frames, profile, monkeypatch):
    _latch(monkeypatch, frames)
    with pytest.raises(PluginError, match="watchdog"):
        N6705bLoadSweep().run(make_ctx(base(profile)))
    assert frames[0].forced_trip and not frames[0].energised_ever


def test_measure_warns_about_a_latch_and_still_reads(make_ctx, frames, monkeypatch):
    _latch(monkeypatch, frames)
    monkeypatch.setattr(n67, "_read_samples", lambda fr, ch, n, ctx: [{"v": 1.8, "i": 0.0, "output": 0, "regulation": "OFF", "range": {}}] * n)
    ctx = make_ctx({"channel": 2, "output": "off", "samples": 1, "map": {"voltage": "P1"}})
    N6705bMeasure().run(ctx)
    assert any(lv == "warn" and "latched" in t for lv, t in ctx.logs) and ctx.proposals


def test_leave_on_logs_the_watchdog_expiry(make_ctx, frames, profile):
    ctx = make_ctx({"profile": str(profile), "volts": 5.0, "channel": 1, "leave": "on"})
    N6705bFirstPower().run(ctx)
    msgs = [t for lv, t in ctx.logs if "switch off by themselves" in t]
    assert msgs and "60 s" in msgs[0] and "watchdog protection afterwards" in msgs[0]
    assert ctx.logs.index(next(x for x in ctx.logs if "switch off by themselves" in x[1])) < ctx.logs.index(next(x for x in ctx.logs if "ch1: V=" in x[1]))


def test_measure_bad_output_value_and_default_unchanged(make_ctx):
    with pytest.raises(ParamError):
        N6705bMeasure().run(make_ctx({"output": "on"}))
    ctx = make_ctx({"samples": 3, "map": {"voltage": "P1"}})
    N6705bMeasure().run(ctx)
    assert float(ctx.proposals[0]["value"]) == pytest.approx(3.3, abs=0.05)


# ---- registration: lint, Plugins tab, doctor ---------------------------------------------------------------------

def _entry_points_current():
    import importlib.metadata as md
    return "n6705b.load_sweep" in {e.name for e in md.entry_points(group="polybench.actions")}


needs_install = pytest.mark.skipif(not _entry_points_current(), reason="package not installed from this checkout (pip install -e .): entry point missing")


@needs_install
def test_protocol_linter_and_plugins_tab_and_doctor(tmp_path, profile):
    from polybench import doctor, plugin_status, protocol
    f = tmp_path / "ls.pcbv.yaml"
    f.write_text(f"""schema: pcbv.protocol/2
design: {{name: T, revision: "1"}}
batch: {{default_boards: 1, label_prefix: B}}
steps:
  - id: powered
    title: P
    applies_to: all
    sections:
      - id: s
        title: S
        actions:
          - {{id: sw, title: Sweep, plugin: n6705b.load_sweep, params: {{profile: "{profile}", volts: 12, points_ma: [0, 100]}}, proposes: [R0]}}
        tests:
          - {{id: R0, title: Rail, kind: number, limit: {{class: record}}}}
""")
    proto = protocol.load(f)
    assert not [i for i in proto.issues if "plugin" in str(i).lower()]
    row = next(r for r in plugin_status.discover() if r["name"] == "n6705b.load_sweep")
    assert row["status"] == "loaded"
    res = doctor.run_checks(str(f), open_pcan=False)
    assert any("n6705b" in c.name.lower() or "N6705B" in c.name for c in res)
    assert not [c for c in res if c.status == doctor.FAIL and "plug" in c.name.lower()]


def test_cancel_seen_within_a_dwell():
    import polybench.plugins.n6705b as m

    class Fr:
        t = 0.0
        def _now(self): return self.t
        def _wait(self, s): self.t += s
    class Ctx:
        n = 0
        def cancelled(self):
            self.n += 1
            return self.n > 3
    fr = Fr()
    with pytest.raises(m.PluginError, match="cancelled"):
        m._cwait(fr, Ctx(), 4.0)
    assert fr.t <= 0.61


def test_sweep_reports_an_analysis_block_for_derive(make_ctx, frames, profile):
    """PW-6: P32-style question (all points, no abort, no trip, outputs read back OFF) is a fact of the run."""
    ctx = make_ctx(base(profile, sim_rail_v=3.3, sim_rail_ohm=0.1, sim_eta=0.9, sim_quiescent_ma=2))
    N6705bLoadSweep().run(ctx)
    an = ctx.analyses[-1]["analysis"]
    assert an["sweep_ok"] is True and an["complete"] and an["points"] == 4 and an["outputs_off"] is True and an["tripped"] is False
    assert any(s["quantity"].startswith("rail V at 200 mA") for s in ctx.analyses[-1]["samples"])
    bad = make_ctx(base(profile, sim_rail_v=3.3, sim_collapse_ma=100))
    with pytest.raises(PluginError):
        N6705bLoadSweep().run(bad)
    an = bad.analyses[-1]["analysis"]
    assert an["sweep_ok"] is False and an["complete"] is False and an["rail_collapsed"] is True and an["outputs_off"] is True
