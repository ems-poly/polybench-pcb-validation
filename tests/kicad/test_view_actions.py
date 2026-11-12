"""The view options (RunAction): zoom to the selection, highlight the net, push to the schematic. Fenced by name, opt-out, never fatal."""
import pytest

from polybench.kicad import guard
from polybench.kicad.bridge import KicadBridge
from polybench.kicad.client import KicadScannerClient
from polybench.kicad.fakeclient import FakeKiCadClient
from polybench.kicad.link import KiCadLink, parse_actions

HL, CLR = "pcbnew.EditorControl.highlightNetSelection", "pcbnew.EditorControl.clearHighlight"
FIT, CEN, SCH = "common.Control.zoomFitSelection", "common.Control.centerSelection", "pcbnew.InteractiveSelection.SelectOnSchematic"


def mk(snap, actions=None):
    fake = FakeKiCadClient(snap)
    link = KiCadLink(client_factory=lambda a: fake, fake=True, actions=actions)
    return fake, link, KicadBridge(link=link), KicadScannerClient(link=link)


def test_defaults_zoom_and_highlight_on_schematic_opt_in(monkeypatch):
    monkeypatch.delenv("POLYBENCH_KICAD_ACTIONS", raising=False)
    assert parse_actions(None) == {"zoom", "highlight"}
    assert parse_actions("none") == frozenset() and parse_actions("") == frozenset()
    assert parse_actions("zoom, SCHEMATIC, bogus") == {"zoom", "schematic"}
    monkeypatch.setenv("POLYBENCH_KICAD_ACTIONS", "highlight")
    assert parse_actions(None) == {"highlight"}


def test_net_selection_highlights_and_fits(snap):
    fake, link, b, c = mk(snap)
    r = c.highlight_targets([], ["IC1.10"], ["3V3_A"])
    assert fake.actions == [HL, FIT] and "net is highlighted" in r.note and link.highlight_on


def test_single_footprint_is_centred_not_fitted_and_clears_old_highlight(snap):
    fake, link, b, c = mk(snap)
    c.highlight_targets([], [], ["3V3_A"])
    fake.actions.clear()
    c.highlight_targets(["R9"])
    assert fake.actions == [CLR, CEN] and not link.highlight_on


def test_clear_clears_the_highlight(snap):
    fake, link, b, c = mk(snap)
    c.highlight_targets([], [], ["3V3_A"])
    fake.actions.clear()
    assert c.clear().ok and fake.actions == [CLR] and not link.highlight_on and fake.selection == []


def test_hand_selection_keeps_its_highlight_state_untouched_on_clear(snap):
    fake, link, b, c = mk(snap)
    c.highlight_targets([], [], ["3V3_A"])
    fake.operator_select([snap.by_ref()["C12"].kiid])
    fake.actions.clear()
    c.clear()
    assert fake.actions == [] and fake.selection == [snap.by_ref()["C12"].kiid]


def test_bridge_reports_view(snap):
    fake, link, b, c = mk(snap)
    r = b.highlight(["VIN"])
    assert r["view"] == {HL: "RAS_OK", FIT: "RAS_OK"}


def test_options_off_send_no_action(snap):
    fake, link, b, c = mk(snap, actions="none")
    c.highlight_targets([], ["IC1.10"], ["3V3_A"])
    b.highlight(["R9"])
    assert fake.actions == [] and "RunAction" not in fake.names


def test_schematic_option_sends_select_on_schematic(snap):
    fake, link, b, c = mk(snap, actions="schematic")
    c.highlight_targets(["R9"])
    assert fake.actions == [SCH]


def test_unknown_action_is_reported_and_selection_stands(snap):
    fake, link, b, c = mk(snap)
    fake.known_actions.discard(FIT)
    r = c.highlight_targets([], [], ["3V3_A"])
    assert r.ok and fake.selection and "view action not done" in r.note and "RAS_INVALID" in r.note


def test_busy_view_action_never_fails_the_selection(snap):
    fake, link, b, c = mk(snap)
    from kipy.errors import ApiError
    from kipy.proto.common import ApiStatusCode
    orig = fake.send

    def send(cmd, t):
        if type(cmd).__name__ == "RunAction":
            raise ApiError("busy", code=ApiStatusCode.AS_BUSY)
        return orig(cmd, t)
    fake.send = send
    r = b.highlight(["VIN"])
    assert r["ok"] and fake.selection and all("busy" in v.lower() for v in r["view"].values())


def test_guard_fences_run_action_by_name_and_option():
    from kipy.proto.common import commands

    class Inner:
        n = 0

        def send(self, c, t):
            Inner.n += 1
    off = guard.GuardedClient(Inner())
    cmd = commands.RunAction()
    cmd.action = FIT
    with pytest.raises(guard.CommandRefused):
        off.send(cmd, object)
    on = guard.GuardedClient(Inner(), actions={FIT})
    on.send(cmd, object)
    bad = commands.RunAction()
    bad.action = "pcbnew.EditorControl.deleteItem"
    with pytest.raises(guard.CommandRefused):
        on.send(bad, object)
    other = commands.RunAction()
    other.action = HL                                  # a known name, but its option is not on for this client
    with pytest.raises(guard.CommandRefused):
        on.send(other, object)
    assert Inner.n == 1
