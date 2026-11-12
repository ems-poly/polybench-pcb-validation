"""Eval harness for the voice extraction tests: runs a sentence through the real pipeline on a fresh session over the
DEMO-1 smoke-test sheet (with a Notes column added) and compares the proposed cells with the expected ones.
Ported from the private scripts/voice_eval_extract.py; the CLI is not carried over."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time

from polybench.voice.session import Session
from poc_engine import Engine

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "DEMO1_Smoke_Test.md")
CASES = os.path.join(HERE, "eval", "extract_cases.json")

def sheet_with_notes(src: str, dst: str):
    """Append a Notes column to the Results table (the one with a Pass/fail column)."""
    out, in_res = [], False
    for ln in open(src, encoding="utf-8").read().split("\n"):
        if ln.startswith("| ID | Check | Unit | Expected |"):
            in_res = True
            ln += " Notes |"
        elif in_res and ln.startswith("|---"):
            ln += "---|"
        elif in_res and ln.startswith("|"):
            ln += " |"
        elif in_res:
            in_res = False
        out.append(ln)
    open(dst, "w", encoding="utf-8").write("\n".join(out))


def make_session(tmp: str, notes: bool = True, mode: str = "board") -> Session:
    p = os.path.join(tmp, "DEMO1_Smoke_Test.md")
    if notes:
        sheet_with_notes(FIXTURE, p)
    else:
        shutil.copy(FIXTURE, p)
    return Session(p, audit_path=os.path.join(tmp, "audit.jsonl"), mode=mode)


def match(exp, got) -> bool:
    if got is None:
        return False
    if isinstance(exp, dict):
        return re.search(exp["re"], got, re.I) is not None
    return got == exp


def run_case(case: dict, model: str, notes: bool = True) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        s = make_session(tmp, notes)
        e = Engine(s, "simulator", "x", True, model, [], False, no_stt=True)
        e.use_llm = True
        if case.get("row"):
            s.ctx.row = next(i for i, r in enumerate(s.script.rows) if r["id"] == case["row"])
        if case.get("board"):
            s.ctx.board = case["board"]
        for k, v in (case.get("pre") or {}).items():
            rid, col = k.split(" ", 1)
            key = next(c["key"] for c in s.script.cells if c["id"] == rid and c["column"] == col)
            s._add(key, v, "voice")
        n_pre = len(s.proposals)
        t = time.perf_counter()
        e.handle_text(case["said"], source="typed")
        ms = round(1000 * (time.perf_counter() - t))
        tr = list(e.traces)[-1]
        got = {}
        for p in s.proposals.values():
            if p["pid"] > n_pre and p["state"] in ("pending", "accepted"):
                got[f"{p['id']} {p['board']}"] = p["value"]
        exp = case["expect"]
        hits = {k: match(v, got.get(k)) for k, v in exp.items()}
        extra = {k: v for k, v in got.items() if k not in exp}
        cmd_ok = True
        if case.get("expect_command"):
            cmd_ok = tr["validator"]["verdict"].startswith("command " + case["expect_command"])
        return {"id": case["id"], "said": case["said"], "parser": tr["parser"], "ms": ms,
                "llm_ms": (tr["llm"] or {}).get("ms"), "expected": exp, "got": got, "hits": hits, "extra": extra,
                "exact": all(hits.values()) and not extra and cmd_ok, "command_ok": cmd_ok,
                "corrections": ((tr["llm"] or {}).get("validation") or {}).get("corrections"),
                "rejected": ((tr["llm"] or {}).get("validation") or {}).get("rejected")}
