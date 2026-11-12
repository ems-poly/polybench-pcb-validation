"""FusionClient against the fake Fusion MCP (integration level, no real Fusion).
Covers the wire side of AC-07 and the contract rule "never fails silently".
What the scripts DO inside Fusion (does the board actually highlight?) is bench-only: B-4.
"""
from __future__ import annotations


from asc_support import need
from asc_fake_fusion_mcp import FakeFusionMCP, SimulatedFusion
from asc_pipeline import BAG_B_DESIG


def client_for(url):
    return need("polybench.assembly_scanner.fusion_client").FusionClient(url)


def assert_loud_failure(fn):
    """Either raise, or return a HighlightResult with ok=False and a non-empty error."""
    try:
        res = fn()
    except Exception:
        return
    assert res is not None and res.ok is False and res.error, f"silent failure: {res!r}"


def test_highlight_uses_real_handshake_and_carries_designators(fake_fusion):
    c = client_for(fake_fusion.url)
    c.highlight(["R4", "R5"])
    m = fake_fusion.methods()
    assert m[0] == "initialize"
    assert m.index("notifications/initialized") < m.index("tools/call")
    calls = fake_fusion.tool_calls()
    assert calls, "highlight sent no tools/call"
    assert all(c.tool in {t["name"] for t in fake_fusion.tools} for c in calls)
    carrying = [c for c in fake_fusion.script_calls() if c.mentions("R4", "R5")]
    assert carrying, "no script carrying R4 and R5"
    # Safety property (fusion agent, 7 Oct): every call is a readOnly script, so the
    # highlight can never modify the design, add an undo entry or dirty the document.
    assert all(c.read_only for c in fake_fusion.tool_calls()), \
        "a non-readOnly tools/call was sent: regression of the read-only highlight"
    assert fake_fusion.fusion.selected == ("R4", "R5")


def test_every_operation_is_read_only(fake_fusion):
    c = client_for(fake_fusion.url)
    c.status(); c.board_designators(); c.highlight(["C1"]); c.clear()
    calls = fake_fusion.tool_calls()
    assert len(calls) == 4
    assert all(x.tool == "fusion_mcp_execute" and x.read_only for x in calls)


def test_highlight_replaces_previous_selection(fake_fusion):
    c = client_for(fake_fusion.url)
    c.highlight(["R4", "R5"])
    r = c.highlight(BAG_B_DESIG)
    assert r.ok and set(r.highlighted) == set(BAG_B_DESIG)
    assert fake_fusion.fusion.selected == BAG_B_DESIG


def test_not_on_board_is_reported(fake_fusion):
    r = client_for(fake_fusion.url).highlight(["R4", "R99"])
    assert r.highlighted == ("R4",) and r.not_on_board == ("R99",)
    assert fake_fusion.fusion.selected == ("R4",)


def test_board_designators_from_fake_board(fake_fusion):
    d = client_for(fake_fusion.url).board_designators()
    assert d is not None and {"R4", "R5", "IC2", "C22"} <= d


def test_no_electronics_document_is_loud():
    with FakeFusionMCP(fusion=SimulatedFusion(active="none")) as fake:
        c = client_for(fake.url)
        st = c.status()
        assert st["is_electronics"] is False
        assert_loud_failure(lambda: c.highlight(["R4"]))


def test_schematic_tab_active_still_highlights():
    """Simulated only: the real cross-probe from the schematic tab is unverified (B-4c)."""
    with FakeFusionMCP(fusion=SimulatedFusion(active="schematic")) as fake:
        r = client_for(fake.url).highlight(["R4", "R5"])
        assert r.ok and fake.fusion.selected == ("R4", "R5")


def test_session_is_reused(fake_fusion):
    c = client_for(fake_fusion.url)
    c.highlight(["R4"])
    c.highlight(["C1"])
    assert fake_fusion.methods().count("initialize") == 1


def test_clear_empties_selection(fake_fusion):
    c = client_for(fake_fusion.url)
    c.highlight(["R4"])
    assert c.clear().ok
    assert fake_fusion.fusion.selected == ()


def test_status_shape(fake_fusion):
    st = client_for(fake_fusion.url).status()
    assert {"connected", "doc_name", "is_electronics"} <= set(st)
    assert st["connected"] is True and st["is_electronics"] is True
    assert st["doc_name"] == fake_fusion.fusion.doc_name


def test_status_when_fusion_not_running():
    with FakeFusionMCP() as fake:
        url = fake.url
    st = client_for(url).status()          # server is gone now
    assert st["connected"] is False


def test_highlight_when_fusion_not_running_is_loud():
    with FakeFusionMCP() as fake:
        url = fake.url
    assert_loud_failure(lambda: client_for(url).highlight(["R4", "R5"]))


def test_tool_error_is_loud(fake_fusion):
    c = client_for(fake_fusion.url)
    fake_fusion.tool_error = "Cannot run a script while a command dialog is open"
    assert_loud_failure(lambda: c.highlight(["R4", "R5"]))


def test_http_500_is_loud(fake_fusion):
    c = client_for(fake_fusion.url)
    c.status()
    fake_fusion.fail_http = 500
    assert_loud_failure(lambda: c.highlight(["R4", "R5"]))


def test_sse_responses_are_understood():
    with FakeFusionMCP(sse=True) as fake:
        c = client_for(fake.url)
        assert c.status()["connected"] is True
        c.highlight(["R4", "R5"])
        assert any(x.mentions("R4", "R5") for x in fake.tool_calls())


def test_recovers_after_fusion_restart(fake_fusion):
    """Fusion restarted -> old session id gets 404 -> client re-handshakes, call still lands."""
    c = client_for(fake_fusion.url)
    c.highlight(["R4"])
    fake_fusion.sessions.clear()
    fake_fusion.initialized.clear()
    fake_fusion.reset()
    c.highlight(["R4", "R5"])
    assert any(x.mentions("R4", "R5") for x in fake_fusion.tool_calls()), \
        "after a session loss the highlight never reached Fusion"
