# Polybench adapter for the ported PoC understanding layer (docs/VOICE.md section 3.1). Pure: no engine, no state,
# no I/O except the optional Ollama call. Routing ported from the PoC's Engine.handle_text / _try_multi / simple.
"""Utterance text + what the operator is looking at -> one JSON-serialisable interpretation.

Grammar first. The grammar result is trusted alone when `simple()` says so (short, plain, binds to the step's tests);
otherwise, and only when the LLM is enabled, the local model reads the utterance and its answer goes through
`extract.resolve`: every number must be in the transcript, every board must have been spoken. Without a number in the
transcript no number value is ever produced, on either route.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from . import extract, grammar, llm as llm_mod, rowmode
from . import units as un
from . import validate
from .script import CaptureScript
from .validate import Context

# --------------------------------------------------------------------------------------------- interface


@dataclass
class BenchView:
    step_id: str
    tests: list[dict]                       # {id, title, kind, unit, expected, options, limit}
    boards: list[str]                       # open boards of this step
    all_boards: list[str]                   # every board of the batch; board n = all_boards[n - 1]
    test_id: str | None = None
    board: str | None = None
    row_mode: bool = True
    settled: dict[str, bool] = field(default_factory=dict)
    recent: list[str] = field(default_factory=list)
    step_title: str = ""
    aoi_review: bool = False                # the Visual step shows the AOI issue review: issue commands apply


# --------------------------------------------------------------------------------------------- phrase commands

def _norm_words(text: str) -> list[str]:
    toks, _ = grammar._tokens(text.replace("'", ""))
    return [t for t in toks if t not in ("uh", "um", "please", "the", "now", "okay", "alright")]


PHRASES: dict[str, str] = {}


def _reg(intent: str, *phrases: str):
    for p in phrases:
        PHRASES[p] = intent


_reg("accept", "accept", "accepted", "accept it", "accept that", "accept all", "confirm", "confirmed", "approve",
     "confirm that", "yes accept")
_reg("accept_fail", "accept fail", "accept failed", "accept failure", "accept as fail", "record fail", "record failed",
     "record failure", "record the fail", "accept fail anyway", "confirm fail")
_reg("reject", "reject", "reject that", "scratch that", "scratch", "discard", "discard that", "delete", "clear", "delete that")
_reg("cancel", "cancel", "cancel that", "stop", "wait", "hold on", "hold it", "never mind", "nevermind")
_reg("undo", "undo", "undo that", "undo last", "undo the last")
_reg("skip", "skip", "skip it", "skip this", "skip test", "skip this test")
_reg("next_test", "next", "next test", "next one", "next row", "next check", "go next", "go to next test", "next step test")
_reg("prev_test", "previous", "back", "previous test", "previous one", "go back", "last test", "back one")
_reg("next_board", "next board", "next one board")
_reg("prev_board", "previous board", "last board", "board before", "back board")
_reg("which_test", "which test", "what test", "where am i", "which test is this", "what test is this", "which one",
     "whats the test", "what is the test", "which test am i on", "what test am i on", "which one am i on")
_reg("progress", "progress", "how many left", "how many to go", "how many remaining", "status", "how many tests left",
     "how far", "how many left to do")
_reg("repeat", "repeat", "say again", "say that again", "read back", "again", "repeat that")

_reg("read_method", "read out test method", "read test method", "read out the test method", "read the test method", "read out the method", "read the method", "read method",
     "test method", "the test method", "method", "how do i test this", "how do i test it", "how to test this", "read out method")
_reg("highlight", "highlight", "highlight it", "highlight in fusion", "show in fusion", "show it in fusion", "show me",
     "show in fusion 360", "show it in fusion 360", "show it", "highlight this",
     "show it in kicad", "show in kicad", "highlight in kicad", "show it in key cad", "show in key cad",
     "highlight in key cad", "show it in kay cad", "show in kay cad", "highlight in kay cad")

_reg("mic_check", "test", "testing", "hello", "hi", "hey", "hello there", "can you hear me", "do you hear me", "are you there",
     "mic check", "microphone check", "testing one two", "test one two", "testing testing", "test test", "hello can you hear me",
     "are you listening")

# Issue review in Visual (only while the review tab is open): decide the selected AOI issue by voice.
AOI_PHRASES: dict[str, str] = {}
for _i, _ps in {
    "aoi_confirm": ("defect fixed", "defect is fixed", "fixed defect", "defect fix", "its fixed", "it is fixed", "fixed", "defect was fixed", "reworked", "defect reworked"),
    "aoi_reject": ("no fault", "no fault found", "not an issue", "not issue", "reject", "rejected", "fine", "its fine", "false alarm", "false positive", "no issue",
                   "not a defect", "dismiss"),
    "aoi_unsure": ("ignore", "ignore it", "ignore this", "ignore issue", "ignored", "skip it"),
    "aoi_next": ("next issue", "next", "next one", "go next"),
    "aoi_prev": ("previous issue", "previous", "back", "previous one", "last issue", "go back"),
    "aoi_next_undecided": ("next undecided", "next undecided issue", "next open issue", "next open", "next unchecked"),
    "aoi_read": ("read issue", "read the issue", "read this issue", "read it", "read issue out", "what is this issue", "which issue", "what issue"),
}.items():
    for _p in _ps:
        AOI_PHRASES[_p] = _i

YES_WORDS = {"yes", "yeah", "yep", "yup"}
NO_WORDS = {"no", "nope"}
GOTO_PREFIXES = (("go", "to"), ("goto",), ("jump", "to"), ("show",), ("test",), ("go", "to", "test"), ("goto", "test"))
COMPLEX = {"except", "apart", "but", "however", "besides", "instead", "actually", "sorry", "wrong", "correction", "same", "rest",
           "others", "remaining", "traced", "found", "because", "which", "while", "although"}

# --------------------------------------------------------------------------------------------- units of Polybench tests

PB_SYMBOL = {"V": "V", "mV": "mV", "uV": "µV", "µV": "µV", "kV": "kV", "A": "A", "mA": "mA", "uA": "µA", "µA": "µA",
             "Ohm": "Ω", "ohm": "Ω", "kOhm": "kΩ", "MOhm": "MΩ", "mOhm": "mΩ", "Hz": "Hz", "kHz": "kHz", "MHz": "MHz",
             "s": "s", "ms": "ms", "us": "µs", "µs": "µs", "%": "%"}
RATIO = {"mV/V": "mV", "uV/V": "µV", "µV/V": "µV", "V/V": "V"}


def poc_unit(test: dict) -> str:
    """The unit string the PoC's unit classifier understands, for a Polybench test."""
    kind, u = test.get("kind", "number"), (test.get("unit") or "").strip()
    if kind == "yn":
        return "Y/N"
    if kind in ("choice",):
        return "OK/text"
    if kind == "text":
        return "text"
    if kind == "hex":
        return "hex"
    return RATIO.get(u) or PB_SYMBOL.get(u) or u


