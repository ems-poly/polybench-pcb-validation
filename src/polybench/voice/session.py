# Ported from the earlier voice prototype prg/session.py. Changes for Polybench: aoi import made lazy (AOI not ported); test/eval harness only, not used by the live app.
"""Session: capture script + proposals + cursor + audit log + atomic write."""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import tempfile
import threading
from datetime import datetime, timezone

from . import grammar, rowmode, sheet as sh
from . import script as script_mod
from . import validate
from .validate import Context

PENDING, ACCEPTED, REJECTED, WRITTEN = "pending", "accepted", "rejected", "written"


class StaleFile(Exception):
    pass


def _conversion(parse: dict, p: dict) -> str | None:
    """'450 mV -> 0.45 V' for a number the operator said, as it lands in the cell. None for other value kinds."""
    v = parse.get("value") or {}
    if v.get("kind") != "number":
        return None
    spoken = v["number"] + (f" {v['unit']}" if v.get("unit") else (f" {v['prefix']}" if v.get("prefix") else ""))
    return f"{spoken} -> {p['value']}"


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class Session:
    def __init__(self, report_path: str, out_path: str | None = None, audit_path: str | None = None, mode: str = "row"):
        self.report_path = os.path.abspath(report_path)
        self.out_path = os.path.abspath(out_path) if out_path else self.report_path
        stem = os.path.splitext(self.out_path)[0]
        self.audit_path = audit_path or (stem + ".audit.jsonl")
        self.lock = threading.RLock()
        self.version = 0
        self.proposals: dict[int, dict] = {}
        self._pid = itertools.count(1)
        self.ctx = Context()
        self.mode = mode                   # "row": batch testing, one test across the boards; "board": one board at a time
        self.ctx.mode = self.mode
        self.ctx.next_board = self._next_board
        self.skipped_boards: dict[tuple[int, str], str] = {}     # (row index, board) -> reason; the cell stays empty
        self.focus: list[int] = []          # proposal ids of the latest utterance
        self.history: list[tuple] = []      # snapshots for undo
        self.info: list[dict] = []          # AOI results that propose nothing
        self.skipped: set[str] = set()
        self.messages: list[dict] = []      # operator-facing feed
        self.aoi_meta: dict | None = None
        self.aoi_evidence: dict[int, str] = {}
        self.warnings: list[str] = []
        self._utt = 0                       # utterance number for LLM extractions ("accept all from this utterance")
        self._load()

    # ---- load -------------------------------------------------------------------------------
    def _load(self):
        self.sheet = sh.load(self.report_path)
        self.base_sha = self.sheet.sha256
        self.script = script_mod.build(self.sheet)
        self.warnings = list(self.script.warnings)
        self.idx = grammar.IdIndex([r["id"] for r in self.script.rows])
        if self.ctx.row is None and self.script.rows:
            self.ctx.row = 0
            if self.mode == "row":
                self.ctx.board = self._first_open(0)

    def reload(self):
        """Re-read the file from disk, keep proposals whose cell still exists."""
        with self.lock:
            old_cells = self.script.by_key()
            self._load()
            new_cells = self.script.by_key()
            dropped = 0
            for pid, p in list(self.proposals.items()):
                if p["cell"] not in new_cells:
                    p["state"] = REJECTED
                    p["flag"] = (p.get("flag") or "") + " cell vanished on reload"
                    dropped += 1
                    continue
                if old_cells.get(p["cell"], {}).get("current") != new_cells[p["cell"]]["current"] and p["state"] != WRITTEN:
                    p["conflict"] = f"cell changed on disk: now '{new_cells[p['cell']]['current']}'"
            if self.ctx.row is not None and self.ctx.row >= len(self.script.rows):
                self.ctx.row = 0
            self._log({"event": "reload", "sha256": self.base_sha, "dropped": dropped})
            self._bump()

    def _bump(self):
        self.version += 1

    # ---- audit ------------------------------------------------------------------------------
    def _log(self, rec: dict):
        rec = {"time": now_iso(), **rec}
        try:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            self.warnings.append(f"audit log not writable: {e}")

    def say(self, text: str, kind: str = "info"):
        self.messages.append({"t": now_iso(), "kind": kind, "text": text})
        del self.messages[:-60]
        self._bump()

    # ---- state snapshots for undo -----------------------------------------------------------
    def _snapshot(self):
        self.history.append((copy.deepcopy(self.proposals), copy.deepcopy(self.focus),
                             self.ctx.board, self.ctx.row, set(self.skipped), dict(self.skipped_boards), self.ctx.recent_row))
        del self.history[:-200]

    def undo(self) -> bool:
        with self.lock:
            if not self.history:
                return False
            (self.proposals, self.focus, self.ctx.board, self.ctx.row, self.skipped,
             self.skipped_boards, self.ctx.recent_row) = self.history.pop()
            self._bump()
            return True

    # ---- proposals --------------------------------------------------------------------------
    def _add(self, cell, value, source, flag=None, transcript=None, extra=None) -> int:
        # supersede any active proposal for this cell
        for p in self.proposals.values():
            if p["cell"] == cell and p["state"] in (PENDING, ACCEPTED):
                p["state"] = REJECTED
                p["superseded"] = True
        pid = next(self._pid)
        c = self.script.by_key()[cell]
        self.proposals[pid] = {"pid": pid, "cell": cell, "id": c["id"], "board": c["board"] or c["column"], "value": value, "source": source, "state": PENDING,
                               "flag": flag, "transcript": transcript, "t": now_iso(), **(extra or {})}
        return pid

    def load_aoi(self, path: str):
        from . import aoi as aoi_mod      # not ported (AOI import is a PoC feature, not used by Polybench voice)
        with self.lock:
            data = aoi_mod.load(path)
            props, info, warns = aoi_mod.merge(data, self.script, self.report_path)
            self._snapshot()
            n = 0
            for p in props:
                ex = {k: p[k] for k in ("status", "confidence", "note", "photo") if k in p}
                ex["overwrites"] = p.get("overwrites")
                pid = self._add(p["cell"], p["value"], "aoi", p["flag"], None, ex)
                ev = [e if os.path.isabs(e) else os.path.join(os.path.dirname(os.path.abspath(path)), e) for e in p["evidence"]]
                self.proposals[pid]["evidence"] = ev
                n += 1
            self.info = info
            for i in info:
                i["evidence"] = [e if os.path.isabs(e) else os.path.join(os.path.dirname(os.path.abspath(path)), e) for e in i["evidence"]]
            self.aoi_meta = {"tool": data.get("tool"), "generated": data.get("generated"), "path": os.path.abspath(path),
                             "proposals": n, "info": len(info)}
            self.warnings += [f"AOI: {w}" for w in warns]
            self._log({"event": "aoi_loaded", **self.aoi_meta, "warnings": warns})
            self.say(f"AOI results loaded: {n} proposed value(s), {len(info)} for review only", "info")

    # ---- batch (row) mode -------------------------------------------------------------------
    def set_mode(self, mode: str) -> bool:
        if mode not in ("row", "board"):
            return False
        with self.lock:
            self.mode = mode
            self.ctx.mode = mode
            self.ctx.recent_row = None
            if mode == "row" and self.ctx.row is not None:
                self.ctx.board = self._first_open(self.ctx.row)
            self.say("Batch mode: one test across the boards, then the next test" if mode == "row"
                     else "Board mode: one board at a time", "ok")
            self._bump()
            return True

    def _slots(self, ri: int) -> list[dict]:
        by = self.script.by_key()
        return [by[k] for k in self.script.rows[ri]["cells"] if by[k]["kind"] in ("board", "entry")]

    def _order(self, ri: int) -> list[str]:
        return [c["board"] for c in self._slots(ri) if c["kind"] == "board"]

    def _active(self, key: str):
        best = None
        for p in self.proposals.values():
            if p["cell"] == key and p["state"] in (PENDING, ACCEPTED):
                best = p
        return best

    def _slot_settled(self, ri: int, c: dict) -> bool:
        if (ri, c["column"]) in self.skipped_boards:
            return True
        return bool((self.effective(c["key"]) or "").strip())

    def _first_open(self, ri: int) -> str | None:
        order = self._order(ri)
        by_col = {c["column"]: c for c in self._slots(ri)}
        return rowmode.next_slot(order, lambda b: self._slot_settled(ri, by_col[b]), None) or (order[0] if order else None)

    def _next_board(self, ri: int, start: str | None, taken=()) -> str | None:
        """Next board to fill in row ri after `start` (exclusive), skipping filled, pending, accepted and skipped boards and `taken`."""
        order = self._order(ri)
        by_col = {c["column"]: c for c in self._slots(ri)}
        return rowmode.next_slot(order, lambda b: self._slot_settled(ri, by_col[b]), start, exclude=taken)

    def row_done(self, ri: int) -> bool:
        return all(self._slot_settled(ri, c) for c in self._slots(ri))

    def _row_state(self, ri: int) -> list[dict]:
        out = []
        for c in self._slots(ri):
            p = self._active(c["key"])
            sk = self.skipped_boards.get((ri, c["column"]))
            state = "skipped" if sk is not None and not p else ("pending" if p and p["state"] == PENDING else
                                                                "accepted" if p else ("filled" if c["current"].strip() else "empty"))
            out.append({"board": c["column"], "state": state, "value": (p["value"] if p else c["current"]) or "",
                        "flag": bool(p and p.get("flag")), "confirm": bool(p and p.get("needs_confirm")),
                        "skip_reason": sk})
        return out

    def goto_row(self, ri: int):
        """Move the cursor to row ri; in row mode the board becomes the first one that still needs a value."""
        self.ctx.row = ri
        self.ctx.recent_row = None
        if self.mode == "row":
            self.ctx.board = self._first_open(ri)

    def _after_entry(self, ri: int, touched: list[str], spoken: bool, before: list[dict], keep_cursor: bool = False) -> dict:
        """Row mode, after boards of row ri received a value or a skip: choose the next board, or move to the next row.
        Returns the trace of the decision."""
        was = {x["board"]: x["state"] for x in before}
        new = [b for b in touched if was.get(b) in ("empty", None)]
        row = self.script.rows[ri]
        tr = {"mode": "row", "row": row["id"], "board_spoken": spoken, "touched": touched,
              "before": " ".join(f"{x['board']}={'-' if x['state'] == 'empty' else x['state']}" for x in before),
              "next_board": None, "row_done": False, "moved_to": None}
        self.ctx.row = ri
        self.ctx.recent_row = None
        if self.row_done(ri):
            tr["row_done"] = True
            if ri + 1 < len(self.script.rows):
                self.ctx.row = ri + 1
                self.ctx.recent_row = ri
                self.ctx.board = self._first_open(ri + 1)
                tr["moved_to"] = self.script.rows[ri + 1]["id"]
                tr["next_board"] = self.ctx.board
            else:
                self.ctx.board = None
            return tr
        cur = self.ctx.board
        cur_open = cur in self._order(ri) and not self._slot_settled(ri, next(c for c in self._slots(ri) if c["column"] == cur))
        if keep_cursor and cur_open and cur not in touched:
            pass                                 # skipping a later board does not move the cursor off the one still waiting
        elif new:
            last = max(new, key=lambda b: self._order(ri).index(b) if b in self._order(ri) else 0)
            self.ctx.board = self._next_board(ri, last)
        elif self.ctx.board is None or self._slot_settled(ri, next((c for c in self._slots(ri) if c["column"] == self.ctx.board), None) or self._slots(ri)[0]):
            self.ctx.board = self._next_board(ri, touched[-1] if touched else None)
        tr["next_board"] = self.ctx.board
        tr["correction_only"] = not new
        return tr

    def skip_boards(self, boards: list[str], reason: str, transcript: str = "", source: str = "voice") -> dict:
        """Mark boards of the current row as skipped (the cell stays empty), with a note proposal when the row has a Notes column."""
        with self.lock:
            ri = self.ctx.row
            if ri is None:
                return {"ok": False, "readback": "no current row"}
            order = self._order(ri)
            bad = [b for b in boards if b not in order]
            if bad:
                self.say(f"Cannot skip {', '.join(bad)}: not a board of {self.script.rows[ri]['id']}", "error")
                return {"ok": False, "readback": "not understood"}
            self._snapshot()
            before = self._row_state(ri)
            by = self.script.by_key()
            nt = next((by[k] for k in self.script.rows[ri]["cells"] if by[k]["kind"] == "notes"), None)
            for b in boards:
                self.skipped_boards[(ri, b)] = reason
                if nt is not None:
                    line = f"{b}: skipped" + (f", {reason}" if reason else "")
                    cur = (self.effective(nt["key"]) or "").strip()
                    if line not in cur:
                        self._add(nt["key"], f"{cur}; {line}" if cur else line, source, None, transcript,
                                  {"overwrites": nt["current"] if nt["current"].strip() else None})
            tr = self._after_entry(ri, boards, True, before, keep_cursor=True)
            self.say(f"Skipped {', '.join(boards)}" + (f" ({reason})" if reason else ""), "ok")
            return {"ok": True, "readback": "skipped " + " ".join(boards), "row_trace": tr}

    def row_strip(self) -> dict | None:
        """What the bench screen shows large: the current test as a strip of boards."""
        ri = self.ctx.row
        if ri is None or not (0 <= ri < len(self.script.rows)):
            return None
        row = self.script.rows[ri]
        slots = self._row_state(ri)
        nxt = self.ctx.board
        for x in slots:
            x["next"] = (x["board"] == nxt) and self.mode == "row"
        nrow = self.script.rows[ri + 1]["id"] if ri + 1 < len(self.script.rows) else None
        return {"mode": self.mode, "index": ri + 1, "of": len(self.script.rows), "id": row["id"], "label": row["label"],
                "expected": row["expected"], "unit": row["unit"], "section": row["section"], "slots": slots,
                "next": nxt, "done": self.row_done(ri), "next_row": nrow,
                "recent_row": self.script.rows[self.ctx.recent_row]["id"] if self.ctx.recent_row is not None else None}

    def _add_voice(self, cell, value, flag, transcript, extra=None) -> int:
        """A proposal from speech. In row mode, replacing a board that already holds a different value asks for a second accept."""
        extra = dict(extra or {})
        c = self.script.by_key()[cell]
        if self.mode == "row" and c["kind"] in ("board", "entry"):
            prev = (self.effective(cell) or "").strip()
            if prev and prev != (value or "").strip():
                extra["needs_confirm"] = True
                extra["replaces"] = prev
                flag = (flag + "; " if flag else "") + f"replaces {prev}: accept twice to confirm"
        return self._add(cell, value, "voice", flag, transcript, extra)

    # ---- utterances -------------------------------------------------------------------------
    def handle_parse(self, parse: dict, source: str, transcript: str, extra: dict | None = None) -> dict:
        """Apply a parse result (from grammar or the LLM). Returns {ok, reason?, readback, prompt?}."""
        with self.lock:
            rec = {"event": "utterance", "source": source, "transcript": transcript, "parsed": parse, **(extra or {})}
            if not parse.get("ok"):
                rec["result"] = "not_understood"
                self._log(rec)
                self.say(f"Not understood: \"{transcript}\" ({parse.get('reason')})", "error")
                return {"ok": False, "reason": parse.get("reason"), "readback": "not understood",
                        "verdict": "not understood: " + str(parse.get("reason")), "targets": []}
            intent = parse["intent"]
            out = {"ok": True, "readback": ""}
            if intent in ("accept", "accept_all", "accept_all_aoi", "reject", "undo", "skip", "skip_board", "next", "back", "repeat", "goto", "set_board"):
                out = self._command(parse, intent, transcript)
            else:
                b = validate.bind(parse, self.script, self.ctx)
                if not b["ok"]:
                    rec["result"] = "rejected"
                    rec["reason"] = b["reason"]
                    self._log(rec)
                    self.say(f"Rejected: \"{transcript}\": {b['reason']}", "error")
                    return {"ok": False, "reason": b["reason"], "readback": "not understood",
                            "verdict": "rejected by the validator: " + b["reason"], "targets": []}
                self._snapshot()
                pids = []
                row_mode = self.mode == "row"
                before = self._row_state(b["row"]) if row_mode else None
                for p in b["proposals"]:
                    if parse.get("route") == "llm":
                        p["flag"] = ("interpreted by the LLM, check; " + p["flag"]) if p["flag"] else "interpreted by the LLM, check"
                    pids.append(self._add_voice(p["cell"], p["value"], p["flag"], transcript,
                                                {"overwrites": p.get("overwrites")}))
                self.focus = pids
                targets = [{"pid": pid, "cell": p["cell"], "id": p["id"], "board": p["board"], "value": p["value"],
                            "flag": self.proposals[pid]["flag"], "overwrites": p.get("overwrites"), "conversion": _conversion(parse, p),
                            "implicit": bool(row_mode and intent == "value" and not parse.get("boards"))}
                           for pid, p in zip(pids, b["proposals"])]
                row_trace = None
                if row_mode and intent == "value":
                    touched = [p["board"] for p in b["proposals"] if p["board"] and p["board"] != "Entry"]
                    row_trace = self._after_entry(b["row"], touched, bool(parse.get("boards")), before)
                elif row_mode:
                    if b["row"] == self.ctx.recent_row:
                        self.ctx.recent_row = None          # the verdict or note belonged to the row that just completed
                    else:
                        self.ctx.row = b["row"]
                else:
                    self.ctx.row = b["row"]
                    if b.get("board"):
                        self.ctx.board = b["board"]
                    elif parse.get("boards") and parse["boards"] != "all" and len(parse["boards"]) == 1:
                        self.ctx.board = f"B{parse['boards'][0]}"
                out = {"ok": True, "readback": b["readback"], "pids": pids, "targets": targets, "row_trace": row_trace,
                       "verdict": "accepted as proposal" + (" (flagged: " + targets[0]["flag"] + ")" if targets and targets[0]["flag"] else "")}
                flagged = [p for p in b["proposals"] if p["flag"]]
                if flagged:
                    out["readback"] += ". Out of range: " + flagged[0]["flag"]
                asks = [self.proposals[pid] for pid in pids if self.proposals[pid].get("needs_confirm")]
                if asks:
                    out["readback"] += ". " + ", ".join(f"{a['board']} already holds {a['replaces']}" for a in asks) + "; accept twice to replace"
                self.say(f"Heard: {b['readback']}" + (f"  [flag: {flagged[0]['flag']}]" if flagged else ""),
                         "flag" if flagged else "ok")
            rec["result"] = "ok"
            rec["readback"] = out.get("readback")
            if "targets" in out:
                rec["targets"] = out["targets"]
                rec["verdict"] = out["verdict"]
            if out.get("row_trace"):
                rec["row_trace"] = out["row_trace"]
            self._log(rec)
            self._bump()
            return out

    # ---- LLM extraction (a list of validated cell updates) ------------------------------------
    def effective(self, key: str) -> str:
        """The cell as the operator sees it: latest pending or accepted proposal, else the file's text."""
        best = None
        for p in self.proposals.values():
            if p["cell"] == key and p["state"] in (PENDING, ACCEPTED):
                best = p
        return best["value"] if best else self.script.by_key()[key]["current"]

    def recent_entries(self, n: int = 6) -> list[str]:
        ps = [p for p in self.proposals.values() if p["state"] in (PENDING, ACCEPTED)]
        return [f"{p['id']} {p['board']}={p['value']}" for p in sorted(ps, key=lambda p: p["pid"])[-n:]]

    def recent_numbers(self, n: int = 12):
        from decimal import Decimal
        import re as _re
        out = []
        for p in sorted((p for p in self.proposals.values() if p["state"] in (PENDING, ACCEPTED)), key=lambda p: p["pid"])[-n:]:
            m = _re.match(r"^\s*(-?\d+(?:\.\d+)?)", p["value"] or "")
            if m:
                out.append(Decimal(m.group(1)))
        return out

    def handle_multi(self, parses: list[dict], source: str, transcript: str, extra: dict | None = None) -> dict:
        """'B1 3.29, B2 3.30, B3 3.31': each board-labelled value was parsed by the grammar; bind them all, then apply as one utterance.
        Nothing is applied unless every pair binds (the caller then falls back to the LLM)."""
        with self.lock:
            cells, rows = [], set()
            for p in parses:
                b = validate.bind(p, self.script, self.ctx)
                if not b["ok"]:
                    return {"ok": False, "reason": b["reason"], "readback": "not understood", "targets": []}
                rows.add(b["row"])
                for pr in b["proposals"]:
                    c = self.script.by_key()[pr["cell"]]
                    cells.append({"cell": pr["cell"], "id": pr["id"], "board": pr["board"], "column": c["column"], "value": pr["value"],
                                  "flag": pr["flag"], "overwrites": pr["overwrites"], "conversion": _conversion(p, pr), "implicit": False})
            if len(rows) != 1:
                return {"ok": False, "reason": "values for more than one row", "readback": "not understood", "targets": []}
            res = {"ok": True, "command": None, "cells": cells, "rejected": [], "corrections": [], "unplaced_notes": [],
                   "checks": [], "row": rows.pop(), "board": None, "skips": []}
            return self.handle_extraction(res, source, transcript, extra, route="grammar")

    def handle_extraction(self, res: dict, source: str, transcript: str, extra: dict | None = None, route: str = "llm") -> dict:
        """Apply a validated extraction (extract.resolve). Every cell arrives pending and flagged as LLM-derived."""
        with self.lock:
            rec = {"event": "utterance", "source": source, "transcript": transcript, "route": "llm-extract" if route == "llm" else "grammar-multi",
                   "extraction": {k: res.get(k) for k in ("command", "cells", "rejected", "corrections", "unplaced_notes", "checks")}, **(extra or {})}
            for n in res.get("unplaced_notes", []):
                self.say(f"Note not placed ({n['reason']}): {n['text']}", "error")
            if res.get("command"):
                out = self._command({"intent": res["command"]}, res["command"], transcript)
                rec["result"] = "ok"
                rec["readback"] = out.get("readback")
                self._log(rec)
                self._bump()
                return {**out, "verdict": f"command {res['command']}: {out.get('readback')}", "targets": []}
            if not res["ok"] and not res.get("skips"):
                why = "; ".join(r["reason"] for r in res["rejected"]) or "nothing in the transcript is a result"
                if res.get("unplaced_notes"):
                    why = "; ".join(n["reason"] for n in res["unplaced_notes"]) + ("; " + why if res["rejected"] else "")
                rec["result"] = "not_understood"
                rec["reason"] = why
                self._log(rec)
                self.say(f"Not understood: \"{transcript}\" ({why})", "error")
                return {"ok": False, "reason": why, "readback": "not understood",
                        "verdict": "not understood: " + why, "targets": []}
            self._snapshot()
            self._utt += 1
            pids, targets, rb = [], [], []
            row_mode = self.mode == "row"
            ri0 = res.get("row")
            before = self._row_state(ri0) if row_mode and ri0 is not None else None
            for c in res["cells"]:
                flags = ["interpreted by the LLM, check"] if route == "llm" else []
                if c.get("flag"):
                    flags.append(c["flag"])
                if c.get("correction"):
                    flags.append("corrected: " + c["correction"])
                flag = "; ".join(flags) or None
                pid = self._add_voice(c["cell"], c["value"], flag, transcript,
                                      {"overwrites": c.get("overwrites"), "utt": self._utt})
                pids.append(pid)
                targets.append({"pid": pid, "cell": c["cell"], "id": c["id"], "board": c["board"] or c["column"], "value": c["value"],
                                "flag": self.proposals[pid]["flag"], "overwrites": c.get("overwrites"), "conversion": c.get("conversion"),
                                "utt": self._utt, "implicit": bool(c.get("implicit"))})
                rb.append(f"{c['id']} {c['board'] or c['column']} {c['value']}")
            self.focus = pids
            row_trace = None
            skips = res.get("skips") or []
            for sk in skips:
                self.skipped_boards[(sk["row"], sk["board"])] = sk.get("reason") or ""
                rb.append(f"{self.script.rows[sk['row']]['id']} {sk['board']} skipped")
            if row_mode and res.get("row") is not None:
                touched = [c["board"] for c in res["cells"] if c.get("board") and c["board"] != "Entry"] + [sk["board"] for sk in skips]
                spoken = not (res["cells"] or skips) or not all(c.get("implicit") for c in res["cells"] if c.get("board"))
                row_trace = self._after_entry(res["row"], touched, spoken, before)
            else:
                if res.get("row") is not None:
                    self.ctx.row = res["row"]
                if res.get("board"):
                    self.ctx.board = res["board"]
            nrej = len(res["rejected"])
            verdict = f"{len(pids)} cell(s) pending (utterance {self._utt})" + (f", {len(skips)} board(s) skipped" if skips else "") + (f", {nrej} rejected by the validator" if nrej else "")
            readback = ", ".join(rb)
            asks = [self.proposals[pid] for pid in pids if self.proposals[pid].get("needs_confirm")]
            if asks:
                readback += ". " + ", ".join(f"{a['board']} already holds {a['replaces']}" for a in asks) + "; accept twice to replace"
            rec.update(result="ok", readback=readback, targets=targets, verdict=verdict, utt=self._utt, row_trace=row_trace)
            self._log(rec)
            self.say(f"Heard ({len(pids)} cells): {readback}", "flag")
            self._bump()
            return {"ok": True, "readback": readback, "pids": pids, "targets": targets, "verdict": verdict, "row_trace": row_trace}

    def add_derived(self, d: dict, transcript: str, res: dict) -> None:
        """Pass/fail cells computed from the Expected column for values the grammar parsed; pending, flagged, added to the utterance's targets."""
        with self.lock:
            for c in d["cells"]:
                flag = c.get("flag") or "computed from Expected"
                pid = self._add(c["cell"], c["value"], "voice", flag, transcript, {"overwrites": c.get("overwrites")})
                self.focus.append(pid)
                res.setdefault("pids", []).append(pid)
                res.setdefault("targets", []).append({"pid": pid, "cell": c["cell"], "id": c["id"], "board": c["column"], "value": c["value"],
                                                      "flag": flag, "overwrites": c.get("overwrites"), "conversion": None})
                self.say(f"Computed {c['id']} {c['column']} = {c['value']} ({c.get('why') or 'from Expected'})", "flag")
            for n in d.get("corrections", []):
                self.say(n, "info")
            if d["cells"]:
                self._log({"event": "derived", "transcript": transcript, "cells": d["cells"], "corrections": d.get("corrections")})
                self._bump()

    def _accept_guarded(self, pids: list[int], by: str) -> tuple[int, list[int]]:
        """Accept by voice or pedal. A proposal that replaces a different value on the same board is held on the first accept
        and taken on the second ('armed'). Returns (accepted, held)."""
        n, held = 0, []
        for pid in pids:
            p = self.proposals[pid]
            if p.get("needs_confirm") and not p.get("armed"):
                p["armed"] = True
                held.append(pid)
                continue
            self._set_state(pid, ACCEPTED, by)
            n += 1
        return n, held

    def _held_msg(self, held: list[int], voice: bool = False) -> str:
        parts = ", ".join(f"{self.proposals[h]['board']} already holds {self.proposals[h]['replaces']}" for h in held)
        return (f". {parts}: accept again to replace" if not voice else f"{parts}. Accept again to replace")

    def decide_utterance(self, utt: int, state: str, by: str = "ui") -> int:
        with self.lock:
            pids = [p["pid"] for p in self.proposals.values() if p.get("utt") == utt and p["state"] == PENDING]
            if not pids:
                return 0
            self._snapshot()
            if state == ACCEPTED:
                n, held = self._accept_guarded(pids, by)
                if held:
                    self.say("Held" + self._held_msg(held), "flag")
                self._bump()
                return n
            for pid in pids:
                self._set_state(pid, state, by)
            self._bump()
            return len(pids)

    def _command(self, parse, intent, transcript: str = "") -> dict:
        rows = self.script.rows
        if intent == "set_board":
            self._snapshot()
            b = parse["boards"]
            if b == "all" or len(b) != 1 or f"B{b[0]}" not in self.script.boards:
                self.say(f"Board not understood: {b}", "error")
                return {"ok": False, "readback": "not understood"}
            self.ctx.board = f"B{b[0]}"
            self.ctx.recent_row = None
            self.say(f"Current board: {self.ctx.board}", "ok")
            return {"ok": True, "readback": f"board {b[0]}"}
        if intent == "goto":
            if parse.get("row_no"):
                ri, err = validate.row_by_number(self.script, self.ctx, parse["row_no"])
                rr = [ri] if ri is not None else []
            elif parse.get("id_candidates"):
                rr = []
            else:
                rr = validate.rows_for_id(self.script, parse["id"], self.ctx)
            if len(rr) != 1:
                self.say(f"ID {parse['id']} {'not on the sheet' if not rr else 'is ambiguous'}", "error")
                return {"ok": False, "readback": "not understood"}
            self._snapshot()
            self.goto_row(rr[0])
            if parse.get("boards") and parse["boards"] != "all" and len(parse["boards"]) == 1:
                self.ctx.board = f"B{parse['boards'][0]}"
            return {"ok": True, "readback": self.prompt()}
        if intent == "skip_board":
            return self.skip_boards([f"B{b}" for b in parse["boards"]], (parse.get("value") or {}).get("text", ""), transcript)
        if intent == "skip" and self.mode == "row" and self.ctx.board and self.ctx.row is not None:
            return self.skip_boards([self.ctx.board], "", transcript)          # bare "skip" in batch mode: this board, not the whole test
        if intent in ("next", "skip", "back"):
            self._snapshot()
            if intent == "skip" and self.ctx.row is not None:
                self.skipped.add(rows[self.ctx.row]["id"] + "@" + str(self.ctx.row))
            if self.ctx.row is not None:
                step = -1 if intent == "back" else 1
                self.goto_row(max(0, min(len(rows) - 1, self.ctx.row + step)))
            return {"ok": True, "readback": self.prompt()}
        if intent == "repeat":
            return {"ok": True, "readback": self.prompt(long=True)}
        if intent == "undo":
            if self.undo():
                self.say("Undone", "ok")
                return {"ok": True, "readback": "undone"}
            self.say("Nothing to undo", "error")
            return {"ok": False, "readback": "nothing to undo"}
        if intent in ("accept", "reject"):
            pids = [p for p in self.focus if self.proposals[p]["state"] == PENDING]
            if not pids:
                self.say("Nothing pending to " + intent, "error")
                return {"ok": False, "readback": "nothing pending"}
            self._snapshot()
            if intent == "accept":
                n, held = self._accept_guarded(pids, "voice")
                msg = f"Accepted {n} value(s)" + (self._held_msg(held) if held else "")
                self.say(msg, "ok" if not held else "flag")
                return {"ok": True, "readback": f"accepted {n}" + (". " + self._held_msg(held, voice=True) if held else "")}
            for p in pids:
                self._set_state(p, REJECTED, "voice")
            self.say(f"Rejected {len(pids)} value(s)", "ok")
            return {"ok": True, "readback": f"rejected {len(pids)}"}
        if intent in ("accept_all", "accept_all_aoi"):
            want = "aoi" if intent == "accept_all_aoi" else "voice"
            pids = [p["pid"] for p in self.proposals.values()
                    if p["state"] == PENDING and p["source"] == want and not p.get("flag") and not p.get("needs_confirm")]
            self._snapshot()
            for p in pids:
                self._set_state(p, ACCEPTED, "voice")
            self.say(f"Accepted {len(pids)} unflagged {want} value(s); flagged ones stay pending", "ok")
            return {"ok": True, "readback": f"accepted {len(pids)}"}
        return {"ok": False, "readback": "not understood"}

    def prompt(self, long: bool = False) -> str:
        if self.ctx.row is None:
            return ""
        r = self.script.rows[self.ctx.row]
        s = f"{r['id']}"
        if long:
            s += f", {r['label']}" if r["label"] else ""
            if r["expected"]:
                s += f". Expected {r['expected']}"
            if r["unit"]:
                s += f", unit {r['unit']}"
        if self.ctx.board:
            s += f", board {self.ctx.board[1:]}"
        return s

    # ---- accept / reject from the UI --------------------------------------------------------
    def _set_state(self, pid: int, state: str, by: str):
        p = self.proposals[pid]
        p["state"] = state
        p["by"] = by
        self._log({"event": state, "pid": pid, "cell": p["cell"], "value": p["value"], "source": p["source"],
                   "transcript": p.get("transcript"), "accepted_by": by if state == ACCEPTED else None,
                   "decided_by": by})

    def decide(self, pid: int, state: str, by: str = "ui") -> bool:
        with self.lock:
            p = self.proposals.get(pid)
            if not p or p["state"] in (WRITTEN,):
                return False
            self._snapshot()
            self._set_state(pid, state, by)
            self._bump()
            return True

    def decide_focus(self, state: str, by: str) -> int:
        with self.lock:
            pids = [p for p in self.focus if self.proposals[p]["state"] == PENDING]
            if not pids:
                return 0
            self._snapshot()
            if state == ACCEPTED:
                n, held = self._accept_guarded(pids, by)
                if held:
                    self.say("Held" + self._held_msg(held), "flag")
                self._bump()
                return n
            for p in pids:
                self._set_state(p, state, by)
            self._bump()
            return len(pids)

    def accept_all(self, source: str, by: str = "ui", include_flagged: bool = False) -> int:
        with self.lock:
            pids = [p["pid"] for p in self.proposals.values()
                    if p["state"] == PENDING and p["source"] == source and (include_flagged or not p.get("flag")) and not p.get("needs_confirm")]
            if not pids:
                return 0
            self._snapshot()
            for p in pids:
                self._set_state(p, ACCEPTED, by)
            self._bump()
            return len(pids)

    # ---- write ------------------------------------------------------------------------------
    def disk_changed(self) -> bool:
        try:
            with open(self.report_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest() != self.base_sha
        except OSError:
            return True

    def accepted_edits(self) -> list[tuple[int, int, str, int]]:
        cells = self.script.by_key()
        edits = []
        for p in self.proposals.values():
            if p["state"] == ACCEPTED:
                c = cells[p["cell"]]
                edits.append((c["start"], c["end"], p["value"], p["pid"]))
        return edits

    def write(self) -> dict:
        with self.lock:
            if self.disk_changed():
                raise StaleFile("the report changed on disk since it was loaded; reload before writing")
            edits = self.accepted_edits()
            if not edits:
                return {"written": 0, "path": self.out_path}
            data = self.sheet.data
            new = sh.apply_edits(data, [(a, b, v) for a, b, v, _ in edits])
            self._verify(data, new, edits)
            d = os.path.dirname(self.out_path)
            fd, tmp = tempfile.mkstemp(prefix=".prg-", suffix=".tmp", dir=d)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(new)
                    f.flush()
                    os.fsync(f.fileno())
                if os.path.exists(self.out_path):
                    os.chmod(tmp, os.stat(self.out_path).st_mode & 0o7777)
                os.replace(tmp, self.out_path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            sha = hashlib.sha256(new).hexdigest()
            self._log({"event": "write", "path": self.out_path, "sha256_before": self.base_sha, "sha256_after": sha,
                       "cells": [{"cell": self.proposals[pid]["cell"], "value": v, "source": self.proposals[pid]["source"]}
                                 for _, _, v, pid in edits]})
            in_place = self.out_path == self.report_path
            if in_place:
                for _, _, _, pid in edits:
                    self.proposals[pid]["state"] = WRITTEN
                self._load()
            self.say(f"Wrote {len(edits)} value(s) to {self.out_path}", "ok")
            return {"written": len(edits), "path": self.out_path, "sha256": sha}

    @staticmethod
    def _verify(old: bytes, new: bytes, edits):
        """Re-parse the output; check each edited cell and that all other bytes are untouched."""
        s_old, s_new = sh.parse(old), sh.parse(new)
        if [len(t.rows) for t in s_old.tables] != [len(t.rows) for t in s_new.tables]:
            raise RuntimeError("write verification failed: table shape changed")
        cur = 0
        rebuilt = bytearray()
        for a, b, v, _ in sorted(edits):
            rebuilt += old[cur:a] + sh.render_cell(v, old[a:b])
            cur = b
        rebuilt += old[cur:]
        if bytes(rebuilt) != new:
            raise RuntimeError("write verification failed: unexpected byte difference")

    # ---- view -------------------------------------------------------------------------------
    def view(self) -> dict:
        with self.lock:
            props = {}
            for p in self.proposals.values():
                if p["state"] in (PENDING, ACCEPTED) or p["state"] == REJECTED and not p.get("superseded"):
                    props[p["pid"]] = p
            return {
                "version": self.version,
                "report": self.report_path, "out": self.out_path, "disk_changed": self.disk_changed(),
                "boards": self.script.boards, "board": self.ctx.board, "mode": self.mode, "strip": self.row_strip(),
                "cursor": self.ctx.row, "prompt": self.prompt(long=True),
                "rows": self.script.rows, "cells": self.script.cells,
                "proposals": list(props.values()), "focus": self.focus, "info": self.info,
                "messages": self.messages[-25:], "warnings": self.warnings,
                "skipped": sorted(self.skipped), "aoi": self.aoi_meta,
                "counts": {s: sum(1 for p in self.proposals.values() if p["state"] == s)
                           for s in (PENDING, ACCEPTED, REJECTED, WRITTEN)},
            }
