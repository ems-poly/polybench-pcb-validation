"""/api/ai/*: what the MCP tools read and do, and the operator's routes for questions and plan approval.

Read routes change nothing and are not journaled. Act routes journal `ai_action` (and their own event) with
`source: assistant`. Operator routes are refused to the assistant by the guard (guard.py)."""
from __future__ import annotations

import asyncio
import json
import statistics
import uuid
from pathlib import Path

from fastapi import Request

from ..engine import CmdError, evaluate, expected_text, source_text
from . import plan_draft
from .guard import HEARTBEAT_S, Hub

ACTION_TYPES = ("ai_action", "ai_question", "ai_answer", "plan_draft", "plan_approved", "plan_draft_rejected")


# ----------------------------------------------------------------------------- views
def test_view(sess, tid: str) -> dict:
    found = sess.engine.state.test_index.get(tid)
    if not found:
        raise CmdError(f"unknown test '{tid}'", 404)
    s, sec, t = found
    lim = t.get("limit") or {}
    return {"id": t["id"], "title": t.get("title", ""), "step": s["id"], "step_title": s.get("title", ""), "section": sec.get("title", sec["id"]),
            "tier": sec.get("tier", t.get("tier", 1)), "conditional": bool(sec.get("conditional")), "kind": t.get("kind"), "unit": t.get("unit"),
            "options": t.get("options") or [], "limit": {k: lim.get(k) for k in ("class", "min", "max", "equals", "closes_on") if lim.get(k) is not None},
            "expected": expected_text(t), "source": source_text(lim.get("source")), "probe": t.get("probe") or [], "nets": t.get("nets") or [],
            "highlight": t.get("highlight") or [], "highlight_pins": t.get("highlight_pins") or [], "stop_if": t.get("stop_if"),
            "outlier": t.get("outlier"), "inputs": t.get("inputs") or [],
            "instructions": "\n\n".join(x for x in (sec.get("instructions") or "", t.get("instructions") or "") if x)}


def retired_info(sess, tid: str) -> dict | None:
    """A test an earlier plan had and the current plan dropped: its recorded results stay, marked retired."""
    for r in sess.engine.state.retired():
        if r["id"] == tid:
            return r
    return None


def retired_view(r: dict, board: str = "") -> dict:
    res = r["results"]
    out = {"id": r["id"], "title": r.get("title", ""), "step": r.get("step", ""), "section": r.get("section", ""), "retired": True,
           "note": "RETIRED: the current test plan no longer has this test. Its results are kept; no new reading can be recorded.",
           "results": [{"board": b, **v} for b, v in sorted(res.items()) if not board or b == board]}
    return out


def _prop_view(p: dict) -> dict:
    src = p.get("source") or {}
    return {"id": p.get("id"), "value": p.get("value"), "unit": p.get("unit"), "verdict": p.get("verdict"), "reason": p.get("reason", ""),
            "from": "assistant" if src.get("kind") == "assistant" else (src.get("component") or src.get("kind")), "origin": p.get("origin")}


def cell_view(sess, board: str, tid: str) -> dict:
    st = sess.engine.state
    c = st.cell(board, tid)
    s = st.test_index[tid][0]
    out = {"board": board, "test_id": tid, "attempt": st.attempt(board, s["id"]), "status": c["status"]}
    if c["status"] == "accepted":
        out.update(value=c.get("value"), unit=c.get("unit"), verdict=c.get("verdict"), flags=c.get("flags") or [], reason=c.get("reason", ""),
                   origin=c.get("origin"), confirmed_out_of_band=bool(c.get("accepted_fail")), ts=c.get("ts"))
    elif c["status"] == "skipped":
        out["skip_reason"] = c.get("skip_reason")
    props = [_prop_view(p) for p in c.get("proposals", {}).values()]
    if props:
        out["proposals"] = props
    return out


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x == 99999 else x