def unit_spec(test: dict) -> un.UnitSpec:
    raw = poc_unit(test)
    spec = un.classify_unit(raw)
    if test.get("kind", "number") == "number" and spec.mode != "number":
        # a unit the PoC has no dimension for (degC, bit, counts, none): the bare number is taken as is, a spoken unit is refused
        return un.UnitSpec(raw, "number", None, [], raw or "value")
    return spec


# --------------------------------------------------------------------------------------------- script from the view

def _label(view: BenchView, n: int) -> str:
    return view.all_boards[n - 1]


def _index_of(view: BenchView, label: str) -> int | None:
    try:
        return view.all_boards.index(label) + 1
    except ValueError:
        return None


@lru_cache(maxsize=64)
def _id_index(ids: tuple) -> grammar.IdIndex:
    return grammar.IdIndex(list(ids))


def build_script(view: BenchView) -> CaptureScript:
    """A PoC CaptureScript: rows = the step's tests, one board cell per board of the batch (B1..Bn = all_boards)."""
    cells, rows = [], []
    boards = [f"B{n}" for n in range(1, len(view.all_boards) + 1)]
    for li, t in enumerate(view.tests):
        spec = unit_spec(t)
        exp = t.get("expected") or ""
        lim = un.parse_limits(exp, spec) if spec.mode == "number" and spec.dimension else {"limits": [], "accepts_ol": False}
        label = t.get("title") or ""
        row = {"table": 0, "table_slug": "step", "section": view.step_id, "id": t["id"], "label": label,
               "unit": poc_unit(t), "expected": exp, "line": li + 1, "cells": []}
        for b in boards:
            key = f"step:{t['id']}:{b}"
            cells.append({"key": key, "id": t["id"], "table": 0, "section": view.step_id, "line": li + 1, "column": b,
                          "kind": "board", "board": b, "label": label, "unit": poc_unit(t), "unit_spec": spec.as_dict(),
                          "expected": exp, "limits": lim, "current": "", "start": 0, "end": 0})
            row["cells"].append(key)
        rows.append(row)
    return CaptureScript("", "", cells, boards, [], [], rows)


