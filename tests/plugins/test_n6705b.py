from pathlib import Path

import pytest

import polybench.plugins.n6705b as n67
from polybench.plugins._util import ParamError, PluginError
from polybench.plugins.n6705b import N6705bFirstPower, N6705bIdentify, N6705bMeasure

from pb_plugin_helpers import EXAMPLE_PROFILE as PROFILE   # 5.25 V, 0.6 A, OVP 5.75 V
pytest.importorskip("polystage_bench", reason="the separate polystage-bench library is not installed")
pytestmark = pytest.mark.skipif(not PROFILE.exists(), reason="polystage-bench example profiles not on this machine")

MAP = {"voltage": "P1", "current": "P2", "power": "P3"}


@pytest.fixture
def frames(monkeypatch):
    """Record every simulated frame the action opens."""
    made = []
    real = n67.open_frame

    def spy(p, simulate, profile):
        fr = real(p, simulate, profile)
        made.append(fr)
        return fr
    monkeypatch.setattr(n67, "open_frame", spy)
    return made


def outputs(fr):
    return [fr.st[c]["out"] for c in fr.MODELS]


def test_first_power_sim(make_ctx, frames):
    ctx = make_ctx({"profile": str(PROFILE), "volts": 5.0, "sim_current_ma": 25, "map": MAP})
    N6705bFirstPower().run(ctx)
    got = {p["test_id"]: p for p in ctx.proposals}
    assert float(got["P1"]["value"]) == pytest.approx(5.0, abs=0.05) and got["P1"]["unit"] == "V"
    assert float(got["P2"]["value"]) == pytest.approx(25.0, abs=0.5) and got["P2"]["unit"] == "mA"
    assert float(got["P3"]["value"]) == pytest.approx(125.0, abs=3)
    assert outputs(frames[0]) == [0, 0, 0]                   # default leave=off
    assert any("outputs off" in t for _, t in ctx.logs)


def test_first_power_current_unit(make_ctx):
    ctx = make_ctx({"profile": str(PROFILE), "volts": 3.3, "sim_current_ma": 10, "current_unit": "uA", "map": {"current": "P2"}})
    N6705bFirstPower().run(ctx)
    assert float(ctx.proposals[0]["value"]) == pytest.approx(10000, rel=0.02) and ctx.proposals[0]["unit"] == "uA"


def test_first_power_leave_on(make_ctx, frames):
    N6705bFirstPower().run(make_ctx({"profile": str(PROFILE), "volts": 3.3, "leave": "on", "map": MAP}))
    assert outputs(frames[0])[0] == 1


def test_volts_above_profile_refused_nothing_energised(make_ctx, frames):
    ctx = make_ctx({"profile": str(PROFILE), "volts": 12.0})
    with pytest.raises(PluginError, match="above the profile limit"):
        N6705bFirstPower().run(ctx)
    assert outputs(frames[0]) == [0, 0, 0] and not any(fr.energised_ever for fr in frames)


def test_current_limit_may_only_be_lowered(make_ctx, frames):
    with pytest.raises(PluginError, match="above the profile limit"):
        N6705bFirstPower().run(make_ctx({"profile": str(PROFILE), "volts": 5.0, "ilim_a": 2.0}))
    assert not frames[0].energised_ever
    N6705bFirstPower().run(make_ctx({"profile": str(PROFILE), "volts": 5.0, "ilim_a": 0.1, "map": MAP}))
    assert outputs(frames[1]) == [0, 0, 0]


def test_profile_required_on_real_runs(make_ctx):
    with pytest.raises(ParamError, match="profile"):
        N6705bFirstPower().run(make_ctx({"volts": 3.3}, simulate=False))


def test_missing_profile_file_real_is_an_error_sim_falls_back(make_ctx):
    with pytest.raises(PluginError, match="Install the profile"):
        n67.load_profile({"profile": "no_such_profile_xyz"}, False, True)
    assert n67.load_profile({"profile": "no_such_profile_xyz"}, True, True).name.startswith("default")