def batch_view(sess, tid: str) -> dict:
    st = sess.engine.state
    s, sec, t = st.test_index[tid]
    rows, nums = [], []
    for b in st.boards:
        lab = b["label"]
        if not st.applies(s, lab) or not st.sec_applies(s, sec, lab):
            rows.append({"board": lab, "status": "n/a"})
            continue
        cv = cell_view(sess, lab, tid)
        rows.append({k: cv[k] for k in ("board", "status", "value", "unit", "verdict", "attempt") if k in cv})
        if cv["status"] == "accepted" and (x := _num(cv.get("value"))) is not None:
            nums.append(x)
    from .. import tiers
    sim = tiers.similarity_all(st).get(tid)
    return {"readings": rows, "n": len(nums), "median": statistics.median(nums) if nums else None,
            "min": min(nums) if nums else None, "max": max(nums) if nums else None, "similarity": sim}


def _journal_rows(sess, since: int, limit: int, types=None) -> list[dict]:
    out = []
    for e in sess.journal.events:
        if e["id"] <= since or (types and e["type"] not in types):
            continue
        out.append({"id": e["id"], "ts": e["ts"], "type": e["type"], "operator": e.get("operator", ""), "payload": e["payload"]})
    return out[-limit:]


def questions(sess) -> list[dict]:
    qs: dict = {}
    for e in sess.journal.events:
        p = e["payload"]
        if e["type"] == "ai_question":
            qs[p["qid"]] = {"qid": p["qid"], "text": p["text"], "options": p.get("options") or [], "board": p.get("board", ""),
                            "test_id": p.get("test_id", ""), "client": p.get("client", ""), "ts": e["ts"], "id": e["id"], "open": True}
        elif e["type"] == "ai_answer" and p.get("qid") in qs:
            qs[p["qid"]].update(open=False, answer=p.get("answer", ""), dismissed=bool(p.get("dismissed")), answered_ts=e["ts"],
                                answered_by=e.get("operator", ""))
    return list(qs.values())


def _action_rows(sess, n=30) -> list[dict]:
    rows = []
    for e in reversed(sess.journal.events):
        p, ty = e["payload"], e["type"]
        src = p.get("source")
        if ty in ACTION_TYPES or (ty in ("proposal", "note") and (src == "assistant" or (isinstance(src, dict) and src.get("kind") == "assistant"))):
            rows.append({"id": e["id"], "ts": e["ts"], "type": ty, "text": _row_text(ty, p, e), "outcome": p.get("outcome", "ok")})
            if len(rows) >= n:
                break
    return rows


def _row_text(ty, p, e) -> str:
    if ty == "ai_action":
        return f"{p.get('tool')}: {p.get('summary') or p.get('route')}"
    if ty == "ai_question":
        return "asked: " + p.get("text", "")
    if ty == "ai_answer":
        return ("dismissed question" if p.get("dismissed") else "operator answered: " + str(p.get("answer", ""))) + f" ({p.get('qid')})"
    if ty == "plan_draft":
        return f"plan draft {p.get('file')}: {p.get('summary')} ({p.get('errors')} errors, {p.get('warnings')} warnings)"
    if ty == "plan_approved":
        return f"plan approved by {p.get('operator')}: {p.get('file')} {p.get('revision')} {p.get('old_hash')} -> {p.get('new_hash')}"
    if ty == "plan_draft_rejected":
        return f"plan draft rejected by {p.get('operator') or e.get('operator')}: {p.get('reason')}"
    if ty == "proposal":
        return f"proposal {p.get('board')} {p.get('test_id')} = {p.get('value')} {p.get('unit') or ''}".strip()
    if ty == "note":
        return "note: " + p.get("text", "")
    return ty