def _context(view: BenchView, script: CaptureScript, test_id: str | None = None) -> Context:
    ctx = Context()
    tid = test_id or view.test_id
    ctx.row = next((i for i, r in enumerate(script.rows) if r["id"] == tid), 0 if script.rows else None)
    n = _index_of(view, view.board) if view.board else None
    ctx.board = f"B{n}" if n else None
    ctx.mode = "row" if view.row_mode else "board"
    open_order = [f"B{_index_of(view, b)}" for b in view.boards if _index_of(view, b)]

    def settled(b: str) -> bool:
        return bool(view.settled.get(_label(view, int(b[1:])), False))

    def next_board(ri, start, taken=()):
        return rowmode.next_slot(open_order, settled, start, exclude=taken)
    ctx.next_board = next_board
    return ctx


# --------------------------------------------------------------------------------------------- value conversion

def _tests_by_id(view: BenchView) -> dict[str, dict]:
    return {t["id"]: t for t in view.tests}


def _final_value(value: str, test: dict) -> str:
    """PoC cell text -> a string engine.evaluate accepts."""
    kind = test.get("kind", "number")
    if kind == "yn":
        v = value.strip().upper().split()[0] if value.strip() else ""
        return "yes" if v in ("Y", "YES", "OK") else "no" if v in ("N", "NO") else value
    if kind == "hex":
        return "0x" + value.lower().removeprefix("0x").upper()
    return value


def _choice_value(words: list[str], test: dict) -> str | None:
    """The option of a choice test named by `words` (original case), or None. Exact, then unique word-subset, then 1-based index."""
    opts = [str(o) for o in test.get("options", [])]
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    said = norm(" ".join(words))
    if not said:
        return None
    exact = [o for o in opts if norm(o) == said]
    if len(exact) == 1:
        return exact[0]
    sw = set(said.split())
    sub = [o for o in opts if sw and sw <= set(norm(o).split())]
    if len(sub) == 1:
        return sub[0]
    sup = [o for o in opts if set(norm(o).split()) and set(norm(o).split()) <= sw]
    if len(sup) == 1:
        return sup[0]
    toks, _ = grammar._tokens(" ".join(words))
    r = grammar.parse_number(toks, 0)
    if r and r[1] == len(toks) and r[0] == r[0].to_integral_value() and 1 <= int(r[0]) <= len(opts):
        return opts[int(r[0]) - 1]
    return None


def _rest_words(text: str, idx: grammar.IdIndex) -> list[str]:
    """Original-case words of the utterance without board references, test IDs and fillers (for choice and text tests)."""
    toks, orig = grammar._tokens(text)
    out, i, rid = [], 0, False
    while i < len(toks):
        b = grammar._board_at(toks, i)
        if b is not None:
            i = b[1]
            continue
        m = idx.match(toks, i) if not rid else None
        if m is not None:
            rid, i = True, m[1]
            continue
        if toks[i] not in grammar.FILLERS:
            out.append(orig[i])
        i += 1
    return out


# --------------------------------------------------------------------------------------------- results

def _blank(text: str) -> dict:
    return {"transcript": text, "route": "none", "model": None, "intent": "unclear", "values": [], "boards": [],
            "test_id": None, "text": "", "reason": None, "grammar": {"ok": False, "reason": None, "fields": None}, "llm": None}


