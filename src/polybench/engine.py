"""Core logic: evaluation of values against limits, journal replay into state, commands.

Everything the UI shows is derived from the journal by State.apply(); a restart replays the same events
and so reaches the same state. The core, not the plugins, computes verdicts.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
import threading
from typing import Any

from . import labels, project, tiers
from .protocol import STEP_KINDS, Protocol
from .store import Journal

# ----------------------------------------------------------------------------- evaluation

YES = {"y", "yes", "true", "1", "ok", "pass", "p"}
NO = {"n", "no", "false", "0", "fail", "f", "ng"}


class CmdError(Exception):
    def __init__(self, msg, status=409, **extra):
        super().__init__(msg)
        self.status = status
        self.extra = extra


def _fmt(x):
    return f"{x:g}"


def source_text(src) -> str:
    if not src:
        return ""
    if isinstance(src, str):
        return src
    parts = [str(src.get("type", ""))]
    for k in ("doc", "section", "note", "ref"):
        if src.get(k):
            parts.append(str(src[k]))
    if src.get("page") is not None:
        parts.append(f"p.{src['page']}")
    return " ".join(p for p in parts if p)


def expected_text(t: dict) -> str:
    lim = t.get("limit")
    unit = f" {t['unit']}" if t.get("unit") else ""
    if t["kind"] == "table":
        parts = []
        for cid, cl in (t.get("limits") or {}).items():
            col = next((c for c in t.get("columns") or [] if c.get("id") == cid), {})
            u = f" {col['unit']}" if col.get("unit") else ""
            lo, hi = cl.get("min"), cl.get("max")
            parts.append(f"{cid} " + (f"{_fmt(lo)} to {_fmt(hi)}" if lo is not None and hi is not None else
                                     f">= {_fmt(lo)}" if lo is not None else f"<= {_fmt(hi)}" if hi is not None else "") + u)
        return "every row: " + "; ".join(parts) if parts else "record only"
    if not lim:
        return "yes" if t["kind"] == "yn" else ""
    if "equals" in lim:
        return f"= {lim['equals']}"
    if "in" in lim:
        return "one of " + ", ".join(str(x) for x in lim["in"])
    lo, hi = lim.get("min"), lim.get("max")
    if lo is not None and hi is not None:
        return f"{_fmt(lo)} to {_fmt(hi)}{unit}"
    if lo is not None:
        return f">= {_fmt(lo)}{unit}"
    if hi is not None:
        return f"<= {_fmt(hi)}{unit}"
    return ""


OVER_RANGE = {"OL", "OC", "OPEN"}
SHORT = {"SC", "SHORT"}
OVER_RANGE_NUM = 1e15  # larger than any limit; OL/OC pass a min-only limit and fail any max


def normalise(test: dict, raw: str) -> dict:
    """Parse a typed or proposed value. Returns {ok, value, num} or {ok: False, error}."""
    kind = test["kind"]
    s = str(raw).strip()
    if s == "":
        return {"ok": False, "error": "empty value"}
    if kind == "number":
        tok = s.upper()
        if tok in OVER_RANGE:        # OL / OC: over-range or open; a finite stand-in keeps the journal plain JSON
            return {"ok": True, "value": tok, "num": OVER_RANGE_NUM}
        if tok in SHORT:             # SC: short circuit reads as zero
            return {"ok": True, "value": tok, "num": 0.0}
        try:
            n = float(s.replace(",", "."))
        except ValueError:
            return {"ok": False, "error": f"'{s}' is not a number"}
        if math.isnan(n) or math.isinf(n):
            return {"ok": False, "error": "not a finite number"}
        return {"ok": True, "value": s.replace(",", "."), "num": n}
    if kind == "yn":
        low = s.lower()
        if low in YES:
            return {"ok": True, "value": "yes", "num": None}
        if low in NO:
            return {"ok": True, "value": "no", "num": None}
        return {"ok": False, "error": "answer y or n"}
    if kind == "choice":
        opts = [str(o) for o in test.get("options", [])]
        for i, o in enumerate(opts):
            if s.lower() == o.lower() or s == str(i + 1):
                return {"ok": True, "value": o, "num": None}
        return {"ok": False, "error": "choose one of: " + ", ".join(opts)}
    if kind == "hex":
        h = s.lower().removeprefix("0x").replace("_", "").replace(" ", "")
        if not re.fullmatch(r"[0-9a-f]+", h):
            return {"ok": False, "error": f"'{s}' is not hexadecimal"}
        return {"ok": True, "value": "0x" + h.upper(), "num": int(h, 16)}
    return {"ok": True, "value": s, "num": None}  # text


IN_RANGE = "IN RANGE"          # a range confirmation by voice: no reading, num None; origin operator_confirmed, never typed


def has_numeric_limit(test: dict) -> bool:
    lim = test.get("limit") or {}
    return test.get("kind") == "number" and ("min" in lim or "max" in lim)


RANK = {"none": 0, "pass": 1, "review": 2, "fail": 3}


def table_columns(test: dict) -> list[str]:
    return [c["id"] for c in test.get("columns") or []]


def evaluate_table(test: dict, raw) -> dict:
    """A table result: {"columns": [ids], "rows": [[...]]} (optional "attachment": CSV path, "plot" is declared on the test).
    Each limited column gives a row a verdict by the same rules as a scalar limit; the cell verdict is the worst row."""
    def bad(msg):
        return {"ok": False, "error": msg, "verdict": "invalid", "flags": ["invalid"], "reason": msg}
    v = raw
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return bad("a table result is a JSON object {columns, rows}; it cannot be typed")
    if not isinstance(v, dict) or not isinstance(v.get("rows"), list):
        return bad("a table result needs columns and rows")
    declared = table_columns(test)
    cols = [str(c) for c in (v.get("columns") or declared)]
    unknown = [c for c in cols if c not in declared]
    if unknown:
        return bad("unknown column(s): " + ", ".join(unknown))
    limits = test.get("limits") or {}
    rows, evs = [], []
    flags: list[str] = []
    for i, r in enumerate(v["rows"]):
        if isinstance(r, dict):
            r = [r.get(c) for c in cols]
        if not isinstance(r, (list, tuple)) or len(r) != len(cols):
            return bad(f"row {i + 1} has {len(r) if isinstance(r, (list, tuple)) else 'no'} cells; expected {len(cols)}")
        row, rv, bad_cols = [], "none", []
        for cid, cell in zip(cols, r):
            lim = limits.get(cid)
            if lim:
                try:
                    cell = float(cell)
                except (TypeError, ValueError):
                    return bad(f"row {i + 1}: column {cid} is not a number")
                if math.isnan(cell) or math.isinf(cell):
                    return bad(f"row {i + 1}: column {cid} is not finite")
                lo, hi = lim.get("min"), lim.get("max")
                inside = (lo is None or cell >= lo) and (hi is None or cell <= hi)
                cls = lim.get("class")
                cv = "none" if cls in (None, "record", "tbr") else ("pass" if inside else ("fail" if cls == "hard" else "review"))
                if not inside:
                    bad_cols.append(cid)
                    if cls in ("guess", "tbr"):
                        flags.append("outside_" + cls)
                if RANK[cv] > RANK[rv]:
                    rv = cv
                elif cv == "pass" and rv == "none":
                    rv = "pass"
            row.append(cell)
        rows.append(row)
        evs.append({"verdict": rv, "out": bad_cols})
    worst = max((e["verdict"] for e in evs), key=RANK.get, default="none")
    outs = [(i, e) for i, e in enumerate(evs) if e["out"]]
    reason = ""
    if outs:
        i, e = outs[0]
        cid = e["out"][0]
        col = next((c for c in test.get("columns") or [] if c["id"] == cid), {})
        lim = limits[cid]
        lo, hi = lim.get("min"), lim.get("max")
        band = f"{_fmt(lo)} to {_fmt(hi)}" if lo is not None and hi is not None else (f">= {_fmt(lo)}" if lo is not None else f"<= {_fmt(hi)}")
        u = f" {col['unit']}" if col.get("unit") else ""
        reason = f"{len(outs)} of {len(rows)} rows out of band; row {i + 1}: {cid} {_fmt(rows[i][cols.index(cid)])}{u} outside {band}{u} ({lim.get('class')} limit)"
    value = {"columns": cols, "rows": rows}
    if v.get("attachment"):
        value["attachment"] = str(v["attachment"])
    return {"ok": True, "value": value, "num": None, "verdict": worst, "flags": sorted(set(flags)), "reason": reason,
            "inside": not outs, "class": None, "row_verdicts": evs, "limit_ref": "table"}


def evaluate(test: dict, raw: str, range_ok: bool = False) -> dict:
    """Return {ok, error?, value, num, verdict, flags, reason}. verdict: pass|fail|review|none.
    range_ok: the operator confirmed the reading is inside the limit without giving a number (voice); only for min/max limits."""
    if test["kind"] == "table":
        return evaluate_table(test, raw)
    if range_ok and str(raw).strip().upper() == IN_RANGE:
        if not has_numeric_limit(test):
            return {"ok": False, "error": "a range confirmation needs a number test with a min or max limit", "verdict": "invalid",
                    "flags": ["invalid"], "reason": "no numeric limit"}
        cls = test["limit"].get("class")
        verdict = "pass" if cls in ("hard", "guess") else "none"
        return {"ok": True, "value": IN_RANGE, "num": None, "verdict": verdict, "flags": [], "reason": "", "inside": True, "class": cls}
    n = normalise(test, raw)
    if not n["ok"]:
        return {"ok": False, "error": n["error"], "verdict": "invalid", "flags": ["invalid"], "reason": n["error"]}
    lim = test.get("limit")
    cls = (lim or {}).get("class")
    inside, reason = None, ""
    if lim is None:
        if test["kind"] == "yn":
            inside, cls = n["value"] == "yes", "hard"
            reason = "expected yes"
    else:
        if test["kind"] in ("number", "hex") and ("min" in lim or "max" in lim):
            v = n["num"]
            lo, hi = lim.get("min"), lim.get("max")
            inside = (lo is None or v >= lo) and (hi is None or v <= hi)
            if not inside:
                reason = f"{raw.strip()} outside {expected_text(test)}"
        elif "equals" in lim or "in" in lim:
            allowed = [lim["equals"]] if "equals" in lim else list(lim["in"])
            if test["kind"] == "hex":
                ok = False
                for a in allowed:
                    try:
                        ok = ok or int(str(a), 16) == n["num"]
                    except ValueError:
                        pass
                inside = ok
            elif test["kind"] == "yn":
                inside = n["value"] in [("yes" if str(a).lower() in YES else "no") for a in allowed]
            else:
                inside = n["value"].lower() in [str(a).lower() for a in allowed]
            if not inside:
                reason = f"{n['value']} not {expected_text(test)}"
    flags = []
    if inside is None or cls in (None, "record"):
        verdict = "none"
    elif cls == "hard":
        verdict = "pass" if inside else "fail"
    elif cls == "guess":
        verdict = "pass" if inside else "review"
    else:  # tbr: never a verdict
        verdict = "none"
    if inside is False and cls in ("guess", "tbr"):
        flags.append("outside_" + cls)
    return {"ok": True, "value": n["value"], "num": n["num"], "verdict": verdict, "flags": flags,
            "reason": reason, "inside": inside, "class": cls}


def stop_tripped(test: dict, ev: dict) -> str | None:
    si = test.get("stop_if")
    if not si or not ev.get("ok"):
        return None
    text = si.get("text", "stop")
    if test.get("kind") == "table":          # stop_if: {column: <id>, below|above|outside: ...} fires on any row
        cols = ev["value"]["columns"]
        if si.get("column") not in cols:
            return None
        j = cols.index(si["column"])
        for r in ev["value"]["rows"]:
            x = r[j]
            if ("below" in si and x < si["below"]) or ("above" in si and x > si["above"]) or \
                    ("outside" in si and not (si["outside"][0] <= x <= si["outside"][1])):
                return text
        return None
    if "below" in si and ev["num"] is not None and ev["num"] < si["below"]:
        return text
    if "above" in si and ev["num"] is not None and ev["num"] > si["above"]:
        return text
    if "outside" in si and ev["num"] is not None and not (si["outside"][0] <= ev["num"] <= si["outside"][1]):
        return text
    if "equals" in si and str(ev["value"]).lower() == str(si["equals"]).lower():
        return text
    return None


# ----------------------------------------------------------------------------- state


NOTE_CATEGORIES = ("observation", "design issue", "process", "question")
MANUAL_PREFIX = "MANUAL-"
# operator-picked kind -> the AOI kind code that decides which visual tests it fails (aoi/link.py) and the Label Studio class
MANUAL_KIND_CODE = {"bridge": "BRIDGE", "missing": "MISSING", "lifted": "LIFT", "polarity": "POL", "tombstone": "LIFT", "damage": "ANOM", "other": "ANOM"}


NOTE_VERSION_KEYS = ("text", "category", "suggested_fix", "step", "boards", "test_id", "line", "designators", "aoi_key", "scan")


def board_labels(n: int, prefix: str) -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(1, n + 1)]


class State:
    def __init__(self, proto: Protocol):
        self.proto = proto
        self.meta: dict[str, Any] = {}
        self.boards: list[dict] = []
        self.attempts: dict[tuple, int] = {}
        self.retests: list[dict] = []
        self.cells: dict[tuple, dict] = {}
        self.overrides: dict[tuple, str] = {}
        self.placed: dict[str, set] = {}
        self.stops: list[dict] = []
        self.notes: list[dict] = []
        self.note_by_id: dict[int, dict] = {}
        self.errata_exports: list[dict] = []
        self.actions: list[dict] = []
        self.scans: list[dict] = []
        self.project: dict[str, Any] = {"select": None, "crosscheck": None, "accepted": None, "skipped": None, "history": []}
        self.alog: list[dict] = []          # assembly log: placements and scans, derived from the journal
        self.assembly: dict[str, Any] = {"declared": "start", "since": None}   # Assembly aid start page (UIR-20): start | assembling | skipped
        self.reports: list[dict] = []
        self.voice: list[dict] = []                 # voice utterances (journal event 'voice'), last 300
        self.aoi_photos: dict[str, dict] = {}       # file (relative to the batch folder) -> board, side, name
        self.aoi_runs: list[dict] = []
        self.aoi_decisions: dict[str, dict] = {}    # '<board>|<result id>' -> decision record (operator is the source)
        self.aoi_manual: dict[str, dict] = {}       # '<board>|MANUAL-<event id>' -> operator-found issue (aoi_manual_issue), independent of any run
        self.aoi_reworked: dict[str, dict] = {}     # '<board>|<result id>' -> {note, ts, operator}: a confirmed issue the operator marked reworked
        self.accepted_protocol: str = ""            # hash of the latest protocol_accepted event ('' until one is journalled)
        self.protocol_accepts: list[dict] = []
        self.assigns: dict[tuple, dict] = {}        # (board, step) -> {sections, attempt, via, reason}: Tier 2 sections the operator or the gate opened
        self.gate_rejects: list[dict] = []
        self.plan: dict | None = None               # the last plan_loaded event
        self.plan_history: list[dict] = []
        self.known_tests: dict[str, dict] = {}      # every test id a plan_loaded event ever listed
        self.sequences: dict[tuple, dict] = {}      # (board, step) -> the latest automated-sequence record (journal event 'sequence')
        self.events = 0
        self.last_ts = ""
        self.test_index = {t["id"]: (s, sec, t) for s, sec, t in proto.tests()}

    # ---- replay
    def apply(self, ev: dict):
        self.events += 1
        if ev["type"] not in ("report_generated", "errata_export"):
            self.last_ts = ev["ts"]
        p, ty = ev["payload"], ev["type"]
        h = getattr(self, "_on_" + ty, None)
        if h:
            h(ev, p)

    def _cell(self, b, t, a) -> dict:
        return self.cells.setdefault((b, t, a), {"status": "empty", "proposals": {}, "history": []})

    def _on_batch_created(self, ev, p):
        self.meta = {"name": p["name"], "simulate": p.get("simulate", False), "protocol_hash": p.get("protocol_hash", ""),
                     "created": ev["ts"], "prefix": p.get("prefix", "S"), "design": p.get("design", "")}
        self._set_boards(p["boards"])

    def _set_boards(self, labels):
        old = {b["label"]: b for b in self.boards}
        self.boards = [old.get(l, {"label": l, "serial": ""}) for l in labels]
        for l in labels:
            self.placed.setdefault(l, set())

    def _on_boards_set(self, ev, p):
        self._set_boards(p["boards"])

    def _on_serial(self, ev, p):
        for b in self.boards:
            if b["label"] == p["board"]:
                b["serial"] = p["serial"]

    def _on_operator(self, ev, p):
        self.meta["operator"] = p["name"]

    def _on_proposal(self, ev, p):
        c = self._cell(p["board"], p["test_id"], p["attempt"])
        key = p["source"].get("key") or p["source"].get("component") or p["source"].get("kind")
        c["proposals"][key] = {**p, "id": ev["id"], "ts": ev["ts"]}

    def _accept(self, ev, p, replaced=False):
        c = self._cell(p["board"], p["test_id"], p["attempt"])
        if c["status"] == "accepted":
            c["history"].append({**{k: c.get(k) for k in ("value", "unit", "verdict", "source", "ts")}, "remeasured": bool(p.get("remeasure"))})
        for o in p.get("overrides") or []:      # an instrument value the operator typed over before it was ever a result: kept beside the earlier results
            c["history"].append({**{k: v for k, v in o.items() if k != "proposal_id"}, "remeasured": False, "proposed_only": True})
            for k in [k for k, q in c["proposals"].items() if q["id"] == o.get("proposal_id")]:
                c["proposals"].pop(k)
        c.update(status="accepted", value=p["value"], unit=p.get("unit"), verdict=p["verdict"], flags=p.get("flags", []),
                 source=p["source"], origin=p.get("origin", "measured"), ts=ev["ts"], event=ev["id"],
                 evidence=p.get("evidence"), raw=p.get("raw"), accepted_fail=p["verdict"] in ("fail", "review"),
                 skip_reason=None, reason=p.get("reason", ""), row_verdicts=p.get("row_verdicts"), auto=bool(p.get("auto")))
        # consume the proposal that was accepted and any with the same value
        for k in [k for k, q in c["proposals"].items() if q["id"] == p.get("proposal_id") or q["value"] == p["value"]]:
            c["proposals"].pop(k)

    _on_entry = _accept
    _on_edit = _accept
    _on_accept = _accept

    def _on_undo(self, ev, p):
        """An accepted value taken back (meter auto-accept Undo). Only the accept event it names is undone; a later change wins."""
        c = self._cell(p["board"], p["test_id"], p["attempt"])
        if c["status"] != "accepted" or c.get("event") != p.get("event"):
            return
        c.update(status="empty", value=None, unit=None, verdict="none", flags=[], source=None, event=None, evidence=None, raw=None,
                 accepted_fail=False, reason="", row_verdicts=None, auto=False, origin=None, ts=ev["ts"])

    def _on_reject(self, ev, p):
        c = self._cell(p["board"], p["test_id"], p["attempt"])
        for k in [k for k, q in c["proposals"].items() if q["id"] == p["proposal_id"]]:
            c["proposals"].pop(k)

    def _on_skip(self, ev, p):
        c = self._cell(p["board"], p["test_id"], p["attempt"])
        c.update(status="skipped", skip_reason=p["reason"], ts=ev["ts"], verdict="none", value=None, flags=[])

    def _on_voice(self, ev, p):
        self.voice.append({**p, "id": ev["id"], "ts": ev["ts"]})
        del self.voice[:-300]

    def _on_retest_open(self, ev, p):
        self.attempts[(p["board"], p["step"])] = p["attempt"]
        self.retests.append({**p, "ts": ev["ts"], "id": ev["id"]})

    def _on_plan_loaded(self, ev, p):
        self.plan = {**p, "ts": ev["ts"], "id": ev["id"]}
        self.plan_history.append({"hash": p.get("hash"), "ts": ev["ts"], "tests": p.get("count"), "file": p.get("file")})
        for t in p.get("tests", []):
            self.known_tests[t["id"]] = t

    def _on_tier2_assign(self, ev, p):
        cur = self.assigns.get((p["board"], p["step"]))
        secs = set(p["sections"])
        if cur and cur["attempt"] == p["attempt"]:
            secs |= set(cur["sections"])
        self.assigns[(p["board"], p["step"])] = {"sections": sorted(secs), "attempt": p["attempt"], "via": p.get("via", "operator"),
                                                 "reason": p.get("reason", ""), "ts": ev["ts"]}

    def _on_tier2_remove(self, ev, p):
        cur = self.assigns.get((p["board"], p["step"]))
        if cur:
            cur["sections"] = [s for s in cur["sections"] if s not in set(p["sections"])]

    def _on_gate_reject(self, ev, p):
        self.gate_rejects.append({**p, "ts": ev["ts"]})

    def _on_gate_override(self, ev, p):
        self.overrides[(p["board"], p["step"])] = p["reason"]

    def _on_stop(self, ev, p):
        self.stops.append({**p, "id": ev["id"], "ts": ev["ts"], "ack": None})

    def _on_stop_ack(self, ev, p):
        for s in self.stops:
            if s["id"] == p["stop_id"]:
                s["ack"] = {"reason": p["reason"], "ts": ev["ts"]}

    def _on_placed(self, ev, p):
        s = self.placed.setdefault(p["board"], set())
        for d in p["designators"]:
            (s.add if p["on"] else s.discard)(d)
        self.alog.append({"id": ev["id"], "ts": ev["ts"], "operator": ev.get("operator", ""), "kind": "place" if p["on"] else "clear",
                          "line": self.line_of(p["designators"]), "boards": [p["board"]], "designators": list(p["designators"]),
                          "via": "per board", "code": ""})

    def _on_placed_batch(self, ev, p):
        for b in p["boards"]:
            s = self.placed.setdefault(b, set())
            for d in p["designators"]:
                (s.add if p["on"] else s.discard)(d)
        self.alog.append({"id": ev["id"], "ts": ev["ts"], "operator": ev.get("operator", ""), "kind": "place" if p["on"] else "clear",
                          "line": p.get("line"), "boards": list(p["boards"]), "designators": list(p["designators"]),
                          "via": p.get("via", "button"), "code": p.get("code", ""), "auto": bool(p.get("auto"))})

    def _on_auto_cancel(self, ev, p):
        self.alog.append({"id": ev["id"], "ts": ev["ts"], "operator": ev.get("operator", ""), "kind": "auto_cancel", "line": p.get("line"),
                          "boards": [], "designators": [], "via": "auto-place", "code": p.get("code", ""), "reason": p.get("reason", "")})

    # ---- Assembly aid start page (UIR-20): Assemble, skip (boards already built) and reverse. Three journal events; the state rebuilds from them.
    def _on_assembly_begin(self, ev, p):
        self.assembly = {"declared": "assembling", "since": ev["ts"]}
        self.alog.append({"id": ev["id"], "ts": ev["ts"], "operator": ev.get("operator", ""), "kind": "begin", "line": None, "boards": [],
                          "designators": [], "via": "start page", "code": ""})

    def _on_assembly_skip(self, ev, p):
        self.assembly = {"declared": "skipped", "since": ev["ts"]}
        self.alog.append({"id": ev["id"], "ts": ev["ts"], "operator": ev.get("operator", ""), "kind": "skip", "line": None, "boards": [],
                          "designators": [], "via": "start page", "code": ""})

    def _on_assembly_unskip(self, ev, p):
        self.assembly = {"declared": "assembling", "since": ev["ts"]}
        self.alog.append({"id": ev["id"], "ts": ev["ts"], "operator": ev.get("operator", ""), "kind": "unskip", "line": None, "boards": [],
                          "designators": [], "via": "start page", "code": ""})

    def has_placements(self) -> bool:
        return any(self.placed.values()) or bool(self.scans)

    def assembly_mode(self) -> str:
        """What the Assembly aid shows: 'start' (nothing placed, Assemble or skip not chosen), 'assembling' or 'skipped'. A batch that
        already has placements or scans is always assembling, so a journal from before the start page keeps working unchanged."""
        d = self.assembly["declared"]
        if d == "skipped":
            return "skipped"
        return "assembling" if d == "assembling" or self.has_placements() else "start"

    # ---- step 1, Project (ECAD link, document, BOM cross-check)
    def _on_project_select(self, ev, p):
        self.project["select"] = {**p, "ts": ev["ts"], "operator": ev.get("operator", "")}
        self.project["crosscheck"], self.project["accepted"], self.project["skipped"] = None, None, None
        self.project["history"].append({"ts": ev["ts"], "what": f"selected {p.get('document')} ({p.get('elements')} parts, {p.get('fingerprint')})"})

    def _on_bom_crosscheck(self, ev, p):
        self.project["crosscheck"] = {**p, "ts": ev["ts"]}
        self.project["accepted"] = None

    def _on_bom_crosscheck_accept(self, ev, p):
        self.project["accepted"] = {**p, "ts": ev["ts"], "operator": ev.get("operator", "")}
        self.project["history"].append({"ts": ev["ts"], "what": f"cross-check reviewed, {p.get('mismatches', 0)} mismatch(es) noted"})

    def _on_ecad_skipped(self, ev, p):
        self.project["skipped"] = {**p, "ts": ev["ts"], "operator": ev.get("operator", "")}
        self.project["history"].append({"ts": ev["ts"], "what": "continued without ECAD: " + str(p.get("reason", ""))})

    def _on_ecad_skip_cleared(self, ev, p):
        self.project["skipped"] = None
        self.project["history"].append({"ts": ev["ts"], "what": "ECAD skip cleared"})

    # ---- project page: settings file changes (polybench-project.json), journalled in the current batch
    def _on_ecad_platform(self, ev, p):
        """The operator chose another ECAD platform: the document recorded with the old one is no longer valid for highlights."""
        what = f"ECAD platform {p.get('previous') or 'none'} -> {p.get('program')}"
        cl = p.get("cleared_document")
        if cl:                      # only a document recorded with another platform is cleared (DV_CONTRACT: cleared_document)
            self.project["select"], self.project["crosscheck"], self.project["accepted"] = None, None, None
            what += f"; document {cl.get('document')} cleared, confirm the document again"
        self.project["history"].append({"ts": ev["ts"], "what": what})

    def _on_bom_source(self, ev, p):
        # the recorded cross-check compared the old BOM: it no longer holds (Session recomputes it right after when it can)
        self.project["crosscheck"], self.project["accepted"] = None, None
        verb = {"loaded": "BOM loaded", "replaced": "BOM replaced", "removed": "BOM file removed"}.get(p.get("action"), "BOM changed")
        self.project["history"].append({"ts": ev["ts"], "what": f"{verb}: {p.get('name') or p.get('previous_name') or ''}"
                                        + (f" ({p.get('assembly_events')} assembly events kept)" if p.get("assembly_events") else "")})

    def _on_project_settings(self, ev, p):
        what = {"created": "project created", "adopted": "project folder adopted in place", "opened": "project opened",
                "ecad_document": "ECAD document saved in the project settings"}.get(p.get("change"), f"project {p.get('change')}")
        h = self.project["history"]
        last = next((x for x in reversed(h) if x["what"].startswith("ECAD platform ")), None)
        if p.get("restored") and last and last["what"].endswith("cleared, confirm the document again"):
            # the same platform switch put the document back: say so instead of asking for a new confirmation
            doc = (p.get("document") or {}).get("name") or "the document"
            last["what"] = last["what"][:-len(", confirm the document again")] + f"; restored {doc}"
            return
        h.append({"ts": ev["ts"], "what": what + (f", batch {p['batch']}" if p.get("batch") else "")})

    def notes_public(self) -> list[dict]:
        out = []
        for n in self.notes:
            d = {k: v for k, v in n.items() if k != "history"}
            d["n_versions"] = 1 + len(n["history"])
            out.append(d)
        return out

    def project_state(self) -> dict:
        pj = self.project
        done = bool(pj["skipped"]) or bool(pj["select"] and pj["accepted"])
        part = bool(pj["select"]) and not done
        out = {"state": "done" if done else "partial" if part else "empty", "select": pj["select"], "crosscheck": pj["crosscheck"],
               "accepted": pj["accepted"], "ecad_skipped": pj["skipped"], "history": pj["history"][-12:]}
        return out

    # ---- operator notes (journal event 'note'; edits are 'note_edit' events that reference the first one)
    def _note_fields(self, p) -> dict:
        """One normalised note version from a payload. Old journals carry only {board, test_id, text}."""
        boards = list(p.get("boards") or ([p["board"]] if p.get("board") else []))
        tid = p.get("test_id") or ""
        step = p.get("step") or (self.test_index[tid][0]["id"] if tid in self.test_index else "")
        des = list(p.get("designators") or [])
        lines = []
        for b in self.proto.data["bom"]:
            if b["line"] == p.get("line") or set(des) & set(b["designators"]):
                lines.append(b["line"])
        cat = p.get("category") if p.get("category") in NOTE_CATEGORIES else "observation"
        return {"text": p.get("text", ""), "category": cat, "suggested_fix": p.get("suggested_fix", "") if cat == "design issue" else "",
                "step": step, "boards": boards, "board": boards[0] if boards else "", "test_id": tid, "line": p.get("line"),
                "lines": sorted(set(lines)), "designators": des, "aoi_key": p.get("aoi_key") or "", "scan": p.get("scan") or ""}

    def _on_note(self, ev, p):
        n = {"id": ev["id"], "ts": ev["ts"], "operator": ev.get("operator", ""), **self._note_fields(p), "edits": [], "history": []}
        self.notes.append(n)
        self.note_by_id[ev["id"]] = n

    def _on_note_edit(self, ev, p):
        n = self.note_by_id.get(p.get("ref"))
        if n is None:
            return                      # an edit of a note this journal does not hold: ignored, never a crash
        n["history"].append({k: n[k] for k in NOTE_VERSION_KEYS} | {"ts": n["edited_ts"] if n["edits"] else n["ts"],
                                                                  "operator": n["edit_operator"] if n["edits"] else n["operator"]})
        n.update(self._note_fields(p))
        n["edits"].append(ev["id"])
        n["edited_ts"], n["edit_operator"] = ev["ts"], ev.get("operator", "")

    def _on_errata_export(self, ev, p):
        self.errata_exports.append({**p, "ts": ev["ts"], "id": ev["id"]})

    def _on_scan(self, ev, p):
        self.scans.append({**p, "ts": ev["ts"]})
        self.alog.append({"id": ev["id"], "ts": ev["ts"], "operator": ev.get("operator", ""), "kind": "scan" if p.get("line") else "scan_miss",
                          "line": p.get("line"), "boards": [], "designators": [], "via": p.get("via", "typed"),
                          "code": p.get("code", ""), "matched_by": p.get("matched_by", ""), "qty": p.get("qty")})

    def _on_sequence(self, ev, p):
        """One event type, a `phase` each: start, end, proceed, defects, final. Rebuilds the record the verdict panel and the report show."""
        key = (p["board"], p["step"])
        ph = p.get("phase")
        if ph == "start":
            self.sequences[key] = {"seq": p.get("seq"), "attempt": p.get("attempt", 1), "started": ev["ts"], "plan": p.get("items", []),
                                   "simulate": bool(p.get("simulate")), "outcome": "running", "items": [], "stop": None, "proceed": None,
                                   "defects": None, "final": None, "image": None, "bench": None}
            return
        rec = self.sequences.get(key)
        if not rec or rec.get("seq") != p.get("seq"):
            return
        if ph == "bench":
            rec["bench"] = {"equipment": p.get("equipment", []), "results": p.get("results", []), "skips": {}, "ts": ev["ts"]}
        elif ph == "bench_ok":
            if rec.get("bench"):
                rec["bench"]["skips"] = p.get("skips") or p.get("overrides") or {}
                rec["bench"]["confirmed"] = ev["ts"]
        elif ph == "end":
            rec.update(outcome=p.get("outcome"), stop=p.get("stop"), items=p.get("items", []), ended=ev["ts"])
        elif ph == "proceed":
            rec["proceed"] = {k: p.get(k) for k in ("accepted", "confirmed", "ts")} | {"ts": ev["ts"]}
            rec["image"] = p.get("image")
        elif ph == "defects":
            rec["defects"] = {"tests": p.get("tests", []), "not_run": p.get("not_run", 0), "note": p.get("note", ""), "ts": ev["ts"]}
        elif ph == "final":
            rec["final"] = {k: p.get(k) for k in ("state", "run", "text", "dur_s")} | {"ts": ev["ts"]}
            rec["image"] = p.get("image") or rec.get("image")

    def _on_action_start(self, ev, p):
        self.actions.append({**p, "start": ev["ts"], "state": "running"})

    def _on_action_end(self, ev, p):
        for a in reversed(self.actions):
            if a["run"] == p["run"]:
                a.update(state=p["state"], end=ev["ts"], text=p.get("text", ""))
                break

    def _on_safe_state(self, ev, p):
        self.actions.append({"run": 0, "plugin": p.get("plugin"), "state": "safe_state", "start": ev["ts"], "text": p.get("text", "")})

    # ---- AOI (events added 16 Oct; older journals simply contain none of them)
    def _on_aoi_photo(self, ev, p):
        self.aoi_photos[p["file"]] = {**p, "ts": ev["ts"], "id": ev["id"]}

    def _on_aoi_photo_move(self, ev, p):
        ph = self.aoi_photos.get(p["file"])
        if ph:
            ph["board"] = p["to"]

    def _on_aoi_photo_removed(self, ev, p):
        self.aoi_photos.pop(p["file"], None)

    def _on_aoi_photo_drop(self, ev, p):
        self.aoi_photos.pop(p["file"], None)

    def _on_aoi_run(self, ev, p):
        self.aoi_runs.append({**p, "ts": ev["ts"], "id": ev["id"]})

    def _on_aoi_reworked(self, ev, p):
        if p.get("on", True):
            self.aoi_reworked[p["key"]] = {"key": p["key"], "board": p.get("board"), "note": p.get("note", ""), "ts": ev["ts"],
                                           "operator": ev.get("operator", "")}
        else:
            self.aoi_reworked.pop(p["key"], None)

    def _on_protocol_accepted(self, ev, p):
        self.accepted_protocol = p.get("hash", "")
        self.protocol_accepts.append({"hash": p.get("hash", ""), "old_hash": p.get("old_hash", ""), "reason": p.get("reason", ""),
                                      "tests_before": p.get("tests_before"), "tests_after": p.get("tests_after"),
                                      "ts": ev["ts"], "operator": ev.get("operator", "")})

    def _on_aoi_decision(self, ev, p):
        key = p["key"]
        self.aoi_reworked.pop(key, None)          # a new decision starts the issue afresh
        if p["decision"] is None:
            self.aoi_decisions.pop(key, None)
        else:
            self.aoi_decisions[key] = {"key": key, "board": p["board"], "id": p["result_id"], "decision": p["decision"],
                                       "note": p.get("note", ""), "decided_at": ev["ts"], "operator": ev.get("operator", ""),
                                       "aoi": p.get("aoi", {}), "run": p.get("run"), "tests": p.get("tests", []),
                                       **({"mode": p["mode"], "issue_board": p.get("issue_board")} if p.get("mode") else {})}
        for tid in p.get("clear_tests", []):
            c = self.cells.get((p["board"], tid, p["attempt"]))
            if c and c["status"] == "accepted" and (c.get("source") or {}).get("key") == key:
                c["history"].append({**{k: c.get(k) for k in ("value", "unit", "verdict", "source", "ts")}, "remeasured": bool(p.get("remeasure"))})
                c.update(status="empty", value=None, verdict=None, flags=[], reason="", source=None, accepted_fail=False)
        for tid in p.get("fail_tests", []):
            self._accept(ev, {"board": p["board"], "test_id": tid, "attempt": p["attempt"], "value": "no", "unit": None,
                              "verdict": "fail", "flags": ["aoi"], "origin": p.get("origin", "measured"),
                              "source": {"kind": "aoi_review", "component": "aoi", "key": key, "operator": ev.get("operator", ""),
                                         "input_method": "aoi review"},
                              "evidence": p.get("evidence"), "raw": json.dumps(p.get("aoi", {}), sort_keys=True),
                              "reason": f"operator confirmed AOI issue {p['result_id']}"})

    def _on_aoi_manual_issue(self, ev, p):
        """An issue the operator found that AOI did not list (an AOI miss). It is decided as confirmed at once and fails the
        matching visual tests exactly like a confirmed candidate. Box is in canonical board millimetres (boardview frame)."""
        rid = f"{MANUAL_PREFIX}{ev['id']}"
        key = f"{p['board']}|{rid}"
        code = MANUAL_KIND_CODE.get(p["kind"], "ANOM")
        des = [d for d in p.get("designators", []) if d]
        self.aoi_manual[key] = {"key": key, "board": p["board"], "id": rid, "kind": code, "manual_kind": p["kind"], "status": "fail",
                                "value": "operator-found", "note": p.get("note", ""), "confidence": 1.0, "evidence": [], "manual": True,
                                "designators": des, "designator": des[0] if des else "", "box_mm": p["box_mm"], "operator": ev.get("operator", ""),
                                "ts": ev["ts"], "event": ev["id"]}
        aoi = {"status": "fail", "value": "operator-found", "confidence": 1.0, "note": p.get("note", ""), "kind": code, "image": None,
               "evidence_sha": "", "manual": True, "manual_kind": p["kind"], "designators": des, "box_mm": p["box_mm"]}
        self._on_aoi_decision(ev, {"key": key, "board": p["board"], "result_id": rid, "decision": "confirmed", "note": p.get("note", ""),
                                   "aoi": aoi, "run": None, "tests": p.get("tests", []), "attempt": p.get("attempt", 1),
                                   "fail_tests": p.get("tests", []), "clear_tests": [], "evidence": None, "origin": p.get("origin", "measured")})

    def aoi_all_items(self, run=None):
        """The review list: the candidates of the run, then the operator-found issues (they belong to the board, not to a run)."""
        run = run or self.aoi_current_run()
        return list((run or {}).get("items", [])) + list(self.aoi_manual.values())

    def aoi_snapshot(self):
        cfg = self.proto.data.get("aoi")
        if not cfg:
            return None
        run = self.aoi_current_run()
        counts = {"total": 0, "decided": 0, "confirmed": 0, "rejected": 0, "unsure": 0}
        per_board = {}
        all_items = self.aoi_all_items(run) if run else []
        decisions = {}
        for it in all_items:
            k = it["key"]
            b = per_board.setdefault(it["board"], {"total": 0, "confirmed": 0, "rejected": 0, "unsure": 0, "undecided": 0})
            b["total"] += 1
            counts["total"] += 1
            d = self.aoi_decision_for(k, run)
            if d:
                b[d["decision"]] += 1
                counts["decided"] += 1
                counts[d["decision"]] += 1
                decisions[k] = d
            else:
                b["undecided"] += 1
        from .aoi import link as _link
        checklist = [{"key": it["key"], "board": it["board"], "id": it["id"], "kind": it["kind"], "manual": bool(it.get("manual")),
                      "manual_kind": it.get("manual_kind", ""), "designator": (it.get("designators") or _link.designators_of(it["id"]) or [""])[0],
                      "designators": it.get("designators") or _link.designators_of(it["id"]),
                      "decision": (decisions.get(it["key"]) or {}).get("decision"), "reworked": it["key"] in self.aoi_reworked} for it in all_items]
        status = {b["label"]: self.aoi_board_status(b["label"]) for b in self.boards}
        for lbl, st in status.items():
            per_board.setdefault(lbl, {"total": 0, "confirmed": 0, "rejected": 0, "unsure": 0, "undecided": 0})
            per_board[lbl].update(reworked=st["reworked"], open_confirmed=st["open_confirmed"], state=st["state"], reason=st["reason"],
                                  photo=st["photo"], run=st["run"], decided=st["decided"])
        return {"photos": sorted(self.aoi_photos.values(), key=lambda x: (x["board"] or "~", x["side"], x["name"])),
                "run": {k: v for k, v in run.items() if k != "items"} if run else None, "counts": counts, "per_board": per_board,
                "decisions": decisions, "reworked": self.aoi_reworked, "items": checklist, "mode": "diff" if cfg.get("mode") == "diff" else None, "manual": len(self.aoi_manual), "runs": len(self.aoi_runs)}

    def aoi_board_status(self, board) -> dict:
        """What the AOI review says about one board. This is the Visual step for the board (16 Oct): done when a photo, a run
        and a decision on every item exist; attention while a confirmed issue is not reworked; unsure only warns."""
        run = self.aoi_current_run()
        photo = any(ph.get("board") == board for ph in self.aoi_photos.values())
        items = [i for i in self.aoi_all_items(run) if i["board"] == board]
        covers = bool(run) and (board in (run.get("boards") or []) or any((x or {}).get("board") == board for x in (run.get("photos") or []))
                                or bool(items))
        t = decided = confirmed = reworked = rejected = unsure = 0
        for it in items:
            t += 1
            d = self.aoi_decision_for(it["key"], run)
            if not d:
                continue
            decided += 1
            if d["decision"] == "confirmed":
                # diff mode: the operator names the board that has the issue; it need not be the board the region was listed under
                if d.get("issue_board") and d["issue_board"] != board:
                    rejected += 1
                    continue
                confirmed += 1
                if it["key"] in self.aoi_reworked:
                    reworked += 1
            elif d["decision"] == "rejected":
                rejected += 1
            else:
                unsure += 1
        for it in self.aoi_all_items(run):          # a region listed under another board, whose issue the operator put on this one
            if it["board"] == board:
                continue
            d = self.aoi_decision_for(it["key"], run)
            if d and d["decision"] == "confirmed" and d.get("issue_board") == board:
                confirmed += 1
                reworked += it["key"] in self.aoi_reworked
        open_c = confirmed - reworked
        if not photo:
            reason = f"no AOI photo for {board}"
        elif not covers:
            reason = f"no AOI run covers {board}"
        elif open_c:
            reason = f"{board} has {open_c} confirmed AOI issue{'s' if open_c != 1 else ''}"
        elif decided < t:
            reason = f"{board} has {t - decided} AOI item{'s' if t - decided != 1 else ''} not decided"
        else:
            reason = ""
        state = "empty" if not (photo and covers) else "attention" if open_c else "done" if decided == t else "partial"
        return {"photo": photo, "run": covers, "total": t, "decided": decided, "confirmed": confirmed, "reworked": reworked,
                "open_confirmed": open_c, "rejected": rejected, "unsure": unsure, "state": state, "reason": reason}

    def aoi_current_run(self):
        return self.aoi_runs[-1] if self.aoi_runs else None

    def aoi_decision_for(self, key, run=None):
        """The decision that counts for a candidate of the current run: one made on this run, or on an earlier run whose evidence
        (crop with its pad overlay) is byte-identical. A re-run starts undecided otherwise (16 Oct review)."""
        run = run or self.aoi_current_run()
        d = self.aoi_decisions.get(key)
        if key in self.aoi_manual:
            return d                      # an operator-found issue belongs to the board, whatever run is current
        if not d or not run:
            return None
        if d.get("run") == run["run"]:
            return d
        it = next((i for i in run.get("items", []) if i["key"] == key), None)
        sha = (d.get("aoi") or {}).get("evidence_sha")
        return d if it and sha and sha == it.get("evidence_sha") else None

    def _on_report_generated(self, ev, p):
        self.reports.append({**p, "ts": ev["ts"]})

    # ---- derived
    def attempt(self, board, step_id) -> int:
        return self.attempts.get((board, step_id), 1)

    def applies(self, step: dict, board: str) -> bool:
        at = step.get("applies_to", "all")
        return at == "all" or board in at

    def sec_applies(self, step: dict, sec: dict, board: str) -> bool:
        return tiers.sec_applies(self, step, sec, board)

    def retired(self) -> list[dict]:
        """Tests recorded or listed by an earlier plan that the current plan no longer has: their results stay."""
        ids = {tid for (_b, tid, _a) in self.cells if tid not in self.test_index} | {t for t in self.known_tests if t not in self.test_index}
        out = []
        for tid in sorted(ids):
            info = self.known_tests.get(tid, {})
            res = {}
            for (b, t2, a), c in self.cells.items():
                if t2 == tid and c.get("status") in ("accepted", "skipped"):
                    prev = res.get(b)
                    if not prev or a >= prev["attempt"]:
                        res[b] = {"attempt": a, "value": c.get("value"), "unit": c.get("unit"), "verdict": c.get("verdict"),
                                  "status": c["status"], "skip_reason": c.get("skip_reason")}
            out.append({"id": tid, "title": info.get("title", ""), "step": info.get("step", ""), "section": info.get("section", ""),
                        "results": res})
        return out

    def bom_lines(self):
        return sorted(self.proto.data["bom"], key=lambda b: (b.get("order", b["line"]), b["line"]))

    def line_of(self, designators):
        ds = set(designators)
        for b in self.bom_lines():
            if ds and ds <= set(b["designators"]):
                return b["line"]
        return None

    def line_status(self, line: dict) -> dict:
        """State of one BOM line over the batch: done / partial / todo / dnf, and which boards are missing."""
        labels_ = [b["label"] for b in self.boards]
        if line.get("dnf"):
            return {"state": "dnf", "done": [], "missing": [], "partial": [], "per_board": 0, "boards": len(labels_)}
        ds = set(line["designators"])
        done, missing, partial = [], [], []
        for l in labels_:
            n = len(ds & self.placed.get(l, set()))
            (done if n == len(ds) else partial if n else missing).append(l)
        state = "done" if len(done) == len(labels_) and labels_ else "partial" if (done or partial) else "todo"
        return {"state": state, "done": done, "missing": missing + partial, "partial": partial,
                "per_board": len(ds), "boards": len(labels_)}

    def bom_designators(self):
        return [d for b in self.bom_lines() if not b.get("dnf") for d in b["designators"]]

    def cell(self, board, test_id) -> dict:
        s, sec, t = self.test_index[test_id]
        a = self.attempt(board, s["id"])
        return self.cells.get((board, test_id, a)) or {"status": "empty", "proposals": {}, "history": []}

    def step_board(self, step: dict, board: str) -> dict:
        """Progress of one board in one step."""
        if not self.applies(step, board):
            return {"state": "na", "applicable": 0, "settled": 0}
        kind = step["kind"]
        fails = reviews = pend = 0
        if kind == "setup":
            ser = next((b["serial"] for b in self.boards if b["label"] == board), "")
            app, ok = 1, 1 if ser else 0
        elif kind == "assembly":
            des = self.bom_designators()
            app, ok = len(des), len(set(des) & self.placed.get(board, set()))
        elif kind == "report":
            return {"state": "na", "applicable": 0, "settled": 0}
        elif kind == "visual" and self.proto.data.get("aoi"):
            st = self.aoi_board_status(board)
            open_stops = sum(1 for x in self.stops if x["board"] == board and x.get("step") == step["id"] and not x["ack"])
            state = "attention" if open_stops and st["state"] != "empty" else st["state"]
            return {"state": state, "applicable": st["total"], "settled": st["decided"], "fails": st["open_confirmed"], "reviews": st["unsure"],
                    "pending": 0, "attempt": self.attempt(board, step["id"]), "stops": open_stops, "aoi": st}
        else:
            app = ok = 0
            for sec in step.get("sections", []):
                if not self.sec_applies(step, sec, board):
                    continue
                for t in sec.get("tests", []):
                    c = self.cell(board, t["id"])
                    if sec.get("diagnose") and c["status"] == "empty" and not c["proposals"]:
                        continue            # a diagnose test counts only once it holds a value
                    app += 1
                    if c["status"] in ("accepted", "skipped"):
                        ok += 1
                    if c["status"] == "accepted" and c.get("verdict") == "fail":
                        fails += 1
                    if c["status"] == "accepted" and c.get("verdict") == "review":
                        reviews += 1
                    pend += len(c["proposals"])
        open_stops = sum(1 for s in self.stops if s["board"] == board and s.get("step") == step["id"] and not s["ack"])
        if app and ok == app:
            state = "done"
        elif ok or pend:
            state = "partial"
        else:
            state = "empty"
        if app == 0:
            state = "empty"
        if fails or open_stops:
            state = "attention" if state != "empty" else state
        return {"state": state, "applicable": app, "settled": ok, "fails": fails, "reviews": reviews, "pending": pend,
                "attempt": self.attempt(board, step["id"]), "stops": open_stops}

    def gate(self, step: dict, board: str) -> dict:
        g = step.get("gate") or {}
        reasons = []
        warnings = []
        for rid in g.get("requires", []) or []:
            rs = self.proto.step(rid)
            if not rs or not self.applies(rs, board):
                continue
            sb = self.step_board(rs, board)
            if sb.get("aoi"):
                if sb["state"] != "done":
                    reasons.append(sb["aoi"]["reason"] or f"{rs['title']} is not complete for {board}")
                if sb["aoi"]["unsure"]:
                    warnings.append(f"{board} has {sb['aoi']['unsure']} AOI item{'s' if sb['aoi']['unsure'] != 1 else ''} marked ignored")
            elif sb["state"] != "done":
                reasons.append(f"{rs['title']} is not complete for {board} ({sb['settled']}/{sb['applicable']})")
            elif sb.get("fails"):
                reasons.append(f"{rs['title']} has {sb['fails']} accepted FAIL for {board}")
        if step.get("kind") == "powered":
            hold = tiers.hold_reason(self, board)
            if hold:
                reasons.append("power interlock: " + hold)
        ov = self.overrides.get((board, step["id"]))
        return {"ok": not reasons or bool(ov), "reasons": reasons, "override": ov, "warnings": warnings}

    def stopped(self, board) -> list[dict]:
        return [s for s in self.stops if s["board"] == board and not s["ack"]]

    # ---- snapshot for the UI
    def pending_instrument(self, board=None) -> list[tuple]:
        """Pending proposals that came from an action whose protocol records automatically: left over from a journal written before
        instrument readings became results. (board, test_id, proposal) for the current attempt of each."""
        modes = {}
        for st in self.proto.data["steps"]:
            for sec in st.get("sections", []):
                for a in sec.get("actions", []):
                    for tid in a.get("proposes") or []:
                        modes.setdefault((a.get("plugin"), tid), a.get("record", "auto"))
        out = []
        for (b, tid, att), c in self.cells.items():
            if (board and b != board) or tid not in self.test_index:
                continue
            s = self.test_index[tid][0]
            if att != self.attempt(b, s["id"]) or c["status"] == "accepted":
                continue
            for q in c["proposals"].values():
                src = q.get("source") or {}
                if src.get("kind") == "action" and modes.get((src.get("component"), tid), "auto") == "auto":
                    out.append((b, tid, q))
        return out

    def anomalies(self, board=None) -> list[dict]:
        """Every accepted result of the current attempt that needs the operator's eye, plus every open stop. An anomaly is a REVIEW
        or FAIL verdict, a value outside a guess or TBR limit, or a stop. An anomaly informs and never gates: nothing waits for it to
        be acknowledged. It stays listed until the step is redone (a retest opens a new attempt)."""
        out = []
        for s in self.proto.data["steps"]:
            for sec in s.get("sections", []):
                for t in sec.get("tests", []):
                    for b in self.boards:
                        bl = b["label"]
                        if (board and bl != board) or not self.applies(s, bl) or not self.sec_applies(s, sec, bl):
                            continue
                        c = self.cells.get((bl, t["id"], self.attempt(bl, s["id"])))
                        if not c or c["status"] != "accepted":
                            continue
                        flags = c.get("flags") or []
                        kind = c["verdict"] if c["verdict"] in ("fail", "review") else ("outside_guess" if "outside_guess" in flags else
                                "outside_tbr" if "outside_tbr" in flags else None)
                        if not kind:
                            continue
                        out.append({"board": bl, "step": s["id"], "test_id": t["id"], "title": t.get("title", t["id"]), "kind": kind,
                                    "verdict": c["verdict"], "value": c.get("value"), "unit": c.get("unit"), "attempt": c.get("attempt") or self.attempt(bl, s["id"]),
                                    "reason": self._anomaly_text(t, c), "auto": bool(c.get("auto")), "redo": self.redo_action(t["id"])})
        for st in self.stops:
            if not st["ack"] and (not board or st["board"] == board):
                out.append({"board": st["board"], "step": st.get("step"), "test_id": st["test_id"], "kind": "stop", "verdict": "stop",
                            "title": (self.test_index.get(st["test_id"]) or (0, 0, {}))[2].get("title", st["test_id"]),
                            "value": st.get("value"), "unit": None, "reason": st["text"], "stop_id": st["id"], "auto": False, "redo": None})
        return out

    def redo_action(self, test_id):
        """The action that measures a test (the first whose protocol 'proposes' it), as {step, section, action, title}, or None.
        Redo of that test re-runs the action through the normal run path; with no action, Redo clears the entry box."""
        for st in self.proto.data["steps"]:
            for sec in st.get("sections", []):
                for a in sec.get("actions", []):
                    if test_id in (a.get("proposes") or []):
                        return {"step": st["id"], "section": sec["id"], "action": a["id"], "title": a.get("title", a["id"])}
        return None

    @staticmethod
    def _anomaly_text(t, c):
        if t["kind"] == "table":
            return c.get("reason") or "table outside its limits"
        r = c.get("reason") or ""
        lim = t.get("limit") or {}
        if " outside " in r and t["kind"] in ("number", "hex"):
            u = f" {c['unit']}" if c.get("unit") else ""
            band = expected_text(t)
            return f"{c.get('value')}{u} outside {band}" + (f" ({lim['class']} limit)" if lim.get("class") else "")
        return r or "outside the limit"

    def snapshot(self) -> dict:
        proto = self.proto.data
        steps_out = []
        for s in proto["steps"]:
            so = {k: v for k, v in s.items() if k != "sections" and not k.startswith("_")}
            so["sections"] = []
            for sec in s.get("sections", []):
                sc = {k: v for k, v in sec.items() if k not in ("tests", "_file", "_line")}
                sc["tests"] = []
                for t in sec.get("tests", []):
                    to = dict(t)
                    to["expected"] = expected_text(t)
                    lim = t.get("limit") or {}
                    to["class"] = lim.get("class") or ("hard" if t["kind"] == "yn" else "")
                    to.pop("plan_line", None)
                    to["source_text"] = source_text(lim.get("source"))
                    sc["tests"].append(to)
                so["sections"].append(sc)
            so["per_board"] = {}
            tot_a = tot_s = 0
            retests = 0
            for b in self.boards:
                sb = self.step_board(s, b["label"])
                sb["gate"] = self.gate(s, b["label"])
                so["per_board"][b["label"]] = sb
                tot_a += sb["applicable"]
                tot_s += sb["settled"]
                retests += max(0, sb.get("attempt", 1) - 1)
            so["applicable"], so["settled"], so["retests"] = tot_a, tot_s, retests
            states = [x["state"] for x in so["per_board"].values() if x["state"] != "na"]
            so["fails"] = sum(x.get("fails", 0) for x in so["per_board"].values())
            if s["kind"] == "report":
                so["state"] = "available"
            elif not states or all(x == "empty" for x in states):
                so["state"] = "empty"
            elif all(x == "done" for x in states):
                so["state"] = "done"
            elif any(x == "attention" for x in states):
                so["state"] = "attention"
            else:
                so["state"] = "partial"
            steps_out.append(so)
        pj = self.project_state()
        pdone = pj["state"] == "done"
        n_set = 3 if pj["ecad_skipped"] else (1 if pj["select"] else 0) + (1 if pj["select"] else 0) + (1 if pj["accepted"] else 0)
        steps_out.insert(0, {"id": "project", "kind": "project", "title": "Project", "sections": [], "applies_to": "all",
                             "per_board": {b["label"]: {"state": "na", "applicable": 0, "settled": 0, "gate": {"ok": True, "reasons": [], "override": None}}
                                           for b in self.boards},
                             "applicable": 3, "settled": n_set, "retests": 0, "fails": 0, "state": pj["state"] if not pdone else "done"})
        cells = {}
        for s in proto["steps"]:
            for sec in s.get("sections", []):
                for t in sec.get("tests", []):
                    for b in self.boards:
                        bl = b["label"]
                        if not self.sec_applies(s, sec, bl):
                            continue
                        a = self.attempt(bl, s["id"])
                        c = self.cells.get((bl, t["id"], a))
                        hist = []
                        for pa in range(1, a):
                            pc = self.cells.get((bl, t["id"], pa))
                            if pc and pc["status"] != "empty":
                                hist.append({"attempt": pa, **{k: pc.get(k) for k in ("status", "value", "unit", "verdict", "skip_reason")}})
                        if not c and not hist:
                            continue
                        c = c or {"status": "empty", "proposals": {}, "history": []}
                        cells[f"{bl}|{t['id']}"] = {
                            "attempt": a, "status": c["status"], "value": c.get("value"), "unit": c.get("unit"),
                            "verdict": c.get("verdict"), "flags": c.get("flags", []), "source": c.get("source"),
                            "origin": c.get("origin"), "ts": c.get("ts"), "skip_reason": c.get("skip_reason"),
                            "evidence": c.get("evidence"), "reason": c.get("reason", ""),
                            "proposals": list(c["proposals"].values()), "edits": c["history"], "prev_attempts": hist,
                            "event": c.get("event"), "auto": bool(c.get("auto")), "row_verdicts": c.get("row_verdicts")}
        origin = "simulated" if self.meta.get("simulate") else "measured"
        return {
            "design": proto["design"], "bom": self.bom_lines(), "capabilities": proto["capabilities"],
            "steps": steps_out, "meta": self.meta, "boards": self.boards, "cells": cells,
            "placed": {b: sorted(v) for b, v in self.placed.items()},
            "stops": self.stops, "retests": self.retests, "notes": self.notes_public(), "notes_v": 2, "errata_exports": self.errata_exports[-3:],
            "overrides": [{"board": b, "step": s, "reason": r} for (b, s), r in self.overrides.items()],
            "scans": self.scans[-20:], "events": self.events, "origin": origin,
            "asm": {str(b["line"]): self.line_status(b) for b in self.bom_lines()},
            "project": self.project_state(),
            "alog": self.alog[-300:],
            "assembly": {"mode": self.assembly_mode(), "declared": self.assembly["declared"], "since": self.assembly["since"]},
            "aoi": self.aoi_snapshot(),
            "voice": self.voice[-300:],
            "protocol_issues": [i.as_dict() for i in self.proto.issues],
            "tier2": {f"{b}|{st}": {"sections": r["sections"], "attempt": r["attempt"], "via": r["via"], "reason": r["reason"]}
                      for (b, st), r in self.assigns.items() if r["sections"]},
            "holds": {b["label"]: h for b in self.boards if (h := tiers.hold_reason(self, b["label"]))},
            "gates": [tiers.eval_gate(self, i) for i in range(len(proto.get("tier_gates") or []))],
            "similarity": tiers.similarity_all(self),
            "retired": self.retired(),
            "anomalies": self.anomalies(),
            "pending_instrument": {b: sum(1 for x in self.pending_instrument(b)) for b in {x[0] for x in self.pending_instrument()}},
            "plan_loaded": self.plan,
            "sequences": {f"{b}|{st}": r for (b, st), r in self.sequences.items()},
        }


# ----------------------------------------------------------------------------- engine (commands)


class Engine:
    def __init__(self, proto: Protocol, journal: Journal, operator: str = ""):
        self.proto = proto
        self.journal = journal
        self.operator = operator
        self.state = State(proto)
        self._emit_lock = threading.RLock()
        self.listeners: list = []          # called with every new event (voice guide); never for the replay below
        for ev in journal.events:
            self.state.apply(ev)

    # --- plumbing
    def _emit(self, type_: str, payload: dict) -> dict:
        with self._emit_lock:            # voice runs in its own thread: journal order and state order must match
            ev = self.journal.append(type_, payload, self.operator)
            self.state.apply(ev)
            for fn in list(self.listeners):
                try:
                    fn(ev)
                except Exception:
                    pass                    # a listener must never break the journal
        return ev

    def _board(self, label):
        if label not in {b["label"] for b in self.state.boards}:
            raise CmdError(f"unknown board '{label}'", 404)
        return label

    def _test(self, tid):
        if tid not in self.state.test_index:
            raise CmdError(f"unknown test '{tid}'", 404)
        return self.state.test_index[tid]

    def _check_section(self, board, test_id):
        s, sec, t = self._test(test_id)
        if not self.state.sec_applies(s, sec, board):
            raise CmdError(f"{board} has not been sent to Tier 2 for '{sec.get('title', sec['id'])}'. Send it from the Decision gate "
                           "or with 'Send to Tier 2' first", 409)

    def _check_open(self, board, step):
        if not self.state.applies(step, board):
            raise CmdError(f"{step['title']} does not apply to {board}", 409)
        g = self.state.gate(step, board)
        if not g["ok"]:
            raise CmdError("step is gated: " + "; ".join(g["reasons"]), 409, gate=g)
        st = self.state.stopped(board)
        if st and not getattr(self, "_allow_stopped", False):
            raise CmdError(f"{board} is stopped: {st[0]['text']} (acknowledge the stop first)", 409, stop=st[0])

    @property
    def simulated(self):
        return bool(self.state.meta.get("simulate"))

    # --- batch
    def create_batch(self, name, boards, prefix, simulate, design=""):
        self._emit("batch_created", {"name": name, "boards": boards, "prefix": prefix, "simulate": simulate,
                                     "protocol_hash": self.proto.hash, "design": design})

    def set_boards(self, n):
        if any(e["type"] in ("entry", "accept", "skip", "proposal", "placed", "placed_batch") for e in self.journal.events):
            raise CmdError("boards can only be changed before any result is recorded", 409)
        n = int(n)
        if not 1 <= n <= 40:
            raise CmdError("1 to 40 boards", 422)
        self._emit("boards_set", {"boards": board_labels(n, self.state.meta.get("prefix", "S"))})

    def set_serial(self, board, serial):
        self._board(board)
        self._emit("serial", {"board": board, "serial": str(serial).strip()})

    def set_operator(self, name):
        self.operator = str(name).strip()
        self._emit("operator", {"name": self.operator})

    # --- results
    def preview(self, test_id, value):
        s, sec, t = self._test(test_id)
        ev = evaluate(t, value)
        trip = stop_tripped(t, ev)
        return {**{k: ev.get(k) for k in ("ok", "error", "value", "verdict", "flags", "reason")}, "stop": trip}

    def _record(self, kind, board, test_id, value, unit, source, origin, confirm=False, replace=False,
                evidence=None, raw=None, proposal_id=None, via=None, question=None, auto=False, remeasure=False):
        self._board(board)
        s, sec, t = self._test(test_id)
        self._check_open(board, s)
        self._check_section(board, test_id)
        ev = evaluate(t, value, range_ok=origin == "operator_confirmed")
        if not ev["ok"]:
            raise CmdError(ev["error"], 422)
        a = self.state.attempt(board, s["id"])
        cur = self.state.cells.get((board, test_id, a))
        if cur and cur["status"] == "accepted" and not replace and kind != "edit":
            if cur["value"] == ev["value"]:
                return {"ok": True, "unchanged": True}
            raise CmdError("cell already holds an accepted value", 409, needs_replace=True, current=cur["value"])
        if ev["verdict"] in ("fail", "review") and not confirm and not auto:
            raise CmdError(f"{ev['verdict'].upper()}: {ev['reason']}", 409, needs_confirm=ev["verdict"], verdict=ev["verdict"])
        etype = "edit" if (cur and cur["status"] == "accepted") else ("accept" if proposal_id else "entry")
        payload = {"board": board, "test_id": test_id, "attempt": a, "value": ev["value"], "unit": unit or t.get("unit"),
                   "verdict": ev["verdict"], "flags": ev["flags"], "reason": ev["reason"], "source": source,
                   "origin": origin, "evidence": evidence, "raw": raw, "confirmed": bool(confirm),
                   "limit_ref": (t.get("limit") or {}).get("class")}
        if ev.get("row_verdicts") is not None:
            payload["row_verdicts"] = ev["row_verdicts"]
        if auto:
            payload["auto"] = True
        if remeasure and cur and cur["status"] == "accepted":
            payload["remeasure"] = True          # the earlier value is superseded by a new measurement, not corrected as a typo
        if proposal_id:
            payload["proposal_id"] = proposal_id
        elif cur and cur["status"] != "accepted":
            # typed over a pending instrument proposal: the proposal stays in the history (the box said it, the operator replaced it)
            ov = [{"value": q["value"], "unit": q.get("unit"), "verdict": q.get("verdict"), "source": q["source"], "ts": q.get("ts"), "proposal_id": q["id"]}
                  for q in cur["proposals"].values() if (q.get("source") or {}).get("kind") == "action" and q.get("value") != ev["value"]
                  and not isinstance(q.get("value"), (dict, list))]
            if ov:
                payload["overrides"] = ov
        if via:
            payload["via"] = via
        if question:
            payload["question"] = question
        self._emit(etype, payload)
        trip = stop_tripped(t, ev)
        if trip:
            self._emit("stop", {"board": board, "step": s["id"], "test_id": test_id, "attempt": a, "text": trip, "value": ev["value"]})
        return {"ok": True, "verdict": ev["verdict"], "stop": trip}

    def entry(self, board, test_id, value, confirm=False, replace=False, redo=False):
        """redo=True: the operator redid this test; the value replaces the cell in the same attempt (earlier value kept in its history)."""
        src = {"kind": "keyboard", "input_method": "keyboard", "component": "operator", "operator": self.operator}
        return self._record("entry", board, test_id, value, None, src, "simulated" if self.simulated else "measured",
                            confirm, replace or redo, remeasure=redo)

    def batch_targets(self, test_id):
        """Sort the boards for a typed batch entry of one test, in the current attempt of each board.
        targets: empty and open; have: already hold an accepted or skipped value; na: the step or Tier 2 section
        does not apply; blocked: gated or stopped (cannot take a result now)."""
        s, sec, t = self._test(test_id)
        out = {"targets": [], "have": [], "na": [], "blocked": []}
        for b in self.state.boards:
            lab = b["label"]
            if not self.state.applies(s, lab) or not self.state.sec_applies(s, sec, lab):
                out["na"].append(lab)
            elif not self.state.gate(s, lab)["ok"] or self.state.stopped(lab):
                out["blocked"].append(lab)
            elif self.state.cell(lab, test_id)["status"] in ("accepted", "skipped"):
                out["have"].append(lab)
            else:
                out["targets"].append(lab)
        return out

    def entry_batch(self, test_id, value, confirm=False):
        """Apply one typed value to every applicable board that is still empty for this test. Each board goes through
        the normal entry path (limits, verdict, stop rule, journal event with via='batch'). A FAIL, a review verdict or a
        stop-rule trip on any board blocks the whole batch until it is confirmed; nothing is recorded before that."""
        with self._emit_lock:
            s, sec, t = self._test(test_id)
            ev = evaluate(t, value)
            if not ev["ok"]:
                raise CmdError(ev["error"], 422)
            sort = self.batch_targets(test_id)
            if not sort["targets"]:
                raise CmdError("no empty board to apply this value to", 409, **sort)
            trip = stop_tripped(t, ev)
            if (ev["verdict"] in ("fail", "review") or trip) and not confirm:
                raise CmdError(f"{(ev['verdict'] if ev['verdict'] in ('fail', 'review') else 'stop').upper()}: "
                               f"{ev['reason'] or trip}", 409, needs_confirm_batch=True, verdict=ev["verdict"], stop=trip,
                               value=ev["value"], unit=t.get("unit"),
                               boards=[{"board": b, "verdict": ev["verdict"], "stop": trip} for b in sort["targets"]], **sort)
            src = {"kind": "keyboard", "input_method": "keyboard", "component": "operator", "operator": self.operator}
            origin = "simulated" if self.simulated else "measured"
            stops = []
            for b in sort["targets"]:
                r = self._record("entry", b, test_id, value, None, src, origin, confirm=bool(confirm), via="batch")
                if r.get("stop"):
                    stops.append({"board": b, "stop": r["stop"]})
            return {"ok": True, "test_id": test_id, "value": ev["value"], "unit": t.get("unit"), "verdict": ev["verdict"],
                    "recorded": sort["targets"], "have": sort["have"], "na": sort["na"], "blocked": sort["blocked"], "stops": stops}

    def accept(self, board, test_id, proposal_id, confirm=False, replace=False, via=None):
        s, sec, t = self._test(test_id)
        a = self.state.attempt(board, s["id"])
        c = self.state.cells.get((board, test_id, a))
        prop = next((q for q in (c["proposals"].values() if c else []) if q["id"] == proposal_id), None)
        if not prop:
            raise CmdError("proposal not found (already accepted or rejected?)", 404)
        return self._record("accept", board, test_id, prop["value"], prop.get("unit"), prop["source"], prop.get("origin", "measured"),
                            confirm, replace, prop.get("evidence"), prop.get("raw"), proposal_id,
                            via=via or prop.get("via"), question=prop.get("question"))

    def undo_accept(self, board, test_id, event_id, via="meter-undo"):
        """Take back the accept `event_id` of a cell (the Undo of the meter's auto-accept). Journalled as its own event 'undo'; the
        accept stays in the journal. Refused when the cell has changed since, or when the accept tripped a stop rule."""
        s, sec, t = self._test(test_id)
        self._board(board)
        a = self.state.attempt(board, s["id"])
        c = self.state.cells.get((board, test_id, a))
        if not c or c["status"] != "accepted" or c.get("event") != event_id:
            raise CmdError("the value has changed since: nothing to undo", 409)
        if any(x["board"] == board and x.get("test_id") == test_id and x["id"] > event_id for x in self.state.stops):
            raise CmdError("that value tripped a stop rule: acknowledge the stop; it cannot be undone", 409)
        self._emit("undo", {"board": board, "test_id": test_id, "attempt": a, "event": event_id, "value": c["value"], "via": via})
        return {"ok": True}

    def accept_all(self, board, step_id):
        self._board(board)
        s = self.proto.step(step_id)
        if not s:
            raise CmdError("unknown step", 404)
        taken = left = 0
        for sec in s.get("sections", []):
            for t in sec.get("tests", []):
                c = self.state.cell(board, t["id"])
                for q in list(c["proposals"].values()):
                    clean = q["verdict"] in ("pass", "none") and not q.get("flags")
                    already = c["status"] == "accepted"
                    if clean and not already:
                        try:
                            self.accept(board, t["id"], q["id"])
                            taken += 1
                            c = self.state.cell(board, t["id"])
                        except CmdError:
                            left += 1
                    else:
                        left += 1
        return {"ok": True, "accepted": taken, "left": left}

    def pending_instrument(self, board=None):
        return self.state.pending_instrument(board)

    def redo_action(self, test_id):
        return self.state.redo_action(test_id)

    def record_pending(self, board=None):
        """One-time: record the pending instrument proposals of an old journal as results (event 'accept', auto=true, via='backfill')."""
        done, left = 0, 0
        with self._emit_lock:
            for b, tid, q in self.pending_instrument(board):
                try:
                    self._record("accept", b, tid, q["value"], q.get("unit"), {**q["source"], "input_method": "instrument"}, q.get("origin", "measured"),
                                 evidence=q.get("evidence"), raw=q.get("raw"), proposal_id=q["id"], via="backfill", auto=True)
                    done += 1
                except CmdError:
                    left += 1
        return {"ok": True, "recorded": done, "left": left}

    def reject(self, board, test_id, proposal_id, reason=""):
        s, sec, t = self._test(test_id)
        a = self.state.attempt(board, s["id"])
        self._emit("reject", {"board": board, "test_id": test_id, "attempt": a, "proposal_id": proposal_id, "reason": reason})
        return {"ok": True}

    def skip(self, board, test_id, reason):
        self._board(board)
        s, sec, t = self._test(test_id)
        if not str(reason).strip():
            raise CmdError("a skip needs a reason", 422)
        self._check_open(board, s)
        self._check_section(board, test_id)
        a = self.state.attempt(board, s["id"])
        c = self.state.cells.get((board, test_id, a))
        if c and c["status"] == "accepted":
            raise CmdError("cell is accepted; edit the value instead of skipping", 409)
        self._emit("skip", {"board": board, "test_id": test_id, "attempt": a, "reason": reason.strip()})
        return {"ok": True}

    def add_proposal(self, board, test_id, value, unit=None, evidence=None, raw=None, plugin="", run=0, simulate=False,
                     source=None, origin=None, via=None, question=None, record="auto"):
        """A value offered to a cell. From an action plugin (source=None) an instrument reading is a result, not a suggestion:
        with record='auto' (the default) it is journalled as the accepted value at once (event 'accept', auto=true, source.input_method
        'instrument'), whatever its verdict; a stop rule and the power interlock then act on it as on a typed value. record='propose'
        (a protocol opt-out), a value that cannot be parsed, or a board that is gated or stopped leaves it as a pending proposal.
        With source= (voice, AI, LLM) the value is only ever a proposal. Returns the proposal event plus 'recorded':
        'recorded' | 'replaced' | 'pending'. A cell that already holds a result is replaced in the same attempt (earlier value in its history)."""
        s, sec, t = self._test(test_id)
        ev = evaluate(t, value, range_ok=origin == "operator_confirmed")
        auto = source is None and record != "propose" and origin != "operator_confirmed" and bool(ev["ok"])
        outcome = "pending"
        replace = False
        why = ""
        if auto:
            try:
                self._board(board)
                self._check_open(board, s)
                self._check_section(board, test_id)
            except CmdError as e:       # gated, stopped or not in Tier 2: nothing is recorded; the reading waits as a proposal
                auto = False
                why = str(e)
        if auto:
            cur = self.state.cells.get((board, test_id, self.state.attempt(board, s["id"])))
            if cur and cur["status"] == "accepted":
                # a re-measure: the new value replaces the cell in the SAME attempt (event 'edit', earlier value kept in its history);
                # the rest of the step is untouched. Only an explicit step retest opens a new attempt.
                replace, outcome = True, "replaced"
        a = self.state.attempt(board, s["id"])
        src = source or {"kind": "action", "component": plugin, "run_ref": run, "key": plugin, "operator": self.operator}
        verdict = ev["verdict"] if ev["ok"] else "invalid"
        shown = ev["value"] if ev["ok"] else (value if isinstance(value, (dict, list)) else str(value))
        payload = {"board": board, "test_id": test_id, "attempt": a, "value": shown,
                   "unit": unit or t.get("unit"), "verdict": verdict, "flags": ev["flags"], "reason": ev.get("reason", ""),
                   "source": src, "origin": origin or ("simulated" if simulate else "measured"), "evidence": evidence, "raw": raw}
        if ev.get("row_verdicts") is not None:
            payload["row_verdicts"] = ev["row_verdicts"]
        if via:
            payload["via"] = via
        if question:
            payload["question"] = question
        with self._emit_lock:
            pev = self._emit("proposal", payload)
            if not auto:
                return {**pev, "recorded": "pending", **({"why": why} if why else {})}
            try:
                self._record("accept", board, test_id, ev["value"], unit, {**src, "input_method": "instrument"}, payload["origin"],
                             confirm=False, replace=replace, evidence=evidence, raw=raw, proposal_id=pev["id"], auto=True, remeasure=replace)
            except CmdError as e:
                return {**pev, "recorded": "pending", "why": str(e)}
        return {**pev, "recorded": outcome if outcome != "pending" else "recorded"}

    # --- automated step: the run's verdict (docs/TEST_PLAN_FORMAT.md is unchanged; see docs/AUTOMATED_STEP.md)
    def seq_rows(self, board, step_id) -> list[dict]:
        """One row per result test of an automated step for this board (diagnose sections excluded): the accepted value, else the
        newest pending proposal, else nothing. `group`: pass | review | fail | invalid | none (no value) | accepted-* mirrors."""
        s = self.proto.step(step_id)
        if not s or s.get("kind") != "automated":
            raise CmdError("not an automated step", 404)
        rows = []
        fin_tests = set()
        for sec in s.get("sections", []):
            if sec.get("hidden"):
                for a in sec.get("actions", []):
                    if a.get("final"):
                        fin_tests.update(a.get("proposes") or [])
        for sec in s.get("sections", []):
            if sec.get("diagnose") or sec.get("hidden"):
                continue
            for t in sec.get("tests", []):
                c = self.state.cell(board, t["id"])
                prop = max(c["proposals"].values(), key=lambda q: q["id"], default=None)
                if c["status"] == "accepted":
                    v, group, src = c, c.get("verdict"), "accepted"
                elif prop:
                    v, group, src = prop, prop.get("verdict"), "proposal"
                elif c["status"] == "skipped":
                    v, group, src = {"reason": c.get("skip_reason")}, "skipped", "skipped"
                else:
                    v, group, src = None, "none", "none"
                flags = (v or {}).get("flags") or []
                clean = group in ("pass", "none") and not flags and src not in ("none", "skipped")
                rows.append({"test": t["id"], "title": t.get("title", t["id"]), "section": sec["id"], "src": src,
                             "value": (v or {}).get("value"), "unit": (v or {}).get("unit"), "verdict": group, "flags": flags,
                             "clean": clean, "proposal": (prop or {}).get("id"), "reason": (v or {}).get("reason", ""),
                             "final": t["id"] in fin_tests})
        return rows

    def seq_accept(self, board, step_id, mode, confirm=False, note="", seq=None):
        """mode 'proceed': accept every proposal of the run in one go. Clean ones are taken as they are; a FAIL, a REVIEW or a value
        outside a guess / TBR limit needs `confirm` (the operator has seen the list). A test with no value blocks it.
        mode 'defects': the same accepts (the dialog is the confirmation) plus the operator's note; it never proceeds to the final image."""
        self._board(board)
        s = self.proto.step(step_id)
        if not s or s.get("kind") != "automated":
            raise CmdError("not an automated step", 404)
        self._check_open(board, s)
        rows = self.seq_rows(board, step_id)
        todo = [r for r in rows if r["src"] == "proposal"]
        bad = [r for r in todo if not r["clean"]]
        miss = [r for r in rows if r["src"] == "none" and not r["final"]]
        if mode == "proceed":
            if miss:
                raise CmdError(f"{len(miss)} test{'s' if len(miss) != 1 else ''} have no result ({', '.join(r['test'] for r in miss[:8])}); "
                               "record defects, or run again", 409, missing=[r["test"] for r in miss])
        elif not str(note).strip():
            raise CmdError("record defects needs a note", 422)
        inv = [r for r in todo if r["verdict"] == "invalid"]
        if inv:
            raise CmdError("cannot accept a value that does not parse: " + ", ".join(r["test"] for r in inv), 422)
        if mode == "proceed" and bad and not confirm:
            raise CmdError(f"{len(bad)} result{'s' if len(bad) != 1 else ''} FAIL or need review: confirm them to proceed", 409,
                           needs_confirm=True, tests=[r["test"] for r in bad])
        accepted, confirmed = 0, []
        with self._emit_lock:
            self._allow_stopped = True              # a stop tripped by one accept must not refuse the rest of the same batch
            try:
                for r in [x for x in todo if x["clean"]] + bad:
                    self.accept(board, r["test"], r["proposal"], confirm=not r["clean"])
                    accepted += 1
                    if not r["clean"]:
                        confirmed.append(r["test"])
            finally:
                self._allow_stopped = False
            if mode == "defects":
                self.note(note, board, "", category="observation", step=step_id)
        return {"ok": True, "accepted": accepted, "confirmed": confirmed, "missing": [r["test"] for r in miss]}

    def seq_event(self, board, step_id, phase, **kw):
        return self._emit("sequence", {"board": board, "step": step_id, "phase": phase, **kw})

    # --- plan and tiers
    def plan_loaded(self, info: dict):
        """Journal the plan a start (or a reload) generated the tests from. Skipped when the last one has the same hash."""
        last = self.state.plan
        if last and last.get("hash") == info.get("hash") and last.get("count") == info.get("count"):
            return False
        self._emit("plan_loaded", info)
        return True

    def tier2_assign(self, board, step_id, sections, reason="", via="operator"):
        self._board(board)
        s = self.proto.step(step_id)
        if not s:
            raise CmdError("unknown step", 404)
        allowed = [sec["id"] for sec in s.get("sections", []) if tiers.is_conditional(sec)]
        secs = allowed if sections in ("all", None) else list(sections)
        bad = [x for x in secs if x not in allowed]
        if bad:
            raise CmdError(f"not a Tier 2 section of {step_id}: {', '.join(bad)}", 422)
        if not secs:
            raise CmdError(f"{step_id} has no Tier 2 section", 422)
        self._emit("tier2_assign", {"board": board, "step": step_id, "attempt": self.state.attempt(board, step_id), "sections": secs,
                                    "reason": str(reason).strip(), "via": via})
        return {"ok": True, "sections": secs}

    def tier2_remove(self, board, step_id, sections, reason=""):
        self._board(board)
        self._emit("tier2_remove", {"board": board, "step": step_id, "sections": list(sections), "reason": str(reason).strip()})
        return {"ok": True}

    def gate_accept(self, gate_idx, boards):
        """Accept the gate's proposal for these boards: open the Tier 2 sections the gate sends them to."""
        gates = self.proto.data.get("tier_gates") or []
        if not 0 <= int(gate_idx) < len(gates):
            raise CmdError("unknown gate", 404)
        ev = tiers.eval_gate(self.state, int(gate_idx))
        tg = tiers.tier2_targets(self.state, ev["to"])
        done = []
        for b in boards:
            row = next((x for x in ev["boards"] if x["board"] == b), None)
            if not row or row["verdict"] != "tier2":
                raise CmdError(f"{b}: the gate does not propose Tier 2 for this board", 409)
            for sid, secs in tg.items():
                self.tier2_assign(b, sid, secs, reason="; ".join(row["reasons"])[:300], via=ev["title"])
            done.append(b)
        return {"ok": True, "boards": done}

    def gate_reject(self, gate_idx, boards, reason):
        if not str(reason).strip():
            raise CmdError("rejecting the gate's proposal needs a reason", 422)
        gates = self.proto.data.get("tier_gates") or []
        if not 0 <= int(gate_idx) < len(gates):
            raise CmdError("unknown gate", 404)
        ev = tiers.eval_gate(self.state, int(gate_idx))
        steps = {s["id"] for s, _sec, _t in tiers._scope_tests(self.state, gates[int(gate_idx)]["after"])}
        for b in boards:
            self._board(b)
            self._emit("gate_reject", {"gate": int(gate_idx), "board": b, "reason": reason.strip(),
                                       "attempts": {sid: self.state.attempt(b, sid) for sid in steps}})
        return {"ok": True}

    def retest(self, board, step_id, cause, fix="", auto=False, plugin="", run_ref=0):
        self._board(board)
        s = self.proto.step(step_id)
        if not s or s["kind"] in ("setup", "report"):
            raise CmdError("this step cannot be retested", 422)
        if not str(cause).strip():
            raise CmdError("a retest needs a cause", 422)
        a = self.state.attempt(board, step_id) + 1
        pl = {"board": board, "step": step_id, "attempt": a, "cause": cause.strip(), "fix": fix.strip(), "reason": cause.strip()}
        if auto:
            pl.update(auto=True, plugin=plugin, run_ref=run_ref)
        self._emit("retest_open", pl)
        if s["kind"] == "assembly":
            pass
        return {"ok": True, "attempt": a}

    def override_gate(self, board, step_id, reason):
        if not str(reason).strip():
            raise CmdError("an override needs a reason", 422)
        self._emit("gate_override", {"board": board, "step": step_id, "reason": reason.strip()})
        return {"ok": True}

    def ack_stop(self, board, stop_id, reason):
        if not str(reason).strip():
            raise CmdError("acknowledging a stop needs a reason", 422)
        self._emit("stop_ack", {"board": board, "stop_id": int(stop_id), "reason": reason.strip()})
        return {"ok": True}

    def set_placed(self, board, designators, on):
        self._board(board)
        self._emit("placed", {"board": board, "designators": list(designators), "on": bool(on)})
        return {"ok": True}

    def recheck_bom(self, design_names, document=None, fingerprint=None):
        """The BOM changed: compare it again with the recorded document's part list (journal: bom_crosscheck)."""
        cc = project.crosscheck(self.state.bom_lines(), design_names)
        self._emit("bom_crosscheck", {**cc, "document": document, "fingerprint": fingerprint, "after": "bom_source"})
        return cc

    def select_project(self, info: dict, design_names):
        """Record the document this batch is built from and the BOM cross-check of it, in one step."""
        bom = self.state.bom_lines()
        cc = project.crosscheck(bom, design_names)
        total = len({d for b in bom for d in b["designators"]})
        self._emit("project_select", {**info, "expected_elements": total, "fingerprint": project.fingerprint(design_names)})
        self._emit("bom_crosscheck", {**cc, "document": info.get("document"), "fingerprint": project.fingerprint(design_names)})
        return cc

    def accept_crosscheck(self, note=""):
        cc = self.state.project["crosscheck"]
        if not self.state.project["select"] or not cc:
            raise CmdError("select a document first", 409)
        n = int(cc.get("mismatches", 0))
        if n and not str(note).strip():
            raise CmdError(f"{n} mismatch(es): say in a note why they are acceptable", 422)
        self._emit("bom_crosscheck_accept", {"note": str(note).strip(), "mismatches": n, "document": cc.get("document"),
                                             "fingerprint": cc.get("fingerprint")})
        return {"ok": True}

    def skip_ecad(self, reason):
        if not str(reason).strip():
            raise CmdError("continuing without ECAD needs a reason", 422)
        self._emit("ecad_skipped", {"reason": str(reason).strip()})
        return {"ok": True}

    def clear_skip_ecad(self):
        self._emit("ecad_skip_cleared", {})
        return {"ok": True}

    WORKSPACE_EVENTS = ("project_settings", "ecad_platform", "bom_source")

    def workspace_event(self, type_: str, payload: dict) -> dict:
        """Journal a project-settings change (Project page). Only the documented types; DV_CONTRACT §Results store."""
        if type_ not in self.WORKSPACE_EVENTS:
            raise ValueError(type_)
        return self._emit(type_, payload)

    def assembly_begin(self):
        """Assemble pressed on the start page. Nothing starts before this: the page does not open the camera until it is journaled."""
        if self.state.assembly_mode() == "skipped":
            raise CmdError("assembly was skipped; use 'Assemble instead' to reverse it", 409)
        if self.state.assembly_mode() == "assembling" and self.state.assembly["declared"] == "assembling":
            return {"ok": True, "mode": "assembling"}
        self._emit("assembly_begin", {})
        return {"ok": True, "mode": "assembling"}

    def assembly_skip(self):
        """The boards are already built. Only before any placement or scan is recorded; no placement is ever removed by it."""
        if self.state.has_placements():
            raise CmdError("placements or scans are already recorded for this batch; skipping is offered only before any", 409)
        if self.state.assembly_mode() == "skipped":
            return {"ok": True, "mode": "skipped"}
        self._emit("assembly_skip", {})
        return {"ok": True, "mode": "skipped"}

    def assembly_unskip(self):
        """Changed their mind: Assemble instead. Journaled like the skip."""
        if self.state.assembly_mode() != "skipped":
            raise CmdError("assembly is not skipped", 409)
        self._emit("assembly_unskip", {})
        return {"ok": True, "mode": "assembling"}

    def auto_cancel(self, line, reason="", code=""):
        """An auto-place countdown was cancelled (Esc, button, a new scan, the line changed). Nothing was placed; it is logged."""
        self._emit("auto_cancel", {"line": line, "reason": str(reason), "code": code})
        return {"ok": True}

    def place_batch(self, line, boards, on=True, via="button", code="", auto=False):
        """One component placed on several boards at once ('I put C5 on all the boards')."""
        ln = next((x for x in self.state.bom_lines() if x["line"] == line), None)
        if not ln:
            raise CmdError("unknown BOM line", 404)
        if ln.get("dnf") and on:
            raise CmdError("this line is DO NOT FIT; nothing to place", 422)
        boards = [self._board(b) for b in dict.fromkeys(boards)]
        if not boards:
            raise CmdError("no boards selected", 422)
        payload = {"line": line, "boards": boards, "designators": list(ln["designators"]), "on": bool(on), "via": via, "code": code}
        if auto:
            payload["auto"] = True          # placed by the auto-place countdown, not by Enter
        self._emit("placed_batch", payload)
        return {"ok": True, "boards": boards}

    def _note_payload(self, text, board="", test_id="", category="observation", suggested_fix="", step="", boards=None,
                      line=None, designators=None, aoi_key="", scan=""):
        text = str(text or "").strip()
        if not text:
            raise CmdError("a note needs some text", 422)
        if category not in NOTE_CATEGORIES:
            raise CmdError("category must be one of: " + ", ".join(NOTE_CATEGORIES), 422)
        bl = list(dict.fromkeys(boards if boards is not None else ([board] if board else [])))
        bl = [self._board(b) for b in bl]
        if test_id:
            self._test(test_id)
        if step and step not in {s["id"] for s in self.proto.data["steps"]}:
            raise CmdError(f"unknown step '{step}'", 404)
        if isinstance(designators, str):
            designators = designators.replace(",", " ").split()
        des = list(dict.fromkeys(str(d).strip().upper() for d in (designators or []) if str(d).strip()))
        known = {d for b in self.proto.data["bom"] for d in b["designators"]}
        bad = [d for d in des if d not in known]
        if bad:
            raise CmdError("not in the BOM: " + ", ".join(bad), 422)
        if line in ("", None):
            line = None
        else:
            line = int(line)
            if line not in {b["line"] for b in self.proto.data["bom"]}:
                raise CmdError(f"unknown BOM line {line}", 404)
        if aoi_key:
            keys = {i["key"] for r in self.state.aoi_runs for i in r.get("items", [])} | set(self.state.aoi_manual)
            if aoi_key not in keys:
                raise CmdError(f"unknown AOI candidate '{aoi_key}'", 404)
        return {"text": text, "category": category, "suggested_fix": str(suggested_fix or "").strip() if category == "design issue" else "",
                "step": step or "", "boards": bl, "board": bl[0] if bl else "", "test_id": test_id or "", "line": line,
                "designators": des, "aoi_key": aoi_key or "", "scan": str(scan or "")}

    def note(self, text, board="", test_id="", **ctx):
        """An operator note, linked to its context (step, boards, test, BOM line, designators, AOI candidate, scan)."""
        ev = self._emit("note", self._note_payload(text, board, test_id, **ctx))
        return {"ok": True, "id": ev["id"]}

    def note_edit(self, ref, text, board="", test_id="", **ctx):
        """Append-only edit: a new event that carries the complete new version and references the first note."""
        if int(ref) not in self.state.note_by_id:
            raise CmdError("unknown note", 404)
        ev = self._emit("note_edit", {"ref": int(ref), **self._note_payload(text, board, test_id, **ctx)})
        return {"ok": True, "id": ev["id"], "ref": int(ref)}

    # --- assembly scan
    def find_line(self, code):
        c = labels.norm(code)
        if not c:
            return None, ""
        lines = self.state.bom_lines()
        for b in lines:
            if c in [labels.norm(x) for x in b.get("barcode", [])]:
                return b, "barcode"
        for b in lines:
            if labels.norm(b.get("mpn", "")) == c:
                return b, "mpn"
        for b in lines:
            if c in [d.upper() for d in b["designators"]]:
                return b, "designator"
        for b in lines:
            if len(c) >= 4 and c in labels.norm(b.get("mpn", "")):
                return b, "mpn (partial)"
        return None, ""

    def resolve_codes(self, codes):
        """Every code seen on one label. The first that is a BOM key wins (partial matches only if nothing is exact)."""
        cands, qty = labels.expand(codes)
        partial = None
        for c in cands:
            b, how = self.find_line(c)
            if b and "partial" not in how:
                return b, how, c, qty
            if b and partial is None:
                partial = (b, how, c)
        if partial:
            return (*partial, qty)
        return None, "", "", qty

    def scan(self, code, via="typed", codes=None, fmt=""):
        seen = [str(x) for x in (codes if codes else [code]) if str(x).strip()]
        b, how, hit, qty = self.resolve_codes(seen)
        shown = hit or (seen[0] if seen else str(code))
        payload = {"code": shown, "line": b["line"] if b else None, "matched_by": how, "via": via}
        if len(seen) > 1:
            payload["codes"] = seen[:12]
        if fmt:
            payload["format"] = fmt
        if qty is not None:
            payload["qty"] = qty
        self._emit("scan", payload)
        return {"ok": bool(b), "line": b["line"] if b else None, "matched_by": how, "code": shown, "qty": qty}

    # --- AOI
    def aoi_cfg(self):
        cfg = self.proto.data.get("aoi")
        if not cfg:
            raise CmdError("this protocol has no aoi: block", 404)
        return cfg

    def aoi_mode(self):
        """'diff' when the protocol's aoi: block selects the layout-free mode, else None (journal events then keep their old shape)."""
        return "diff" if (self.proto.data.get("aoi") or {}).get("mode") == "diff" else None

    def aoi_add_photo(self, board, side, name, file, sha256, size, source, assigned_by=None):
        if board is not None:
            self._board(board)
        if side not in ("top", "bottom"):
            raise CmdError("side must be top or bottom", 422)
        self._emit("aoi_photo", {"board": board, "side": side, "name": name, "file": file, "sha256": sha256, "size": size,
                                 "source": source, "assigned_by": assigned_by or ("filename" if board and source == "filename" else "unassigned" if not board else source),
                                 **({"mode": "diff"} if self.aoi_mode() else {})})

    def aoi_assign(self, file, board, side=None, assigned_by="select"):
        ph = self.state.aoi_photos.get(file)
        if not ph:
            raise CmdError("unknown photo", 404)
        if board:
            self._board(board)
        self._emit("aoi_photo", {**{k: ph[k] for k in ("name", "file", "sha256", "size")}, "board": board or None,
                                 "side": side or ph["side"], "source": "chosen", "assigned_by": assigned_by})
        return {"ok": True}

    def aoi_move(self, file, board):
        """A photo already assigned to a board is moved to another one (drag of its thumbnail onto a tile). The file is never touched."""
        ph = self.state.aoi_photos.get(file)
        if not ph:
            raise CmdError("unknown photo", 404)
        self._board(board)
        if ph.get("board") == board:
            return {"ok": True, "moved": False}
        old = ph.get("board")           # _emit applies the event to ph in place
        self._emit("aoi_photo_move", {"file": file, "from": old, "to": board, "name": ph["name"], "assigned_by": "tile"})
        return {"ok": True, "moved": True, "from": old, "to": board}

    def aoi_remove(self, file, reason, moved_to):
        """The operator removed a photo: it leaves the batch list and the next run; the file itself is kept (moved_to)."""
        ph = self.state.aoi_photos.get(file)
        if not ph:
            raise CmdError("unknown photo", 404)
        self._emit("aoi_photo_removed", {"file": file, "name": ph["name"], "board": ph.get("board"), "side": ph.get("side"),
                                         "sha256": ph.get("sha256"), "reason": (reason or "").strip(), "moved_to": moved_to})
        return {"ok": True}

    def aoi_drop(self, file):
        if file not in self.state.aoi_photos:
            raise CmdError("unknown photo", 404)
        self._emit("aoi_photo_drop", {"file": file})
        return {"ok": True}

    def aoi_run_done(self, board, m):
        """From the aoi.run child: the run finished and wrote its result; record the candidate list in the journal."""
        self._emit("aoi_run", {k: m.get(k) for k in ("run", "result", "dir", "items", "boards", "photos", "log", "counts", "notes", "synthetic", "mode", "reference", "registration") if m.get(k) is not None})

    def _aoi_items(self, run):
        return {i["key"]: i for i in self.state.aoi_all_items(run)}

    def aoi_links(self, item):
        from .aoi import link
        des = item["designators"] if item.get("manual") else link.designators_of(item["id"])
        return link.tests_for(self.proto.data["steps"], item["kind"], des)

    def aoi_manual_issue(self, board, box_mm, kind, designators, note=""):
        """The operator adds an issue AOI did not list. Decided as confirmed; fails the matching visual tests; journalled as
        aoi_manual_issue. box_mm is [x0, y0, x1, y1] in the boardview frame (board mm, y up)."""
        from .aoi import board_view as BV
        run = self.state.aoi_current_run()
        if not run:
            raise CmdError("no AOI run yet: the board view of a run is the frame an operator-found issue is placed in", 409)
        self._board(board)
        if kind not in MANUAL_KIND_CODE:
            raise CmdError("kind must be one of " + ", ".join(MANUAL_KIND_CODE), 422)
        try:
            x0, y0, x1, y1 = (round(float(v), 3) for v in box_mm)
        except (TypeError, ValueError):
            raise CmdError("box_mm must be four numbers [x0, y0, x1, y1] in board mm", 422)
        if not (x1 > x0 and y1 > y0):
            raise CmdError("the box has no area", 422)
        bv = BV.load(Path(run["dir"]))
        if bv:
            bx = bv["box_mm"]
            if x1 < bx[0] or x0 > bx[2] or y1 < bx[1] or y0 > bx[3]:
                raise CmdError("the box is outside the board", 422)
        des = []
        for d in designators if isinstance(designators, list) else str(designators or "").replace(";", ",").replace(" ", ",").split(","):
            d = str(d).strip()
            if d and d not in des:
                des.append(d)
        if bv:
            bad = [d for d in des if d not in bv.get("parts", {})]
            if bad:
                raise CmdError("not a part of this layout: " + ", ".join(bad), 422)
        a = self.state.attempt(board, "visual")
        from .aoi import link
        tests = link.tests_for(self.proto.data["steps"], MANUAL_KIND_CODE[kind], des) if des else []
        ev = self._emit("aoi_manual_issue", {"board": board, "box_mm": [x0, y0, x1, y1], "kind": kind, "designators": des,
                                             "note": (note or "").strip(), "tests": tests, "attempt": a, "run": run["run"],
                                             "origin": "simulated" if self.simulated else "measured"})
        key = f"{board}|{MANUAL_PREFIX}{ev['id']}"
        self.write_aoi_labels()
        return {"ok": True, "key": key, "tests": tests, "failed": tests}

    def write_aoi_labels(self):
        """aoi/labels.jsonl of the batch: every decision, as Label Studio tasks. Never lets a decision fail."""
        try:
            from .aoi import train_labels
            return train_labels.write(self.state, Path(self.journal.path).parent)
        except Exception as e:      # noqa: BLE001
            print(f"aoi labels not written: {type(e).__name__}: {e}", flush=True)
            return None

    def aoi_decide(self, key, decision, note="", issue_board=None):
        """Operator decision on one AOI candidate: fixed | rejected | unsure (shown as Ignore) | confirmed (legacy, blocks until
        Reworked) | None (clear). 'fixed' is the "Defect - fixed" button: it journals confirmed and then aoi_reworked, one call."""
        fixed = decision == "fixed"
        if fixed:
            decision = "confirmed"
        run = self.state.aoi_current_run()
        items = self._aoi_items(run)
        if key not in items:
            raise CmdError(f"{key} is not a candidate of the current AOI run", 404)
        if decision not in (None, "confirmed", "rejected", "unsure"):
            raise CmdError("decision must be fixed, rejected, unsure or null", 422)
        it = items[key]
        board = it["board"]
        dmeta = it.get("diff") or None
        if dmeta:                                 # diff mode: a confirmed difference must say which board has the issue
            if decision == "confirmed" and issue_board not in dmeta["boards"]:
                raise CmdError("choose which board has the issue: " + " or ".join(dmeta["boards"]), 422)
            if decision != "confirmed":
                issue_board = None
        a = self.state.attempt(board, "visual")
        tests = self.aoi_links(it)
        # tests this decision fails; tests whose AOI fail cell it may clear (no other confirmed issue still fails them)
        fail_tests = tests if decision == "confirmed" else []
        clear = []
        if decision != "confirmed":
            for tid in tests:
                c = self.state.cells.get((board, tid, a))
                if not c or c["status"] != "accepted" or (c.get("source") or {}).get("key") != key:
                    continue
                other = [k for k in items if k != key and (self.state.aoi_decision_for(k) or {}).get("decision") == "confirmed"
                         and items[k]["board"] == board and tid in self.aoi_links(items[k])]
                if not other:
                    clear.append(tid)
        aoi = {"status": it["status"], "value": it.get("value", ""), "confidence": it.get("confidence"), "note": it.get("note", ""),
               "kind": it["kind"], "image": it.get("evidence", [None])[0] if it.get("evidence") else None,
               "evidence_sha": it.get("evidence_sha", "")}
        crop = aoi["image"]
        self._emit("aoi_decision", {"key": key, "board": board, "result_id": it["id"], "decision": decision, "note": (note or "").strip(),
                                    "aoi": aoi, "run": run["run"], "tests": tests, "attempt": a, "fail_tests": fail_tests,
                                    "clear_tests": clear, "evidence": crop, "origin": "simulated" if self.simulated else "measured",
                                    **({"mode": "diff", "issue_board": issue_board} if dmeta else {})})
        if decision == "rejected":
            self._aoi_pass_proposals(board, items, a)
        if fixed:
            self._emit("aoi_reworked", {"key": key, "board": board, "note": (note or "").strip(), "on": True})
        self.write_aoi_labels()
        return {"ok": True, "tests": tests, "failed": fail_tests, "cleared": clear}

    def aoi_rework(self, key, note="", on=True):
        """The operator reworked a confirmed AOI issue (the board was fixed on the bench). Journalled as aoi_reworked; it replays from the
        journal. on=False takes the mark back. A new decision on the item also clears it."""
        run = self.state.aoi_current_run()
        items = self._aoi_items(run)
        if key not in items:
            raise CmdError(f"{key} is not an AOI item of the current run", 404)
        d = self.state.aoi_decision_for(key, run)
        if on and not (d and d["decision"] == "confirmed"):
            raise CmdError("only a confirmed issue can be marked reworked", 409)
        self._emit("aoi_reworked", {"key": key, "board": items[key]["board"], "note": (note or "").strip(), "on": bool(on)})
        self.write_aoi_labels()
        return {"ok": True}

    def protocol_accept(self, new_hash, old_hash="", reason="", tests_before=None, tests_after=None):
        """The operator accepts the protocol as it is now; the PROTOCOL CHANGED badge compares against this hash from here on."""
        if not new_hash:
            raise CmdError("no protocol hash to accept", 422)
        self._emit("protocol_accepted", {"hash": new_hash, "old_hash": old_hash, "reason": (reason or "").strip(),
                                         "tests_before": tests_before, "tests_after": tests_after})
        return {"ok": True, "hash": new_hash}

    def _aoi_pass_proposals(self, board, items, attempt):
        """A test whose every AOI candidate on this board has been dismissed by the operator gets a PASS *proposal*
        (the operator accepts it with the usual button). AOI having nothing to report is not proposed as a pass:
        only the operator's dismissal of what it did report counts, and only for the tests those candidates touch."""
        per_test: dict[str, list[str]] = {}
        for k, it in items.items():
            if it["board"] == board:
                for tid in self.aoi_links(it):
                    per_test.setdefault(tid, []).append(k)
        for tid, keys in per_test.items():
            if not all((self.state.aoi_decision_for(k) or {}).get("decision") == "rejected" for k in keys):
                continue
            if any(items[k]["kind"] in ("POL", "POLSYS", "LIFT") for k in keys):
                continue            # polarity fails closed and lift is review only: these are never proposed as a pass
            c = self.state.cells.get((board, tid, attempt))
            if c and (c["status"] == "accepted" or any((q.get("source") or {}).get("component") == "aoi" for q in c["proposals"].values())):
                continue
            ev = evaluate(self.state.test_index[tid][2], "yes")
            if not ev["ok"]:
                continue
            payload = {"board": board, "test_id": tid, "attempt": attempt, "value": "yes", "unit": None, "verdict": ev["verdict"],
                       "flags": ev["flags"], "reason": "", "origin": "simulated" if self.simulated else "measured",
                       "source": {"kind": "aoi_review", "component": "aoi", "key": "aoi", "run_ref": 0, "operator": self.operator},
                       "evidence": None, "raw": "operator dismissed AOI candidates: " + ", ".join(k.split("|", 1)[1] for k in keys)}
            self._emit("proposal", payload)

    def journal_hash(self):
        return self.journal.head_hash