def card_state(sess, hub: Hub) -> dict:
    import time as _t
    url = hub.base_url or "http://127.0.0.1:8750"
    dr = plan_draft.status(sess, with_text=False) if (sess.proto.data.get("test_plan") or {}).get("files") else {"configured": False, "master_exists": False, "draft_exists": False}
    return {"mode": hub.mode, "mode_note": hub.mode_note, "connected": hub.connected(), "client": hub.client, "session": hub.session,
            "last_seen_s": round(_t.time() - hub.last_seen, 1) if hub.last_seen else None, "heartbeat_window_s": HEARTBEAT_S,
            "setup": setup_commands(url), "actions": _action_rows(sess), "questions": [q for q in questions(sess) if q["open"]],
            "draft": {k: dr.get(k) for k in ("configured", "master_exists", "draft_exists", "summary", "draft_name", "stale", "can_approve", "why_not")}
            | {"errors": (dr.get("lint") or {}).get("errors"), "load_errors": (dr.get("load") or {}).get("errors")},
            "recovery": getattr(sess, "recovery", None)}


def mcp_command() -> str:
    """The polybench-mcp of this install (absolute, so the assistant finds it without the venv on its PATH)."""
    import shutil
    import sys
    exe = Path(sys.executable).parent / "polybench-mcp"
    return str(exe) if exe.is_file() else (shutil.which("polybench-mcp") or "polybench-mcp")


TRIAL_FILE = "~/.config/polybench/claude-mcp-trial.json"


def setup_commands(url: str) -> dict:
    cmd = mcp_command()
    env = "" if url.rstrip("/") == "http://127.0.0.1:8750" else f" --env POLYBENCH_URL={url}"
    codex_env = "" if not env else f'\nenv = {{ POLYBENCH_URL = "{url}" }}'
    return {"url": url,
            "claude_trial": f"claude --mcp-config {TRIAL_FILE}",
            "claude_trial_p": f"claude -p --mcp-config {TRIAL_FILE} --allowedTools mcp__polybench \"<your question>\"",
            "trial_setup": "polybench ai setup --trial" + ("" if not env else f" --url {url}"),
            "claude": f"claude mcp add polybench{env} -- {cmd} --client claude-code",
            "codex": f'# ~/.codex/config.toml\n[mcp_servers.polybench]\ncommand = "{cmd}"\nargs = ["--client", "codex"]{codex_env}',
            "skills": (str(Path(cmd).with_name("polybench")) if Path(cmd).is_absolute() else "polybench") + " ai install-skills",
            "check": "polybench ai setup"}