def _fields(p: dict) -> dict:
    v = p.get("value") or {}
    b = p.get("boards")
    return {"intent": p.get("intent"), "boards": b, "id": p.get("id"), "id_candidates": p.get("id_candidates"),
            "value_kind": v.get("kind"), "number": v.get("number"), "unit": v.get("unit") or v.get("prefix"),
            "text": v.get("text"), "yn": v.get("values"), "pairs": p.get("pairs")}


def _unclear(res: dict, reason: str, route: str | None = None) -> dict:
    res.update(intent="unclear", values=[], boards=[], reason=reason)
    if route:
        res["route"] = route
    return res


def _heard(vd: dict) -> str:
    if vd.get("kind") == "number":
        return f"{vd['number']} {vd.get('unit') or vd.get('prefix') or ''}".strip()
    return vd.get("text") or vd.get("kind") or ""


def _resolve_boards(spoken, view: BenchView):
    """(open board numbers, reason|None). 'all' = view.boards; an explicit board that is not open is refused."""
    if spoken == "all":
        ns = [_index_of(view, b) for b in view.boards]
        return [n for n in ns if n], None if view.boards else "no open board on this step"
    if not spoken:
        return None, None
    bad = [n for n in spoken if n > len(view.all_boards)]
    if bad:
        return [], f"board {bad[0]} does not exist ({len(view.all_boards)} boards: {view.all_boards[0]} to {view.all_boards[-1]})"
    open_ = [n for n in spoken if _label(view, n) in view.boards]
    closed = [_label(view, n) for n in spoken if _label(view, n) not in view.boards]
    if not open_:
        return [], f"{', '.join(closed)} {'is' if len(closed) == 1 else 'are'} not open on this step"
    return open_, (f"{', '.join(closed)} not open, left out" if closed else None)


def _bind_values(parse: dict, view: BenchView, script: CaptureScript, idx, note: list[str]) -> tuple[list[dict] | None, str | None]:
    """Grammar parse (intent value) -> contract values, or (None, reason)."""
    tests = _tests_by_id(view)
    ns, reason = _resolve_boards(parse.get("boards"), view)
    if ns == [] or (parse.get("boards") and ns is None):
        return None, reason
    if reason:
        note.append(reason)
    p = dict(parse)
    p["boards"] = ns
    if parse.get("id") and parse["id"] not in tests:
        return None, f"test {parse['id']} is not on this step"
    tid = parse.get("id") or view.test_id
    if tid is None or tid not in tests:
        return None, "no current test"
    ctx = _context(view, script, tid)
    if not p["boards"] and not ctx.board:
        return None, "which board? say the board and the value"
    b = validate.bind(p, script, ctx)
    if not b["ok"]:
        return None, b["reason"]
    t = tests[tid]
    heard = _heard(parse["value"])
    vals = [{"board": _label(view, int(x["board"][1:])), "test_id": tid, "value": _final_value(x["value"], t),
             "unit": t.get("unit") or "", "heard": heard} for x in b["proposals"]]
    return vals, None


def _apply_grammar(res: dict, parse: dict, view: BenchView, script_f, idx, text: str) -> dict:
    """Fill `res` from a grammar parse. May leave intent 'unclear' with the binder's reason."""
    it = parse["intent"]
    tests = _tests_by_id(view)
    simple_map = {"accept": "accept", "accept_all": "accept", "accept_all_aoi": "accept", "reject": "reject", "undo": "undo",
                  "skip": "skip", "next": "next_test", "back": "prev_test", "repeat": "repeat"}
    if it in simple_map:
        res["intent"] = simple_map[it]
        return res
    if it in ("set_board", "skip_board", "note"):
        sp = parse.get("boards")
        if sp == "all":
            if it == "skip_board":
                res.update(intent="skip_board", boards=list(view.boards), text=(parse.get("value") or {}).get("text", ""))
                return res
            return _unclear(res, "all boards is not a single board")
        labels = []
        for n in sp or []:
            if n > len(view.all_boards):
                return _unclear(res, f"board {n} does not exist ({view.all_boards[0]} to {view.all_boards[-1]})")
            labels.append(_label(view, n))
        res["boards"] = labels
        if it == "set_board":
            res["intent"] = "set_board"
        elif it == "skip_board":
            res.update(intent="skip_board", text=(parse.get("value") or {}).get("text", ""))
        else:
            res.update(intent="note", text=parse["value"]["text"], test_id=parse.get("id") if parse.get("id") in tests else view.test_id)
        return res
    if it == "goto":
        rid = parse.get("id")
        if rid in tests:
            res.update(intent="goto_test", test_id=rid)
            return res
        return _unclear(res, f"test {rid or parse.get('row_no')} is not on this step")
    if it in ("pass", "fail"):
        return _unclear(res, "Polybench computes pass or fail from the value; say the value")
    if it == "value":
        note: list[str] = []
        vals, why = _bind_values(parse, view, script_f(), idx, note)
        if vals is None:
            return _unclear(res, why or "value not accepted")
        res.update(intent="values", values=vals, test_id=vals[0]["test_id"], boards=[v["board"] for v in vals],
                   reason="; ".join(note) or None)
        return res
    return _unclear(res, f"intent {it} not supported")


