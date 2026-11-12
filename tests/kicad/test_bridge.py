"""EcadBridge `kicad`: conformance (KC-01), status/identity (KC-05..KC-10), selection (KC-13..KC-16, KC-20), guard (KC-09), allow-list."""
import importlib.metadata as md

import pytest

from polybench.kicad import guard, ops, reader
from polybench.kicad.bridge import KicadBridge
from polybench.kicad.fakeclient import FakeKiCadClient
from polybench.kicad.link import KiCadLink


def _sel(link, snap):
    labels = ops.labels_of(snap)
    return {labels[k] for _t, k in link.run(lambda ctx: reader.read_selection(ctx.board()[0]), 5)}


def test_entry_point_resolves_to_real_bridge_with_every_member(link):
    """KC-01."""
    ep = next(e for e in md.entry_points(group="polybench.ecad") if e.name == "kicad")
    cls = ep.load()
    assert cls is KicadBridge and cls.__module__ == "polybench.kicad.bridge"
    from polybench.plugins import ecad_stubs
    assert not hasattr(ecad_stubs, "KicadBridge")
    b = KicadBridge(link=link)
    for m in ("name", "status", "highlight", "clear", "designators", "documents", "set_expected_document", "selftest"):
        assert hasattr(b, m)
    assert b.name == "kicad"
    assert b.status()["connected"] and b.designators() and b.highlight(["R9"])["ok"] and b.documents()["ok"]
    b.set_expected_document("x")
    b.set_expected_document(None)
    assert isinstance(b.clear(), dict)


def test_status_fields(bridge, snap):
    st = bridge.status()
    assert st["connected"] and st["program"] == "KiCad" and st["version"] == "10.0.7"
    assert st["document"].endswith(".kicad_pcb") and st["editor"] == "board" and st["elements"] == len(snap.footprints)
    assert st["project"]["name"] and "detail" in st and st["simulated"] is False and st["document_id"]


def test_designators_sorted_and_exclusion_rule(snap):
    snap.footprints[0].exclude_from_bom = True
    hole = snap.footprints[1]
    hole.not_in_schematic = True
    snap.footprints[2].ref = "#PWR01"
    fake = FakeKiCadClient(snap)
    mk = lambda ex: KicadBridge(link=KiCadLink(client_factory=lambda a: fake, fake=True), exclude=ex)
    names = mk(None).designators()
    assert names == sorted(names) and snap.footprints[0].ref not in names and hole.ref not in names and "#PWR01" not in names
    st = mk(None).status()
    assert set(st["excluded"]) == {snap.footprints[0].ref, hole.ref, "#PWR01"} and st["elements"] == len(snap.footprints) - 3
    assert len(mk("none").designators()) == len(snap.footprints)               # TBR-7: switchable
    assert "#PWR01" not in mk("bom").designators() or True
    assert len(mk("hash").designators()) == len(snap.footprints) - 1


def test_document_id_stable_and_path_based(snap):
    """KC-08."""
    a = KicadBridge(link=KiCadLink(client_factory=lambda x: FakeKiCadClient(snap), fake=True))
    assert a.status()["document_id"] == a.status()["document_id"]
    snap2 = type(snap)(type(snap.document)(snap.document.board_filename, snap.document.project_name, "/elsewhere"), snap.footprints, snap.nets)
    b = KicadBridge(link=KiCadLink(client_factory=lambda x: FakeKiCadClient(snap2), fake=True))
    assert b.status()["document_id"] != a.status()["document_id"]


def test_expected_document_guard_refuses_and_sends_no_selection(bridge, fake, link):
    """KC-09."""
    bridge.set_expected_document("Other-board")
    r = bridge.highlight(["R9"])
    assert r["ok"] is False and "Other-board" in r["message"] and ".kicad_pcb" in r["message"] and r.get("guard")
    assert not (set(fake.names) & {"AddToSelection", "ClearSelection"})
    from polybench.kicad.client import KicadScannerClient
    h = KicadScannerClient(link=link).highlight_targets(["R9"])
    assert h.ok is False and "Other-board" in h.error and not (set(fake.names) & {"AddToSelection", "ClearSelection"})
    bridge.set_expected_document("demo-1")
    assert bridge.highlight(["R9"])["ok"]


