"""The embedded Assembly Scanner inside the server: recorded camera frames -> PoC decoder and resolver -> checklist line ->
PoC Fusion client selection (against the Fusion MCP stand-in) -> place on boards -> log. No real camera, no real Fusion."""
import json
import shutil
import time

import pytest

from helpers import DEMO, ROOT

ASC = ROOT / "tests" / "assembly_scanner"
DEMO_DESIGNATORS = "U1 U2 U3 C1 C2 C3 C4 C5 C6 C7 R1 R2 R3 R4 R5 D1 J1 J2 IC2 TP99".split()


@pytest.fixture
def rig(tmp_path, monkeypatch, demo_files):
    """A demo protocol whose lines 3 and 5 carry the Mouser P/Ns of two generated label images, a frame folder, a Fusion stand-in."""
    from asc_fake_fusion_mcp import FakeFusionMCP, SimulatedFusion
    proto = tmp_path / "proto"
    shutil.copytree(DEMO, proto)
    a = (proto / "assembly.yaml").read_text(encoding="utf-8")
    a = a.replace('barcode: ["595-DEMOCAN8"]', 'barcode: ["903-DEMO-XCVR/MF", "DEMO-XCVR/MF"]')
    a = a.replace('designators: [R3, R4, R5], mpn: "RC0402FR-0710KL"', 'designators: [R4, R5], mpn: "DEMO-R2K2"')
    a = a.replace('barcode: ["603-RC0402FR-0710KL"]', 'barcode: ["901-DEMO-R2K2", "DEMO-R2K2"]')
    a = a.replace("designators: [U3]", "designators: [IC2]")
    (proto / "assembly.yaml").write_text(a, encoding="utf-8")
    frames = tmp_path / "frames"
    frames.mkdir()
    for n in ("label_a.heic", "label_b_tilted.jpg"):
        shutil.copy(demo_files[n], frames / n)
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


