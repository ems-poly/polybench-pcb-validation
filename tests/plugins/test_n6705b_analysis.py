"""n6705b actions report an analysis block (PW-6): min/max/mean/final, CV/CC mode, protection, time at limit, settled. Simulated frame."""
import pytest

from polybench.plugins.n6705b import N6705bFirstPower
from pb_plugin_helpers import EXAMPLE_PROFILE

pytest.importorskip("polystage_bench", reason="the separate polystage-bench library is not installed")
pytestmark = pytest.mark.skipif(not EXAMPLE_PROFILE.exists(), reason="polystage-bench example profiles not on this machine")


def test_first_power_reports_analysis_and_ramp_samples(make_ctx):
    ctx = make_ctx({"profile": str(EXAMPLE_PROFILE), "volts": 5.0, "ilim_a": 0.1, "ramp": {"start_v": 1.0, "step_v": 1.0, "dwell_s": 0.0},
                    "settle_s": 0.1, "sim_current_ma": 25, "map": {"voltage": "P1", "current": "P2"}})
    N6705bFirstPower().run(ctx)
    an, rows = ctx.analyses[0]["analysis"], ctx.analyses[0]["samples"]
    assert an["tripped"] is False and an["within_limit"] is True and an["cc_seen"] is False and an["final_mode"] == "CV"
    assert an["current"]["final"] == pytest.approx(25, abs=0.5) and an["current_unit"] == "mA" and an["settled"] is True
    assert an["ramp_current"]["n"] == 4 and an["time_at_limit_s"] == 0
    assert any(r["quantity"] == "ramp current" for r in rows) and sum(1 for r in rows if r.get("map_key") == "current") == 5
    ctx2 = make_ctx({"profile": str(EXAMPLE_PROFILE), "volts": 5.0, "ilim_a": 0.1, "ramp": {"start_v": 1.0, "step_v": 1.0, "dwell_s": 0.0},
                     "settle_s": 0.1, "sim_current_ma": 150})            # the load would draw 150 mA against a 100 mA limit
    try:
        N6705bFirstPower().run(ctx2)
    except Exception:
        pass
    an2 = ctx2.analyses[-1]["analysis"] if ctx2.analyses else None
    assert an2 is not None and an2["within_limit"] is False and (an2["tripped"] or an2["cc_seen"] or an2["at_limit"])
