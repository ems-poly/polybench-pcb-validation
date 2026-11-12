"""The `--ecad kicad` switch (KC-03, KC-04), the server's bounds with a stalled KiCad (KC-27, KC-30), the simulator (KC-37), selftest
(KC-38), doctor (KC-40), docs (KC-41). Session and HTTP level, against the fake KiCad server and the Fusion MCP stand-in."""
import importlib.metadata as md
import os
import re
import time
from pathlib import Path

import pytest

from helpers import DEMO, ROOT
from polybench import registry
from polybench.kicad import link as linkmod
from polybench.kicad.bridge import KicadBridge
from polybench.kicad.fakeclient import FakeKiCadClient
from polybench.kicad.link import KiCadLink
from polybench.kicad.synth import demo_board
from polybench.plugins.fusion import FusionBridge

import shutil
import time

import pytest

from helpers import DEMO

DEMO_DESIGNATORS = "U1 U2 U3 C1 C2 C3 C4 C5 C6 C7 R1 R2 R3 R4 R5 D1 J1 J2 IC2 TP99".split()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """The demo protocol, an empty camera frame folder and a Fusion stand-in (these tests assert that Fusion is never called)."""
    from asc_fake_fusion_mcp import FakeFusionMCP, SimulatedFusion
    proto = tmp_path / "proto"
    shutil.copytree(DEMO, proto)
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "CURRENT").write_text("", encoding="utf-8")
    monkeypatch.setenv("POLYBENCH_SCANNER_FAKE_FRAMES", str(frames))
    monkeypatch.setenv("ASCAN_CONFIG_DIR", str(tmp_path / "cfg"))
    with FakeFusionMCP(fusion=SimulatedFusion(DEMO_DESIGNATORS, doc_name="Demo-board")) as fake:
        monkeypatch.setenv("POLYBENCH_FUSION_URL", fake.url)
        yield {"proto": proto / "demo.pcbv.yaml", "frames": frames, "fake": fake, "tmp": tmp_path}


def wait(fn, timeout=15.0, what="condition"):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("timed out waiting for " + what)


def test_load_ecad_default_is_fusion_and_kicad_by_flag_or_env(monkeypatch):
    """KC-04."""
    monkeypatch.delenv("POLYBENCH_ECAD", raising=False)
    assert isinstance(registry.load_ecad(None)[0], FusionBridge)
    assert isinstance(registry.load_ecad("kicad")[0], KicadBridge)
    monkeypatch.setenv("POLYBENCH_ECAD", "kicad")
    assert isinstance(registry.load_ecad(None)[0], KicadBridge)
    monkeypatch.delenv("POLYBENCH_ECAD")
    b, note = registry.load_ecad("kicad", simulate=True)
    assert isinstance(b, KicadBridge) and b.simulate and "KiCad" in note
    assert next(e for e in md.entry_points(group="polybench.ecad") if e.name == "fusion").value.endswith("FusionBridge")


def _fake_link():
    fake = FakeKiCadClient(demo_board(DEMO_DESIGNATORS + ["TP1", "TP2", "TP3", "TP4"]))
    link = KiCadLink(client_factory=lambda a: fake, fake=True)
    linkmod.reset_shared(link)
    return fake, link


def test_ecad_kicad_sends_nothing_to_fusion(rig):          # noqa: F811
    """KC-03: step 1 probe and select, a test highlight, an Assembly selection and the polarity card, all with --ecad kicad; the
    Fusion stand-in (POLYBENCH_FUSION_URL is set to it) records no request."""
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    fake, link = _fake_link()
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "k3", False, 3, "kicad")
    assert sess.bridge.name == "kicad" and sess.ecad_program == "KiCad"
    with TestClient(create_app(sess)) as c:
        pr = c.get("/api/project/probe").json()
        assert pr["status"]["connected"] and pr["status"]["program"] == "KiCad" and pr["documents"]["open"][0]["name"].endswith(".kicad_pcb")
        assert pr["crosscheck"] is not None and pr["bridge"] == "kicad"
        r = c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]})
        assert r.status_code == 200, r.text
        assert sess.engine.state.project["select"]["program"] == "KiCad" and sess.engine.state.project["select"]["document_id"]
        tid = "U01"
        r = c.post("/api/cmd/select_test", json={"test_id": tid}).json()
        assert r["ecad"]["ok"], r
        assert {x for x in r["ecad"]["highlighted"]} == {"TP1", "TP2"}
        r = c.post("/api/cmd/select_line", json={"line": 2}).json()
        assert r["ecad"].get("ok") and r["ecad"]["highlighted"] == ["U2"], r
        assert c.get("/api/polarity?line=2").status_code == 200
        res, err, _cached = sess._pol_footprints(["U1"])
        assert res and res["parts"]["U1"]["pads"] and res["parts"]["U1"].get("program") == "KiCad"
        st = c.get("/api/state").json()
        assert st["ecad_program"] == "KiCad"
        assert c.post("/api/cmd/highlight", json={"designators": ["R9"]}).json()["ecad"]["highlighted"] == ["R9"]
    assert rig["fake"].tool_calls() == [] and rig["fake"].methods() == []
    assert set(fake.names) <= {"GetVersion", "GetOpenDocuments", "GetItems", "GetNets", "GetSelection", "AddToSelection", "ClearSelection", "RunAction"}


