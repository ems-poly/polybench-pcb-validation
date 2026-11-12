"""Live 'not similar to the batch' highlight: display only. Pure function `tiers.similarity` plus the snapshot key."""
from polybench.tiers import similarity, similarity_all


def T(unit="kOhm", **kw):
    return {"id": "X1", "kind": "number", "unit": unit, **kw}


def flagged(test, **readings):
    r = similarity(test, readings)
    return None if r is None else sorted(r["flagged"])


def test_low_ohm_floor_absolute():
    # median 0.2 ohm: band = max(50 %, 0.3 ohm) = 0.3, so 0.4 is inside and 1.5 is outside
    t = T("Ohm")
    assert flagged(t, S01=0.2, S02=0.2, S03=0.2, S04=0.4) == []
    assert flagged(t, S01=0.2, S02=0.2, S03=0.2, S04=1.5) == ["S04"]


def test_low_ohm_percent_above_floor():
    t = T("Ohm")        # median 6: 50 % = 3, so 8.9 is inside and 9.2 is outside
    assert flagged(t, S01=6, S02=6, S03=6, S04=8.9) == []
    assert flagged(t, S01=6, S02=6, S03=6, S04=9.2) == ["S04"]


def test_low_band_converted_from_kohm():
    t = T("kOhm")       # 0.005 kOhm = 5 ohm is a low-ohm median; floor 0.3 ohm = 0.0003 kOhm, 50 % dominates
    assert flagged(t, S01=0.005, S02=0.005, S03=0.005, S04=0.0070) == []
    assert flagged(t, S01=0.005, S02=0.005, S03=0.005, S04=0.0080) == ["S04"]


def test_mid_range_25_percent():
    t = T("kOhm")
    assert flagged(t, S01=1.2, S02=1.2, S03=1.2, S04=1.49) == []
    r = similarity(t, dict(S01=1.2, S02=1.2, S03=1.2, S04=1.51))
    assert list(r["flagged"]) == ["S04"] and r["band"] == "±25 %" and r["median"] == 1.2
    assert flagged(t, S01=1.2, S02=1.2, S03=1.2, S04=0.89) == ["S04"]
    assert flagged(t, S01=1.2, S02=1.2, S03=1.2, S04=0.91) == []


def test_high_kohm_50_percent():
    t = T("kOhm")       # median 100 kOhm = 100000 ohm, above 10 kOhm
    assert flagged(t, S01=100, S02=100, S03=100, S04=140) == []
    assert flagged(t, S01=100, S02=100, S03=100, S04=160) == ["S04"]
    t = T("MOhm")
    assert flagged(t, S01=1, S02=1, S03=1, S04=1.4) == []


def test_other_units_25_percent():
    t = T("V")
    assert flagged(t, S01=5.0, S02=5.0, S03=5.0, S04=6.2) == []
    assert flagged(t, S01=5.0, S02=5.0, S03=5.0, S04=6.3) == ["S04"]


def test_overrange_minority_finite():
    r = similarity(T(), dict(S01="OL", S02="OL", S03=120, S04="OL"))
    assert list(r["flagged"]) == ["S03"] and r["median"] is None
    assert r["flagged"]["S03"]["text"] == "S03 reads 120 kΩ while S01 S02 S04 are OL"


def test_overrange_minority_ol():
    r = similarity(T(), dict(S01=120, S02=118, S03="OL", S04=121))
    assert list(r["flagged"]) == ["S03"]
    assert r["flagged"]["S03"]["text"] == "S03 reads OL while S01 S02 S04 read finite"
    assert r["median"] == 120          # over-range takes no part in the median


def test_overrange_number_and_tie():
    assert flagged(T(), S01=99999, S02=99999, S03=5, S04=99999) == ["S03"]
    assert flagged(T(), S01="OL", S02="OL", S03=5, S04=6) == []         # 2 against 2: no minority
    assert flagged(T(), S01="OL", S02="OC", S03="open") == []


def test_sc_is_numeric_zero():
    assert flagged(T("Ohm"), S01=0.1, S02=0.2, S03="SC", S04=0.15) == []
    assert flagged(T("Ohm"), S01=2, S02=2, S03="SC", S04=2) == ["S03"]


def test_fewer_than_three_readings():
    assert similarity(T(), dict(S01=1, S02=100)) is None
    assert similarity(T(), dict(S01=1, S02=100, S03="")) is None
    assert similarity({"id": "Y", "kind": "yn"}, dict(S01="yes", S02="no", S03="no")) is None


def test_plan_band_overrides_default():
    r = dict(S01=1.2, S02=1.2, S03=1.2, S04=1.6)
    assert flagged(T(), **r) == ["S04"]                                   # default 25 %
    assert flagged(T(outlier={"pct": 50.0}), **r) == []                   # plan: 50 %
    s = similarity(T(outlier={"ratio": [0.5, 2.0]}), dict(S01=1.2, S02=1.2, S03=1.2, S04=2.6))
    assert list(s["flagged"]) == ["S04"] and s["band"] == "x0.5..x2"
    assert flagged(T(outlier={"ratio": [0.5, 2.0]}), S01=1.2, S02=1.2, S03=1.2, S04=2.3) == []
    # plan floor in its own unit
    t = T("Ohm", outlier={"pct": 10.0, "floor": 0.5, "floor_unit": "Ohm"})
    assert flagged(t, S01=2, S02=2, S03=2, S04=2.4) == []                 # 0.4 inside the 0.5 floor
    assert flagged(t, S01=2, S02=2, S03=2, S04=2.6) == ["S04"]


def test_text_names_median_and_band():
    r = similarity(T(), dict(S01=1.2, S02=1.2, S03=1.2, S04=2.5))
    assert r["flagged"]["S04"]["text"] == "≠ batch: 2.1× median (median 1.2 kΩ, band ±25 %)"


def test_engine_snapshot_and_gate_untouched(engine):
    eng = engine
    for b in ("S01", "S02", "S03"):
        eng.override_gate(b, "unpowered", "test")
    for b, v in (("S01", "20"), ("S02", "21"), ("S03", "50")):
        assert eng.entry(b, "U01", v, True)["ok"]
    snap = eng.state.snapshot()
    sim = snap["similarity"]["U01"]
    assert sim["median"] == 21 and list(sim["flagged"]) == ["S03"]
    assert snap["cells"]["S03|U01"]["verdict"] == "pass"           # display only: the verdict is not changed
    assert all("similarity" not in g for g in snap["gates"])
    assert "U02" not in snap["similarity"]
