"""Local web server (127.0.0.1): JSON API + server-sent events + static UI."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import errata, registry
from . import platform as _platform
from .ecad_sim import SimulatedEcad
from . import partsort
from . import polarity_card as polcard
from . import project as projectlib
from . import extras
from . import plugin_status
from .engine import CmdError, Engine, board_labels
from .protocol import Protocol
from .report import build_model, to_html, to_markdown
from .runner import ActionRunner
from .store import Journal, default_data_dir
from . import bomfile
from . import workspace as wslib
from .sequence import SequenceRunner

STATIC = Path(__file__).parent / "static"


class _NoCacheStatic(StaticFiles):
    """Static files revalidate every time (ETag still answers 304), so a browser never mixes an old script with a new one."""

    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


def _index_html() -> str:
    """index.html with ?v=<content hash> on every local script and stylesheet link: a changed file is a new URL."""
    import hashlib
    import re as _re
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    def tag(m):
        path = STATIC / m.group(2)[len("/static/"):]
        v = hashlib.sha256(path.read_bytes()).hexdigest()[:10] if path.is_file() else "0"
        return f'{m.group(1)}{m.group(2)}?v={v}"'
    return _re.sub(r'((?:src|href)=")(/static/[^"?]+\.(?:js|css))"', tag, html)


class Session:
    """Everything one running batch needs."""

    def __init__(self, proto: Protocol, data_dir: Path, batch: str | None, simulate: bool, boards: int | None,
                 ecad: str | None, operator: str = "", workspace: "wslib.Workspace | None" = None):
        self.workspace = workspace or wslib.Workspace(data_dir, persist=False)
        self.bom_error: str | None = None
        self.proto = proto
        self._apply_bom_override()
        d = proto.data["design"]
        self.batch = batch or f"{d['name']}_{time.strftime('%Y-%m-%d')}"
        self.dir = Path(data_dir) / self.batch
        self.workspace.fallback = {"protocol_path": str(Path(proto.path).resolve()), "current_batch": self.batch}
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.dir / "journal.jsonl")
        self.engine = Engine(proto, self.journal, operator)
        st = self.engine.state
        if not st.meta:
            prefix = proto.data["batch"].get("label_prefix", "S")
            n = boards or int(proto.data["batch"].get("default_boards", 10))
            self.engine.create_batch(self.batch, board_labels(n, prefix), prefix, simulate, d["name"])
            self._copy_protocol()
        elif bool(st.meta.get("simulate")) != bool(simulate):
            raise SystemExit(f"batch '{self.batch}' was created {'WITH' if st.meta.get('simulate') else 'WITHOUT'} --simulate; "
                             f"simulated and real data are never mixed. Use another --batch name or match the flag.")
        self.simulate = bool(st.meta.get("simulate"))
        self.bridge, self.ecad_note = self._make_bridge(ecad)
        self.clients: set[asyncio.Queue] = set()
        self.runner = ActionRunner(self.engine, self.dir, self.broadcast, self.simulate)
        self.sequencer = SequenceRunner(self.engine, self.runner, self.broadcast, self.simulate)
        self.ecad_last: dict = {"designators": [], "result": None}
        sel = self.engine.state.project["select"]
        if sel and hasattr(self.bridge, "set_expected_document") and not sel.get("simulated") and not self.stale_selection():
            self.bridge.set_expected_document(sel.get("document"))
        self._status_cache: tuple | None = None
        self._plat_lock = asyncio.Lock()
        self.hl_last: str | None = None          # test whose targets are selected in Fusion (debounce: a re-render does not send again)
        self.engine.plan_loaded(self.plan_event()) if self.plan_event() else None
        self.scanner = None            # the embedded Assembly Scanner, built when the Assembly page first asks for it
        self.loop: asyncio.AbstractEventLoop | None = None
        self._voice = None

    @property
    def voice(self):
        """Hands-free voice (lazy: importing or building it loads no model and opens no microphone)."""
        if self._voice is None:
            from .voice.controller import VoiceController
            self._voice = VoiceController(self)
        return self._voice

    # ---- ECAD platform (Project page card 1): chosen per project, swapped at runtime
    def _make_bridge(self, program: str | None):
        bridge, note = registry.load_ecad(program, self.simulate)
        d = self.proto.data["design"]
        des = [x for b in self.proto.data["bom"] for x in b["designators"]]
        if isinstance(bridge, SimulatedEcad):      # a "connected Fusion" that holds this batch's board
            drop = tuple(x for x in os.environ.get("POLYBENCH_SIM_ECAD_DROP", "").split(",") if x)
            bridge.configure(des, drop=drop)
        if getattr(bridge, "name", "") == "kicad":      # the KiCad link: the plan's optional alias table, the simulated board
            bridge.configure_session(des, d.get("ecad_aliases"))
        return bridge, note

    @property
    def ecad_id(self) -> str:
        """fusion | kicad: the platform of the running bridge (the simulated Fusion counts as fusion)."""
        return "kicad" if getattr(self.bridge, "name", "") == "kicad" else "fusion"

    def stale_selection(self) -> dict | None:
        """The recorded document was selected with another ECAD platform (e.g. a Fusion urn while KiCad runs): not usable."""
        sel = self.engine.state.project["select"]
        if not sel:
            return None
        b = sel.get("bridge") or ""
        made = "kicad" if b == "kicad" else "fusion" if b in ("fusion", "simulated") else None
        return {"document": sel.get("document"), "program": made} if made and made != self.ecad_id else None

    async def set_ecad_platform(self, program: str) -> dict:
        """Swap the ECAD bridge without a restart, clear the document recorded with the old platform, save and journal it.
        The departing platform's confirmed document (and its accepted cross-check) is kept in the settings file; switching back
        restores it when its fingerprint still matches what is open there (F2)."""
        if program not in wslib.PLATFORM_IDS:
            raise CmdError(f"unknown ECAD platform '{program}'", 422)
        async with self._plat_lock:
            return await self._set_ecad_platform(program)

    @staticmethod
    def _made_with(sel: dict | None) -> str | None:
        b = (sel or {}).get("bridge") or ""
        return "kicad" if b == "kicad" else "fusion" if b in ("fusion", "simulated") else None

    async def _set_ecad_platform(self, program: str) -> dict:
        prev = self.ecad_id
        saved = self.workspace.ecad_program()
        if program == prev and saved == program:
            return {"ok": True, "program": program, "changed": False, "cleared": None, "restored": None}
        pj = self.engine.state.project
        sel, acc = pj["select"], pj["accepted"]
        made = self._made_with(sel)
        docs = self.workspace.documents()
        if sel and made == prev and program != prev:      # the departing platform's document is remembered for the way back
            rec = {"name": sel.get("document"), "id": sel.get("document_id"), "fingerprint": sel.get("fingerprint"),
                   "elements": sel.get("elements"), "program": prev, "editor": sel.get("editor")}
            if acc:
                rec["accepted"] = {"note": acc.get("note", ""), "mismatches": acc.get("mismatches", 0)}
            docs[prev] = rec
        if program != prev:
            bridge, note = await asyncio.to_thread(self._make_bridge, program)
            if hasattr(bridge, "set_expected_document"):
                bridge.set_expected_document(None)          # the KiCad link is shared: never carry an old document name across
            old_scanner, self.scanner = self.scanner, None   # rebuilt on next use with the new platform's client (KC-03)
            if old_scanner is not None:
                try:
                    await asyncio.to_thread(old_scanner.shutdown)
                except Exception:
                    pass
            self.bridge, self.ecad_note = bridge, note
            self._status_cache, self.hl_last = None, None    # an in-flight status of the old bridge is not cached (ecad_status)
            self.ecad_last = {"designators": [], "result": None}
        # the recorded document is cleared when it was made with another platform than the one now chosen (not merely when the
        # running bridge changes: saving the platform a CLI override already runs must clear a stale selection and keep a valid one)
        stale = bool(sel) and (made != program if made else program != prev)
        cleared = {"document": sel.get("document"), "program": made or prev} if stale else None
        self.workspace.overrides.pop("ecad", None)
        if cleared or (program != saved and not sel):
            doc = None
        elif program != saved:                      # a selection made with this platform is kept: the settings name it
            doc = {"name": sel.get("document"), "id": sel.get("document_id"), "fingerprint": sel.get("fingerprint"),
                   "elements": sel.get("elements"), "program": program, "editor": sel.get("editor")}
        else:
            doc = "keep"
        diff = self.workspace.set_ecad(program, doc, docs)
        self.engine.workspace_event("ecad_platform", {"program": program, "previous": prev, "saved_before": saved,
                                                      "cleared_document": cleared, "file": str(self.workspace.file),
                                                      "changed": diff})
        restored = None
        if (cleared or not self.engine.state.project["select"]) and docs.get(program) and not self.engine.state.project["skipped"]:
            restored = await self._restore_document(program, docs[program])
        return {"ok": True, "program": program, "changed": True, "cleared": cleared, "restored": restored}

    async def _restore_document(self, program: str, rec: dict) -> dict | None:
        """Switching back: if the document confirmed earlier on this platform is still the one open (same fingerprint), record it
        again, re-run the BOM cross-check and, when the mismatches are the ones the operator accepted, the acceptance too."""
        bridge = self.bridge
        try:
            st = await self.ecad_status(fresh=True, bridge=bridge)
            if not st.get("connected") or not st.get("document") or st.get("editor") in (None, "none"):
                return None
            names = await self._bridge_call(bridge.designators)
        except Exception:
            return None
        if not names or projectlib.fingerprint(names) != rec.get("fingerprint") or st.get("document") != rec.get("name"):
            return None
        if bridge is not self.bridge:
            return None
        r = self._record_document(bridge, st, names, rec["fingerprint"], restored=True)
        acc = rec.get("accepted")
        accepted = False
        if acc and int(r["crosscheck"].get("mismatches", 0)) == int(acc.get("mismatches", 0)):
            try:
                self.engine.accept_crosscheck(acc.get("note", ""))
                accepted = True
            except CmdError:
                pass
        return {"document": r["document"], "accepted": accepted, "mismatches": r["crosscheck"].get("mismatches", 0)}

    # ---- BOM file (Project page card 4): referenced in the settings file, never copied
    def _apply_bom_override(self):
        """Swap the protocol's BOM for the file named in the project settings (if any). The protocol's own BOM is kept aside."""
        if "_protocol_bom" not in self.proto.data:
            self.proto.data["_protocol_bom"] = list(self.proto.data["bom"])
        path = self.workspace.get("bom_path")
        self.bom_error = None
        if not path:
            self.proto.data["bom"] = list(self.proto.data["_protocol_bom"])
            return
        try:
            lines, _meta = bomfile.load(path)
        except bomfile.BomFileError as e:
            self.bom_error = f"the BOM file in the project settings could not be read ({e}); the protocol's BOM is used"
            self.proto.data["bom"] = list(self.proto.data["_protocol_bom"])
            return
        self.proto.data["bom"] = lines

    def assembly_events(self) -> int:
        """Placements and scans recorded in this batch (journal); a BOM change keeps them, but asks first."""
        st = self.engine.state
        return len(st.alog) + len(st.scans)

    def _bom_changed(self):
        """The ECAD board does not change because a BOM file changed (the simulated boards are not rebuilt either)."""
        self.hl_last = None

    def _recheck_after_bom(self) -> dict | None:
        """After a BOM change: the bom_source event cleared the cross-check; compare the new BOM with the recorded document again,
        when that document is still the active one (same fingerprint). Read-only; otherwise step 1 asks for a new check."""
        pj = self.engine.state.project
        sel = pj["select"]
        if not sel or pj["skipped"] or self.stale_selection():
            return None
        try:
            names = self.bridge.designators()
        except Exception:
            return None
        if not names or projectlib.fingerprint(names) != sel.get("fingerprint"):
            return None
        return self.engine.recheck_bom(names, sel.get("document"), sel.get("fingerprint"))

    def bom_load(self, path: str, confirm: bool = False) -> dict:
        try:
            lines, meta = bomfile.load(path)
        except bomfile.BomFileError as e:
            raise CmdError(str(e), 422)
        n = self.assembly_events()
        if n and not confirm:
            raise CmdError(f"{n} assembly event(s) are recorded against the current BOM. Replacing the BOM keeps them in the journal; "
                           f"confirm to continue.", 409, needs_confirm=True, assembly_events=n)
        prev = self.bom_source()
        self.workspace.save(bom_path=meta["path"])
        self.proto.data["bom"] = lines
        self.bom_error = None
        self._bom_changed()
        now = self.bom_source()
        self.engine.workspace_event("bom_source", {
            "action": "replaced" if prev.get("file") else "loaded", "path": meta["path"], "name": Path(meta["path"]).name,
            "format": meta["format"], "size_bytes": meta["size_bytes"], "lines": now["lines"], "fitted": now["fitted"], "dnf": now["dnf"],
            "previous": prev.get("file"), "previous_source": prev.get("source"), "assembly_events": n, "confirmed": bool(confirm),
            "file": str(self.workspace.file)})
        return {"ok": True, "bom": now, "crosscheck": self._recheck_after_bom()}

    def bom_remove(self, confirm: bool = False) -> dict:
        prev = self.bom_source()
        broken = bool(self.bom_error and self.workspace.get("bom_path"))      # the file named in the settings could not be read
        if prev.get("source") != "settings" and not broken:
            raise CmdError("This BOM is part of the protocol file; Replace it with another file, or edit the protocol.", 409)
        n = self.assembly_events()
        if n and not confirm:
            raise CmdError(f"{n} assembly event(s) are recorded against the current BOM. Removing the BOM file keeps them in the "
                           f"journal; confirm to continue.", 409, needs_confirm=True, assembly_events=n)
        gone = self.workspace.get("bom_path")
        self.workspace.save(bom_path=None)
        self.proto.data["bom"] = list(self.proto.data["_protocol_bom"])
        self.bom_error = None
        self._bom_changed()
        if broken:
            prev = {**prev, "file": gone, "name": Path(gone).name}
        self.engine.workspace_event("bom_source", {"action": "removed", "previous": prev.get("file"), "previous_name": prev.get("name"),
                                                   "now": self.bom_source().get("file"), "assembly_events": n, "confirmed": bool(confirm),
                                                   "file": str(self.workspace.file)})
        return {"ok": True, "bom": self.bom_source(), "crosscheck": self._recheck_after_bom()}

    def seed_document_from_journal(self) -> dict | None:
        """On adoption: the settings file starts with the document step 1 already recorded in the journal (same platform only)."""
        sel = self.engine.state.project["select"]
        if not sel or self.stale_selection() or (self.workspace.get("ecad") or {}).get("document"):
            return None
        doc = {"name": sel.get("document"), "id": sel.get("document_id"), "fingerprint": sel.get("fingerprint"),
               "elements": sel.get("elements"), "program": self.ecad_id, "editor": sel.get("editor")}
        e = dict(self.workspace.get("ecad") or {})
        e["document"] = doc
        e["documents"] = {**self.workspace.documents(), self.ecad_id: dict(doc)}
        self.workspace.settings["ecad"] = e
        return doc

    def workspace_info(self) -> dict:
        ws = self.workspace
        sel = self.engine.state.project["select"]
        return {"project": ws.info(), "batch": {"name": self.batch, "dir": str(self.dir), "journal": str(self.journal.path),
                                                "events": len(self.journal.events)},
                "batches": wslib.batch_folders(ws.dir),
                "protocol": {"path": str(self.proto.path), "name": self.proto.path.name},
                "ecad": {"program": self.ecad_id, "label": "KiCad" if self.ecad_id == "kicad" else "Fusion", "saved": ws.ecad_program(),
                         "platforms": wslib.PLATFORMS, "requests": wslib.ecad_requests(), "stale_selection": self.stale_selection(),
                         "document": (ws.get("ecad") or {}).get("document"), "selected_with": (sel or {}).get("bridge")},
                "recent": wslib.recent_marked()[:8], "simulate": self.simulate, "bom_error": self.bom_error,
                "assembly_events": self.assembly_events()}

    def _copy_protocol(self):
        dst = self.dir / "protocol"
        dst.mkdir(exist_ok=True)
        base = self.proto.path.parent
        for f in self.proto.files:
            try:
                rel = f.relative_to(base)
            except ValueError:
                rel = Path(f.name)
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst / rel)
        (dst / "HASH").write_text(self.proto.hash + "\n", encoding="utf-8")

    async def broadcast(self, msg: dict):
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    @property
    def ecad_program(self) -> str:
        """Operator-facing name of the active ECAD program (KC-43)."""
        return "KiCad" if getattr(self.bridge, "name", "") == "kicad" else "Fusion"

    def snapshot(self):
        s = self.engine.state.snapshot()
        st = self.engine.state
        baseline = st.accepted_protocol or s["meta"].get("protocol_hash")
        s["meta"] = {**s["meta"], "protocol_changed": baseline != self.proto.hash, "protocol_baseline": baseline,
                     "protocol_current": self.proto.hash, "protocol_tests_now": sum(1 for _ in self.proto.tests()), "protocol_tests_before": (st.plan or {}).get("count"),
                     "protocol_accepts": st.protocol_accepts[-5:],
                     "journal_problems": self.journal.problems, "dir": str(self.dir), "batch": self.batch}
        s["action"] = self.runner.status()
        s["sequence"] = self.sequencer.status()
        s["verdicts"] = self.sequencer.verdicts()
        s["bench_cache"] = self.sequencer.bench_cache_status()
        s["ecad_note"] = self.ecad_note
        s["ecad_program"] = self.ecad_program
        s["project_bom"] = self.bom_source()
        s["workspace"] = self.workspace_info()
        self._project_count(s)
        s["ecad_last"] = self.ecad_last
        s["operator"] = self.engine.operator
        s["test_plan"] = self.proto.data.get("test_plan")
        s["plan"] = self.proto.data.get("plan")
        return s

    def _project_count(self, s: dict) -> None:
        """The Project step counts the same five items the left-hand checklist lists (F3): folder, ECAD platform, document, BOM,
        cross-check reviewed. 'Continue without ECAD' settles platform, document and cross-check."""
        pj = self.engine.state.project
        sk = bool(pj["skipped"])
        src = s.get("project_bom") or {}
        items = [True, bool(self.workspace.ecad_program()) or sk, bool(pj["select"]) or sk,
                 bool(src.get("source")) and src.get("source") != "none" and not src.get("error"), bool(pj["accepted"]) or sk]
        for st in s.get("steps", []):
            if st.get("kind") == "project":
                st["applicable"], st["settled"] = len(items), sum(items)

    # ---- test plan as master
    def plan_event(self) -> dict | None:
        pl = self.proto.data.get("plan")
        if not pl or not pl.get("generated"):
            return None
        tests = [{"id": t["id"], "title": t["title"], "step": s["id"], "section": sec["id"], "tier": sec.get("tier", 1)}
                 for s, sec, t in self.proto.tests() if s.get("plan_generated")]
        return {"file": pl["name"], "path": pl["file"], "hash": pl["hash"], "count": len(tests), "tests": tests,
                "format": pl.get("format")}

    def plan_report(self) -> dict:
        """Step 4: what the plan generated, how well it covers the BOM, and its lint status."""
        from . import planfmt
        d = self.proto.data
        tp = (d.get("test_plan") or {}).get("files") or []
        out: dict = {"generate": bool((d.get("test_plan") or {}).get("generate")), "file": tp[0]["name"] if tp else None}
        if not tp or not tp[0]["exists"]:
            return out
        path = tp[0]["path"]
        if d.get("plan"):
            steps = [s for s in d["steps"] if s.get("plan_generated")]
            issues = [i.as_dict() for i in self.proto.issues if i.file == path]
            out.update(hash=d["plan"]["hash"], generated=True, history=self.engine.state.plan_history[-6:])
            gates = d.get("tier_gates") or []
        else:
            lib = {a["id"]: a for s in d["steps"] for sec in s["sections"] for a in sec.get("actions", [])}
            plan = planfmt.parse(path, d["bom"], set(lib))
            steps = planfmt.generate(plan, lib)["steps"] if not plan.errors else []
            issues = [i.as_dict() for i in plan.issues]
            out.update(hash=plan.hash, generated=False)
            gates = [{"after": g["after"], "to": g["to"], "rules": [r["type"] for r in g["rules"]]} for g in plan.gates]
        cov = planfmt.coverage(steps, d["bom"])
        out["coverage"] = cov
        out["lint"] = {"errors": sum(1 for i in issues if i["severity"] == "error"), "warnings": sum(1 for i in issues if i["severity"] == "warning"),
                       "issues": issues[:200]}
        out["gates"] = [tiers_title(g) for g in gates]
        out["retired"] = [{"id": r["id"], "title": r["title"], "boards": len(r["results"])} for r in self.engine.state.retired()]
        return out

    async def reload_plan(self):
        """Read the protocol and the plan again and swap the generated tests in, without a restart. The old tests stay when the new
        plan does not lint clean. Results stay by test ID; a test the plan dropped shows as retired."""
        from .protocol import load
        new = await asyncio.to_thread(load, self.proto.path)
        if new.errors:
            return {"ok": False, "error": f"{len(new.errors)} error(s); the running tests are unchanged", "issues": [i.as_dict() for i in new.errors][:30]}
        self.proto = new
        self._apply_bom_override()
        self.engine.proto = new
        st = self.engine.state
        st.proto = new
        st.test_index = {t["id"]: (s, sec, t) for s, sec, t in new.tests()}
        ev = self.plan_event()
        journaled = self.engine.plan_loaded(ev) if ev else False
        self.hl_last = None
        if getattr(self.bridge, "name", "") == "kicad":
            self.bridge.set_aliases(new.data["design"].get("ecad_aliases"))
        await self.broadcast({"kind": "state"})
        return {"ok": True, "hash": new.hash, "plan_loaded": journaled, "tests": ev["count"] if ev else None}

    async def select_test(self, test_id: str, force: bool = False):
        """A test came into focus: select its parts, pins and nets in Fusion with the scanner's Fusion client (the selection method
        that works), unless the batch continues without ECAD. Same test again: nothing is sent unless forced."""
        found = self.engine.state.test_index.get(test_id)
        if not found:
            raise CmdError("unknown test", 404)
        t = found[2]
        parts, pins, nets = list(t.get("highlight") or []), list(t.get("highlight_pins") or []), list(t.get("nets") or [])
        ctx = {"designators": parts, "pins": pins, "nets": nets, "test_id": test_id}
        if self.engine.state.project["skipped"]:
            res = {"ok": False, "disabled": True, "highlighted": [], "missing": [],
                   "error": "ECAD is off: this batch continues without ECAD (step 1). Nothing was sent"}
            self.ecad_last = {**ctx, "result": res}
            return res
        if not force and self.hl_last == test_id and self.ecad_last and self.ecad_last.get("test_id") == test_id \
                and (self.ecad_last.get("result") or {}).get("ok"):
            return self.ecad_last["result"]
        svc = self.get_scanner()
        svc.touch()
        res = await asyncio.to_thread(svc.select_targets, parts, pins, nets)
        if res.get("simulated") is None and self.simulate:
            res["simulated"] = True
        self.hl_last = test_id
        self.ecad_last = {**ctx, "result": res}
        return res

    async def highlight_test(self, t, force: bool = True):
        """Entry point the voice controller calls with a test dict: the same selection as a test coming into focus."""
        return await self.select_test(t["id"] if isinstance(t, dict) else str(t), force)

    async def ecad_status(self, fresh: bool = False, bridge=None):
        """Cached for a few seconds: the screen asks on every redraw and each real status is three reads through Fusion's MCP.
        `bridge` is the snapshot the caller works with; the answer is cached only while that bridge is still the running one,
        so a platform switch during the read can never leave the old platform's status behind (D9)."""
        br = bridge or self.bridge
        now = time.monotonic()
        c = self._status_cache
        if not fresh and c and now - c[0] < 4.0 and len(c) > 2 and c[2] is br:
            return c[1]
        try:
            res = await asyncio.wait_for(asyncio.to_thread(br.status), 5)
        except asyncio.TimeoutError:
            res = {"connected": False, "detail": "ECAD bridge not responding (5 s)"}
        except Exception as e:
            res = {"connected": False, "detail": f"ECAD bridge error: {e}"}
        if br is self.bridge:
            self._status_cache = (time.monotonic(), res, br)
        return res

    def get_scanner(self):
        """The embedded Assembly Scanner service (camera, decoder, PoC resolver and Fusion client). Built on first use."""
        if self.scanner is None:
            from .assembly_scanner.polybench_link import build_service
            self.loop = asyncio.get_running_loop()
            self.scanner = build_service(self, on_fire=self._scanner_fired, on_change=self._scanner_changed)
            self.scanner.program = self.ecad_program       # operator-facing text names the active ECAD program (KC-43)
        from .assembly_scanner.polybench_link import sync_project
        sync_project(self, self.scanner)         # step 1 Project's BOM and board, via project_info()
        return self.scanner

    def _scanner_changed(self):
        if self.loop and self.clients:
            asyncio.run_coroutine_threadsafe(self.broadcast({"kind": "scanner"}), self.loop)

    def _scanner_fired(self, ev, source):
        """Scan thread: a label was read and debounced. Journal it on the server loop, then tell the page which line it is."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._record_scan(ev, source), self.loop)

    async def _record_scan(self, ev, source):
        res = ev.fired
        code = res.mouser_pn or (ev.codes[0] if ev.codes else "")
        r = self.engine.scan(code, via=source, codes=list(ev.codes) or [code], fmt="Code128")
        self.scanner.mark_fired(source, code, r["line"], r["ok"])
        await self.broadcast({"kind": "state"})

    async def select_line(self, line_no, fmt_codes=None, wait=8.0):
        """Assembly aid: select the line's designators in Fusion with the scanner's own method; same result shape as before."""
        if self.engine.state.project["skipped"]:
            res = {"ok": False, "disabled": True, "highlighted": [], "missing": [], "error": "ECAD is off: this batch continues without ECAD (step 1). Nothing was sent"}
            self.ecad_last = {"designators": [], "result": res}
            return res
        svc = self.get_scanner()
        svc.touch()
        line = next((x for x in self.engine.state.bom_lines() if x["line"] == line_no), None)
        if line is None:
            raise CmdError("unknown BOM line", 404)
        if line.get("dnf"):
            res = {"highlighted": [], "missing": [], "ok": True, "message": "DO NOT FIT: nothing to select"}
            self.ecad_last = {"designators": [], "result": res}
            return res
        await asyncio.to_thread(svc.ensure_board)
        r = await asyncio.to_thread(svc.select_line, line_no, "typed", fmt_codes)
        if not r["ok"]:
            res = {"error": r["error"]}
        else:
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait:
                st = svc.state()["result"]
                if st and not st["pending"]:
                    break
                await asyncio.sleep(0.05)
            st = svc.state()["result"] or {}
            if st.get("blocked"):
                res = {"error": f"not selected in {self.ecad_program}: " + st["blocked"]}
            elif st.get("pending"):
                res = {"error": f"{self.ecad_program} did not answer in time; the app keeps working"}
            elif st.get("fusion_error"):
                res = {"error": st["fusion_error"]}
            else:
                res = {"ok": True, "highlighted": st.get("highlighted") or [], "missing": st.get("not_on_board") or [],
                       "board": st.get("board")}
        self.ecad_last = {"designators": list(line["designators"]), "result": res}
        return res

    # ---- Assembly aid: polarity card (symbol, part, board), from design data only
    POL_TTL = 45.0

    def _pol_cache_file(self) -> Path:
        return self.dir / "polarity_cache.json"

    def _pol_cache_get(self, designators, doc_hint):
        try:
            data = json.loads(self._pol_cache_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        docs = data.get("documents", {})
        doc = docs.get(doc_hint) if doc_hint else (next(iter(docs.values())) if len(docs) == 1 else None)
        if not doc:
            return None
        parts = {d: doc["parts"][d] for d in designators if d in doc.get("parts", {})}
        return {"document": doc["name"], "parts": parts, "saved": doc.get("saved")} if parts else None

    def _pol_cache_put(self, doc_name, parts):
        good = {d: fp for d, fp in parts.items() if not fp.get("error")}
        if not good:
            return
        try:
            f = self._pol_cache_file()
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            ent = data.setdefault("documents", {}).setdefault(doc_name, {"name": doc_name, "parts": {}})
            ent["parts"].update(good)
            ent["saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        except OSError:
            pass

    def _pol_footprints(self, designators):
        """Footprints of the designators: live read through the scanner's Fusion client (memoised for a short time), then the batch's
        cache of an earlier read. Returns (read result | None, error text | None, cached flag)."""
        key = tuple(designators)
        memo = getattr(self, "_pol_memo", None)
        if memo is None:
            memo = self._pol_memo = {}
        hit = memo.get(key)
        if hit and time.monotonic() - hit[0] < self.POL_TTL:
            return hit[1], None, False
        pj = self.engine.state.project
        doc_hint = (pj.get("select") or {}).get("document")
        if pj["skipped"]:
            err, res = "ECAD is off for this batch", None
        else:
            err, res = None, None
            try:
                fusion = self.scanner.fusion if self.scanner is not None else self.get_scanner().fusion
                fn = getattr(fusion, "footprints", None)
                if fn is None:
                    err = "the ECAD link cannot read footprints"
                else:
                    for attempt in range(3):         # the highlight brings the board tab forward; the read needs it active
                        res = fn(designators)
                        if res.get("ok") or not res.get("not_active"):
                            break
                        time.sleep(0.8)
                    if not res.get("ok"):
                        err, res = res.get("error") or f"{self.ecad_program} read failed", None
            except Exception as e:
                err, res = f"ECAD link error: {e}", None
        if res:
            memo[key] = (time.monotonic(), res)
            self._pol_cache_put(res["document"], res["parts"])
            return res, None, False
        cached = self._pol_cache_get(designators, doc_hint)
        if cached:
            return {"document": cached["document"], "parts": cached["parts"]}, None, True
        return None, err, False

    def assembly_ranks(self):
        """Hand-assembly rank of every BOM line (polybench.partsort). Footprints come only from reads that already happened
        (the 45 s memo and the per-document cache file): ranking never starts a Fusion read, selects or highlights anything."""
        fps = {}
        try:
            data = json.loads(self._pol_cache_file().read_text(encoding="utf-8"))
            docs = data.get("documents", {})
            hint = (self.engine.state.project.get("select") or {}).get("document")
            doc = docs.get(hint) if hint else (next(iter(docs.values())) if len(docs) == 1 else None)
            fps.update((doc or {}).get("parts", {}))
        except (OSError, ValueError):
            pass
        for _, res in (getattr(self, "_pol_memo", None) or {}).values():
            fps.update({d: fp for d, fp in res.get("parts", {}).items() if not fp.get("error")})
        return partsort.rank_bom(self.engine.state.bom_lines(), fps)

    def polarity_card(self, line_no, designator=None):
        """The polarity card of a BOM line. Blocking (Fusion reads); call from a worker thread."""
        line = next((x for x in self.engine.state.bom_lines() if x["line"] == line_no), None)
        if line is None:
            raise CmdError("unknown BOM line", 404)
        if not polcard.is_polarised(line):
            return {"status": "none", "line": line_no}
        ds = list(line["designators"])
        d = designator if designator in ds else ds[0]
        res, err, cached = self._pol_footprints(ds)
        fp = (res or {}).get("parts", {}).get(d)
        card = polcard.build_card(line, d, ds, fp, doc=(res or {}).get("document"), cached=cached, error=err)
        card["simulated"] = bool(self.simulate)
        return card

    # ---- step 1: read-only look at the ECAD program (never opens, switches or changes a document)
    def bom_source(self) -> dict:
        """Which BOM is loaded: the protocol's `bom:` block, its file and sizes. The file is the protocol's own; nothing is copied.
        Never raises: a file that cannot be read (binary, permissions, gone) is reported in `error`, never as a 500 on /api/state."""
        base = {"file": None, "name": None, "file_lines": 0, "first_line": None, "lines": 0, "designators": 0, "fitted": 0, "dnf": 0,
                "extra_files": [], "source": "none", "size_bytes": None, "format": None, "removable": False, "error": self.bom_error}
        try:
            return self._bom_source()
        except Exception as e:                      # noqa: BLE001 (the card shows it; /api/state must stay up)
            base["error"] = self.bom_error or f"the BOM facts could not be read ({type(e).__name__}: {e})"
            return base

    def _bom_source(self) -> dict:
        bom = self.proto.data["bom"]
        files = sorted({b.get("_file") for b in bom if b.get("_file")})
        f = files[0] if files else None
        override = self.workspace.get("bom_path") if not self.bom_error else None
        n_file, err = 0, self.bom_error
        if f:
            if Path(f).suffix.lower() in (".xlsx", ".xlsm"):
                # a workbook is not text: the importer counted its rows; the last row a BOM line came from is the extent
                n_file = max((b.get("_line", 0) or 0 for b in bom), default=0)
            else:
                try:
                    with open(f, "rb") as fh:       # bytes: a CSV in another encoding still counts, never a decode error
                        n_file = sum(1 for _ in fh)
                except OSError as e:
                    err = err or f"the BOM file cannot be read: {e}"
        des = [d for b in bom for d in b["designators"]]
        fitted = [d for b in bom if not b.get("dnf") for d in b["designators"]]
        first = min((b.get("_line", 0) for b in bom), default=0)
        return {"file": f, "name": Path(f).name if f else None, "file_lines": n_file, "first_line": first or None,
                "lines": len(bom), "designators": len(set(des)), "fitted": len(set(fitted)), "dnf": len(set(des)) - len(set(fitted)),
                "extra_files": files[1:], "source": "settings" if override and bom else "protocol" if bom else "none",
                "size_bytes": _size(f), "format": Path(f).suffix.lstrip(".").lower() if f else None,
                "removable": bool(override and bom) or bool(self.bom_error and self.workspace.get("bom_path")), "error": err}

    def project_info(self, status: dict | None = None) -> dict | None:
        """The project as the rest of Polybench (the embedded Assembly Scanner) needs it. The one accessor behind GET /api/project;
        the scanner's sync_project() calls it. None when the batch continues without ECAD.

        document_*, board and crosscheck are what step 1 recorded (the document the batch is built from); ecad_connected is the
        last known link state (`status`, else the cached one: a recorded selection counts as connected until a check says otherwise).
        """
        pj = self.engine.state.project
        sel, cc = pj["select"], pj["crosscheck"]
        if pj["skipped"]:
            return None
        st = status or (self._status_cache[1] if self._status_cache else None)
        src = self.bom_source()
        bom = self.engine.state.bom_lines()
        board = None if not sel else {"name": sel.get("document"), "id": sel.get("document_id"), "editor": sel.get("editor"),
                                      "elements": sel.get("elements"), "fingerprint": sel.get("fingerprint")}
        return {"ecad_connected": bool(st.get("connected")) if st else bool(sel), "document_name": board["name"] if board else None,
                "document_id": board["id"] if board else None, "board": board, "bom_source": src["file"], "bom_lines": bom,
                "bom_line_count": len(bom), "bom_file_lines": src["file_lines"], "crosscheck": cc,
                "state": self.engine.state.project_state()["state"], "reviewed": bool(pj["accepted"]), "ecad_skipped": False}

    async def project_result(self, fresh: bool = False) -> dict:
        """GET /api/project. With ECAD skipped it still answers (ecad_connected false, no board) so the caller can tell why."""
        info = self.project_info(await self.ecad_status(fresh) if not self.engine.state.project["skipped"] else None)
        if info is None:
            src = self.bom_source()
            bom = self.engine.state.bom_lines()
            return {"ecad_connected": False, "document_name": None, "document_id": None, "board": None, "bom_source": src["file"],
                    "bom_lines": bom, "bom_line_count": len(bom), "bom_file_lines": src["file_lines"], "crosscheck": None,
                    "state": "done", "reviewed": False, "ecad_skipped": True}
        return info

    def _default_search(self, status: dict) -> str:
        want = (self.proto.data["design"].get("ecad_document") or "").strip()
        if want:
            return want
        doc = (status or {}).get("document") or ""
        return "" if doc.lower().startswith("untitled") else doc

    async def _bridge_call(self, fn, *args, timeout=12):
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)

    @staticmethod
    def _id_of(bridge) -> str:
        return "kicad" if getattr(bridge, "name", "") == "kicad" else "fusion"

    async def project_probe(self, search: str | None = None) -> dict:
        bridge = self.bridge                       # one snapshot for the whole call: a switch meanwhile cannot mix platforms (D9)
        st = await self.ecad_status(fresh=True, bridge=bridge)
        bom = self.engine.state.bom_lines()
        total = len({d for b in bom for d in b["designators"]})
        out: dict = {"status": st, "documents": None, "crosscheck": None, "fingerprint": None, "design_total": None,
                     "expected_elements": total, "bom_source": self.bom_source(), "search": search if search is not None else self._default_search(st),
                     "bridge": getattr(bridge, "name", ""), "ecad": self._id_of(bridge),
                     "url": getattr(getattr(bridge, "client", None), "url", "http://127.0.0.1:27182/mcp")}
        if not st.get("connected"):
            out["stale"] = bridge is not self.bridge
            return out
        try:
            if hasattr(bridge, "documents"):
                out["documents"] = await self._bridge_call(bridge.documents, out["search"] or None)
            names = await self._bridge_call(bridge.designators)
        except asyncio.TimeoutError:
            out["documents"] = out["documents"] or {"ok": False, "open": [], "candidates": [], "message": "ECAD bridge not responding (12 s)"}
            out["stale"] = bridge is not self.bridge
            return out
        except Exception as e:
            out["documents"] = out["documents"] or {"ok": False, "open": [], "candidates": [], "message": f"ECAD bridge error: {e}"}
            out["stale"] = bridge is not self.bridge
            return out
        if names:
            out["crosscheck"] = projectlib.crosscheck(bom, names)
            out["fingerprint"] = projectlib.fingerprint(names)
            out["design_total"] = len(names)
        out["stale"] = bridge is not self.bridge       # the platform changed while this was read: the caller discards it
        return out

    async def project_select(self, expect_fingerprint: str | None = None):
        bridge = self.bridge                       # snapshot (D9)
        pid, pname = self._id_of(bridge), ("KiCad" if self._id_of(bridge) == "kicad" else "Fusion")
        st = await self.ecad_status(fresh=True, bridge=bridge)
        if not st.get("connected"):
            raise CmdError("ECAD is not reachable: " + str(st.get("detail", "")), 409)
        if not st.get("document") or st.get("editor") in (None, "none"):
            raise CmdError(f"The active ECAD document is not a board or schematic. Open the board in {pname}, then check again.", 409)
        try:
            names = await self._bridge_call(bridge.designators)
        except Exception as e:
            raise CmdError(f"could not read the designators: {e}", 409)
        if not names:
            raise CmdError(f"The active document has no parts to read. Make the board or schematic tab active in {pname}.", 409)
        fp = projectlib.fingerprint(names)
        if expect_fingerprint and expect_fingerprint != fp:
            raise CmdError(f"The active {pname} document changed since the check. Press Check again and review it.", 409)
        if bridge is not self.bridge:               # no await below this line: the check and the writes are one step
            raise CmdError("The ECAD platform was changed while the document was being read. Press Check again.", 409)
        return self._record_document(bridge, st, names, fp)

    def _record_document(self, bridge, st: dict, names, fp: str, accepted: dict | None = None, restored: bool = False) -> dict:
        pid = self._id_of(bridge)
        info = {"bridge": getattr(bridge, "name", ""), "program": st.get("program"), "version": st.get("version", ""),
                "document": st.get("document"), "document_id": st.get("document_id"), "editor": st.get("editor"), "elements": len(names),
                "simulated": bool(st.get("simulated"))}
        cc = self.engine.select_project(info, names)
        if hasattr(bridge, "set_expected_document") and not st.get("simulated"):
            bridge.set_expected_document(info["document"])
        doc = {"name": info["document"], "id": info.get("document_id"), "fingerprint": fp, "elements": len(names), "program": pid,
               "editor": info.get("editor")}
        docs = self.workspace.documents()
        docs[pid] = dict(doc)
        diff = self.workspace.set_ecad(pid if not self.workspace.ecad_program() else None, doc, docs)
        self.engine.workspace_event("project_settings", {"change": "ecad_document", "file": str(self.workspace.file), "document": doc,
                                                         "changed": list(diff), **({"restored": True} if restored else {})})
        self._status_cache = None
        return {"ok": True, "crosscheck": cc, "document": info["document"]}

    async def highlight(self, designators):
        designators = list(designators)
        if self.engine.state.project["skipped"]:
            res = {"ok": False, "disabled": True, "highlighted": [], "missing": [],
                   "message": "ECAD is off: this batch continues without ECAD (step 1). Nothing was sent"}
            self.ecad_last = {"designators": designators, "result": res}
            return res
        try:
            if not designators:
                await asyncio.wait_for(asyncio.to_thread(self.bridge.clear), 8)
                res = {"highlighted": [], "missing": []}
            else:
                res = await asyncio.wait_for(asyncio.to_thread(self.bridge.highlight, designators), 8)
        except asyncio.TimeoutError:
            res = {"error": "ECAD bridge not responding (8 s); the app keeps working"}
        except Exception as e:
            res = {"error": f"ECAD bridge error: {e}"}
        self.ecad_last = {"designators": designators, "result": res}
        return res


def _size(f) -> int | None:
    try:
        return Path(f).stat().st_size if f else None
    except OSError:
        return None


def tiers_title(g: dict) -> dict:
    from . import tiers
    return {"title": tiers.gate_title(g), "to": g.get("to"), "rules": [r if isinstance(r, str) else r["type"] for r in g["rules"]]}


def _note_ctx(b: dict) -> dict:
    return {k: b[k] for k in ("category", "suggested_fix", "step", "boards", "line", "designators", "aoi_key", "scan") if k in b}


class _Fwd:
    """Forwards every attribute to the current Session (or its engine), so the routes below keep working after the Project page
    opens another project or batch: the holder gets a new Session, the closures keep this proxy."""
    __slots__ = ("_get",)

    def __init__(self, get):
        object.__setattr__(self, "_get", get)

    def __getattr__(self, k):
        return getattr(object.__getattribute__(self, "_get")(), k)

    def __setattr__(self, k, v):
        setattr(object.__getattribute__(self, "_get")(), k, v)


def reveal_path(path: str) -> dict:
    """Show a file or folder in the OS file manager (Finder, Explorer). POLYBENCH_NO_REVEAL=1 (tests) records the request and opens nothing."""
    if os.environ.get("POLYBENCH_NO_REVEAL") == "1":
        return {"ok": True, "stubbed": True, "path": path}
    try:
        _platform.show_in_file_manager(path)
    except Exception as e:
        return {"ok": False, "error": str(e), "path": path}
    return {"ok": True, "path": path}


def adoption_mismatch(batches: list[dict], design: str) -> str | None:
    """Adopting a legacy folder writes a protocol into its settings. Refuse when a batch journal recorded another design."""
    bad = []
    for bt in batches:
        d = wslib.design_of_batch(Path(bt["journal"]))
        if d and d != design:
            bad.append(f"{bt['name']} (design {d})")
    if not bad:
        return None
    return (f"This protocol is for design {design}, but the folder holds batches of other designs: {', '.join(bad[:4])}. "
            f"Choose the protocol those batches were recorded with. Nothing was changed.")


def open_workspace_session(ws: "wslib.Workspace", batch: str | None, simulate: bool, boards: int | None, operator: str = "",
                           ecad_override: str | None = None, protocol_override: str | None = None) -> Session:
    """A Session for a project folder: protocol and ECAD platform from its settings unless a CLI flag overrides them for this run."""
    from .protocol import load
    pfile = protocol_override or ws.get("protocol_path")
    if not pfile:
        raise CmdError(f"{ws.file} names no protocol file", 422)
    if not Path(pfile).expanduser().is_file():
        raise CmdError(f"the protocol file {pfile} does not exist", 422)
    proto = load(pfile)
    if not batch:
        batch = ws.get("current_batch")
        if not batch:
            bs = wslib.batch_folders(ws.dir)
            batch = bs[0]["name"] if bs else None
    recovery = None
    if proto.errors:                      # a master plan that does not load: fall back to the last good plan (ai/recovery.py)
        from .ai import recovery as _rec
        good, info = _rec.try_recover(proto, pfile, ws.dir, batch)
        for i in proto.issues:
            print(i, file=sys.stderr)
        print("\n" + _rec.console_text(info, good is not None), file=sys.stderr)
        if good is None:
            raise CmdError(f"the protocol has {len(proto.errors)} error(s): " + "; ".join(str(i) for i in proto.errors[:3]), 422)
        proto, recovery = good, info
    ecad = ecad_override or ws.ecad_program()
    try:
        sess = Session(proto, ws.dir, batch, simulate, boards, ecad, operator, workspace=ws)
        sess.recovery = recovery
        return sess
    except SystemExit as e:                 # simulated vs real data mismatch
        raise CmdError(str(e), 409)


DEMO_BATCH = "DEMO-1_demo"
# Paths the server answers with no project open (welcome page). Everything else under /api/ or /aoi/ is 409 "no project is open".
NO_PROJECT_OK = {"/", "/api/session", "/api/events", "/api/state", "/api/console", "/api/workspace", "/api/fs/list", "/api/project/inspect",
                 "/api/project/protocol_design", "/api/project/new", "/api/project/open", "/api/recent/remove", "/api/plugins",
                 "/api/demo/open", "/api/demo/close"}


def demo_source() -> Path:
    return Path(__file__).parent / "examples" / "demo"


def prepare_demo() -> "wslib.Workspace":
    """The demo project folder: a copy of the packaged demo protocol and a settings file, under the platform user-data dir.
    Refuses a folder inside the user's data folder (~/polybench-data or POLYBENCH_DATA) so a demo can never write there."""
    d = wslib.demo_dir().expanduser().resolve()
    data = default_data_dir().resolve()
    if d == data or data in d.parents or d in data.parents:
        raise CmdError(f"the demo folder {d} overlaps the data folder {data}; refusing to write the demo there", 409)
    proto_dir = d / "protocol"
    proto_dir.mkdir(parents=True, exist_ok=True)
    for f in demo_source().iterdir():
        if f.is_file():
            shutil.copy2(f, proto_dir / f.name)
    protocol = str((proto_dir / "demo.pcbv.yaml").resolve())
    if (d / wslib.SETTINGS_NAME).is_file():
        ws = wslib.Workspace(d)
        if ws.get("protocol_path") != protocol or ws.get("current_batch") != DEMO_BATCH:
            ws.save(protocol_path=protocol, current_batch=DEMO_BATCH)
        return ws
    return wslib.Workspace.create(d, "Demo project", protocol, "fusion", DEMO_BATCH)