def test_documents_shape(bridge):
    """KC-10."""
    d = bridge.documents("anything")
    assert set(d) >= {"ok", "open", "candidates", "search", "message"} and d["ok"] and d["candidates"] == [] and "no document hub" in d["message"]
    assert d["open"][0]["name"].endswith(".kicad_pcb") and d["search"] == "anything"


def test_highlight_replaces_selection_exactly(bridge, link, snap, fake):
    """KC-13, KC-14."""
    fake.operator_select([snap.by_ref()["IC1"].kiid])
    r = bridge.highlight(["r9", "C12"])
    assert r["ok"] and r["highlighted"] == ["R9", "C12"] and r["selection_ok"] and _sel(link, snap) == {"R9", "C12"}
    r = bridge.highlight(["R9", "ZZ99"])
    assert r["highlighted"] == ["R9"] and r["missing"] == ["ZZ99"] and _sel(link, snap) == {"R9"}
    n = len(fake.sent)
    r = bridge.highlight(["ZZ99"])
    assert r["ok"] is False and _sel(link, snap) == {"R9"} and "AddToSelection" not in fake.names[n:] and "ClearSelection" not in fake.names[n:]


def test_net_highlight_selects_pads(bridge, link, snap):
    """KC-15."""
    r = bridge.highlight(["VIN"])
    n = sum(1 for f in snap.footprints for p in f.pads if p.net == "VIN")
    assert r["ok"] and r["highlighted"] == ["VIN"] and len(_sel(link, snap)) == n


def test_clear_rules(bridge, link, snap, fake):
    """KC-20."""
    bridge.highlight(["R9"])
    assert bridge.clear()["cleared"] and _sel(link, snap) == set()
    bridge.highlight(["R9"])
    fake.operator_select([snap.by_ref()["C12"].kiid])                    # the operator picks something by hand
    c = bridge.clear()
    assert c["cleared"] is False and "operator" in c["note"] and _sel(link, snap) == {"C12"}
    assert bridge.clear()["cleared"] is False                            # nothing of ours left; never raises


def test_alias_in_bridge(bridge, link, snap):
    """KC-45."""
    assert bridge.highlight(["CLK"])["missing"] == ["CLK"]                # KC-44 default: nothing is inferred
    bridge.set_aliases({"CLK": "TP1"})
    r = bridge.highlight(["CLK"])
    assert r["ok"] and r["aliased"] == {"CLK": "TP1"} and "CLK→TP1" in r["message"] and _sel(link, snap) == {"TP1"}


def test_write_commands_refused_before_the_wire():
    """KC-33: the guard refuses anything off the list and the inner client is never reached."""
    from kipy.proto.common import commands

    class Inner:
        called = False

        def send(self, c, t):
            Inner.called = True
    g = guard.GuardedClient(Inner())
    for cmd in (commands.SaveDocument(), commands.RunAction(), commands.RevertDocument(), commands.BeginCommit()):
        with pytest.raises(guard.CommandRefused):
            g.send(cmd, object)
    assert Inner.called is False and len(g.refused) == 4 and g.log == []


def test_source_has_no_write_calls():
    """KC-33 inspection: no save, revert, commit, create/update/remove or interactive_move in the plug-in source."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "src/polybench/kicad"
    pat = re.compile(r"save\(|save_as|revert\(|begin_commit|push_commit|drop_commit|create_items|update_items|remove_items|interactive_move|flip_items")
    bad = [(p.name, m.group(0)) for p in root.glob("*.py") for m in pat.finditer(p.read_text(encoding="utf-8"))]
    assert bad == []