# ----------------------------------------------------------------------------- troubleshooting bundle
def troubleshoot(sess, board: str, test_id: str = "") -> dict:
    eng, st = sess.engine, sess.engine.state
    eng._board(board)
    from .. import tiers
    sim = tiers.similarity_all(st)
    missing = []
    if test_id and test_id not in st.test_index:
        r = retired_info(sess, test_id)
        if not r:
            raise CmdError(f"unknown test '{test_id}'", 404)
        rv = retired_view(r, board)
        return {"board": board, "test_id": test_id, "simulated": bool(st.meta.get("simulate")), "retired": True,
                "readings": [{"test": rv, "this_board": next(iter(rv["results"]), None), "retired": True}], "parts": [], "bom_lines": [],
                "nets": {}, "polarised": [], "aoi_on_these_parts": [], "aoi_all_findings_on_board": [], "aoi_photos": [], "notes": [],
                "missing": ["this test is retired (not in the current plan): only its kept result is available"] if rv["results"]
                else [f"{board} has no kept result for the retired test {test_id}"],
                "rules": "Label every explanation 'Hypothesis:' with its evidence. Never state a board passes or fails: the operator decides."}
    if test_id:
        test_view(sess, test_id)
        tids = [test_id]
    else:
        tids = []
        for s, sec, t in sess.proto.tests():
            if not st.applies(s, board) or not st.sec_applies(s, sec, board):
                continue
            c = st.cell(board, t["id"])
            bad = c["status"] == "accepted" and c.get("verdict") in ("fail", "review")
            flagged = bool(((sim.get(t["id"]) or {}).get("flagged") or {}).get(board))
            stopped = any(x["board"] == board and x.get("test_id") == t["id"] for x in st.stops)
            if bad or flagged or stopped:
                tids.append(t["id"])
        if not tids:
            missing.append(f"{board} has no failed, review, stopped or not-similar reading yet")
    readings = []
    parts, nets = set(), set()
    known = {d for b in sess.proto.data["bom"] for d in b["designators"]}
    for tid in tids:
        tv = test_view(sess, tid)
        cv = cell_view(sess, board, tid)
        bv = batch_view(sess, tid)
        readings.append({"test": tv, "this_board": cv, "batch": bv,
                         "stops": [x for x in st.stops if x["board"] == board and x.get("test_id") == tid]})
        if cv["status"] == "empty":
            missing.append(f"{tid}: no reading on {board} yet")
        parts.update(tv["highlight"])
        parts.update(p.split(".")[0] for p in tv["highlight_pins"])
        parts.update(p["point"] for p in tv["probe"] if p.get("point") in known)
        nets.update(tv["nets"])
    bom = [b for b in st.bom_lines() if set(b["designators"]) & parts]
    polarised = []
    from .. import polarity_card as polcard
    for b in bom:
        if polcard.is_polarised(b):
            polarised.append({"line": b["line"], "designators": [d for d in b["designators"] if d in parts], "polarity": b.get("polarity", ""),
                              "hint": "get_polarity(line) gives the polarity card"})
    net_tests = {}
    for s, sec, t in sess.proto.tests():
        for n in t.get("nets") or []:
            if n in nets:
                net_tests.setdefault(n, []).append(t["id"])
    aoi = aoi_view(sess, board)
    if aoi.get("configured") is False:
        missing.append("this protocol has no AOI block: no photos or AOI findings")
    else:
        if not aoi["photos"]:
            missing.append(f"no AOI photo for {board}")
        if not aoi.get("run"):
            missing.append("no AOI run yet")
    near = [f for f in aoi.get("findings", []) if set(f.get("designators") or []) & parts]
    if st.project["skipped"]:
        missing.append("ECAD is off for this batch (continued without ECAD): no highlight, footprints read from the cache only")
    return {"board": board, "test_id": test_id or None, "simulated": bool(st.meta.get("simulate")), "readings": readings,
            "parts": sorted(parts), "bom_lines": [{k: b.get(k) for k in ("line", "designators", "value", "mpn", "package", "polarity", "dnf")} for b in bom],
            "nets": {n: {"tests": net_tests.get(n, [])} for n in sorted(nets)}, "polarised": polarised,
            "aoi_on_these_parts": near, "aoi_all_findings_on_board": aoi.get("findings", []), "aoi_photos": aoi.get("photos", []),
            "notes": [n for n in st.notes_public() if board in (n.get("boards") or [n.get("board")])][-10:],
            "missing": missing,
            "rules": "Label every explanation 'Hypothesis:' with its evidence. Post the next measurement with ask_operator (or propose_value "
                     "only for a value the operator gave you). Never state a board passes or fails: the operator decides."}


