"""KicadScannerClient: the interface `AssemblyService` and `Session` use on the scanner's `FusionClient`, over KiCad's API.

Test highlight (parts, pins, nets), the Assembly aid, board choice, the polarity card's footprints and the voice "show it" all
go through here (KC-02). Every method blocks and is called from the service's worker thread; none raises for KiCad problems:
errors come back in the result (HighlightResult.error, or `error` in a dict), as FusionClient does.

Board choice follows the Fusion rules unchanged (KC-12): auto mode scores every reachable board (one per KiCad instance socket) by
coverage of the BOM's placeable designators; coverage >= AUTO_MIN_COVERAGE and margin >= AUTO_MIN_MARGIN make a confident pick;
a manual pin is never overridden; the target is never swapped silently.
"""
from __future__ import annotations

import os
import time

from polybench.assembly_scanner.fusion_client import AUTO_MIN_COVERAGE, AUTO_MIN_MARGIN
from polybench.assembly_scanner.models import HighlightResult

from . import fpconv, ops, select
from .bridge import exclusion
from .link import BUDGET_READ, BUDGET_SELECT, BUDGET_STATUS, KiCadLink, LinkError, shared_link


class KicadScannerClient:
    program = "KiCad"

    def __init__(self, link: KiCadLink | None = None, aliases: dict | None = None, exclude: str | None = None,
                 min_coverage: float = AUTO_MIN_COVERAGE, min_margin: float = AUTO_MIN_MARGIN, simulate: bool = False,
                 designators=(), bridge=None):
        if simulate and link is None:
            from .bridge import simulated_link
            link = simulated_link(designators)
        self.link = link or shared_link()
        self.simulate = simulate
        self._aliases = select.norm_aliases(aliases)
        self._bridge = bridge          # when given, its alias table is the one used (a plan reload updates both)
        self._excluded = exclusion(exclude)
        self.min_coverage, self.min_margin = min_coverage, min_margin
        self.last_info: dict = {}
        self.net_mode: str | None = None
        self.probe_boards = True
        self._pinned: dict | None = None
        self._auto: dict | None = None

    @property
    def last_latency_ms(self):
        return self.link.last_latency_ms

    @property
    def aliases(self) -> dict:
        return self._bridge.aliases if self._bridge is not None else self._aliases

    def set_aliases(self, table) -> None:
        self._aliases = select.norm_aliases(table)

    # ------------------------------------------------------------------ board list and choice
    @property
    def mode(self) -> str:
        return "manual" if self._pinned else "auto"

    @property
    def target(self) -> dict | None:
        return self._pinned or self._auto

    def _address(self) -> str | None:
        t = self.target
        return (t or {}).get("socket")

    def _entry(self, snap, address: str | None) -> dict:
        d = snap.document
        refs = sorted(f.ref for f in snap.footprints if not self._excluded(f))
        key = d.document_id
        return {"key": key, "lineage": key, "version": None, "name": d.board_filename, "ui_name": d.board_filename,
                "design": (d.project_path, "", d.project_name.casefold()), "editor": "board", "has_board": True,
                "has_schematic": None, "is_active": True, "is_modified": None, "designator_count": len(refs),
                "designators": refs, "socket": address}

    def _read_board_at(self, address: str | None, budget: float):
        snap = self.link.run(lambda ctx: ops.snapshot(ctx)[0], budget, address)
        return snap

    def list_boards(self) -> list[dict]:
        """One entry per reachable KiCad instance. Raises LinkError (the service shows it) when none can be reached."""
        if self.link.fake:
            return [self._entry(self._read_board_at(None, BUDGET_STATUS), None)]
        cands = [c for c in self.link.socket_candidates()]
        out, last = [], None
        for c in cands:
            addr = c if "://" in c else "ipc://" + c
            try:
                if not os.path.exists(addr[len("ipc://"):]):
                    continue
                out.append(self._entry(self._read_board_at(addr, 3.0), addr))
            except LinkError as e:
                if e.state != "no_board":
                    last = e
        if not out:
            if last is not None:
                raise last
            self.link.preflight()          # raises the precise reason (no_kipy / api_off / not_running)
            raise LinkError("no_board", "KiCad is running but no board is open in the PCB editor")
        return out

    def select_board(self, doc):
        if doc is None:
            self._pinned = None
            return None
        boards = self.list_boards()
        hit = [b for b in boards if b["key"] == doc] or [b for b in boards if b["name"].casefold() == str(doc).casefold()]
        if len(hit) != 1:
            names = ", ".join(f"{b['name']} [{b['key']}]" for b in boards) or "none"
            raise LinkError("error", ("no open board matches" if not hit else "several open boards match") + f" {doc!r}; open boards: {names}")
        self._pinned = _strip(hit[0])
        return self._pinned

    def auto_select_for(self, designators, pin: bool = True, boards: list[dict] | None = None):
        want = {str(d).strip().upper() for d in designators if str(d).strip()}
        boards = self.list_boards() if boards is None else boards
        scored = []
        for b in boards:
            have = {str(x).upper() for x in b.get("designators", [])}
            m = want & have
            s = _strip(b)
            s.update(coverage=(len(m) / len(want)) if want else 0.0, precision=(len(m) / len(have)) if have else 0.0,
                     matched=len(m), missing=sorted(want - have), read=bool(have),
                     design_docs=[_option(b)], options=[_option(b)])
            scored.append(s)
        scored.sort(key=lambda s: (s["coverage"], s["precision"], s["is_active"]), reverse=True)
        if not scored:
            if pin:
                self._auto = None
            return None, 0.0, None
        best = scored[0]
        runner = scored[1] if len(scored) > 1 else None
        reasons = []
        if not want:
            reasons.append("the BOM has no placeable designators")
        elif best["read"] or runner:
            if best["coverage"] < self.min_coverage:
                reasons.append(f"best coverage {best['coverage']:.0%} < {self.min_coverage:.0%}")
        if runner and best["coverage"] - runner["coverage"] < self.min_margin:
            reasons.append(f"{runner['name']} covers {runner['coverage']:.0%} too (margin < {self.min_margin:.0%})")
        best["confident"] = not reasons
        best["reason"] = "; ".join(reasons) or "ok"
        if runner:
            runner["confident"], runner["reason"] = False, "runner-up"
            best["options"] = [o for s in scored for o in s["options"]]
        if pin:
            self._auto = best if best["confident"] else None
        return best, best["coverage"], runner

    # ------------------------------------------------------------------ public API
    def status(self) -> dict:
        try:
            boards = self.list_boards()
        except LinkError as e:
            return {"connected": False, "doc_name": None, "is_electronics": False, "mode": self.mode, "board": self.target,
                    "boards": None, "open_boards": [], "error": e.detail, "busy": e.busy}
        err = None
        tgt = (self.target or {}).get("key")
        if tgt:
            cur = [b for b in boards if b["key"] == tgt]
            board = _strip(cur[0]) if cur else None
            if not cur:
                err = f"the selected board ({self.target['name']}) is no longer open in KiCad"
        else:
            board = _strip(boards[0]) if len(boards) == 1 else None
            if len(boards) > 1:
                err = f"{len(boards)} boards open; choose one (auto needs a loaded BOM)"
        return {"connected": True, "doc_name": board["name"] if board else None, "is_electronics": True, "mode": self.mode,
                "board": board, "boards": len(boards), "open_boards": boards,
                "active_doc": boards[0]["name"] if boards else None, "error": err}

    def board_designators(self) -> set[str] | None:
        try:
            snap = self._read_board_at(self._address(), BUDGET_READ)
        except LinkError:
            return None
        return {f.ref for f in snap.footprints if not self._excluded(f)}

    def footprints(self, designators) -> dict:
        """Placed footprints for the polarity card (KC-46). Read-only; never selects. {ok, document, key, parts, error}."""
        want = [str(d) for d in designators]

        def job(ctx):
            return ops.snapshot(ctx, with_shapes=True)[0]
        try:
            snap = self.link.run(job, BUDGET_READ, self._address())
        except LinkError as e:
            return {"ok": False, "error": e.detail}
        by = snap.by_ref()
        parts = {}
        for d in want:
            f = by.get(d.upper())
            parts[d] = fpconv.convert(f) if f else {"designator": d, "error": f"{d} is not on the open board"}
        return {"ok": True, "document": snap.document.board_filename, "key": snap.document.document_id, "parts": parts, "error": None}

    # ---- selection
    def _select(self, build_plan) -> tuple[HighlightResult | None, select.Plan | None, dict | None]:
        def job(ctx):
            snap, board = ops.snapshot(ctx)
            ops.check_expected(self.link, snap.document)
            plan = build_plan(snap)
            if not plan.ids:
                return snap, plan, None, ops.clear_if_ours(ctx, self.link, board)
            sel = ops.select_ids(ctx, self.link, board, plan.ids)
            sel['view'] = ops.view(ctx, self.link, plan)
            return snap, plan, sel, None
        try:
            snap, plan, sel, cleared = self.link.run(job, BUDGET_SELECT, self._address())
        except LinkError as e:
            return HighlightResult(ok=False, error=e.detail), None, None
        self.last_info = {"board": self._entry_light(snap), "activated": False}
        return None, plan, {"sel": sel, "cleared": cleared, "snap": snap}

    def _entry_light(self, snap):
        return _strip(self._entry(snap, self._address()))

    def _note(self, plan, extra: dict) -> str:
        bits = []
        if plan.aliased:
            bits.append(f"aliases: {plan.alias_note}")
        sel = extra["sel"]
        if sel is None:
            c = extra["cleared"] or {}
            bits.append("nothing of this request exists on the KiCad board; "
                        + ("the previous Polybench selection was cleared" if c.get("cleared") else "the selection was not changed"))
        elif not sel["ok"]:
            bits.append("KiCad's selection read-back differs from the request")
        if plan.nets:
            bits.append("nets are selected as pads" + (", the net is highlighted in KiCad" if sel and sel.get("view", {}).get("pcbnew.EditorControl.highlightNetSelection") == "RAS_OK" else ""))
        bad = {k: v for k, v in ((sel or {}).get("view") or {}).items() if v != "RAS_OK"}
        if bad:
            bits.append("view action not done: " + ", ".join(f"{k.rsplit('.', 1)[-1]} ({v})" for k, v in bad.items()))
        return "; ".join(bits)

    def highlight(self, designators) -> HighlightResult:
        want = select.clean(designators)
        err, plan, extra = self._select(lambda snap: select.plan_targets(snap, want, (), (), self.aliases))
        if err:
            return err
        return HighlightResult(ok=True, highlighted=tuple(plan.parts), not_on_board=tuple(plan.parts_missing),
                               note=self._note(plan, extra))

    def highlight_targets(self, parts, pins=(), nets=()) -> HighlightResult:
        parts, pins, nets = select.clean(parts), select.clean(pins), select.clean(nets)
        err, plan, extra = self._select(lambda snap: select.plan_targets(snap, parts, pins, nets, self.aliases))
        if err:
            return err
        self.net_mode = "pads" if plan.nets else self.net_mode
        return HighlightResult(ok=True, highlighted=tuple(plan.parts), not_on_board=tuple(plan.parts_missing),
                               pins=tuple(plan.pins), pins_missing=tuple(plan.pins_missing), nets=tuple(plan.nets),
                               nets_missing=tuple(plan.nets_missing), net_mode="pads" if plan.nets else "",
                               note=self._note(plan, extra))

    def clear(self) -> HighlightResult:
        def job(ctx):
            board, _doc = ctx.board()
            return ops.clear_if_ours(ctx, self.link, board)
        try:
            r = self.link.run(job, BUDGET_SELECT, self._address())
        except LinkError as e:
            return HighlightResult(ok=False, error=e.detail)
        return HighlightResult(ok=True, note=r.get("note", ""))


def _strip(b: dict) -> dict:
    return {k: v for k, v in b.items() if k != "designators"}


def _option(b: dict) -> dict:
    return {"key": b.get("key"), "name": b.get("name"), "editor": "board", "label": f"{b.get('name')} (board)"}
