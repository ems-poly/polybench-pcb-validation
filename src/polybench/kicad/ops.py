"""The few operations on an open board that both seams need. Each takes a link `Ctx` (inside KiCadLink.run), so it runs on the
worker thread under the call's deadline. Reads, then (for select/clear) the three selection commands: nothing else.
"""
from __future__ import annotations

from types import SimpleNamespace

from . import reader
from .link import GuardRefused, KiCadLink, LinkError
from .model import BoardSnapshot, DocumentInfo
from .select import Plan


def snapshot(ctx, with_shapes: bool = False) -> tuple[BoardSnapshot, object]:
    board, doc = ctx.board()
    ctx.check()
    snap = reader.read_snapshot(ctx.kicad, board, reader.document_info(doc), with_shapes)
    return snap, board


def check_expected(link: KiCadLink, doc: DocumentInfo) -> None:
    exp = link.expected_document
    if exp and exp.lower() not in (doc.board_filename or "").lower():
        raise GuardRefused(f"The open KiCad board is {doc.board_filename!r}, expected one containing {exp!r}. "
                           "Open the right board in KiCad. Nothing was sent")


def _items(ids):
    from kipy.proto.common.types.base_types_pb2 import KIID
    return [SimpleNamespace(id=KIID(value=i)) for i in ids]


def select_ids(ctx, link: KiCadLink, board, ids: list[str]) -> dict:
    """Replace the selection with exactly `ids`. Returns {readback: [(kind, kiid)], ok: bool}. The caller already refused the guard."""
    ctx.check()
    board.clear_selection()
    ctx.check()
    board.add_to_selection(_items(ids))
    ctx.check()
    back = reader.read_selection(board)
    link.owned_ids = list(ids)
    return {"readback": back, "ok": {k for _t, k in back} == set(ids)}


def run_action(ctx, name: str) -> str:
    """One view action through RunAction. Returns KiCad's status name (RAS_OK, RAS_INVALID, ...). The guard has already checked the name."""
    ctx.check()
    r = ctx.kicad.run_action(name)
    return r.DESCRIPTOR.fields_by_name["status"].enum_type.values_by_number[r.status].name


def view(ctx, link: KiCadLink, plan: Plan) -> dict:
    """After a selection: zoom to it, highlight the nets of its pads, and (opt-in) push it to the schematic. Each is a KiCad view
    action, none edits anything. A failed or busy action is reported and never fails the selection. Returns {action: status}."""
    out: dict[str, str] = {}

    def go(name):
        try:
            out[name] = run_action(ctx, name)
        except Exception as e:      # noqa: BLE001 - the selection stands
            out[name] = f"{type(e).__name__}: {e}"[:120]
    if "highlight" in link.actions:
        if plan.pins or plan.nets:
            go("pcbnew.EditorControl.highlightNetSelection")
            link.highlight_on = out.get("pcbnew.EditorControl.highlightNetSelection") == "RAS_OK"
        elif link.highlight_on:
            go("pcbnew.EditorControl.clearHighlight")
            link.highlight_on = False
    if "zoom" in link.actions and plan.ids:
        go("common.Control.zoomFitSelection" if len(plan.ids) > 1 else "common.Control.centerSelection")
    if "schematic" in link.actions and plan.ids:
        go("pcbnew.InteractiveSelection.SelectOnSchematic")
    return out


def clear_highlight(ctx, link: KiCadLink) -> None:
    if link.highlight_on and "highlight" in link.actions:
        try:
            run_action(ctx, "pcbnew.EditorControl.clearHighlight")
        except Exception:      # noqa: BLE001
            pass
        link.highlight_on = False


def clear_if_ours(ctx, link: KiCadLink, board) -> dict:
    """KC-20: clear the selection only when it is still the one this process made. Otherwise leave it and say so."""
    cur = {k for _t, k in reader.read_selection(board)}
    ours = set(link.owned_ids)
    if not cur:
        link.owned_ids = []
        clear_highlight(ctx, link)
        return {"cleared": False, "note": "nothing was selected"}
    if ours and cur == ours:
        ctx.check()
        board.clear_selection()
        link.owned_ids = []
        clear_highlight(ctx, link)
        return {"cleared": True, "note": ""}
    return {"cleared": False,
            "note": "the selection in KiCad is not the one Polybench made (the operator changed it); it was left alone"}


def labels_of(snap: BoardSnapshot) -> dict[str, str]:
    """KIID -> 'R9' / 'IC1.10' for every footprint and pad of the board (read-back reporting)."""
    out: dict[str, str] = {}
    for f in snap.footprints:
        out[f.kiid] = f.ref
        for p in f.pads:
            out[p.kiid] = f"{f.ref}.{p.number}"
    return out


def plan_ok(plan: Plan) -> bool:
    return bool(plan.ids)