def test_expected_document_is_armed_by_step_1(rig):          # noqa: F811
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    fake, link = _fake_link()
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "k9", False, 3, "kicad")
    with TestClient(create_app(sess)) as c:
        pr = c.get("/api/project/probe").json()
        c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]})
        assert link.expected_document == pr["status"]["document"]
        fake.snap.document.board_filename = "Other.kicad_pcb"            # another board comes up in KiCad
        r = c.post("/api/cmd/highlight", json={"designators": ["R9"]}).json()["ecad"]
        assert r["ok"] is False and "Other.kicad_pcb" in r["message"]


def test_stalled_kicad_never_blocks_the_server(rig, monkeypatch):          # noqa: F811
    """KC-27, KC-30: a KiCad that never answers; the server's own 'not responding' path is not taken, other endpoints stay up."""
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    fake = FakeKiCadClient(demo_board(DEMO_DESIGNATORS + ["TP1", "TP2"]))
    fake.delay, fake.hang = 0.4, True                  # every request ends in a receive timeout after 0.4 s
    linkmod.reset_shared(KiCadLink(client_factory=lambda a: fake, fake=True))
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "k27", False, 3, "kicad")
    with TestClient(create_app(sess)) as c:
        t = time.monotonic()
        st = c.get("/api/ecad/status?fresh=1").json()
        assert time.monotonic() - t < 5 and st["connected"] is False and "not responding" not in st["detail"]
        t = time.monotonic()
        r = c.post("/api/cmd/highlight", json={"designators": ["R9"]}).json()["ecad"]
        assert time.monotonic() - t < 8 and r["ok"] is False and "not responding" not in str(r)
        pr = c.get("/api/project/probe").json()
        assert pr["status"]["connected"] is False
        assert c.get("/api/state").status_code == 200


def test_simulated_kicad_session_sends_nothing_and_selects(rig, monkeypatch):          # noqa: F811
    """KC-37: --simulate --ecad kicad: an in-memory demo board, no socket, no Fusion."""
    import pynng
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app

    def boom(*a, **k):
        raise AssertionError("socket opened")
    monkeypatch.setattr(pynng, "Req0", boom)
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "ks", True, 3, "kicad")
    assert sess.bridge.simulate and sess.ecad_program == "KiCad"
    with TestClient(create_app(sess)) as c:
        st = c.get("/api/ecad/status?fresh=1").json()
        assert st["connected"] and st["simulated"] and "simulated" in st["program"]
        r = c.post("/api/cmd/highlight", json={"designators": ["U1", "R9"]}).json()["ecad"]
        assert r["ok"] and r["highlighted"] == ["U1", "R9"]
        r = c.post("/api/cmd/select_test", json={"test_id": "U01"}).json()["ecad"]
        assert r["ok"] and r["highlighted"] == ["TP1", "TP2"] or r["missing"]
    assert rig["fake"].tool_calls() == []


def test_simulator_bridge_shapes_with_no_socket(monkeypatch):
    import pynng
    monkeypatch.setattr(pynng, "Req0", lambda *a, **k: (_ for _ in ()).throw(AssertionError("socket opened")))
    b = KicadBridge(simulate=True)
    st = b.status()
    assert st["connected"] and st["simulated"] is True and st["editor"] == "board"
    assert b.highlight(["R9"])["ok"] and b.clear()["cleared"]
    from polybench.kicad.client import KicadScannerClient
    c = KicadScannerClient(simulate=True)
    assert c.status()["connected"] and c.highlight_targets(["R9"], ["IC1.10"], ["3V3_A"]).ok and c.clear().ok
    assert c.footprints(["D1"])["parts"]["D1"]["pads"]


