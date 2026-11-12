import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from polybench.plugins.ecad_stubs import AltiumBridge
from polybench.plugins.fusion import (FakeClient, FusionBridge, FusionUnavailable, McpClient, build_script,
                                      build_show_command, build_text_command)

PARTS = {"R1": "10k", "C1": "100n", "U1": "mcu", "FRAME1": "", "TP1": ""}


def bridge(**kw):
    return FusionBridge(client=FakeClient(dict(PARTS), **kw), simulate=False)


def test_highlight_sends_one_show_and_lists_unknown():
    b = bridge()
    r = b.highlight(["R1", "C1", "R99", "VIN"])
    assert r["ok"] and r["highlighted"] == ["R1", "C1", "VIN"] and r["missing"] == ["R99"]
    assert b.client.commands == ['Electron.run "SHOW R1 C1 VIN"']


def test_second_highlight_ends_the_first_show():
    b = bridge()
    b.highlight(["R1"])
    assert b.client.active == "Electron::Show"
    r = b.highlight(["C1"])
    assert r["ok"] and b.client.ended >= 1 and b.client.commands[-1] == 'Electron.run "SHOW C1"'


def test_clear_ends_show():
    b = bridge()
    b.highlight(["R1"])
    b.clear()
    assert b.client.active is None


def test_operator_dialog_is_never_cancelled():
    b = bridge()
    b.client.active = "Electron::Move"
    r = b.highlight(["R1"])
    assert not r["ok"] and r["busy"] and b.client.commands == [] and b.client.ended == 0
    b.clear()
    assert b.client.active == "Electron::Move"


def test_only_plain_names_are_sent():
    for bad in ("R1; DELETE", 'R1" SHOW', "R1 R2", "", "$x"):
        assert not bridge().highlight([bad])["ok"] or " " in bad
    with pytest.raises(ValueError):
        build_text_command("WRITE foo")
    with pytest.raises(ValueError):
        build_script('Electron.run "SHOW R1;"')
    assert build_show_command(["R1", "R1", "TP2"]) == "SHOW R1 TP2"


def test_status_variants():
    s = bridge().status()
    assert s["connected"] and s["document"] == "simulated-board" and s["editor"] == "board" and "4 parts" in s["detail"]
    assert bridge(editor="schematic").status()["editor"] == "schematic"
    assert bridge(editor="none").status()["editor"] == "none"
    down = bridge(fail="cannot reach Fusion MCP").status()
    assert down["connected"] is False and "cannot reach" in down["detail"]


def test_no_editor_sends_nothing():
    b = bridge(editor="none")
    r = b.highlight(["R1"])
    assert not r["ok"] and "No Electronics editor" in r["message"] and b.client.commands == []


def test_expected_document_guard():
    b = bridge()
    b.set_expected_document("demo-1")
    r = b.highlight(["R1"])
    assert not r["ok"] and "expected" in r["message"] and b.client.commands == []
    b.set_expected_document("simulated")
    assert b.highlight(["R1"])["ok"]


def test_designators_exclude_frames():
    assert bridge().designators() == ["C1", "R1", "TP1", "U1"]
    assert bridge(fail="x").designators() is None


def test_nothing_in_document():
    r = bridge().highlight(["R77"])
    assert not r["ok"] and r["missing"] == ["R77"]


def test_simulate_bridge():
    b = FusionBridge(simulate=True)
    assert b.status()["connected"] and b.highlight(["R1", "C2"])["ok"] and "R1" in b.designators()


def test_unreachable_fusion_returns_status_not_exception():
    b = FusionBridge(client=McpClient("http://127.0.0.1:9/mcp", timeout=0.5), simulate=False)
    s = b.status()
    assert s["connected"] is False
    r = b.highlight(["R1"])
    assert not r["ok"] and "not reachable" in r["message"]
    assert b.designators() is None
    b.clear()


class _Handler(BaseHTTPRequestHandler):
    log = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.log.append(body)
        m = body.get("method")
        if m == "notifications/initialized":
            self.send_response(202); self.end_headers(); return
        if m == "initialize":
            res = {"protocolVersion": "2025-03-26", "capabilities": {}}
            hdr = {"MCP-Session-Id": "s1"}
        else:
            a = body["params"]["arguments"]
            hdr = {}
            if body["params"]["name"] == "fusion_mcp_read":
                if a.get("queryType") == "document":
                    txt = {"results": [{"name": "boardX", "isActive": True, "isModified": False}]}
                else:
                    txt = {"activeCommand": None}
            elif body["params"]["name"] == "fusion_mcp_electronics_read":
                txt = {"items": [{"name": "R1", "value": "1k"}, {"name": "C1", "value": ""}] if a["entity_type"] == "electronics.Element" else [],
                       "pagination": {"hasMore": False}}
            else:
                txt = {"success": True}
            res = {"content": [{"type": "text", "text": json.dumps(txt)}]}
        data = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": res}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for k, v in hdr.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_mcp_transport_against_fake_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        b = FusionBridge(client=McpClient(f"http://127.0.0.1:{srv.server_port}/mcp", timeout=3), simulate=False)
        s = b.status()
        assert s["connected"] and s["document"] == "boardX" and s["editor"] == "board"
        assert b.designators() == ["C1", "R1"]
        r = b.highlight(["R1"])
        assert r["ok"], r
        sent = [x for x in _Handler.log if x.get("params", {}).get("name") == "fusion_mcp_execute"]
        assert any("SHOW R1" in x["params"]["arguments"]["object"]["script"] for x in sent)
        assert all(x["params"]["arguments"]["object"].get("readOnly") or "executeTextCommand" in x["params"]["arguments"]["object"]["script"]
                   for x in sent)
    finally:
        srv.shutdown()


def test_stubs_report_not_installed():
    for cls in (AltiumBridge,):                # `kicad` is a real bridge now (polybench.kicad)
        b = cls()
        assert b.status()["connected"] is False and "not installed" in b.status()["detail"]
        assert b.highlight(["R1"])["ok"] is False and b.designators() is None
        b.clear()