# --------------------------------------------------------------------------------------------- grammar stage

def _split_board_number(text: str, view: BenchView | None) -> str:
    """Whisper writes "S2 0.2 volts" as "S20.2 volts": split board and value where exactly one reading names a board of the batch
    and leaves a well-formed number ("S20.2" -> S2 0.2 with 10 boards; "S103.3" -> S10 3.3; "S10.2" -> S1 0.2)."""
    if view is None:
        return text
    n = len(view.all_boards)

    def fix(m):
        digits = m.group(1)
        cands = []
        for k in (1, 2):
            b, rest = digits[:k], digits[k:]
            if not rest or not b.isdigit() or not 1 <= int(b) <= n:
                continue
            if re.fullmatch(r"(0|[1-9]\d*)\.\d+", rest):
                cands.append(f"S{int(b)} {rest}")
        return cands[0] if len(cands) == 1 else m.group(0)
    return re.sub(r"\b[sS](\d{2,4}\.\d+)\b", fix, text)


def _preclean(text: str, view: BenchView | None = None) -> str:
    """Spoken forms the grammar need not know: 'millivolts per volt' on a ratio test, 'degrees Celsius', a board fused to its value."""
    text = _split_board_number(text, view)
    t = re.sub(r"\bper\s+volts?\b", " ", text, flags=re.I)
    t = re.sub(r"\bdegrees?\s+(celsius|centigrade|c)\b|\bdeg\s*c\b|\bcelsius\b|\bdegrees?\b", " ", t, flags=re.I)
    return t


def _grammar_parse(text: str, view: BenchView, idx) -> tuple[dict, list[dict] | None]:
    """The grammar's parse of `text`, adapted to the test kind (choice and text tests take words); plus row-mode board/value pairs."""
    t = _preclean(text, view)
    words = t.lower().replace(",", " ").replace(".", " ").split()
    multi = None
    if view.row_mode and not (COMPLEX & set(words)):
        segs = rowmode.split_pairs(t, idx)
        if segs:
            out = []
            for sg in segs:
                p = grammar.parse(sg, idx)
                if not p["ok"] or p["intent"] != "value" or not p.get("boards") or p["boards"] == "all" or len(p["boards"]) != 1:
                    out = None
                    break
                p["route"] = "grammar"
                out.append(p)
            if out and not (len({(p.get("id") or "") for p in out}) > 1 and not all(p.get("id") for p in out)):
                multi = out
    parse = grammar.parse(t, idx)
    tests = _tests_by_id(view)
    tid = parse.get("id") or view.test_id
    test = tests.get(tid)
    if test and test.get("kind") in ("choice", "text") and parse["intent"] not in ("accept", "reject", "undo", "skip", "next", "back",
                                                                                    "repeat", "note", "skip_board", "goto", "accept_all"):
        rest = _rest_words(t, idx)
        if rest and not (parse["ok"] and parse["intent"] in ("set_board",)):
            if test["kind"] == "choice":
                opt = _choice_value(rest, test)
                if opt:
                    parse = {**parse, "ok": True, "intent": "value", "reason": None, "value": {"kind": "text", "text": opt}}
            elif not parse["ok"] or parse["intent"] != "value":
                body = grammar.strip_board_scope(" ".join(rest).strip(" ,.;:"))
                if body:
                    parse = {**parse, "ok": True, "intent": "value", "reason": None, "value": {"kind": "text", "text": body}}
    return parse, multi