def test_camera_scan_selects_line_highlights_in_fusion_and_places(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb", True, 3, None)
    with TestClient(create_app(sess)) as c:
        st = c.get("/api/scanner/state?hb=1").json()
        assert st["bom"]["name"] and st["fired"]["seq"] == 0
        wait(lambda: c.get("/api/scanner/state?hb=1").json()["board"]["ok"], what="board auto pick")
        st = c.get("/api/scanner/state?hb=1").json()
        assert st["board"]["mode"] == "auto" and st["board"]["name"] == "Demo-board"
        assert st["camera"]["state"] == "running" and not st["idle"]

        (rig["frames"] / "CURRENT").write_text("label_a.heic", encoding="utf-8")
        st = wait(lambda: (lambda s: s if s["fired"]["seq"] >= 1 else None)(c.get("/api/scanner/state?hb=1").json()), what="a fired scan")
        assert st["fired"]["ok"] and st["fired"]["line"] == 5 and st["fired"]["code"] == "901-DEMO-R2K2"
        st = wait(lambda: (lambda s: s if s["result"] and s["result"]["highlighted"] else None)(c.get("/api/scanner/state?hb=1").json()), what="Fusion selection")
        assert st["result"]["status"] == "MATCH" and st["result"]["designators"] == ["R4", "R5"]
        assert st["result"]["highlighted"] == ["R4", "R5"] and st["result"]["board"] == "Demo-board"
        assert any("HIGHLIGHTED on Demo-board" in t for t, _l, _s in st["hud"]["bottom"])
        assert rig["fake"].fusion.selected == ("R4", "R5")                       # the PoC client's selection reached "Fusion"

        s = c.get("/api/state").json()
        assert s["alog"][-1]["kind"] == "scan" and s["alog"][-1]["via"] == "camera" and s["alog"][-1]["line"] == 5
        r = c.post("/api/cmd/place_batch", json={"line": 5, "boards": ["S01", "S03"], "via": "scan", "code": "901-DEMO-R2K2"})
        assert r.status_code == 200
        s = c.get("/api/state").json()
        assert s["asm"]["5"]["state"] == "partial" and s["asm"]["5"]["missing"] == ["S02"]
        assert [x["kind"] for x in s["alog"]][-2:] == ["scan", "place"] and s["alog"][-1]["boards"] == ["S01", "S03"]

        # same bag held in view: no second scan (PoC debouncer); a different bag: next line
        time.sleep(1.0)
        assert c.get("/api/scanner/state?hb=1").json()["fired"]["seq"] == 1
        (rig["frames"] / "CURRENT").write_text("label_b_tilted.jpg", encoding="utf-8")
        st = wait(lambda: (lambda s: s if s["fired"]["seq"] >= 2 else None)(c.get("/api/scanner/state?hb=1").json()), what="second scan")
        assert st["fired"]["line"] == 3
        wait(lambda: rig["fake"].fusion.selected == ("IC2",), what="IC2 selected")


def test_typed_scan_and_picked_line_use_the_same_selection_path(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb2", True, 2, None)
    with TestClient(create_app(sess)) as c:
        c.get("/api/scanner/state?hb=1")
        r = c.post("/api/cmd/scan", json={"code": "595-DEMOADC16"}).json()
        assert r["ok"] and r["line"] == 2 and r["ecad"]["highlighted"] == ["U2"]
        assert rig["fake"].fusion.selected == ("U2",)
        r = c.post("/api/cmd/select_line", json={"line": 4}).json()
        assert r["ecad"]["highlighted"] == ["C1", "C2", "C3", "C4", "C5", "C6"]
        st = c.get("/api/scanner/state?hb=1").json()
        assert st["result"]["mouser_pn"] == "187-CL05B104KO5NNNC" and st["result"]["designators"][0] == "C1"
        # a DNF line selects nothing and does not error
        assert c.post("/api/cmd/select_line", json={"line": 8}).json()["ecad"]["highlighted"] == []


def test_camera_is_released_when_paused_stopped_or_the_page_is_left(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb3", True, 2, None)
    with TestClient(create_app(sess)) as c:
        c.get("/api/scanner/state?hb=1")
        cam = sess.scanner.camera
        wait(lambda: cam.state == "running", what="camera running")
        assert c.post("/api/scanner/control", json={"action": "pause"}).json()["idle"] and cam.state == "stopped"
        c.post("/api/scanner/control", json={"action": "resume"})
        wait(lambda: cam.state == "running", what="camera resumed")
        c.post("/api/scanner/control", json={"action": "stop"})
        assert cam.state == "stopped"
        c.post("/api/scanner/control", json={"action": "start"})
        wait(lambda: cam.state == "running", what="camera restarted")
        c.post("/api/scanner/leave")                                            # the operator left the Assembly step
        assert cam.state == "stopped" and c.get("/api/scanner/state").json()["idle"]
        c.get("/api/scanner/state?hb=1")                                        # back on the page
        wait(lambda: cam.state == "running", what="camera back")
        assert c.post("/api/scanner/control", json={"action": "mirror", "value": True}).json()["mirror"]
        assert c.post("/api/scanner/control", json={"action": "nonsense"}).status_code == 404


def test_stream_serves_jpeg_frames(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb4", True, 2, None)
    (rig["frames"] / "CURRENT").write_text("label_b_tilted.jpg", encoding="utf-8")
    with TestClient(create_app(sess)) as c:
        c.get("/api/scanner/state?hb=1")
        wait(lambda: sess.scanner.jpeg()[1], what="a frame")
        seq, jpg = sess.scanner.jpeg()
        assert jpg[:2] == b"\xff\xd8" and len(jpg) > 5000


def test_label_not_in_the_bom_is_logged_as_a_miss_and_the_selection_stays(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    a = rig["proto"].parent / "assembly.yaml"
    a.write_text(a.read_text(encoding="utf-8").replace('"903-DEMO-XCVR/MF", "DEMO-XCVR/MF"', '"595-DEMOCAN8"'), encoding="utf-8")     # the photo's P/N is no longer a BOM key
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb5", True, 2, None)
    with TestClient(create_app(sess)) as c:
        wait(lambda: c.get("/api/scanner/state?hb=1").json()["board"]["ok"], what="board")
        c.post("/api/cmd/scan", json={"code": "595-DEMOADC16"})                       # U2 selected first
        (rig["frames"] / "CURRENT").write_text("label_b_tilted.jpg", encoding="utf-8")
        st = wait(lambda: (lambda s: s if s["fired"]["seq"] >= 1 else None)(c.get("/api/scanner/state?hb=1").json()), what="fired")
        assert st["fired"]["ok"] is False and st["result"]["status"] == "NOT_IN_BOM"
        assert any("NOT IN CURRENT BOM" in t for t, _l, _s in st["hud"]["bottom"])
        assert rig["fake"].fusion.selected == ("U2",)                                  # highlight unchanged, as in the PoC
        assert c.get("/api/state").json()["alog"][-1]["kind"] == "scan_miss"


def test_project_step_decides_bom_and_board_when_present(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb6", True, 2, None)
    lines = [l for l in sess.engine.state.bom_lines() if l["line"] in (2, 4)]
    sess.project_info = lambda: {"ecad_connected": True, "document_name": "Demo-board", "document_id": None,
                                 "bom_source": "test", "bom_lines": lines}
    with TestClient(create_app(sess)) as c:
        st = c.get("/api/scanner/state?hb=1").json()
        assert "project BOM" in st["bom"]["name"]
        wait(lambda: c.get("/api/scanner/state?hb=1").json()["board"]["ok"], what="board")
        assert sorted(sess.scanner.line_map) == [2, 4]
        assert c.get("/api/scanner/state").json()["board"]["mode"] == "manual"


def test_clear_endpoint_clears_the_fusion_selection_and_the_result_card(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb7", True, 2, None)
    with TestClient(create_app(sess)) as c:
        c.get("/api/scanner/state?hb=1")
        assert c.post("/api/cmd/scan", json={"code": "595-DEMOADC16"}).json()["ecad"]["highlighted"] == ["U2"]
        assert rig["fake"].fusion.selected == ("U2",)
        r = c.post("/api/scanner/clear").json()
        assert r == {"ok": True, "sent": True}
        wait(lambda: rig["fake"].fusion.selected == (), what="selection cleared by the PoC client's clear()")
        assert c.get("/api/scanner/state?hb=1").json()["result"] is None


def test_page_visibility_pauses_and_resumes_the_scanner_and_ignores_late_heartbeats(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb8", True, 2, None)
    with TestClient(create_app(sess)) as c:
        c.get("/api/scanner/state?hb=1")
        cam = sess.scanner.camera
        wait(lambda: cam.state == "running", what="camera running")
        assert c.post("/api/scanner/visibility", json={"visible": False}).json()["background"] is True
        assert cam.state == "stopped"
        st = c.get("/api/scanner/state?hb=1").json()                 # a heartbeat that was already in flight
        assert st["idle"] and st["background"] and cam.state == "stopped"
        assert "Paused (page in background)" in st["hud"]["status"]
        c.post("/api/scanner/visibility", json={"visible": True})    # visible: unfocused windows keep scanning
        st = c.get("/api/scanner/state?hb=1").json()
        assert not st["idle"] and not st["background"]
        wait(lambda: cam.state == "running", what="camera resumed")


def test_auto_flag_and_cancel_are_journalled_and_replay(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb9", True, 2, None)
    with TestClient(create_app(sess)) as c:
        assert c.post("/api/cmd/place_batch", json={"line": 5, "boards": ["S01"], "via": "scan", "code": "x", "auto": True}).status_code == 200
        assert c.post("/api/cmd/place_batch", json={"line": 3, "boards": ["S01"], "via": "button"}).status_code == 200
        assert c.post("/api/cmd/auto_cancel", json={"line": 5, "reason": "new_scan", "code": "x"}).status_code == 200
        al = c.get("/api/state").json()["alog"]
        assert [(r["kind"], r.get("auto")) for r in al] == [("place", True), ("place", False), ("auto_cancel", None)]
        assert al[-1]["reason"] == "new_scan" and al[-1]["line"] == 5
    placed = [e for e in sess.journal.events if e["type"] == "placed_batch"]
    assert placed[0]["payload"].get("auto") is True and "auto" not in placed[1]["payload"]     # old shape unchanged
    sess2 = Session(load(rig["proto"]), rig["tmp"] / "data", "emb9", True, 2, None)          # restart: replay
    assert [r["kind"] for r in sess2.engine.state.alog] == ["place", "place", "auto_cancel"]


def test_continue_without_ecad_sends_nothing_to_fusion(rig):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    sess = Session(load(rig["proto"]), rig["tmp"] / "data", "emb10", True, 2, None)
    sess.engine.skip_ecad("no Fusion on this PC")
    fake = rig["fake"]
    with TestClient(create_app(sess)) as c:
        st = c.get("/api/scanner/state?hb=1").json()
        assert st["ecad_off"] and "ECAD disabled in step 1" in st["hud"]["status"]
        n0 = len(fake.tool_calls())
        r = c.post("/api/cmd/scan", json={"code": "595-DEMOADC16"}).json()           # typed scan: line found, not selected
        assert r["ok"] and r["line"] == 2 and r["ecad"]["disabled"] and r["ecad"]["highlighted"] == []
        assert c.post("/api/cmd/select_line", json={"line": 4}).json()["ecad"]["disabled"]
        (rig["frames"] / "CURRENT").write_text("label_a.heic", encoding="utf-8")                      # camera scan: journalled, not selected
        st = wait(lambda: (lambda s: s if s["fired"]["seq"] >= 1 else None)(c.get("/api/scanner/state?hb=1").json()), what="a fired scan")
        assert st["fired"]["ok"] and st["result"]["blocked"] == "ECAD disabled in step 1"
        assert c.post("/api/scanner/clear").json()["sent"] is False
        c.post("/api/scanner/control", json={"action": "rescan"})
        time.sleep(1.0)
        assert len(fake.tool_calls()) == n0 and fake.fusion.selected == ()
        # clearing the skip in step 1 gives Fusion back
        sess.engine.clear_skip_ecad()
        st = c.get("/api/scanner/state?hb=1").json()
        assert not st["ecad_off"]
