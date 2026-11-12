"""Schematic, board and 3D PCB of ONE design are one candidate; different designs give a picker; a manual pin wins.

Fixture: the open-document list read from a live Fusion on 16 Oct 2026 (read-only): three documents all named
"DEMO-1", same project and folder, one lineage each. The list carries no editor type. No network here:
FusionClient._read_tool and _run are replaced, so nothing can reach (or switch a tab in) a real Fusion.
"""
from __future__ import annotations

import pytest

from polybench.assembly_scanner.fusion_client import FusionClient, FusionError

PROJ = "a.YnVzaW5lc3M6cG9ydDU3IzIwMjMwNjI4NjUxNTAyOTgx"
SCH, PCB3D, BRD = ("urn:adsk.wipprod:dm.lineage:buTcs4B0QCinECaxtKsg_A",
                   "urn:adsk.wipprod:dm.lineage:4nBIJrJzSr6DKlQ9yAu4pw",
                   "urn:adsk.wipprod:dm.lineage:dBKKJhjrQ3G1qSa7f5-Ieg")
VIBDOC = "urn:adsk.wipprod:dm.lineage:vibvibvib"
PARTS_A = [f"R{i}" for i in range(1, 21)] + [f"C{i}" for i in range(1, 11)]
VIB_PARTS = [f"R{i}" for i in range(1, 6)] + [f"U{i}" for i in range(1, 4)]


def _doc(i, name="DEMO-1", active=None):
    d = {"id": i, "isModified": False, "name": name, "parentFolder": "DEMO", "parentProject": "Example",
         "parentProjectId": PROJ}
    if i == active:
        d["isActive"] = True
    return d


class FakeFusion:
    """What the read tools answer; `board_ids` are the documents that are boards, parts by id."""

    def __init__(self, docs, parts_by_board, active):
        self.docs, self.parts, self.active = docs, parts_by_board, active
        self.run_calls = []

    def read(self, name, args):
        if name == "fusion_mcp_read":
            return {"results": [_doc(d["id"], d["name"], self.active) for d in self.docs]}
        if args.get("entity_type") == "electronics.Board":
            return {"items": [{"name": "x.brd"}] if self.active in self.parts else []}
        names = self.parts.get(self.active, [])
        return {"items": [{"name": n} for n in names]}


def make_client(fake, probe=False):
    c = FusionClient(url="http://127.0.0.1:9/mcp")   # never contacted
    c._read_tool = fake.read
    c.probe_boards = probe

    def run(op, designators=None, target=None, clear_keys=None, extra=None):
        fake.run_calls.append((op, target))
        if op == "list":
            return {"boards": [{"key": k, "designators": v} for k, v in fake.parts.items()]}
        return {"op": op}
    c._run = run
    return c


def three_docs():
    return [_doc(SCH), _doc(PCB3D), _doc(BRD)]


def test_schematic_board_and_pcb_of_one_design_are_one_candidate():
    fake = FakeFusion(three_docs(), {BRD: PARTS_A}, active=BRD)
    c = make_client(fake)
    c.list_boards()                                  # the board is the active tab: learned
    fake.active = PCB3D                              # Pat clicks to another tab of the same design
    best, cov, runner = c.auto_select_for(set(PARTS_A))
    assert runner is None, "one design must not produce a runner-up"
    assert best["confident"] and best["key"] == BRD and cov == 1.0
    assert c.target["key"] == BRD
    assert fake.run_calls == [], "no tab switch (activate) while choosing"


def test_cold_start_on_non_board_tab_reads_board_by_probe_without_switching():
    fake = FakeFusion(three_docs(), {BRD: PARTS_A}, active=PCB3D)
    c = make_client(fake, probe=True)
    best, cov, runner = c.auto_select_for(set(PARTS_A))
    assert best["confident"] and best["key"] == BRD and cov == 1.0 and runner is None
    assert [op for op, _ in fake.run_calls] == ["list"], "read-only probe only, never activate"


def test_cold_start_without_probe_offers_use_board_document_and_does_not_switch():
    fake = FakeFusion(three_docs(), {BRD: PARTS_A}, active=PCB3D)
    c = make_client(fake, probe=False)
    best, _, _ = c.auto_select_for(set(PARTS_A))
    assert not best["confident"] and c.target is None
    ids = {o["key"] for o in best["needs_board_doc"]}
    assert BRD in ids and PCB3D not in ids, "the active tab is known not to be a board"
    assert fake.run_calls == [], "must not switch tabs by itself"


def test_two_designs_give_a_picker_when_unread():
    docs = three_docs() + [_doc(VIBDOC, "DEMO-2")]
    fake = FakeFusion(docs, {}, active=SCH)
    c = make_client(fake)
    best, _, runner = c.auto_select_for(set(PARTS_A))
    assert runner is not None and not best["confident"] and c.target is None
    names = {o["name"] for o in best["options"]}
    assert names == {"DEMO-1", "DEMO-2"}
    assert "margin" in best["reason"]


def test_two_designs_clear_winner_needs_no_picker():
    docs = three_docs() + [_doc(VIBDOC, "DEMO-2")]
    fake = FakeFusion(docs, {BRD: PARTS_A, VIBDOC: VIB_PARTS}, active=BRD)
    c = make_client(fake, probe=True)
    best, _, runner = c.auto_select_for(set(PARTS_A))
    assert best["confident"] and best["key"] == BRD and runner is not None


def test_manual_pin_wins_and_is_never_auto_changed():
    docs = three_docs() + [_doc(VIBDOC, "DEMO-2")]
    fake = FakeFusion(docs, {BRD: PARTS_A, VIBDOC: VIB_PARTS}, active=BRD)
    c = make_client(fake, probe=True)
    c.auto_select_for(set(PARTS_A))
    c.select_board(VIBDOC)                           # the operator pins the other design
    fake.active = VIBDOC
    assert c.mode == "manual" and c.target["key"] == VIBDOC
    c.auto_select_for(set(PARTS_A))             # a later auto pass would prefer board A
    c.list_boards()
    assert c.mode == "manual" and c.target["key"] == VIBDOC


def test_pinning_a_schematic_document_pins_the_boards_of_that_design():
    fake = FakeFusion(three_docs(), {BRD: PARTS_A}, active=BRD)
    c = make_client(fake, probe=True)
    c.list_boards()
    fake.active = SCH
    c.list_boards()                                  # SCH now known not to be a board
    fake.active = BRD
    got = c.select_board(SCH)
    assert got["key"] == BRD


def test_service_card_gets_a_picker_for_real_ambiguity():
    """The test card's error carries `pick` options (what the Probe card renders as one-click buttons)."""
    from types import SimpleNamespace
    from polybench.assembly_scanner.service import AssemblyService
    docs = three_docs() + [_doc(VIBDOC, "DEMO-2")]
    c = make_client(FakeFusion(docs, {}, active=SCH))
    info = c.auto_select_for(set(PARTS_A))
    me = SimpleNamespace(pick_err=None, fusion_status={"connected": True}, fusion=c, bom=object(), auto_err=None,
                         auto_info=info, _poll_circuit_open=False)
    ctx = AssemblyService.board_ctx(me)
    assert not ctx["_ok"] and "pick one below" in ctx["_reason"]
    assert ctx["_pick"]["kind"] == "ambiguous" and len(ctx["_pick"]["options"]) >= 2