def aoi_view(sess, board: str | None = None) -> dict:
    st = sess.engine.state
    snap = st.aoi_snapshot()
    if snap is None:
        return {"configured": False, "photos": [], "findings": []}
    pdir = sess.dir / "aoi" / "photos"
    photos = []
    for ph in snap["photos"]:
        if board and ph.get("board") != board:
            continue
        f = Path(ph["file"])
        photos.append({"board": ph.get("board"), "side": ph.get("side"), "name": ph.get("name"),
                       "path": str(f if f.is_absolute() else pdir / f), "file": ph["file"],
                       "thumb_url": None if f.is_absolute() else f"/aoi/photo/{ph['file']}?thumb=640"})
    findings = []
    run = st.aoi_current_run()
    if run and run.get("result") and Path(run["result"]).is_file():
        from ..aoi import link, review_merge as M
        try:
            doc = json.loads(Path(run["result"]).read_text())
            items = M.review_items(doc)
        except Exception as e:           # a damaged result file: say so, do not guess
            items = []
            findings.append({"error": f"AOI result unreadable: {e}"})
        for it in items:
            if board and it["board"] != board:
                continue
            ev, urls = [], []
            for e in it.get("evidence") or []:
                p = Path(e)
                if not p.is_absolute():
                    p = Path(run["dir"]) / p
                if p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                    ev.append(str(p))
                    try:
                        urls.append("/aoi/review/file/" + str(p.resolve().relative_to(sess.dir.resolve())).replace("\\", "/"))
                    except ValueError:
                        pass
            d = st.aoi_decision_for(it["key"], run) or {}
            findings.append({"key": it["key"], "board": it["board"], "id": it["id"], "kind": it["kind"], "aoi_status": it["status"],
                             "value": it.get("value"), "confidence": it.get("confidence"), "note": it.get("note", ""),
                             "designators": link.designators_of(it["id"]), "decision": d.get("decision"), "operator_note": d.get("note", ""),
                             "reworked": it["key"] in st.aoi_reworked, "evidence": ev, "evidence_urls": urls})
    for m in st.aoi_manual.values():
        if board and m["board"] != board:
            continue
        d = st.aoi_decision_for(m["key"]) or {}
        findings.append({"key": m["key"], "board": m["board"], "id": m.get("id"), "kind": m.get("kind"), "manual": True,
                         "designators": m.get("designators") or [], "note": m.get("note", ""), "decision": d.get("decision"), "evidence": []})
    per = snap["per_board"].get(board) if board else snap["per_board"]
    return {"configured": True, "photos": photos, "run": snap["run"], "per_board": per, "findings": findings,
            "rule": "AOI lists candidates; the operator's Y / N / Unsure decides. Nothing is a PASS because AOI found nothing."}


# ----------------------------------------------------------------------------- mount
class _LiveEngine:
    """The current session's engine, looked up on every use: the Project page swaps sessions, and the server may start with none."""
    __slots__ = ("_sess",)

    def __init__(self, sess):
        object.__setattr__(self, "_sess", sess)

    def __getattr__(self, k):
        return getattr(object.__getattribute__(self, "_sess").engine, k)


