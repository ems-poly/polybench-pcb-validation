import json

import pytest

from polybench import registry
from polybench.plugins.camera import CameraSnapshot

from pb_plugin_helpers import installed, run_child

import importlib.util
NEEDS_BENCH = pytest.mark.skipif(importlib.util.find_spec("polystage_bench") is None, reason="the separate polystage-bench library is not installed")
ACTIONS = ["openocd.identify", "openocd.flash", "pcan.capture", "pcan.stats", "pcan.query", "n6705b.identify",
           "n6705b.first_power", "n6705b.measure", "n6705b.load_sweep", "camera.snapshot", "dmm.read"]


def test_camera_sim(make_ctx):
    ctx = make_ctx({"map": {"evidence": "V9"}})
    CameraSnapshot().run(ctx)
    from pathlib import Path
    assert Path(ctx.proposals[0]["evidence"]).read_bytes()[:2] == b"\xff\xd8"


def test_camera_real_without_opencv_is_a_clear_error(make_ctx, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "cv2", None)
    with pytest.raises(Exception, match="OpenCV"):
        CameraSnapshot().run(make_ctx({}, simulate=False))


@pytest.mark.skipif(not installed("pcan.stats"), reason="package not installed (pip install -e .): entry points missing")
@pytest.mark.parametrize("name", [pytest.param(n, marks=NEEDS_BENCH) if n.startswith("n6705b") else n for n in ACTIONS])
def test_registered_and_selftest(name):
    a = registry.find_action(name)
    assert a.name == name and isinstance(a.capabilities, list)
    assert a.selftest() is True


@pytest.mark.skipif(not installed("pcan.stats"), reason="package not installed (pip install -e .)")
def test_ecad_bridges_registered():
    import importlib.metadata as md
    names = {e.name for e in md.entry_points(group="polybench.ecad")}
    assert {"fusion", "kicad", "altium"} <= names


SIM_RUNS = {
    "openocd.identify": {"map": {"device_id": "F2"}},
    "pcan.capture": {"duration_s": 1, "decode": [{"name": "x", "id": 0x100, "start": 0, "length": 16}],
                     "sim": {"values": {"x": {"mean": 100, "sd": 1}}}, "propose": [{"test": "C1", "signal": "x"}]},
    "pcan.stats": {"duration_s": 1, "signal": "x", "full_scale": 65536, "decode": [{"name": "x", "id": 0x100, "start": 0, "length": 16}],
                   "sim": {"values": {"x": {"mean": 100, "sd": 1}}}, "map": {"std": "N1", "noise_free_bits": "N2"}},
    "pcan.query": {"request": {"id": 0x7E0, "data": "01"}, "response": {"id": 0x7E8},
                   "decode": [{"name": "st", "id": 0x7E8, "start": 0, "length": 8}],
                   "sim": {"reply": {"request_id": 0x7E0, "id": 0x7E8, "data": "5A"}}, "propose": [{"test": "Q1", "signal": "st", "as": "hex"}]},
    "n6705b.identify": {"map": {"idn": "P0"}},
    "n6705b.measure": {"map": {"voltage": "P1"}},
    "camera.snapshot": {"map": {"evidence": "V9"}},
    "dmm.read": {"map": {"value": "P2"}, "unit": "mV", "timeout_s": 5},
}


@pytest.mark.skipif(not installed("pcan.stats"), reason="package not installed (pip install -e .)")
@pytest.mark.parametrize("name", [pytest.param(n, marks=NEEDS_BENCH) if n.startswith("n6705b") else n for n in sorted(SIM_RUNS)])
def test_child_process_emits_json_lines(name, tmp_path):
    """The exact path the core takes: python -m polybench.run_action, JSON lines on stdout, simulate=true."""
    msgs, err, rc = run_child(name, SIM_RUNS[name], tmp_path)
    kinds = [m["t"] for m in msgs]
    assert rc == 0, err
    assert kinds[-1] == "done" and msgs[-1].get("cancelled") is False
    assert "proposal" in kinds and "error" not in kinds
    for m in msgs:
        if m["t"] == "proposal":
            assert isinstance(m["value"], str) and m["test_id"]


@NEEDS_BENCH
@pytest.mark.skipif(not installed("pcan.stats"), reason="package not installed (pip install -e .)")
def test_child_flash_and_first_power(tmp_path):
    from pb_plugin_helpers import EXAMPLE_PROFILE
    img = tmp_path / "i.hex"
    img.write_text(":00000001FF\n", encoding="utf-8")
    msgs, err, rc = run_child("openocd.flash", {"image": str(img), "map": {"flashed": "F1"}}, tmp_path)
    assert rc == 0 and [m for m in msgs if m["t"] == "proposal"][0]["value"] == "Y"
    if EXAMPLE_PROFILE.exists():
        msgs, err, rc = run_child("n6705b.first_power", {"profile": str(EXAMPLE_PROFILE), "volts": 3.3, "map": {"current": "P2"}}, tmp_path)
        assert rc == 0 and any(m["t"] == "proposal" and m["unit"] == "mA" for m in msgs)
        msgs, err, rc = run_child("n6705b.first_power", {}, tmp_path, safe_state=True)
        assert msgs[-1]["t"] == "done" and "outputs off" in msgs[-1]["text"]


@pytest.mark.skipif(not installed("pcan.stats"), reason="package not installed (pip install -e .)")
def test_child_error_is_reported_as_error_line(tmp_path):
    msgs, err, rc = run_child("pcan.capture", {"duration_s": 99999, "decode": []}, tmp_path)
    assert msgs[-1]["t"] == "error" and "duration_s" in msgs[-1]["text"] and rc == 1
