"""Test highlight: parts, pins and nets through the vendored scanner Fusion client (stubbed transport), and the simulator."""
import ast
import json
import re

import pytest

from polybench.assembly_scanner.fusion_client import FusionClient
from polybench.assembly_scanner.sim import SimulatedFusionClient

ELEMENTS = [{"name": n, "object_id": i} for i, n in enumerate(["IC1", "IC2", "R3", "C2", "TP1"], 100)]
EID = {e["name"]: e["object_id"] for e in ELEMENTS}
# placed pads: (contact_object_id, pad name, net)
PADS = [(1, "1", "VIN"), (2, "2", "ADC_RST"), (3, "3", "GND"), (4, "P$1", "VIN"), (5, "1", "VIN"), (6, "2", "GND")]
REFS = [(EID["IC1"], 2), (EID["IC1"], 3), (EID["TP1"], 4), (EID["R3"], 5), (EID["R3"], 6)]
SIGNALS = [{"name": "VIN", "object_id": 900}, {"name": "GND", "object_id": 901}, {"name": "ADC_RST", "object_id": 902}]
SIG_ID = {s["name"]: s["object_id"] for s in SIGNALS}
NET_OF = {c: n for c, _p, n in PADS}


class Fake(FusionClient):
    """FusionClient with Fusion's MCP tools replaced by a small in-memory board. `reject` = the script modes Fusion refuses."""

    def __init__(self, reject=()):
        super().__init__(url="http://none.invalid/mcp")
        self.reject = set(reject)
        self.runs = []

    def _call_tool(self, name, args):
        if name == "fusion_mcp_read":
            return json.dumps({"results": [{"id": "doc1", "name": "demo", "isActive": True, "isModified": False}]})
        if name == "fusion_mcp_electronics_read":
            et, o = args["entity_type"], args.get("object") or {}
            flt = {f["property"]: f["value"] for f in o.get("filters", [])}
            if et == "electronics.Board":
                return json.dumps({"items": [{"name": "demo.brd"}]})
            if et == "electronics.Element":
                items = [e for e in ELEMENTS if "name" not in flt or e["name"] == flt["name"]]
                return json.dumps({"items": items, "pagination": {"hasMore": False}})
            if et == "electronics.Signal":
                return json.dumps({"items": SIGNALS, "pagination": {"hasMore": False}})
            if et == "electronics.Smd":
                return json.dumps({"items": [{"contact_object_id": c, "name": p, "signal": n} for c, p, n in PADS], "pagination": {"hasMore": False}})
            if et == "electronics.Pad":
                return json.dumps({"items": [], "pagination": {"hasMore": False}})
            if et == "electronics.ContactRef":
                rows = [{"element_object_id": e, "contact_object_id": c, "signal_object_id": SIG_ID[NET_OF[c]]} for e, c in REFS]
                if "element_object_id" in flt:
                    rows = [r for r in rows if r["element_object_id"] == flt["element_object_id"]]
                if "signal_object_id" in flt:
                    rows = [r for r in rows if r["signal_object_id"] == flt["signal_object_id"]]
                return json.dumps({"items": rows, "pagination": {"hasMore": False}})
            raise AssertionError(et)
        if name == "fusion_mcp_execute":
            src = args["object"]["script"]
            raw = re.search(r"json\.loads\((.*)\)\n", src).group(1)
            a = json.loads(ast.literal_eval(raw))
            self.runs.append(a)
            if a.get("mode") in self.reject:
                return json.dumps({"success": False, "error": f"SELECT_REJECTED: no {a['mode']}"})
            return json.dumps({"success": True, "message": ""})
        raise AssertionError(name)


def test_parts_only_uses_the_verified_selection():
    f = Fake()
    r = f.highlight_targets(["R3", "TP1", "Q9"])
    assert r.ok and r.highlighted == ("R3", "TP1") and r.not_on_board == ("Q9",) and r.net_mode == ""
    assert f.runs[-1]["op"] == "highlight" and f.runs[-1].get("mode") is None


def test_nets_are_selected_as_net_objects_when_fusion_takes_them():
    f = Fake()
    r = f.highlight_targets(["IC1"], [], ["VIN", "GND", "NOPE"])
    assert r.ok and r.net_mode == "nets" and r.nets == ("VIN", "GND") and r.nets_missing == ("NOPE",)
    assert f.runs[-1]["mode"] == "nets" and f.runs[-1]["nets"] == ["VIN", "GND"] and f.runs[-1]["designators"] == ["IC1"]
    assert f.net_mode == "nets"


def test_nets_fall_back_to_pin_references_then_to_parts():
    f = Fake(reject={"nets"})
    r = f.highlight_targets(["IC1"], [], ["VIN"])
    assert r.ok and r.net_mode == "pinrefs" and [x["mode"] for x in f.runs] == ["nets", "pinrefs"] and f.net_mode == "pinrefs"
    assert "nets selection refused" in r.note
    g = Fake(reject={"nets", "pinrefs"})
    r2 = g.highlight_targets(["IC1"], [], ["VIN"])
    assert r2.ok and r2.net_mode == "parts"
    assert set(r2.highlighted) == {"IC1", "TP1", "R3"}                     # IC1 plus every part with a pin on VIN
    assert "cannot select a net here" in r2.note
    # the remembered mode is tried first next time
    f.runs.clear()
    f.highlight_targets(["R3"], [], ["GND"])
    assert [x["mode"] for x in f.runs] == ["pinrefs"]


def test_pins_resolve_to_their_net_and_select_that_part_s_pin_references():
    f = Fake()
    r = f.highlight_targets(["IC1"], ["IC1.2", "IC1.99", "TP1.P$1"], [])
    assert r.ok and r.pins == ("IC1.2", "TP1.P$1") and r.pins_missing == ("IC1.99",)
    run = f.runs[-1]
    assert run["mode"] == "pinrefs" and run["pinrefs"] == [["IC1", "ADC_RST"], ["TP1", "VIN"]] and run["designators"] == ["IC1", "TP1"]


def test_unreachable_fusion_is_a_result_not_an_exception():
    class Down(Fake):
        def _call_tool(self, name, args):
            from polybench.assembly_scanner.fusion_client import FusionError
            raise FusionError("cannot reach Fusion MCP")
    r = Down().highlight_targets(["R3"], [], ["VIN"])
    assert not r.ok or r.note   # never raises


def test_simulated_client_records_what_would_be_selected():
    s = SimulatedFusionClient(["IC1", "R3", "TP1"], netlist={"VIN": ["IC1.1", "TP1.P$1"], "GND": ["R3.2"]})
    r = s.highlight_targets(["R3"], ["IC1.1", "IC1.9"], ["VIN", "XX"])
    assert r.highlighted == ("R3", "IC1") and r.pins == ("IC1.1",) and r.pins_missing == ("IC1.9",) and r.nets == ("VIN",) and r.nets_missing == ("XX",)
    assert r.net_mode == "nets" and s.history[-1][0] == "targets"
    s2 = SimulatedFusionClient(["IC1", "R3", "TP1"], netlist={"VIN": ["IC1.1", "TP1.P$1"]}, net_support=False, pinref_support=False)
    r2 = s2.highlight_targets([], [], ["VIN"])
    assert r2.net_mode == "parts" and set(r2.highlighted) == {"IC1", "TP1"} and "cannot select a net" in r2.note