def install(app, sess, hub: Hub):
    eng = _LiveEngine(sess)

    def ident_of(request: Request) -> dict:
        ident = getattr(request.state, "ai", None)
        if not ident:
            raise CmdError("assistant routes need the assistant identity headers (use the polybench MCP server)", 400)
        return ident

    async def act(request, tool, fn, summary="", **extra):
        ident = ident_of(request)
        try:
            r = fn(ident)
            if asyncio.iscoroutine(r):
                r = await r
        except CmdError as e:
            hub.journal_action(ident, tool, request.url.path, "error", f"{summary}: {e}", **extra)
            await sess.broadcast({"kind": "state"})
            raise
        hub.journal_action(ident, tool, request.url.path, "ok", summary, **extra)
        await sess.broadcast({"kind": "state"})
        return r

    # ---- read
    @app.get("/api/ai/status")
    async def ai_status():
        return card_state(sess, hub)

    @app.post("/api/ai/heartbeat")
    async def ai_heartbeat(request: Request):
        return {"ok": True, "mode": hub.mode}

    @app.get("/api/ai/context")
    async def ai_context():
        snap = eng.state.snapshot()
        steps = [{k: s.get(k) for k in ("id", "kind", "title", "state", "settled", "applicable", "fails", "retests")} for s in snap["steps"]]
        dr = plan_draft.status(sess, with_text=False)
        return {"batch": sess.batch, "design": snap["design"], "simulated": bool(snap["meta"].get("simulate")),
                "data_note": "SIMULATED batch: values are not measurements" if snap["meta"].get("simulate") else "real batch",
                "operator": eng.operator, "permission_mode": hub.mode, "boards": [{"label": b["label"], "serial": b.get("serial", "")} for b in snap["boards"]],
                "steps": steps, "open_stops": [s for s in snap["stops"] if not s.get("ack")], "focus": hub.focus,
                "open_questions": [q for q in questions(sess) if q["open"]], "journal_head": sess.journal.events[-1]["id"] if sess.journal.events else 0,
                "ecad": {"program": sess.ecad_program, "skipped": bool(eng.state.project["skipped"]), "selected": (eng.state.project.get("select") or {}).get("document")},
                "test_plan": {k: dr.get(k) for k in ("configured", "master_name", "master_hash", "master_exists", "draft_exists", "stale", "can_approve")},
                "aoi_configured": bool(sess.proto.data.get("aoi"))}

    @app.get("/api/ai/focus")
    async def ai_focus():
        f = dict(hub.focus)
        if not f:
            return {"focus": None, "note": "the operator's screen has not reported its focus yet (is the Polybench page open?)"}
        out = {"focus": f}
        tid, b = f.get("test"), f.get("board")
        if tid and tid in eng.state.test_index:
            out["test"] = test_view(sess, tid)
            if b:
                out["cell"] = cell_view(sess, b, tid)
        return out

    @app.get("/api/ai/board")
    async def ai_board(board: str):
        eng._board(board)
        st = eng.state
        from .. import tiers
        sim = tiers.similarity_all(st)
        steps = []
        for s in sess.proto.data["steps"]:
            if not st.applies(s, board):
                continue
            sb = st.step_board(s, board)
            tests = []
            for sec in s.get("sections", []):
                if not st.sec_applies(s, sec, board):
                    continue
                for t in sec.get("tests", []):
                    cv = cell_view(sess, board, t["id"])
                    if cv["status"] == "empty" and not cv.get("proposals"):
                        continue
                    cv.pop("board", None)
                    fl = ((sim.get(t["id"]) or {}).get("flagged") or {}).get(board)
                    if fl:
                        cv["not_similar"] = fl.get("text") if isinstance(fl, dict) else fl
                    cv["title"] = t.get("title")
                    tests.append(cv)
            steps.append({"step": s["id"], "title": s.get("title"), "state": sb.get("state"), "settled": sb.get("settled"),
                          "applicable": sb.get("applicable"), "fails": sb.get("fails", 0), "gate": st.gate(s, board), "tests": tests})
        retired = [retired_view(r, board) for r in st.retired()]
        retired = [r for r in retired if r["results"]]
        return {"board": board, "simulated": bool(st.meta.get("simulate")), "steps": steps, "retired_results": retired, "stops": [x for x in st.stops if x["board"] == board],
                "notes": [n for n in st.notes_public() if board in (n.get("boards") or [n.get("board")])][-20:],
                "aoi": st.aoi_board_status(board) if sess.proto.data.get("aoi") else None}

    @app.get("/api/ai/test")
    async def ai_test(test_id: str, board: str = ""):
        if test_id not in eng.state.test_index and (r := retired_info(sess, test_id)):
            if board:
                eng._board(board)
            return {"test": retired_view(r, board), "retired": True}
        out = {"test": test_view(sess, test_id), "batch": batch_view(sess, test_id)}
        if board:
            eng._board(board)
            out["cell"] = cell_view(sess, board, test_id)
        return out

    @app.get("/api/ai/journal")
    async def ai_journal(since: int = 0, limit: int = 50, types: str = ""):
        limit = max(1, min(int(limit), 200))
        ev = _journal_rows(sess, int(since), limit, set(t for t in types.split(",") if t) or None)
        return {"events": ev, "head": sess.journal.events[-1]["id"] if sess.journal.events else 0}

    @app.get("/api/ai/aoi")
    async def ai_aoi(board: str = ""):
        if board:
            eng._board(board)
        return await asyncio.to_thread(aoi_view, sess, board or None)

    @app.get("/api/ai/ecad")
    async def ai_ecad(designators: str = ""):
        want = {d.strip().upper() for d in designators.replace(",", " ").split() if d.strip()}
        status = await sess.ecad_status()
        bom = [{k: b.get(k) for k in ("line", "designators", "value", "mpn", "package", "polarity", "dnf", "side", "note")}
               for b in eng.state.bom_lines() if not want or set(b["designators"]) & want]
        nets: dict = {}
        for s, sec, t in sess.proto.tests():
            hit = not want or (set(t.get("highlight") or []) | {p.split(".")[0] for p in t.get("highlight_pins") or []}
                               | {p.get("point") for p in t.get("probe") or []}) & want
            if hit:
                for n in t.get("nets") or []:
                    nets.setdefault(n, []).append(t["id"])
        return {"status": status, "program": sess.ecad_program, "skipped": eng.state.project["skipped"],
                "selected": eng.state.project.get("select"), "parts": bom, "nets_in_tests": nets,
                "note": "Nets are those the tests name; the ECAD link is read / select / clear only."}

    @app.get("/api/ai/plan")
    async def ai_plan():
        dr = await asyncio.to_thread(plan_draft.status, sess, False)
        if not dr.get("configured"):
            return {"configured": False, "error": dr.get("error")}
        m = Path(dr["master"])
        text = m.read_text(encoding="utf-8") if m.is_file() else ""
        rep = await asyncio.to_thread(sess.plan_report)
        return {"configured": True, "path": str(m), "hash": dr["master_hash"], "text": text, "report": rep,
                "draft": {k: dr.get(k) for k in ("draft_exists", "draft", "hash", "summary", "stale", "can_approve", "why_not", "lint", "load")},
                "format": "docs/TEST_PLAN_FORMAT.md (plan_format 1)"}

    @app.get("/api/ai/plan_draft")
    async def ai_plan_draft():
        return await asyncio.to_thread(plan_draft.status, sess, True)

    @app.get("/api/ai/polarity")
    async def ai_polarity(line: int, d: str | None = None):
        if not eng.state.project["skipped"]:
            sess.get_scanner()
        return await asyncio.to_thread(sess.polarity_card, line, d)

    @app.get("/api/ai/troubleshoot")
    async def ai_troubleshoot(board: str, test_id: str = ""):
        return await asyncio.to_thread(troubleshoot, sess, board, test_id)

    @app.get("/api/ai/questions")
    async def ai_questions():
        return {"questions": questions(sess)}

    # ---- act (assistant, mode propose)
    @app.post("/api/ai/propose")
    async def ai_propose(request: Request, body: dict):
        b = body or {}
        board, tid, value = str(b.get("board", "")), str(b.get("test_id", "")), str(b.get("value", "")).strip()
        reason = " ".join(str(b.get("reason", "")).split())[:300]

        def do(ident):
            eng._board(board)
            s, sec, t = eng._test(tid)
            if not reason:
                raise CmdError("a proposal from the assistant needs a reason (where the value comes from)", 422)
            ev = evaluate(t, value)
            if not ev["ok"]:
                raise CmdError(ev["error"], 422)
            src = {"kind": "assistant", "component": "AI assistant", "key": "assistant", "client": ident["client"],
                   "session": ident["session"], "run_ref": "", "reason": reason}
            e = eng.add_proposal(board, tid, value, b.get("unit") or None, raw=reason, source=src,
                                 origin="simulated" if eng.simulated else "assistant")
            return {"ok": True, "proposal_id": e["id"], "verdict_if_accepted": e["payload"]["verdict"],
                    "next": "The operator sees it under Proposals for this test and accepts or rejects it."}
        return await act(request, "propose_value", do, f"{board} {tid} = {value} ({reason})", board=board, test_id=tid)

    @app.post("/api/ai/note")
    async def ai_note(request: Request, body: dict):
        b = body or {}

        def do(ident):
            p = eng._note_payload(b.get("text", ""), b.get("board", ""), b.get("test_id", ""), category=b.get("category") or "observation",
                                  designators=b.get("designators"), step=b.get("step", ""), boards=b.get("boards"))
            p.update(source="assistant", client=ident["client"])
            e = eng._emit("note", p)
            return {"ok": True, "id": e["id"]}
        return await act(request, "add_note", do, str(b.get("text", ""))[:120], board=b.get("board", ""), test_id=b.get("test_id", ""))

    @app.post("/api/ai/highlight")
    async def ai_highlight(request: Request, body: dict):
        des = [str(x).strip() for x in ((body or {}).get("designators") or []) if str(x).strip()][:60]

        async def do(ident):
            return {"ok": True, "ecad": await sess.highlight(des)}
        return await act(request, "highlight", do, ("select " + " ".join(des)) if des else "clear selection")

    @app.post("/api/ai/ask")
    async def ai_ask(request: Request, body: dict):
        b = body or {}

        def do(ident):
            text = " ".join(str(b.get("text", "")).split())[:500]
            if not text:
                raise CmdError("a question needs text", 422)
            opts = [str(o)[:60] for o in (b.get("options") or [])][:6]
            if b.get("board"):
                eng._board(b["board"])
            if b.get("test_id"):
                eng._test(b["test_id"])
            qid = uuid.uuid4().hex[:8]
            eng._emit("ai_question", {"source": "assistant", "client": ident["client"], "qid": qid, "text": text, "options": opts,
                                      "board": b.get("board", ""), "test_id": b.get("test_id", "")})
            return {"ok": True, "qid": qid, "next": "Read the answer later with get_questions (or get_journal)."}
        return await act(request, "ask_operator", do, str(b.get("text", ""))[:120], board=b.get("board", ""), test_id=b.get("test_id", ""))

    @app.post("/api/ai/plan_draft")
    async def ai_write_draft(request: Request, body: dict):
        b = body or {}

        def do(ident):
            return plan_draft.write(sess, b.get("text", ""), b.get("summary", ""), ident)
        return await act(request, "write_plan_draft", do, str(b.get("summary", ""))[:120])

    # ---- operator (the guard refuses these to the assistant)
    @app.post("/api/ai/focus")
    async def ai_set_focus(body: dict):
        b = body or {}
        hub.focus = {k: str(b.get(k) or "") for k in ("step", "board", "test", "view")}
        return {"ok": True}

    @app.post("/api/ai/answer")
    async def ai_answer(body: dict):
        b = body or {}
        q = next((x for x in questions(sess) if x["qid"] == b.get("qid")), None)
        if not q:
            raise CmdError("unknown question", 404)
        if not q["open"]:
            raise CmdError("already answered", 409)
        dismissed = bool(b.get("dismissed"))
        ans = " ".join(str(b.get("answer", "")).split())[:500]
        if not dismissed and not ans:
            raise CmdError("type an answer, or dismiss the question", 422)
        eng._emit("ai_answer", {"qid": q["qid"], "answer": ans, "dismissed": dismissed})
        await sess.broadcast({"kind": "state"})
        return {"ok": True}

    @app.post("/api/ai/plan_approve")
    async def ai_plan_approve(body: dict):
        b = body or {}
        r = await asyncio.to_thread(plan_draft.approve, sess, str(b.get("hash", "")), b.get("operator"))
        if (sess.proto.data.get("test_plan") or {}).get("generate"):
            rl = await sess.reload_plan()
            r["reload"] = {k: rl.get(k) for k in ("ok", "error", "plan_loaded", "tests")}
            if rl.get("ok") and getattr(sess, "recovery", None):
                sess.recovery = None                  # the master loads again: the red banner goes
        await sess.broadcast({"kind": "state"})
        return r

    @app.post("/api/ai/plan_reject")
    async def ai_plan_reject(body: dict):
        b = body or {}
        r = await asyncio.to_thread(plan_draft.reject, sess, b.get("reason", ""), b.get("operator") or eng.operator)
        await sess.broadcast({"kind": "state"})
        return r
