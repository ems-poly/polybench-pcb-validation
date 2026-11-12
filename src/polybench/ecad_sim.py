"""Built-in simulated ECAD bridge (used with --simulate or when no bridge is installed).

It behaves like a connected Fusion with a board open: it knows a set of designators (the batch's BOM plus a few extras and
minus a few, so the cross-check has something to show), reports designators it does not know as missing, and lists three
same-named documents that differ only in how many parts they hold, like the three real ones. Nothing is drawn anywhere.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


class SimulatedEcad:
    name = "simulated"

    def __init__(self, note: str = ""):
        self._last: list[str] = []
        self._note = note
        self.known: list[str] | None = None        # None: accept any name
        self.document = "(simulated board)"
        self.docs: list[dict] = []                 # candidate documents the list shows
        self.active = 0                            # index into docs of the "open" document

    # ---- set up from the session
    def configure(self, bom_designators, extras=("TP99",), drop=(), name="DEMO-1"):
        base = [d for d in bom_designators if d not in set(drop)]
        self.known = sorted(set(base) | set(extras))
        self.document = name
        n = len(self.known)
        self.docs = [
            {"id": "sim-A", "name": name, "folder": "DEMO", "project": "Example", "open": True, "active": True,
             "elements": n, "editor": "board", "modified": False},
            {"id": "sim-B", "name": name, "folder": "DEMO", "project": "Example", "open": True, "active": False,
             "elements": None, "editor": None, "modified": None},
            {"id": "sim-C", "name": name + "-3d", "folder": "DEMO", "project": "Example", "open": False, "active": False,
             "elements": None, "editor": None, "modified": None},
        ]

    # ---- EcadBridge
    def status(self) -> dict:
        flag = os.environ.get("POLYBENCH_SIM_ECAD_DOWN")      # test aid: the link is down while this file exists
        if flag and Path(flag).exists():
            return {"connected": False, "program": "Fusion", "document": None, "editor": None,
                    "detail": "cannot reach Fusion MCP at http://127.0.0.1:27182/mcp: [Errno 61] Connection refused (simulated)"}
        n = len(self.known) if self.known is not None else None
        return {"connected": True, "program": "Fusion (simulated)", "version": "sim 1.0", "document": self.document, "document_id": "sim-A", "editor": "board",
                "elements": n, "detail": self._note or "simulated: nothing is drawn anywhere", "simulated": True}

    def highlight(self, designators):
        want = [str(d) for d in designators]
        miss = [d for d in want if self.known is not None and d not in self.known and re.fullmatch(r"[A-Za-z]{1,4}\d+[A-Za-z]?", d)]
        self._last = [d for d in want if d not in miss]
        return {"ok": True, "highlighted": self._last, "missing": miss, "simulated": True,
                "message": "Highlighted (simulated)" + (f"; not in the document: {miss}" if miss else "")}

    def clear(self):
        self._last = []

    def designators(self):
        return list(self.known) if self.known is not None else None

    def documents(self, search=None) -> dict:
        open_ = [{"name": d["name"], "active": d["active"], "modified": bool(d["modified"]), "id": d["id"], "open": True} for d in self.docs if d["open"]]
        cands = [{k: d[k] for k in ("id", "name", "folder", "project", "open")} for d in self.docs
                 if search and search.lower() in d["name"].lower()]
        return {"ok": True, "open": open_, "candidates": cands, "search": search or "", "message": "simulated document list"}