# --------------------------------------------------------------------------------------------- LLM stage

def _llm_stage(text: str, view: BenchView, script: CaptureScript, cfg: dict) -> tuple[dict | None, dict]:
    """Run the model and the PoC validator. Returns (ext or None, trace)."""
    model = cfg.get("model") or llm_mod.DEFAULT_MODEL
    tr = {"model": model, "ms": 0, "ok": False, "raw": None, "reason": None}
    ctx = _context(view, script)
    state = extract.context_state(script, ctx, lambda k: "", list(view.recent))
    t0 = time.perf_counter()
    try:
        raw = llm_mod.extract(text, script, state, model, cfg.get("url") or llm_mod.URL, float(cfg.get("timeout_s") or 30.0))
    except Exception as e:                                   # Ollama down, model missing, timeout, bad JSON
        tr.update(ms=round(1000 * (time.perf_counter() - t0)), reason=f"LLM not available: {e}")
        return None, tr
    tr.update(ms=round(1000 * (time.perf_counter() - t0)), raw=raw)
    try:
        ext = extract.resolve(raw, text, script, ctx, lambda k: "", [])
    except Exception as e:
        tr["reason"] = f"LLM answer not usable: {e}"
        return None, tr
    tr["ok"] = bool(ext["ok"])
    if not ext["ok"]:
        tr["reason"] = "; ".join(r["reason"] for r in ext["rejected"]) or "nothing usable in the transcript"
    return ext, tr


def _apply_ext(res: dict, ext: dict, view: BenchView) -> dict:
    tests = _tests_by_id(view)
    cmd = ext.get("command")
    if cmd:
        res["intent"] = {"next": "next_test", "back": "prev_test"}.get(cmd, cmd)
        return res
    vals = []
    for c in ext["cells"]:
        t = tests.get(c["id"])
        if t is None or not c.get("board"):
            continue
        n = int(c["board"][1:])
        if n > len(view.all_boards) or _label(view, n) not in view.boards:
            continue                                         # a board that is not open never gets a value
        v = c["value"]
        if t.get("kind") == "choice":
            opt = _choice_value([v], t)
            if opt is None:
                continue
            v = opt
        vals.append({"board": _label(view, n), "test_id": c["id"], "value": _final_value(v, t), "unit": t.get("unit") or "",
                     "heard": (c.get("conversion") or c["value"])})
    notes = [n["text"] for n in ext.get("unplaced_notes", [])]
    if vals:
        res.update(intent="values", values=vals, test_id=vals[0]["test_id"], boards=sorted({v["board"] for v in vals}))
        if notes:
            res["note"] = "; ".join(notes)
        if ext.get("corrections"):
            res["reason"] = "; ".join(ext["corrections"])
        return res
    if ext.get("skips"):
        labs = [f"B{s['board'][1:]}" for s in ext["skips"]]
        res.update(intent="skip_board", boards=[_label(view, int(b[1:])) for b in labs if int(b[1:]) <= len(view.all_boards)],
                   text="; ".join(dict.fromkeys(s["reason"] for s in ext["skips"] if s["reason"])))
        return res
    if notes:
        res.update(intent="note", text="; ".join(notes), test_id=view.test_id)
        return res
    return _unclear(res, "; ".join(r["reason"] for r in ext["rejected"]) or "nothing usable in the transcript")


# --------------------------------------------------------------------------------------------- the routing

def _simple(text: str, parse: dict, bound_ok) -> bool:
    """PoC Engine.simple: trust the grammar alone for a short, plain utterance that also binds."""
    words = text.lower().replace(",", " ").replace(".", " ").split()
    if parse["intent"] not in ("value", "pass", "fail", "note"):
        return True
    if len(words) > 14 or COMPLEX & set(words) or re.search(r"\.\s+\S", text.strip()) and parse["intent"] != "note":
        return False
    if parse["intent"] == "note":
        return True
    return bool(bound_ok())


