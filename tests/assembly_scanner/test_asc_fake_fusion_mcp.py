"""Self-tests for the fake Fusion MCP: it must behave like the real one's handshake, or the
integration tests built on it prove nothing. Pure stdlib client; no app code involved."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from asc_fake_fusion_mcp import FakeFusionMCP, load_tools

ACCEPT = "application/json, text/event-stream"


def post(url, payload, sid=None, accept=ACCEPT):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", accept)
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def handshake(url):
    st, hdr, body = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}}})
    assert st == 200
    sid = {k.lower(): v for k, v in hdr.items()}["mcp-session-id"]
    st, _, _ = post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    assert st == 202
    return sid


def test_tool_list_matches_real_server_fixture():
    names = {t["name"] for t in load_tools()}
    assert names == {"fusion_mcp_electronics_read", "fusion_mcp_execute",
                     "fusion_mcp_read", "fusion_mcp_update"}


def test_handshake_and_tools_call_recorded(fake_fusion):
    sid = handshake(fake_fusion.url)
    st, _, body = post(fake_fusion.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    assert st == 200 and len(json.loads(body)["result"]["tools"]) == 4
    script = "def run(_context: str):\n    print('R4 R5')\n"
    st, _, body = post(fake_fusion.url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "fusion_mcp_execute", "arguments": {
                           "featureType": "script", "object": {"script": script}}}}, sid)
    res = json.loads(body)["result"]
    assert res["isError"] is False and res["content"][0]["type"] == "text"
    assert json.loads(res["content"][0]["text"]) == {"success": True, "message": "R4 R5\n"}
    assert fake_fusion.methods() == ["initialize", "notifications/initialized",
                                     "tools/list", "tools/call"]
    [c] = fake_fusion.script_calls(writes_only=True)
    assert c.mentions("R4", "R5")


def test_requires_session_header(fake_fusion):
    st, _, _ = post(fake_fusion.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert st == 400
    st, _, _ = post(fake_fusion.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "bogus")
    assert st == 404
    assert fake_fusion.tool_calls() == []


def test_requires_initialized_notification(fake_fusion):
    st, hdr, _ = post(fake_fusion.url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {}})
    sid = {k.lower(): v for k, v in hdr.items()}["mcp-session-id"]
    st, _, _ = post(fake_fusion.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    assert st == 400


def test_requires_accept_header(fake_fusion):
    st, _, _ = post(fake_fusion.url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {}}, accept="application/json")
    assert st == 406


@pytest.mark.parametrize("args, msg", [
    ({"featureType": "script"}, "Missing required argument"),
    ({"featureType": "macro", "object": {}}, "Invalid value"),
    ({"featureType": "script", "object": {"script": "print(1)"}}, "named 'run'"),
])
def test_rejects_calls_the_real_schema_rejects(fake_fusion, args, msg):
    sid = handshake(fake_fusion.url)
    _, _, body = post(fake_fusion.url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "fusion_mcp_execute", "arguments": args}}, sid)
    assert msg in json.loads(body)["error"]["message"]


def test_sse_mode_and_tool_error_injection():
    with FakeFusionMCP(sse=True) as fake:
        fake.tool_error = "Cannot run a script while a command dialog is open"
        sid = handshake(fake.url)
        _, hdr, body = post(fake.url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "fusion_mcp_execute", "arguments": {
                                "featureType": "script",
                                "object": {"script": "def run(_c):\n    pass\n"}}}}, sid)
        assert {k.lower(): v for k, v in hdr.items()}["content-type"] == "text/event-stream"
        data = json.loads(body.split("data: ", 1)[1])
        assert data["result"]["isError"] is True


def test_simulated_fusion_runs_scripts_against_fake_adsk(fake_fusion):
    from asc_fake_fusion_mcp import SimulatedFusion
    f = SimulatedFusion()
    src = ("import adsk.core, adsk.electron\n"
           "def run(_c):\n"
           "    b = adsk.electron.Board.cast(adsk.core.Application.get().activeProduct)\n"
           "    s = b.linkedSchematic\n"
           "    s.selectObjects([p for p in s.parts if p.name in ('R4','R5')], False)\n"
           "    print(b.elements.count)\n")
    env = json.loads(f.run_script(src))
    assert env == {"success": True, "message": f"{len(f.board_designators)}\n"}
    assert f.selected == ("R4", "R5")
    env = json.loads(f.run_script("def run(_c):\n    raise RuntimeError('boom')\n"))
    assert env["success"] is False and "boom" in env["error"]
    import sys
    assert "adsk" not in sys.modules
