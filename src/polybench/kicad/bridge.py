"""EcadBridge `kicad`: step 1 (status, documents, designators), the expected-document guard and the plain designator highlight.

Read-only toward KiCad. Everything goes through the process-wide KiCadLink (one connection, one worker thread, hard budgets). A
failure is returned as a status, never raised: the bench UI must not hang or crash because KiCad is closed (KC-24..KC-27).
"""
from __future__ import annotations

import os

from . import ops, select
from .guard import SELECTION_WRITES
from .link import BUDGET_READ, BUDGET_SELECT, BUDGET_STATUS, KiCadLink, LinkError, shared_link
from .model import BoardSnapshot

DEFAULT_EXCLUDE = "hash,bom,not_in_schematic"       # TBR-7 (PM default): `#` references, exclude-from-BOM, not-in-schematic


def exclusion(spec: str | None):
    """Predicate over FootprintInfo for the designators() rule. 'none' excludes nothing."""
    spec = (os.environ.get("POLYBENCH_KICAD_EXCLUDE", DEFAULT_EXCLUDE) if spec is None else spec).lower()
    parts = {p.strip() for p in spec.split(",") if p.strip()} - {"none"}

    def excluded(f) -> bool:
        return (("hash" in parts and f.ref.startswith("#")) or ("bom" in parts and f.exclude_from_bom)
                or ("not_in_schematic" in parts and f.not_in_schematic) or not f.ref)
    return excluded


def simulated_link(designators=()) -> KiCadLink:
    from .fakeclient import FakeKiCadClient
    from .synth import demo_board
    fake = FakeKiCadClient(demo_board(designators), with_schematic=False)
    link = KiCadLink(client_factory=lambda addr: fake, fake=True)
    link.fake_server = fake
    return link