def create_app(sess: "Session | None", simulate: bool | None = None, meter=None) -> FastAPI:
    """`sess` None starts on the welcome page (no project). `simulate` is the process mode (--simulate): projects opened from the
    page use it. The demo is the only session that is simulated whatever the process mode is."""
    holder = {"cur": sess, "clients": set(), "demo": None,
              "sim": bool(simulate) if simulate is not None else bool(sess.simulate if sess else False)}
    if sess is not None:
        sess.clients = holder["clients"]

    @asynccontextmanager
    async def lifespan(_app):
        if meter is not None:                    # the live multimeter (meter/service.py): started with the server, never by create_app alone
            loop = asyncio.get_running_loop()
            meter.get_session = lambda: holder["cur"]
            meter.sink = lambda msg: asyncio.run_coroutine_threadsafe(broadcast(msg), loop)
            if holder["cur"] is not None:
                holder["cur"].runner.meter = meter
            meter.start()
        yield
        if meter is not None:
            meter.stop()
        cur = holder["cur"]
        if cur is not None and cur.scanner is not None:          # release the camera and stop the Fusion worker with the server
            cur.scanner.shutdown()

    app = FastAPI(title="Polybench DV", docs_url=None, redoc_url=None, lifespan=lifespan)
    sess = _Fwd(lambda: holder["cur"])          # noqa: F811 (every route reads the current session through the proxy)
    eng = _Fwd(lambda: holder["cur"].engine)
    app.state.sess = sess
    app.state.holder = holder

    async def broadcast(msg: dict):
        for q in list(holder["clients"]):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    attach_ai = [None]               # set by the AI mount below: gives every later session its hub

    async def swap_session(new: "Session | None", recent: bool = True, demo: dict | None = None):
        """Make `new` the session (None: back to the welcome page). `demo` marks a demo session and says where Back goes."""
        old = holder["cur"]
        holder["cur"] = new
        holder["demo"] = demo
        if new is not None:
            new.clients = holder["clients"]
            if meter is not None:
                new.runner.meter = meter
            if attach_ai[0]:
                attach_ai[0](new)
        if old is not None:
            for fn in (lambda: old.scanner and old.scanner.shutdown(), lambda: old._voice and old._voice.enable(False), old.journal.close):
                try:
                    await asyncio.to_thread(fn)
                except Exception:
                    pass
        if new is not None and recent:
            wslib.recent_touch(new.workspace.dir, new.workspace.name, new.batch)
        await broadcast({"kind": "state"})
        await broadcast({"kind": "project_switched"})

    def no_project_workspace() -> dict:
        return {"project": {}, "batch": {}, "batches": [], "protocol": {}, "no_project": True,
                "ecad": {"program": None, "label": "Fusion", "saved": None, "platforms": wslib.PLATFORMS, "requests": [], "stale_selection": None},
                "recent": wslib.recent_marked()[:8], "simulate": False}

    def demo_info() -> dict | None:
        d = holder["demo"]
        if not d:
            return None
        return {"active": True, "dir": str(wslib.demo_dir()), "back": d.get("back")}

    def _busy_check():
        cur = holder["cur"]
        if cur is not None and cur.runner.status():
            raise CmdError("an action is running; finish or abort it before opening another project", 409)

    # ---- Project page: project folder, ECAD platform, BOM file (polybench-project.json)
    @app.get("/api/workspace")
    async def workspace_get():
        if holder["cur"] is None:
            return no_project_workspace()
        return sess.workspace_info()

    @app.get("/api/fs/list")
    async def fs_list(path: str | None = None, mode: str = "dir", exts: str = "", bom: int = 0):
        """Lists ANY folder this user can read (loopback, single user; docs/PROJECT_PAGE_VERIFICATION.md). Unreadable entries are
        marked, never a 500. bom=1 in file mode keeps only files that can be a BOM."""
        cur = holder["cur"]
        if cur is None:
            return await asyncio.to_thread(wslib.browse, path or str(Path.home()), mode, [e for e in exts.split(",") if e], [], bool(bom))
        extra = [("Project", str(cur.workspace.dir)), ("Protocol folder", str(cur.proto.path.parent))]
        return await asyncio.to_thread(wslib.browse, path or str(cur.workspace.dir.parent), mode,
                                       [e for e in exts.split(",") if e], extra, bool(bom))

    @app.get("/api/project/inspect")
    async def project_inspect(path: str = ""):
        return await asyncio.to_thread(wslib.inspect, path)

    @app.get("/api/project/protocol_design")
    async def project_protocol_design(path: str = ""):
        """The design a protocol file names, for the New project hint. Read-only; never raises."""
        from .protocol import load
        pth = Path(path).expanduser()
        if not path.strip() or not wslib.is_file(pth):
            return {"ok": False, "design": None, "problem": "no such file"}
        try:
            pr = await asyncio.to_thread(load, str(pth))
        except Exception as e:      # noqa: BLE001
            return {"ok": False, "design": None, "problem": f"cannot be read ({type(e).__name__})"}
        if pr.errors:
            return {"ok": False, "design": (pr.data.get("design") or {}).get("name") if isinstance(pr.data, dict) else None,
                    "problem": f"{len(pr.errors)} protocol error(s)"}
        return {"ok": True, "design": pr.data["design"]["name"], "problem": None}

    @app.post("/api/project/new")
    async def project_new(body: dict):
        _busy_check()
        cur = holder["cur"]
        b = body or {}
        name = " ".join(str(b.get("name") or "").split())
        if not name:
            raise CmdError("give the project a name", 422)
        info = wslib.inspect(b.get("dir") or "")
        if info["kind"] == "corrupt":
            raise CmdError(f"{info['path']} holds a project whose {info['problem']}", 409)
        if info["kind"] in ("project", "legacy"):
            raise CmdError(f"{info['path']} already holds a project; choose an empty folder or open it as an existing project", 409)
        if info["kind"] in ("file", "unreadable") or not info["creatable"] or (info["exists"] and not info["writable"]):
            raise CmdError(f"cannot use {info['path'] or 'that folder'}: {info.get('problem') or 'not writable'}", 422)
        if info.get("warnings") and not b.get("confirm"):
            raise CmdError(" ".join(w["text"] for w in info["warnings"]) + " Confirm to create the project here anyway.", 409,
                           needs_confirm=True, warnings=info["warnings"])
        raw_protocol = b.get("protocol") or (cur.proto.path if cur is not None else "")
        if not str(raw_protocol).strip():
            raise CmdError("choose the protocol file (.pcbv.yaml) for the new project", 422)
        protocol = str(Path(str(raw_protocol)).expanduser())
        if not Path(protocol).is_file():
            raise CmdError(f"the protocol file {protocol} does not exist", 422)
        from .protocol import load
        proto = await asyncio.to_thread(load, protocol)
        if proto.errors:
            raise CmdError(f"the protocol has {len(proto.errors)} error(s): " + "; ".join(str(i) for i in proto.errors[:3]), 422)
        batch = " ".join(str(b.get("batch") or "").split()).replace(" ", "-") or f"{proto.data['design']['name']}_{time.strftime('%Y-%m-%d')}"
        ecad = b.get("ecad") if b.get("ecad") in wslib.PLATFORM_IDS else (cur.ecad_id if cur is not None else "fusion")
        ws = wslib.Workspace.create(Path(info["path"]), name, str(Path(protocol).resolve()), ecad, batch)
        new = await asyncio.to_thread(lambda: Session(proto, ws.dir, batch, holder["sim"], b.get("boards"), ecad,
                                                      cur.engine.operator if cur is not None else "", workspace=ws))
        new.engine.workspace_event("project_settings", {"change": "created", "file": str(ws.file), "name": name, "protocol_path": protocol,
                                                        "ecad_program": ecad, "batch": batch})
        await swap_session(new)
        return {"ok": True, "project": str(ws.dir), "batch": new.batch}

    @app.post("/api/project/open")
    async def project_open(body: dict):
        _busy_check()
        cur = holder["cur"]
        b = body or {}
        info = wslib.inspect(b.get("dir") or "")
        if info["kind"] == "corrupt":
            ws_bad = wslib.SettingsUnreadable(Path(info["path"]) / wslib.SETTINGS_NAME, info.get("settings_error", ""),
                                              wslib.backup_corrupt(Path(info["path"]) / wslib.SETTINGS_NAME))
            raise CmdError(str(ws_bad), 422)
        if info["kind"] == "unreadable":
            raise CmdError(f"{info['path']}: {info['problem']}", 422)
        if info["kind"] == "batch":
            raise CmdError(f"{info['path']} is a batch folder; open its parent {info.get('parent')} (the project)", 422)
        if info["kind"] not in ("project", "legacy"):
            raise CmdError(f"{info['path'] or 'that folder'} holds no Polybench project (no {wslib.SETTINGS_NAME} and no batch folders)", 422)
        batch = " ".join(str(b.get("batch") or "").split()).replace(" ", "-") or None
        adopted = info["kind"] == "legacy"
        if adopted:
            raw = str(b.get("protocol") or "").strip()
            if not raw:
                raise CmdError("Choose the protocol file this folder was recorded with: adopting a folder writes it into the project settings.", 422)
            protocol = str(Path(raw).expanduser().resolve())
            if not Path(protocol).is_file():
                raise CmdError(f"the protocol file {protocol} does not exist", 422)
            from .protocol import load
            pr = await asyncio.to_thread(load, protocol)
            if pr.errors:
                raise CmdError(f"the protocol has {len(pr.errors)} error(s): " + "; ".join(str(i) for i in pr.errors[:3]), 422)
            bad = adoption_mismatch(info["batches"], pr.data["design"]["name"])
            if bad:
                raise CmdError(bad, 409)
            first = batch or info["batches"][0]["name"]
            ws = wslib.Workspace(Path(info["path"]), settings={}, persist=True, adopted=True)
            ws.settings = {"schema": wslib.SCHEMA, "name": b.get("name") or Path(info["path"]).name, "created": wslib.now(),
                           "updated": wslib.now(), "protocol_path": protocol, "bom_path": None,
                           "ecad": {"program": cur.ecad_id if cur is not None else "fusion", "document": None, "documents": {}}, "current_batch": first}
            batch = first
        else:
            try:
                ws = wslib.Workspace(Path(info["path"]))
            except wslib.SettingsUnreadable as e:
                raise CmdError(str(e), 422)
        new = await asyncio.to_thread(open_workspace_session, ws, batch, holder["sim"], b.get("boards"), cur.engine.operator if cur is not None else "")
        if adopted:
            new.seed_document_from_journal()
            ws.save(adopted=True)                   # the settings file is written only once the session opened (a bad protocol leaves no file)
        diff = ws.save(current_batch=new.batch) if ws.get("current_batch") != new.batch else {}
        new.engine.workspace_event("project_settings", {"change": "adopted" if adopted else "opened", "file": str(ws.file),
                                                        "batch": new.batch, "changed": list(diff)})
        await swap_session(new)
        return {"ok": True, "project": str(ws.dir), "batch": new.batch, "adopted": adopted}

    @app.post("/api/recent/remove")
    async def recent_remove(body: dict):
        wslib.recent_remove(str((body or {}).get("dir") or ""))
        return {"ok": True, "recent": wslib.recent_marked()[:8]}

    @app.post("/api/reveal")
    async def reveal(body: dict):
        cur = holder["cur"]
        raw = str((body or {}).get("path") or "").strip()
        p = str(Path(raw).expanduser().resolve()) if raw else ""          # resolved: `..` or a symlink cannot climb out
        allowed = [str(Path(x).expanduser().resolve()) for x in
                   [cur.workspace.dir, cur.dir, cur.proto.path.parent] + [Path(y).parent for y in [cur.workspace.get("bom_path")] if y]]
        if not p or not any(p == a or p.startswith(a.rstrip("/") + "/") for a in allowed):
            raise CmdError("only the project, batch, protocol and BOM paths can be shown in Finder", 403)
        return reveal_path(p)

    @app.post("/api/ecad/platform")
    async def ecad_platform(body: dict):
        r = await sess.set_ecad_platform(str((body or {}).get("program") or ""))
        await sess.broadcast({"kind": "state"})
        return r

    @app.post("/api/ecad/request")
    async def ecad_request(body: dict):
        try:
            return wslib.ecad_request_add((body or {}).get("text", ""), str(holder["cur"].workspace.dir))
        except ValueError as e:
            raise CmdError(str(e), 422)

    @app.post("/api/bom/load")
    async def bom_load(body: dict):
        b = body or {}
        r = await asyncio.to_thread(sess.bom_load, str(b.get("path") or ""), bool(b.get("confirm")))
        await sess.broadcast({"kind": "state"})
        return r

    @app.post("/api/bom/remove")
    async def bom_remove(body: dict):
        r = await asyncio.to_thread(sess.bom_remove, bool((body or {}).get("confirm")))
        await sess.broadcast({"kind": "state"})
        return r

    @app.get("/api/bom/locate")
    async def bom_locate(name: str = "", size: int | None = None):
        cur = holder["cur"]
        home = Path.home()
        roots = [str(cur.workspace.dir), str(cur.proto.path.parent), str(home / "Downloads"), str(home / "Desktop"), str(home / "Documents")]
        return {"matches": await asyncio.to_thread(wslib.locate, name, size, roots)}

    @app.exception_handler(extras.MissingExtra)
    async def _missing_extra(request, exc):
        body = {"error": str(exc), "missing_extra": exc.extra, "missing": list(exc.missing)}
        if request.url.path.rstrip("/") == "/aoi/review":      # the review page is an iframe: show the text, not JSON
            import html as _h
            return HTMLResponse(f"<!doctype html><meta charset=utf-8><body style='font:15px system-ui;padding:24px'>"
                                f"<p data-missing-extra='{exc.extra}'>{_h.escape(str(exc))}</p>", status_code=424)
        return JSONResponse(body, status_code=424)

    @app.exception_handler(CmdError)
    async def _cmd_error(request, exc: CmdError):
        return JSONResponse({"error": str(exc), **exc.extra}, status_code=exc.status)

    @app.get("/")
    async def index():
        return HTMLResponse(_index_html(), headers={"Cache-Control": "no-store"})

    @app.get("/api/session")
    async def session_cookie():
        """The browser calls this when its event stream (re)connects: the guard hands a fresh operator cookie to a stale or missing one."""
        return {"ok": True}

    app.mount("/static", _NoCacheStatic(directory=STATIC), name="static")

    @app.get("/api/state")
    async def state():
        if holder["cur"] is None:
            return {"no_project": True, "workspace": no_project_workspace(), "plugins": plugin_status.summary(None), "demo": None}
        cur = holder["cur"]
        if (cur.engine.proto.data.get("aoi") or {}).get("photos"):          # photos named by the protocol (aoi/seed.py), once per batch
            try:
                from .aoi import seed
                if seed.seed_photos(cur.engine, cur.dir):
                    await cur.broadcast({"kind": "state"})
            except Exception as e:  # noqa: BLE001
                cur.engine.note_aoi_seed_error = f"{type(e).__name__}: {e}"
        s = sess.snapshot()
        s["plugins"] = plugin_status.summary(holder["cur"])
        s["demo"] = demo_info()
        return s

    @app.get("/api/measurements")
    async def measurements(board: str, step: str):
        """The Results explorer: raw measurements, derived answers and proposals of one board in one step (read only)."""
        from . import measurements as _m
        if holder["cur"] is None:
            return {"rows": [], "runs": []}
        return await asyncio.to_thread(_m.collect, sess.engine, sess.dir, board, step)

    @app.get("/api/console")
    async def console():
        if holder["cur"] is None:
            return []
        return list(sess.runner.console)

    @app.get("/api/plugins")
    async def plugins_get(rescan: int = 0):
        """Every discovered action and ECAD bridge: status from imports and metadata, the protocol's settings (secrets masked), the
        last result in this batch's journal. No port is opened, no instrument or ECAD program is contacted."""
        return await asyncio.to_thread(plugin_status.build, holder["cur"], bool(rescan))

    @app.post("/api/demo/open")
    async def demo_open():
        """Open the packaged demo, simulated, in its own project folder. Whatever the process mode is, this session is simulated."""
        _busy_check()
        cur = holder["cur"]
        if holder["demo"]:
            return {"ok": True, "already": True, "project": str(wslib.demo_dir())}
        back = ({"kind": "project", "dir": str(cur.workspace.dir), "batch": cur.batch, "name": cur.workspace.name, "simulate": bool(cur.simulate)}
                if cur is not None else {"kind": "welcome"})
        ws = await asyncio.to_thread(prepare_demo)
        new = await asyncio.to_thread(open_workspace_session, ws, DEMO_BATCH, True, None, cur.engine.operator if cur is not None else "", "fusion")
        if not new.simulate:
            raise CmdError("the demo batch on disk is not simulated; refusing to open it", 409)
        await swap_session(new, recent=False, demo={"back": back})
        return {"ok": True, "project": str(ws.dir), "batch": new.batch, "simulated": True, "back": back}

    @app.post("/api/demo/close")
    async def demo_close():
        """Back from the demo: the project that was open before it, or the welcome page when there was none."""
        _busy_check()
        d = holder["demo"]
        if not d:
            raise CmdError("the demo is not open", 409)
        back = d.get("back") or {"kind": "welcome"}
        note = None
        if back.get("kind") == "project":
            try:
                ws = wslib.Workspace(Path(back["dir"]))
                if not ws.exists:
                    raise CmdError(f"{back['dir']} holds no project any more", 409)
                new = await asyncio.to_thread(open_workspace_session, ws, back.get("batch"), bool(back.get("simulate", holder["sim"])), None, "")
                await swap_session(new)
                return {"ok": True, "back": "project", "project": str(ws.dir), "batch": new.batch}
            except (CmdError, wslib.SettingsUnreadable) as e:
                note = f"could not reopen {back.get('dir')}: {e}"
        await swap_session(None)
        return {"ok": True, "back": "welcome", "note": note}

    @app.get("/api/journal")
    async def journal(limit: int = 300):
        return [{"id": e["id"], "ts": e["ts"], "type": e["type"], "payload": e["payload"], "hash": e["hash"][:10]}
                for e in sess.journal.events[-limit:]]

    @app.get("/api/events")
    async def events(request: Request):
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        holder["clients"].add(q)

        async def gen():
            try:
                yield "retry: 1500\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        m = await asyncio.wait_for(q.get(), 10)
                        yield f"data: {json.dumps(m)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                holder["clients"].discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/preview")
    async def preview(body: dict):
        return eng.preview(body["test_id"], body.get("value", ""))

    @app.post("/api/cmd/{name}")
    async def cmd(name: str, body: dict):
        b = body or {}
        r: dict
        if name == "entry":
            r = eng.entry(b["board"], b["test_id"], b["value"], b.get("confirm", False), b.get("replace", False), b.get("redo", False))
        elif name == "entry_batch":
            r = eng.entry_batch(b["test_id"], b["value"], b.get("confirm", False))
        elif name == "accept":
            r = eng.accept(b["board"], b["test_id"], b["proposal_id"], b.get("confirm", False), b.get("replace", False))
        elif name == "accept_all":
            r = eng.accept_all(b["board"], b["step"])
        elif name == "reject":
            r = eng.reject(b["board"], b["test_id"], b["proposal_id"], b.get("reason", ""))
        elif name == "record_pending":
            r = eng.record_pending(b.get("board"))
        elif name == "skip":
            r = eng.skip(b["board"], b["test_id"], b.get("reason", ""))
        elif name == "retest":
            r = eng.retest(b["board"], b["step"], b.get("cause", ""), b.get("fix", ""))
        elif name == "override_gate":
            r = eng.override_gate(b["board"], b["step"], b.get("reason", ""))
        elif name == "ack_stop":
            r = eng.ack_stop(b["board"], b["stop_id"], b.get("reason", ""))
        elif name == "placed":
            r = eng.set_placed(b["board"], b["designators"], b["on"])
        elif name == "serial":
            r = eng.set_serial(b["board"], b["serial"]) or {"ok": True}
        elif name == "boards":
            r = eng.set_boards(b["n"]) or {"ok": True}
        elif name == "operator":
            r = eng.set_operator(b["name"]) or {"ok": True}
        elif name == "note":
            r = eng.note(b.get("text", ""), b.get("board", ""), b.get("test_id", ""), **_note_ctx(b))
        elif name == "note_edit":
            r = eng.note_edit(b.get("ref", 0), b.get("text", ""), b.get("board", ""), b.get("test_id", ""), **_note_ctx(b))
        elif name == "project_select":
            r = await sess.project_select(b.get("fingerprint"))
        elif name == "crosscheck_accept":
            r = eng.accept_crosscheck(b.get("note", ""))
        elif name == "ecad_skip":
            r = eng.skip_ecad(b.get("reason", ""))
        elif name == "ecad_skip_clear":
            r = eng.clear_skip_ecad()
        elif name == "assembly_begin":
            r = eng.assembly_begin()
        elif name == "assembly_skip":
            r = eng.assembly_skip()
        elif name == "assembly_unskip":
            r = eng.assembly_unskip()
        elif name == "place_batch":
            r = eng.place_batch(int(b["line"]), b["boards"], b.get("on", True), b.get("via", "button"), b.get("code", ""),
                                bool(b.get("auto")))
        elif name == "auto_cancel":
            r = eng.auto_cancel(int(b["line"]), b.get("reason", ""), b.get("code", ""))
        elif name == "scan":
            r = eng.scan(b.get("code", ""), b.get("via", "typed"), b.get("codes"), b.get("format", ""))
            if r["ok"]:
                r["ecad"] = await sess.select_line(r["line"])
        elif name == "select_line":
            r = {"ok": True, "ecad": await sess.select_line(b["line"])}
        elif name == "highlight":
            r = {"ok": True, "ecad": await sess.highlight(b.get("designators", []))}
        elif name == "select_test":
            r = {"ok": True, "ecad": await sess.select_test(b["test_id"], bool(b.get("force")))}
        elif name == "tier2_assign":
            r = eng.tier2_assign(b["board"], b["step"], b.get("sections", "all"), b.get("reason", ""), "operator")
        elif name == "tier2_remove":
            r = eng.tier2_remove(b["board"], b["step"], b.get("sections", []), b.get("reason", ""))
        elif name == "gate_accept":
            r = eng.gate_accept(b["gate"], b.get("boards", []))
        elif name == "gate_reject":
            r = eng.gate_reject(b["gate"], b.get("boards", []), b.get("reason", ""))
        elif name == "aoi_rework":
            r = eng.aoi_rework(b.get("key"), b.get("note", ""), b.get("on", True))
        elif name == "protocol_accept":
            if b.get("hash") != sess.proto.hash:
                raise CmdError("the protocol changed again; reopen the dialog", 409)
            r = eng.protocol_accept(sess.proto.hash, b.get("old_hash", ""), b.get("reason", ""), b.get("tests_before"),
                                    sum(1 for _ in sess.proto.tests()))
        elif name == "plan_reload":
            r = await sess.reload_plan()
        else:
            raise CmdError(f"unknown command '{name}'", 404)
        await sess.broadcast({"kind": "state"})
        return r

    # ---- live multimeter (docs/MULTIMETER.md). The reader thread decides and proposes; these routes are the card's controls.
    @app.get("/api/meter")
    async def meter_state():
        return meter.public_state() if meter is not None else {"enabled": False}

    def _need_meter():
        if meter is None:
            raise CmdError("the live meter is not enabled in this run", 409)
        return meter

    @app.post("/api/meter/auto")
    async def meter_auto(body: dict):
        _need_meter().set_auto(bool((body or {}).get("on")))
        return meter.public_state()

    @app.post("/api/meter/config")
    async def meter_config(body: dict):
        try:
            _need_meter().set_config(body or {})
        except ValueError as e:
            raise CmdError(str(e), 422) from None
        return meter.public_state()

    @app.post("/api/meter/undo")
    async def meter_undo():
        try:
            return await asyncio.to_thread(_need_meter().undo)
        except ValueError as e:
            raise CmdError(str(e), 409) from None

    @app.post("/api/meter/sim")
    async def meter_sim(body: dict):
        """Simulator only: choose the scripted scene the fake meter shows (dc_v, ohm_ol, hold, ac_v, ...) or a displayed value."""
        m = _need_meter()
        if not m.simulate:
            raise CmdError("only the simulated meter takes scenes", 409)
        b = body or {}
        try:
            m.sim_scene(b.get("scene"), value=b.get("value"), mode=b.get("mode"), range_idx=b.get("range"))
        except (KeyError, ValueError) as e:
            raise CmdError(f"unknown scene: {e}", 422) from None
        return {"ok": True}

    # ---- hands-free voice (docs/VOICE.md §3.4); the controller does the work, blocking calls run in a thread
    def _voice_loop():
        sess.loop = asyncio.get_running_loop()
        return sess.voice

    @app.get("/api/voice/status")
    async def voice_status():
        return await asyncio.to_thread(_voice_loop().status)

    @app.post("/api/voice/config")
    async def voice_config(body: dict):
        try:
            return await asyncio.to_thread(_voice_loop().set_config, body or {})
        except ValueError as e:
            raise CmdError(str(e), 422)

    @app.get("/api/voice/models")
    async def voice_models():
        return await asyncio.to_thread(_voice_loop().models, True)

    @app.get("/api/voice/voices")
    async def voice_voices():
        return await asyncio.to_thread(_voice_loop().voices)

    @app.post("/api/voice/open-speech-settings")
    async def voice_open_speech_settings():
        """Opens System Settings at Spoken Content. Fixed URL, no parameters, so nothing from the page reaches `open`."""
        from .voice import tts as _tts
        return await asyncio.to_thread(_tts.open_speech_settings)

    @app.post("/api/voice/test-voice")
    async def voice_test_voice(body: dict | None = None):
        v = (body or {}).get("voice")
        return await asyncio.to_thread(_voice_loop().test_voice, v if isinstance(v, str) else None)

    @app.post("/api/voice/enable")
    async def voice_enable(body: dict):
        return await asyncio.to_thread(_voice_loop().enable, bool(body.get("on")))

    @app.post("/api/voice/handsfree")
    async def voice_handsfree(body: dict):
        return await asyncio.to_thread(_voice_loop().handsfree, bool(body.get("on")))

    @app.post("/api/voice/visibility")
    async def voice_visibility(body: dict):
        return await asyncio.to_thread(_voice_loop().set_visible, bool(body.get("visible")), _client_id(body))

    def _client_id(body: dict):
        c = body.get("client")
        return c[:64] if isinstance(c, str) and c else None

    @app.post("/api/voice/context")
    async def voice_context(body: dict):
        v = _voice_loop()
        v.set_context(body.get("step"), body.get("test_id"), body.get("board"), body.get("aoi_review"))
        # a context post comes from a page that is on screen: it counts as visible unless the page says otherwise
        vis = body.get("visible")
        await asyncio.to_thread(v.set_visible, vis if isinstance(vis, bool) else True, _client_id(body))
        return {"ok": True}

    @app.post("/api/voice/ptt")
    async def voice_ptt(body: dict):
        v = _voice_loop()
        fn = {"start": v.ptt_start, "beat": v.ptt_beat, "stop": v.ptt_stop, "cancel": v.ptt_cancel}.get(body.get("action"))
        if fn is None:
            raise CmdError("action must be start, beat, stop or cancel", 422)
        return await asyncio.to_thread(fn)

    @app.post("/api/voice/text")
    async def voice_text(body: dict):
        if isinstance(body.get("context"), dict):          # the page's view at the moment of typing: never rely on an older post
            c = body["context"]
            _voice_loop().set_context(c.get("step"), c.get("test_id"), c.get("board"), c.get("aoi_review"))
            if isinstance(c.get("visible"), bool):
                _voice_loop().set_visible(c["visible"], _client_id(c))
        return await asyncio.to_thread(_voice_loop().handle_text, str(body.get("text", "")), "typed")

    @app.post("/api/voice/clip")
    async def voice_clip(body: dict):
        if not (sess.simulate or os.environ.get("POLYBENCH_VOICE_TEST") == "1"):
            raise CmdError("clip input is only available with --simulate or POLYBENCH_VOICE_TEST=1", 403)
        return await asyncio.to_thread(_voice_loop().feed_wav, body["path"], body.get("via", "ptt"))

    @app.post("/api/voice/cancel")
    async def voice_cancel():
        return await asyncio.to_thread(_voice_loop().cancel_pending)

    @app.post("/api/voice/guide")
    async def voice_guide(body: dict):
        """The Guide button of a section: the same as saying "guide <section>" (voice/guide.py)."""
        v = _voice_loop()
        if isinstance(body.get("context"), dict):
            c = body["context"]
            v.set_context(c.get("step"), c.get("test_id"), c.get("board"), c.get("aoi_review"))
        sid = body.get("section")
        return await asyncio.to_thread(v.guide_start, sid if isinstance(sid, str) and sid else None)

    # ---- embedded Assembly Scanner (camera + decoder + Fusion selection run here, in the server process)
    @app.get("/api/scanner/state")
    async def scanner_state(hb: int = 0):
        svc = sess.get_scanner()
        if hb:
            svc.touch()
        return await asyncio.to_thread(svc.state)

    @app.post("/api/scanner/leave")
    async def scanner_leave():
        if sess.scanner is not None:
            sess.scanner.leave()
        return {"ok": True}

    @app.post("/api/scanner/clear")
    async def scanner_clear():
        """Waiting for the next scan: clear the Fusion selection (PoC client clear()) and the result card."""
        svc = sess.get_scanner()
        r = await asyncio.to_thread(svc.clear_selection)
        sess.ecad_last = None
        return r

    @app.post("/api/scanner/visibility")
    async def scanner_visibility(body: dict):
        """Page Visibility API from the page: hidden pauses capture and decoding at once, visible resumes."""
        svc = sess.get_scanner()
        svc.set_background(not bool(body.get("visible")))
        return {"ok": True, "background": svc._background}

    @app.post("/api/scanner/control")
    async def scanner_control(body: dict):
        svc = sess.get_scanner()
        a, v = body.get("action"), body.get("value")
        if a == "start":
            svc.set_enabled(True)
        elif a == "stop":
            svc.set_enabled(False)
        elif a == "pause":
            svc.set_paused(True)
        elif a == "resume":
            svc.set_paused(False)
        elif a == "mirror":
            svc.set_mirror(bool(v))
        elif a == "camera":
            await asyncio.to_thread(svc.open_camera, int(v))
        elif a == "board":
            svc.pick_board(v or None)
        elif a == "rescan":
            svc.rescan_boards()
        else:
            raise CmdError(f"unknown scanner action '{a}'", 404)
        return await asyncio.to_thread(svc.state)

    @app.get("/api/scanner/stream.mjpg")
    async def scanner_stream(request: Request):
        svc = sess.get_scanner()

        async def gen():
            last = -2
            while not await request.is_disconnected():
                seq, jpg = await asyncio.to_thread(svc.jpeg)
                if jpg and seq != last:
                    last = seq
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n"
                await asyncio.sleep(0.08)
        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/api/assembly/rank")
    async def assembly_rank():
        return {str(k): v for k, v in sess.assembly_ranks().items()}

    @app.get("/api/testplan")
    async def testplan(i: int = 0, sub: str | None = None):
        tp = sess.proto.data.get("test_plan") or {"files": []}
        if not 0 <= i < len(tp["files"]):
            return {"configured": False, "exists": False, "path": ""}
        from .testplan import load_plan
        r = await asyncio.to_thread(load_plan, tp["files"][i]["path"], sub)
        out = {"configured": True, "index": i, **r}
        if i == 0 and not sub:
            out["plan"] = await asyncio.to_thread(sess.plan_report)
        return out

    @app.get("/api/testplan/agent_prompt")
    async def testplan_agent_prompt():
        """UIR-46: the prompt the operator copies into their own agent. Built from an allow-list of project facts only."""
        from . import agent_prompt
        facts = await asyncio.to_thread(agent_prompt.facts_of, sess)
        return {"prompt": agent_prompt.build(facts), "ecad_linked": bool(facts["ecad_document"]), "bom_loaded": bool(facts["bom_path"]),
                "plan_configured": facts["plan_configured"]}

    @app.get("/api/polarity")
    async def polarity(line: int, d: str | None = None):
        if not sess.engine.state.project["skipped"]:
            sess.get_scanner()              # built on the server loop (it needs the running loop); the Fusion reads run in a thread
        return await asyncio.to_thread(sess.polarity_card, line, d)

    @app.get("/api/ecad/status")
    async def ecad_status(fresh: int = 0):
        return await sess.ecad_status(bool(fresh))

    @app.get("/api/project")
    async def project_get(fresh: int = 0):
        return await sess.project_result(bool(fresh))

    @app.get("/api/project/probe")
    async def project_probe(search: str | None = None):
        return await sess.project_probe(search)

    @app.post("/api/action/run")
    async def action_run(body: dict):
        r = await sess.runner.start(body["step"], body["section"], body["action"], body["board"], bool(body.get("confirmed")))
        return r

    # ---- automated step (docs/AUTOMATED_STEP.md): operator-only, like accepting; the AI guard refuses all of them
    @app.post("/api/sequence/run")
    async def sequence_run(body: dict):
        return await sess.sequencer.start(body["step"], body["board"], bool(body.get("confirmed")), bool(body.get("fresh_bench")))

    @app.post("/api/sequence/check")
    async def sequence_check(body: dict):
        r = await sess.sequencer.bench_check_only(body["step"], body.get("ticked") or (), body.get("board"))
        await sess.broadcast({"kind": "state"})
        return r

    @app.post("/api/sequence/answer")
    async def sequence_answer(body: dict):
        r = await sess.sequencer.answer(body.get("value", ""), body.get("note", ""), body.get("via", "keyboard"))
        await sess.broadcast({"kind": "state"})
        return r

    @app.post("/api/sequence/bench")
    async def sequence_bench(body: dict):
        r = await sess.sequencer.bench_reply(body.get("action", ""), body.get("skips") or body.get("overrides"), body.get("ticked"), via="ui")
        await sess.broadcast({"kind": "state"})
        return r

    @app.get("/api/sequence/asset")
    async def sequence_asset(step: str, equipment: str):
        """The picture of one equipment entry, only the file the protocol names for it."""
        st = sess.proto.step(step)
        e = next((x for x in (st or {}).get("equipment") or [] if isinstance(x, dict) and x.get("id") == equipment), None)
        pth = Path(str((e or {}).get("image") or "")).expanduser()
        if not e or not e.get("image") or not pth.is_file() or pth.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            raise CmdError("no picture for this equipment", 404)
        return FileResponse(pth)

    @app.post("/api/sequence/proceed")
    async def sequence_proceed(body: dict):
        return await sess.sequencer.proceed(body["step"], body["board"], bool(body.get("confirm")), body.get("image"))

    @app.post("/api/sequence/retry_final")
    async def sequence_retry_final(body: dict):
        return await sess.sequencer.retry_final(body["step"], body["board"], body.get("image"))

    @app.post("/api/sequence/defects")
    async def sequence_defects(body: dict):
        return await sess.sequencer.defects(body["step"], body["board"], body.get("note", ""))

    @app.get("/api/sequence/image")
    async def sequence_image(step: str, path: str):
        return await asyncio.to_thread(sess.sequencer.image_preview, step, path)

    @app.post("/api/action/answer")
    async def action_answer(body: dict):
        await sess.runner.answer(body["value"])
        return {"ok": True}

    @app.post("/api/action/cancel")
    async def action_cancel():
        return await sess.runner.cancel()

    @app.post("/api/action/safe_state")
    async def action_safe():
        res = await sess.runner.safe_state_all()
        await sess.broadcast({"kind": "state"})
        return {"ok": True, "result": res}

    # ---------------------------------------------------------------- AOI (photos, review page)
    class _LazyAoiReview:
        """aoi.review imports cv2: loaded on first use, and only when the aoi extra is installed."""
        def __getattr__(self, name):
            extras.need("aoi")
            from .aoi import review
            return getattr(review, name)

    aoi_review = _LazyAoiReview()

    def _need_aoi():
        extras.need("aoi")
        eng.aoi_cfg()

    @app.post("/api/aoi/photos")
    async def aoi_photos(request: Request):
        """Raw body upload: ?name=<file name>&board=<label or empty>&side=top|bottom. One request per file.
        An explicit board (a drop or click on a board tile) wins over the file name and is journalled with assigned_by=tile."""
        _need_aoi()
        import hashlib
        from .aoi import link
        q = request.query_params
        name = Path(q.get("name") or "photo").name
        if Path(name).suffix.lower() not in link.PHOTO_SUFFIXES:
            raise CmdError(f"{name}: not a photo (heic, jpg, png)", 422)
        data = await request.body()
        if not data:
            raise CmdError("empty upload", 422)
        from .aoi import privacy
        try:                    # DIFF-06: no photo is stored with its EXIF, GPS included (an iPhone photo carries the location)
            data, name, _info = privacy.strip_metadata(data, name)
        except Exception as e:  # noqa: BLE001
            raise CmdError(f"{name}: cannot read this photo ({type(e).__name__}); it was not stored", 422)
        sha = hashlib.sha256(data).hexdigest()
        labels = [b["label"] for b in eng.state.boards]
        chosen = q.get("board") or ""
        if chosen and chosen not in labels:
            raise CmdError(f"unknown board {chosen}", 422)
        board = chosen if chosen in labels else link.board_from_name(name, labels, eng.state.meta.get("prefix", "S"))
        side = q.get("side") or link.side_from_name(name)
        rel = f"aoi/photos/{sha[:10]}_{name}"
        dst = sess.dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dup = next((p for p in eng.state.aoi_photos.values() if p["sha256"] == sha), None)
        if dup:
            # the same bytes dropped on a different tile: the operator's tile wins, the stored file stays as it is
            if chosen and dup["board"] != chosen:
                r = eng.aoi_move(dup["file"], chosen)
                await sess.broadcast({"kind": "state"})
                return {"ok": True, "duplicate": dup["name"], "board": chosen, "moved_from": r.get("from"), "assigned_by": "tile"}
            return {"ok": True, "duplicate": dup["name"], "board": dup["board"]}
        dst.write_bytes(data)
        by = "tile" if chosen else ("filename" if board else "unassigned")
        eng.aoi_add_photo(board, side, name, rel, sha, len(data), "chosen" if chosen else ("filename" if board else "unassigned"), by)
        await sess.broadcast({"kind": "state"})
        return {"ok": True, "board": board, "side": side, "file": rel, "assigned_by": by}

    @app.post("/api/aoi/assign")
    async def aoi_assign(body: dict):
        _need_aoi()
        r = eng.aoi_assign(body["file"], body.get("board") or None, body.get("side"))
        await sess.broadcast({"kind": "state"})
        return r

    @app.post("/api/aoi/move")
    async def aoi_move(body: dict):
        _need_aoi()
        r = eng.aoi_move(body["file"], body["board"])
        await sess.broadcast({"kind": "state"})
        return r

    @app.post("/api/aoi/remove")
    async def aoi_remove(body: dict):
        """Remove a photo from the batch: the file moves to <batch>/aoi/removed/ (never deleted) and the next run ignores it."""
        _need_aoi()
        import shutil
        file = body.get("file", "")
        ph = eng.state.aoi_photos.get(file)
        if not ph:
            raise CmdError("unknown photo", 404)
        src = (sess.dir / file).resolve()
        try:
            src.relative_to((sess.dir / "aoi" / "photos").resolve())
        except ValueError:
            raise CmdError("outside the photo folder", 403)
        rem = sess.dir / "aoi" / "removed"
        rem.mkdir(parents=True, exist_ok=True)
        dst = rem / src.name
        n = 1
        while dst.exists():
            n += 1
            dst = rem / f"{src.stem}_{n}{src.suffix}"
        moved = ""
        if src.is_file():
            shutil.move(str(src), str(dst))
            moved = str(dst.relative_to(sess.dir))
        r = eng.aoi_remove(file, body.get("reason", ""), moved)
        await sess.broadcast({"kind": "state"})
        return r

    @app.get("/api/aoi/used")
    async def aoi_used():
        """Which photo of each board and side the next AOI run analyses (the sharpest; the same choice the run makes)."""
        _need_aoi()
        from .aoi import runner
        photos = {f: p for f, p in eng.state.aoi_photos.items()}
        used = await asyncio.to_thread(lambda: runner.pick_mapping(sess.dir, photos, lambda *a, **k: None)[1])
        return {"used": [u["file"] for u in used]}

    @app.post("/api/aoi/drop")
    async def aoi_drop(body: dict):
        _need_aoi()
        r = eng.aoi_drop(body["file"])
        await sess.broadcast({"kind": "state"})
        return r

    @app.get("/aoi/photo/{path:path}")
    async def aoi_photo(path: str, thumb: int = 0):
        target = (sess.dir / "aoi" / "photos" / path).resolve()
        try:
            target.relative_to((sess.dir / "aoi" / "photos").resolve())
        except ValueError:
            raise CmdError("outside the photo folder", 403)
        if not target.is_file():
            raise CmdError("not found", 404)
        if thumb:
            from .aoi import thumbs
            jpg = await asyncio.to_thread(thumbs.thumbnail, target, sess.dir / "aoi" / "thumbs", thumb)
            return FileResponse(jpg, headers={"Cache-Control": "max-age=3600"})
        return FileResponse(target)

    @app.get("/aoi/review/")
    async def aoi_review_page():
        from fastapi.responses import HTMLResponse
        _need_aoi()
        return HTMLResponse(aoi_review.page_html("/aoi/review"), headers={"Cache-Control": "no-store"})

    @app.get("/aoi/review/api/state")
    async def aoi_review_state():
        return await asyncio.to_thread(aoi_review.state, eng, sess.dir, "/aoi/review")

    @app.get("/aoi/review/api/boardview")
    async def aoi_review_bv():
        extras.need("aoi")
        from .aoi import board_view as BV
        run = eng.state.aoi_current_run()
        bv = BV.load(Path(run["dir"])) if run else None
        if bv is None:
            raise CmdError("no board views for this run", 404)
        return {**bv, "run": run["run"]}

    @app.get("/aoi/review/bvimg/{board}")
    async def aoi_review_bvimg(board: str):
        """The registered board photo of the current run (boardview/<board>.jpg in the RUN folder, which is not the batch folder
        that /aoi/review/file serves: that route is why the full-board view showed no photo)."""
        extras.need("aoi")
        from .aoi import board_view as BV
        run = eng.state.aoi_current_run()
        bv = BV.load(Path(run["dir"])) if run else None
        rel = (bv or {}).get("boards", {}).get(board)
        target = (Path(run["dir"]) / rel).resolve() if rel else None
        if not target or not target.is_file():
            raise CmdError("no board image for " + board, 404)
        return FileResponse(target, headers={"Cache-Control": "max-age=300"})

    @app.post("/aoi/review/api/manual")
    async def aoi_review_manual(body: dict):
        r = eng.aoi_manual_issue(body.get("board"), body.get("box_mm"), body.get("kind"), body.get("designators", []), body.get("note", ""))
        await sess.broadcast({"kind": "state"})
        return r

    @app.get("/aoi/review/file/{path:path}")
    async def aoi_review_file(path: str):
        target = (sess.dir / path).resolve()
        try:
            target.relative_to(sess.dir.resolve())
        except ValueError:
            raise CmdError("outside the batch folder", 403)
        if not target.is_file() or target.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            raise CmdError("not found", 404)
        return FileResponse(target, headers={"Cache-Control": "no-store"})

    @app.post("/aoi/review/api/decision")
    async def aoi_review_decision(body: dict):
        r = eng.aoi_decide(body.get("key"), body.get("decision"), body.get("note", ""), body.get("issue_board"))
        await sess.broadcast({"kind": "state"})
        st = await asyncio.to_thread(aoi_review.state, eng, sess.dir, "/aoi/review")
        return {**r, "progress": st["progress"]}

    @app.post("/aoi/review/api/cursor")
    async def aoi_review_cursor(body: dict):
        aoi_review.set_cursor(body.get("index", 0))
        return {"ok": True}

    @app.post("/aoi/review/api/export")
    async def aoi_review_export():
        from .aoi import review_merge as M
        run = eng.state.aoi_current_run()
        if not run:
            raise CmdError("no AOI run yet", 409)
        result = Path(run["result"])
        dec = M.empty_decisions(result)
        for k in [i['key'] for i in run.get('items', [])]:
            d = eng.state.aoi_decision_for(k)
            if not d:
                continue
            dec["decisions"][k] = {"board": d["board"], "id": d["id"], "decision": d["decision"], "note": d["note"], "decided_at": d["decided_at"]}
        tmp = result.with_name(M.DECISIONS_NAME)
        M._atomic_write(tmp, dec)
        paths, summary = M.export(result, tmp)
        labels_path = eng.write_aoi_labels()
        return {"ok": True, "paths": {**paths, "labels": str(labels_path) if labels_path else None},
                "summary": {**summary, "aoi_missed": len(eng.state.aoi_manual)}}

    @app.get("/api/report/preview")
    async def report_preview():
        m = build_model(eng.state, eng.journal_hash())
        return {"md": to_markdown(m), "html": to_html(m)}

    @app.post("/api/report/generate")
    async def report_generate():
        m = build_model(eng.state, eng.journal_hash())
        folder = sess.dir / "reports" / time.strftime("%Y%m%d-%H%M%S")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.md").write_text(to_markdown(m), encoding="utf-8")
        (folder / "report.html").write_text(to_html(m), encoding="utf-8")
        eng._emit("report_generated", {"folder": str(folder), "journal_hash": eng.journal_hash()})
        await sess.broadcast({"kind": "state"})
        return {"ok": True, "folder": str(folder)}

    @app.get("/api/aoi/item")
    async def aoi_item(key: str):
        """Board and designators of one AOI candidate, to prefill a note linked to it."""
        from .aoi import link
        for r in reversed(eng.state.aoi_runs):
            for it in r.get("items", []):
                if it["key"] == key:
                    return {"key": key, "board": it["board"], "id": it["id"], "kind": it["kind"], "designators": link.designators_of(it["id"])}
        raise CmdError("unknown AOI candidate", 404)

    @app.post("/api/report/errata")
    async def report_errata():
        """Write reports/errata_candidates.md in the batch folder and journal it. Nothing outside the batch folder is touched."""
        rows = errata.collect(eng.state)
        folder = sess.dir / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "errata_candidates.md"
        text = errata.to_markdown(eng.state, rows, sess.batch, eng.journal_hash(), time.strftime("%Y-%m-%d %H:%M:%S"))
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        _platform.replace_file(tmp, path)
        counts = {k: sum(r["kind"] == k for r in rows) for k in ("note", "fail", "aoi")}
        eng._emit("errata_export", {"file": "reports/errata_candidates.md", "count": len(rows), "by_source": counts, "journal_hash": eng.journal_hash()})
        await sess.broadcast({"kind": "state"})
        return {"ok": True, "path": str(path), "count": len(rows), "by_source": counts}

    from .ai import mount as _ai_mount; attach_ai[0] = _ai_mount(app, sess)     # AI connector (docs/AI_CONNECTOR_ARCHITECTURE.md)
    if holder["cur"] is not None:
        attach_ai[0](holder["cur"])

    @app.middleware("http")
    async def no_project_guard(request, call_next):
        """With no project open only the welcome page's endpoints answer; every other API route says so (409), never a 500."""
        if holder["cur"] is None:
            path = request.url.path
            if (path.startswith("/api/") or path.startswith("/aoi/")) and path not in NO_PROJECT_OK:
                return JSONResponse({"error": "no project is open: create or open one on the Project page", "no_project": True}, status_code=409)
        return await call_next(request)
    return app