def test_polybench_selftest_has_a_passing_kicad_line(capsys):
    """KC-38."""
    from polybench import selftest
    assert selftest.main() == 0 if hasattr(selftest, "main") else selftest.run() == 0
    out = capsys.readouterr().out
    assert re.search(r"PASS\s+KiCad bridge selftest", out)


def test_selftest_line_fails_when_the_fake_is_broken(monkeypatch, capsys):
    from polybench import selftest
    monkeypatch.setattr(KicadBridge, "selftest", lambda self: False)
    assert (selftest.main() if hasattr(selftest, "main") else selftest.run()) == 1
    assert re.search(r"FAIL\s+KiCad bridge selftest", capsys.readouterr().out)


# ---- doctor (KC-40)
def _doctor(link=None):
    from polybench import doctor
    return doctor.check_kicad("demo-1", link)


def test_doctor_states(monkeypatch, tmp_path, snap):
    from polybench.kicad import settings
    monkeypatch.setattr(settings, "socket_dirs", lambda: [str(tmp_path / "none")])
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setenv("POLYBENCH_KICAD_PREFS", str(tmp_path / "prefs"))
    (tmp_path / "prefs" / "10.0").mkdir(parents=True)
    (tmp_path / "prefs" / "10.0" / "kicad_common.json").write_text('{"api": {"enable_server": false}}', encoding="utf-8")
    linkmod.reset_shared(KiCadLink())
    c = _doctor()
    assert c.status == "warn" and "API server is off" in c.detail and "enable the API server" in c.hint           # today's state (L1)
    (tmp_path / "prefs" / "10.0" / "kicad_common.json").write_text('{"api": {"enable_server": true}}', encoding="utf-8")
    linkmod.reset_shared(KiCadLink())
    c = _doctor()
    assert c.status == "warn" and "not running" in c.detail and "start KiCad" in c.hint
    monkeypatch.setitem(__import__("sys").modules, "kipy", None)
    linkmod.reset_shared(KiCadLink())
    c = _doctor()
    assert c.status == "warn" and "pip install -e .[kicad]" in c.hint + c.detail
    monkeypatch.undo()
    fake = FakeKiCadClient(snap)
    ok = _doctor(KiCadLink(client_factory=lambda a: fake, fake=True))
    assert ok.status == "ok" and "DEMO-1" in ok.detail and "footprints" in ok.detail and "10.0.7" in ok.detail
    fake.no_board = True
    c = _doctor(KiCadLink(client_factory=lambda a: fake, fake=True))
    assert c.status == "warn" and "no board is open" in c.detail
    fake.no_board = False
    fake.snap.document.board_filename = "other.kicad_pcb"
    c = doctor_expect = __import__("polybench.doctor", fromlist=["x"]).check_kicad("other-board", KiCadLink(client_factory=lambda a: fake, fake=True))
    assert c.status == "warn" and "not a 'other-board' document" in c.detail


def test_doctor_does_not_warn_about_bridges_when_kicad_registered():
    from polybench import doctor
    checks = doctor.check_plugins()
    bridges = next(c for c in checks if c.name == "ECAD bridges")
    # OC-04: the list of installed bridges is information, never green or amber
    assert bridges.status == "info" and "kicad" in bridges.detail


# ---- documentation (KC-41, KC-42)
def test_docs_updated_for_kicad():
    if not (ROOT / "docs/PRODUCT_SPEC.md").exists():
        pytest.skip("internal product spec is not in the public tree")
    plugins = (ROOT / "docs/PLUGINS.md").read_text(encoding="utf-8")
    assert re.search(r"\|\s*`?kicad`?\s*\|", plugins, re.I) and "IPC API" in plugins and "HARDWARE-VALIDATED" in plugins
    spec = (ROOT / "docs/PRODUCT_SPEC.md").read_text(encoding="utf-8")
    assert "kicad-cli" not in spec.split("DP-01")[1][:2000] or "superseded" in spec
    quick = (ROOT / "docs/BENCH_QUICKSTART.md").read_text(encoding="utf-8")
    assert "--ecad kicad" in quick and ".[kicad]" in quick and "enable_server" in quick