def test_outputs_off_when_a_step_fails(make_ctx, frames, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("link glitch")
    monkeypatch.setattr(n67, "_read_samples", boom)
    with pytest.raises(RuntimeError, match="link glitch"):
        N6705bFirstPower().run(make_ctx({"profile": str(PROFILE), "volts": 5.0}))
    assert frames[0].energised_ever and outputs(frames[0]) == [0, 0, 0]


def test_outputs_off_on_cancel(make_ctx, frames):
    ctx = make_ctx({"profile": str(PROFILE), "volts": 5.0})
    orig = ctx.cancelled
    calls = {"n": 0}

    def cancelled():                      # cancel after the output is on, at the settle check
        calls["n"] += 1
        return calls["n"] >= 2
    ctx.cancelled = cancelled
    with pytest.raises(PluginError, match="cancelled"):
        N6705bFirstPower().run(ctx)
    assert frames[0].energised_ever and outputs(frames[0]) == [0, 0, 0]


def test_protection_trip_fails_and_switches_off(make_ctx, frames, monkeypatch):
    orig = n67.open_frame

    def tripping(p, sim, prof):
        fr = orig(p, sim, prof)
        frames.append(fr)
        fr.check_trips = lambda report=True: ["ch1 OVP"]
        return fr
    monkeypatch.setattr(n67, "open_frame", tripping)
    with pytest.raises(Exception, match="protection tripped"):
        N6705bFirstPower().run(make_ctx({"profile": str(PROFILE), "volts": 5.0, "map": MAP}))
    assert outputs(frames[-1]) == [0, 0, 0]


def test_measure_sim(make_ctx):
    ctx = make_ctx({"samples": 3, "map": {"voltage": "P1", "current": "P2"}, "sim_current_ma": 12})
    N6705bMeasure().run(ctx)
    got = {p["test_id"]: float(p["value"]) for p in ctx.proposals}
    assert got["P1"] == pytest.approx(3.3, abs=0.05) and got["P2"] == pytest.approx(12.0, abs=0.5)


def test_identify_sim(make_ctx):
    ctx = make_ctx({"map": {"idn": "X1"}})
    N6705bIdentify().run(ctx)
    assert "N6705B" in ctx.proposals[0]["value"]


def test_unreachable_frame_message(make_ctx):
    ctx = make_ctx({"host": "127.0.0.1", "port": 9, "serial": "MY00000000", "timeout_s": 0.5, "patience_s": 1,
                    "subnet": "127.0.0.0/30"}, simulate=False)
    with pytest.raises(PluginError, match="not reachable"):
        N6705bIdentify().run(ctx)


def test_safe_state_sim_and_selftest(make_ctx, tmp_path):
    N6705bIdentify().run(make_ctx({}))          # writes the 'last run was simulated' note
    a = N6705bFirstPower()
    assert "outputs off" in a.safe_state()
    assert a.selftest() is True


def _range_profile(tmp_path, max_volts=12.6, range_v=20):
    f = tmp_path / "range20.toml"
    f.write_text(f'name = "range20"\n[channel.1]\nmax_volts = {max_volts}\nmax_current_a = 0.10\novp_max_v = 13.5\n'
                 f'output_range_v = {range_v}\nallow_sink = false\nmode = "PS1Q"\nwatchdog_s = 60\n')
    return f


def _force_6v_range(monkeypatch):
    """Make the opened frame report the 6 V range (VOLT? MAX 6.12 V), as the real frame did on 19 Oct."""
    real = n67.open_frame

    def opened(p, simulate, profile):
        fr = real(p, simulate, profile)
        for c in fr.channels.values():
            c["vmax"], c["imax"] = 6.12, 3.06
        return fr
    monkeypatch.setattr(n67, "open_frame", opened)


def test_profile_output_range_used_for_the_precheck(make_ctx, tmp_path, monkeypatch):
    _force_6v_range(monkeypatch)
    fr0 = n67.open_frame({}, True, n67.load_profile({"profile": str(_range_profile(tmp_path))}, True, True))
    if fr0.channels[1].get("model") != "N6781A":
        pytest.skip("simulated ch1 is not an N6781A")
    ctx = make_ctx({"profile": str(_range_profile(tmp_path)), "volts": 12.0, "ilim_a": 0.1, "map": {"voltage": "P1"}})
    try:
        N6705bFirstPower().run(ctx)
    except PluginError as e:
        assert "above the profile limit" not in str(e), e


def test_precheck_still_refuses_above_profile_with_range(make_ctx, tmp_path, monkeypatch):
    _force_6v_range(monkeypatch)
    with pytest.raises(PluginError, match="above the profile limit 12.6"):
        N6705bFirstPower().run(make_ctx({"profile": str(_range_profile(tmp_path)), "volts": 13.0}))


def test_watchdog_disarmed_on_open_and_close_when_nothing_is_energised(make_ctx):
    prof = n67.load_profile({"profile": str(PROFILE)}, True, True)
    fr = n67.open_frame({}, True, prof)
    fr.write("OUTP:PROT:WDOG ON")
    assert int(float(fr._try("OUTP:PROT:WDOG?"))) == 1
    n67._watchdog_hygiene(fr)
    assert int(float(fr._try("OUTP:PROT:WDOG?"))) == 0
    fr.close()


def test_watchdog_kept_while_an_output_is_on(make_ctx):
    N6705bFirstPower().run(make_ctx({"profile": str(PROFILE), "volts": 3.3, "leave": "on"}))
    prof = n67.load_profile({"profile": str(PROFILE)}, True, True)
    fr = n67.open_frame({}, True, prof)
    fr.write("OUTP:PROT:WDOG ON")
    fr.set_output(1, 3.3, 0.1, 4.0)
    n67._watchdog_hygiene(fr)
    assert int(float(fr._try("OUTP:PROT:WDOG?"))) == 1
    fr.output_off()