def interpret(text: str, view: BenchView, llm: dict | None = None) -> dict:
    text = (text or "").strip()
    res = _blank(text)
    if not text:
        return _unclear(res, "empty")
    cfg = llm or {}
    use_llm = bool(cfg.get("enabled"))
    tests = _tests_by_id(view)
    idx = _id_index(tuple(t["id"] for t in view.tests))
    cache: dict = {}

    def script_f() -> CaptureScript:
        if "s" not in cache:
            cache["s"] = build_script(view)
        return cache["s"]

    # ---- fixed commands
    words = _norm_words(text)
    phrase = " ".join(words)
    cur = tests.get(view.test_id) if view.test_id else None
    if view.aoi_review and phrase in AOI_PHRASES:
        res.update(route="grammar", intent=AOI_PHRASES[phrase])
        res["grammar"] = {"ok": True, "reason": None, "fields": {"intent": res["intent"]}}
        return res
    if len(words) == 1 and words[0] in YES_WORDS | NO_WORDS:
        yn_open = (cur is not None and cur.get("kind") == "yn" and view.board is not None and view.board in view.boards
                   and not view.settled.get(view.board, False))
        if not yn_open:
            res.update(route="grammar", intent="accept" if words[0] in YES_WORDS else "reject")
            res["grammar"] = {"ok": True, "reason": None, "fields": {"intent": res["intent"]}}
            return res
    elif phrase in PHRASES:
        res.update(route="grammar", intent=PHRASES[phrase])
        res["grammar"] = {"ok": True, "reason": None, "fields": {"intent": res["intent"]}}
        return res
    for pre in GOTO_PREFIXES:
        if words[:len(pre)] == list(pre) and len(words) > len(pre):
            m = idx.match(words, len(pre))
            if m and m[1] == len(words):
                rid = m[0]
                res["route"] = "grammar"
                res["grammar"] = {"ok": True, "reason": None, "fields": {"intent": "goto", "id": rid}}
                if rid in tests:
                    res.update(intent="goto_test", test_id=rid)
                    return res
                return _unclear(res, f"test {rid} is not on this step")

    # ---- grammar
    parse, multi = _grammar_parse(text, view, idx)
    parse["route"] = "grammar"
    if multi:
        vals, why = [], None
        for p in multi:
            v, why = _bind_values(p, view, script_f(), idx, [])
            if v is None:
                break
            vals += v
        if why is None and vals:
            res.update(route="grammar", intent="values", values=vals, test_id=vals[0]["test_id"], boards=[v["board"] for v in vals])
            res["grammar"] = {"ok": True, "reason": None, "fields": {"intent": "multi", "pairs": [_fields(p) for p in multi]}}
            return res
    res["grammar"] = {"ok": bool(parse["ok"]), "reason": parse.get("reason"), "fields": _fields(parse)}
    if parse["ok"] and parse["intent"] in ("pass", "fail"):
        return _unclear(res, "Polybench computes pass or fail from the value; say the value", "grammar")

    def bound_ok():
        r = _apply_grammar(_blank(text), parse, view, script_f, idx, text)
        return r["intent"] != "unclear"

    if parse["ok"] and (not use_llm or _simple(text, parse, bound_ok)):
        res["route"] = "grammar"
        return _apply_grammar(res, parse, view, script_f, idx, text)
    if not use_llm:
        res["route"] = "grammar"
        return _unclear(res, parse.get("reason") or "not understood")

    # ---- LLM
    grammar_res = _apply_grammar(_blank(text), parse, view, script_f, idx, text) if parse["ok"] else None
    ext, tr = _llm_stage(text, view, script_f(), cfg)
    res["llm"] = tr
    if ext is None:                                          # model unavailable: the grammar's answer, or unclear with the reason
        if grammar_res is not None and grammar_res["intent"] != "unclear":
            grammar_res.update(route="grammar", grammar=res["grammar"], llm=tr)
            return grammar_res
        return _unclear(res, tr["reason"] or "LLM not available", "none")
    if ext["ok"] or not parse["ok"]:
        res["route"], res["model"] = "llm", tr["model"]
        return _apply_ext(res, ext, view)
    if grammar_res is not None:                              # grammar parsed, binder refused it, the model found nothing better
        grammar_res.update(route="grammar", grammar=res["grammar"], llm=tr)
        return grammar_res
    return _unclear(res, tr["reason"] or "not understood", "none")