class KicadBridge:
    name = "kicad"
    program = "KiCad"

    def __init__(self, link: KiCadLink | None = None, simulate: bool | None = None, expected_document: str | None = None,
                 aliases: dict | None = None, exclude: str | None = None, designators=()):
        if simulate is None:
            simulate = os.environ.get("POLYBENCH_SIMULATE") == "1"
        self.simulate = simulate
        self.link = link or (simulated_link(designators) if simulate else shared_link())
        self.link.expected_document = expected_document or os.environ.get("POLYBENCH_KICAD_DOC") or self.link.expected_document
        self.aliases = select.norm_aliases(aliases)
        self._excluded = exclusion(exclude)
        self.last_selection: list[str] = []

    # -- configuration
    def set_expected_document(self, substring: str | None) -> None:
        self.link.expected_document = substring or None

    def set_aliases(self, table) -> None:
        self.aliases = select.norm_aliases(table)

    def configure_session(self, bom_designators, aliases=None) -> None:
        """Called by Session: the protocol's optional `design.ecad_aliases` table (KC-45), and for the simulator the BOM's parts."""
        self.set_aliases(aliases)
        if self.simulate:
            self.link = simulated_link(bom_designators)

    @property
    def prog(self) -> str:
        return "KiCad (simulated)" if self.simulate else "KiCad"

    # -- helpers
    def _fail(self, e: LinkError, **extra) -> dict:
        return {"ok": False, "highlighted": [], "missing": [], "message": e.detail, "busy": e.busy, "state": e.state, **extra}

    def _refs(self, snap: BoardSnapshot) -> tuple[list[str], list[str]]:
        keep = sorted(f.ref for f in snap.footprints if not self._excluded(f))
        out = sorted(f.ref for f in snap.footprints if self._excluded(f) and f.ref)
        return keep, out

    # -- EcadBridge
    def status(self) -> dict:
        base = {"connected": False, "program": self.prog, "document": None, "document_id": None, "editor": None,
                "version": "", "elements": None, "project": None, "detail": "", "simulated": self.simulate}

        flag = os.environ.get("POLYBENCH_SIM_ECAD_DOWN") if self.simulate else None        # test aid, as for the simulated Fusion: down while the file exists
        if flag and os.path.exists(flag):
            return {**base, "detail": "KiCad is not running or its API server is off (simulated)", "state": "not_running"}

        def job(ctx):
            full, note = self.link.version_note(ctx)
            docs = ctx.documents()
            if not docs:
                return full, note, None, None
            snap, _b = ops.snapshot(ctx)
            return full, note, snap, docs[0]
        try:
            full, note, snap, _doc = self.link.run(job, BUDGET_STATUS)
        except LinkError as e:
            return {**base, "detail": e.detail, "state": e.state, "busy": e.busy}
        base.update(connected=True, version=full)
        if snap is None:
            return {**base, "editor": "none", "detail": "KiCad is running but no board is open in the PCB editor"
                    + (f". {note}" if note else "")}
        keep, out = self._refs(snap)
        d = snap.document
        detail = f"{len(keep)} footprints on the open board" + (f" ({len(out)} excluded)" if out else "")
        base.update(document=d.board_filename, document_id=d.document_id, editor="board", elements=len(keep),
                    project={"name": d.project_name, "path": d.project_path}, excluded=out,
                    detail=detail + (f". {note}" if note else ""))
        return base

    def documents(self, search: str | None = None) -> dict:
        res: dict = {"ok": False, "open": [], "candidates": [], "search": search or "", "message": ""}

        def job(ctx):
            pcb = ctx.documents()
            from kipy.proto.common.types import DocumentType
            try:
                sch = list(ctx.kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC))
            except Exception:
                sch = []
            return pcb, sch
        try:
            pcb, sch = self.link.run(job, BUDGET_READ)
        except LinkError as e:
            res["message"] = e.detail
            return res
        from .reader import document_info
        for d in pcb:
            i = document_info(d)
            res["open"].append({"name": i.board_filename, "active": True, "modified": None, "id": i.document_id, "open": True})
        res["schematic_open"] = [getattr(s, "board_filename", "") for s in sch]
        res["message"] = "KiCad has no document hub: only the board open in the PCB editor is listed"
        res["ok"] = True
        return res

    def designators(self) -> list[str] | None:
        try:
            snap = self.link.run(lambda ctx: ops.snapshot(ctx)[0], BUDGET_READ)
        except LinkError:
            return None
        keep, _out = self._refs(snap)
        return keep or None

    def highlight(self, designators: list[str]) -> dict:
        names = select.clean(designators)
        if not names:
            return {"ok": False, "highlighted": [], "missing": [], "message": "nothing to show", "command": None}

        def job(ctx):
            snap, board = ops.snapshot(ctx)
            ops.check_expected(self.link, snap.document)
            plan = select.plan_designators(snap, names, self.aliases)
            if not plan.ids:
                return plan, snap, None
            sel = ops.select_ids(ctx, self.link, board, plan.ids)
            sel['view'] = ops.view(ctx, self.link, plan)
            return plan, snap, sel
        try:
            plan, snap, sel = self.link.run(job, BUDGET_SELECT)
        except LinkError as e:
            return self._fail(e, guard=e.state == "guard")
        res: dict = {"ok": False, "highlighted": [], "missing": list(plan.parts_missing), "message": "", "command": None,
                     "aliased": dict(plan.aliased), "document": snap.document.board_filename, "editor": "board"}
        if sel is None:
            res["message"] = f"None of {names} is in the open document. Nothing was sent"
            return res
        labels = ops.labels_of(snap)
        res.update(ok=True, highlighted=plan.parts + plan.nets, selected=[labels.get(i, i) for _t, i in
                                                                          [(k, v) for k, v in sel["readback"]]] if False else
                   [labels.get(k, k) for _t, k in sel["readback"]], selection_ok=sel["ok"], view=sel.get("view", {}),
                   message="Highlighted" + (f"; aliases: {plan.alias_note}" if plan.aliased else "")
                   + (f"; not in the document: {res['missing']}" if res["missing"] else ""))
        self.last_selection = list(plan.ids)
        return res

    def clear(self) -> dict:
        """Clear the selection only if it is still the one made here (KC-20). Never raises."""
        def job(ctx):
            _snap_board = ctx.board()
            return ops.clear_if_ours(ctx, self.link, _snap_board[0])
        try:
            r = self.link.run(job, BUDGET_SELECT)
        except LinkError as e:
            return {"cleared": False, "note": e.detail}
        self.last_selection = []
        return r

    def selftest(self) -> bool:
        """No KiCad. The resolution rules always run (pure python). When kicad-python is installed, the whole path also runs: the
        fake API server, the real kipy classes, highlight, read-back, clear. Without the extra only the first part runs."""
        try:
            from .synth import demo_board
            snap = demo_board(["R1"])
            plan = select.plan_designators(snap, ["R9", "ZZ99", "VIN", "R9;X"])
            t = select.plan_targets(snap, ["R9"], ["IC1.10", "IC1.99"], ["3V3_A", "SIG_P"])
            a = select.plan_targets(snap, ["CLK"], aliases={"CLK": "TP1"})
            rules = (plan.parts == ["R9"] and plan.nets == ["VIN"] and plan.parts_missing == ["ZZ99", "R9;X"]
                     and t.pins == ["IC1.10"] and t.pins_missing == ["IC1.99"] and t.nets == ["3V3_A"] and t.nets_missing == ["SIG_P"]
                     and a.parts == ["TP1"] and a.aliased == {"CLK": "TP1"})
            if not rules:
                return False
            try:
                import kipy  # noqa: F401
            except ImportError:
                return True
            b = KicadBridge(link=simulated_link(["R1"]), simulate=True)
            r = b.highlight(["R9", "ZZ99"])
            ok = r["ok"] and r["highlighted"] == ["R9"] and r["missing"] == ["ZZ99"] and r["selection_ok"]
            c = b.clear()
            return bool(ok and c.get("cleared") and b.link.fake_server.selection == [])
        except Exception:
            return False
