# Ported from the earlier voice prototype prg/extract.py. Changes for Polybench: none.
"""Validate the LLM's list of cell updates and turn it into proposals.

The model is a reader, not an authority. Every update is checked here, deterministically:
- the row ID must exist (and not be ambiguous), the boards must exist and must have been spoken
  ("all", an explicit list, a range, or the current board when none was spoken);
- a number must be a number the technician said (or one already entered in the row for "same as before");
- the unit must be compatible with the row after conversion; the Whisper slips megahertz/milliamps for ohm are
  corrected against the row's unit and recorded as a correction;
- the value must be numerically sane for the row;
- Pass/fail is computed from the Expected column where that column is numeric; the model's verdict is only used
  where there is no checkable criterion, and it can never turn a failing reading into a pass.
Nothing is written here; the caller adds the returned cells as pending proposals.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from . import grammar, llm
from . import units as un
from . import validate
from .validate import Context

CAPS = {"resistance": Decimal("1e10"), "voltage": Decimal("100"), "current": Decimal("20"),
        "frequency": Decimal("1e9"), "time": Decimal("1e5"), "percent": Decimal("1000")}
BASE_OF = {"resistance": "Ω", "frequency": "Hz", "current": "A", "voltage": "V", "time": "s"}
# Whisper confuses these pairs; only these dimension changes are treated as a slip (megahertz for megaohm, milliamps for milliohms)
SLIP_PAIRS = {("frequency", "resistance"), ("current", "resistance"), ("resistance", "frequency"), ("resistance", "current")}
ALL_WORDS = {"all", "every", "each", "everything", "rest", "others", "remaining", "both"}
SAME_WORDS = {"same", "ditto", "likewise", "before", "again"}
OL_RE = re.compile(r"\b(o\.?l\.?|ol|open|over|overload|overrange|overflow|out of range|infinity|infinite|same|ditto)\b", re.I)
RANGE_WORDS = {"to", "through", "thru", "till", "until"}
PASS_RE = re.compile(r"\b(pass|passed|passes|passing|all good)\b", re.I)
FAIL_RE = re.compile(r"\b(fail|failed|fails|failing|bad|defect|defective|faulty)\b", re.I)
RESTATE = {"have", "has", "had", "passed", "pass", "passes", "cleared", "clear", "and", "all", "ok", "okay", "fine", "good", "open", "load", "ol",
           "boards", "board", "the", "is", "was", "were", "are", "it", "that", "that's", "below", "above", "limit", "failed", "fail", "a", "on", "for", "in", "of"}


def _redundant_note(note: str, vd: dict | None, cells: list[dict], rid: str) -> bool:
    """A note that only restates the result (passed, cleared, below limit) or repeats the text value just entered adds nothing."""
    words = re.findall(r"[a-z0-9']+", note.lower())
    if all(w in RESTATE for w in words):
        return True
    n = " ".join(words)
    for c in cells:
        if c["id"] == rid and c.get("value") and " ".join(re.findall(r"[a-z0-9']+", c["value"].lower())) == n:
            return True
    return False


# ---------------------------------------------------------------------------------------------
# what the transcript licenses
def spoken_boards(text: str, nboards: int) -> tuple[set[int], bool]:
    """(boards named in the transcript, whether 'all'/'every'/'rest' was said). Ranges 'B1 to B4' are expanded."""
    toks, _ = grammar._tokens(text)
    found: list[tuple[int, int]] = []          # (token index, board)
    i, allflag = 0, False
    while i < len(toks):
        t = toks[i]
        if t in ALL_WORDS:
            allflag = True
        if t in ("five", "5") and i + 1 < len(toks) and toks[i + 1] in ("boards", "board", "ports", "units") and nboards == 5:
            allflag = True
        r = grammar._board_at(toks, i) if t not in ("all", "every") else None
        if r and r[0] != "all":
            for b in r[0]:
                found.append((i, b))
            i = max(i + 1, r[1])
            continue
        i += 1
    boards = {b for _, b in found}
    # "B1 to B4": a range word between two mentions
    for (i1, b1), (i2, b2) in zip(found, found[1:]):
        if b2 > b1 and any(w in RANGE_WORDS for w in toks[i1 + 1:i2]) and not any(w in ("and", "apart", "except") for w in toks[i1 + 1:i2]):
            boards.update(range(b1, b2 + 1))
    return boards, allflag


def spoken_numbers(text: str) -> set[Decimal]:
    toks, _ = grammar._tokens(text)
    out: set[Decimal] = set()
    for i in range(len(toks)):
        try:
            r = grammar.parse_number(toks, i)
        except Exception:
            r = None
        if r:
            out.add(r[0])
    return out


def _scaled(vals: set[Decimal]) -> set[Decimal]:
    out = set()
    for v in vals:
        for k in range(-6, 7, 3):
            out.add((v * (Decimal(10) ** k)).normalize())
    return out


def _dec(s) -> Decimal | None:
    try:
        d = Decimal(str(s).strip())
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


# ---------------------------------------------------------------------------------------------
# deterministic criterion
def criterion(text: str, c: dict) -> tuple[str, str]:
    """('fail' | 'ok' | 'unknown', reason) for the cell text `text` against the row's Expected column."""
    spec = un.UnitSpec(**{k: c["unit_spec"][k] for k in ("raw", "mode", "dimension", "allowed", "canonical", "repeat")})
    lim, exp, v = c["limits"], (c["expected"] or "").strip(), (text or "").strip()
    if spec.mode in ("yn", "ok_text"):
        if exp in ("Y", "N", "OK") and v and spec.repeat == 1:
            return ("ok", "") if v.upper() == exp.upper() else ("fail", f"{v}, expected {exp}")
        return "unknown", ""
    if spec.mode != "number":
        return "unknown", ""
    good = [l for l in lim["limits"] if not l["bad"]]
    if v.upper() == "OL":
        if lim["accepts_ol"]:
            return "ok", ""
        return ("fail", "OL where a number is expected") if good else ("unknown", "")
    m = re.match(r"^(%s)(?:\s*(\S+))?$" % un._NUM, v)
    if not m:
        return "unknown", ""
    val = _dec(m.group(1))
    usym = un.norm_symbol(m.group(2)) if m.group(2) else spec.canonical
    if val is None or usym is None:
        return "unknown", ""
    if re.match(r"^OL\s*(\(|;|$)", exp) and not good:
        return "fail", f"{v} where the pass criterion is OL"
    verdict, why = "ok", ""
    for l in lim["limits"]:
        lu = l["unit"] or usym
        try:
            x = un.convert(val, usym, lu) if un.SYMBOL_DIM.get(usym) == un.SYMBOL_DIM.get(lu) else val
        except Exception:
            x = val
        lo = _dec(l["lo"]) if l["lo"] is not None else None
        hi = _dec(l["hi"]) if l["hi"] is not None else None
        if l["kind"] == "range":
            inside = (lo is None or x >= lo) and (hi is None or x <= hi)
        elif l["kind"] == "max":
            inside = x < hi
        else:
            inside = x >= lo
        if l["bad"] and inside:
            return "fail", "in defect zone: " + l["clause"]
        if not l["bad"] and not inside:
            if l["soft"]:
                verdict, why = "unknown", "outside guess limit: " + l["clause"]
            else:
                return "fail", "outside " + l["clause"]
    return verdict, why


# ---------------------------------------------------------------------------------------------
def _slip(sym: str, want_dim: str) -> str | None:
    """megahertz/milliamps heard on an ohm row -> MΩ/mΩ (same prefix, the row's base unit)."""
    have = un.SYMBOL_DIM.get(sym)
    if have is None or (have, want_dim) not in SLIP_PAIRS:
        return None
    base = BASE_OF[have]
    prefix = sym[: len(sym) - len(base)]
    cand = prefix + BASE_OF[want_dim]
    return cand if cand in un.SYMBOL_DIM else None


def _heard_symbol(word: str) -> str | None:
    w = (word or "").strip()
    if not w:
        return None
    toks = w.lower().split()
    sym, n = un.spoken_unit(toks, w.split())
    return sym if n else un.norm_symbol(w)


def context_state(script, ctx: Context, eff, recent: list[str]) -> dict:
    """The dynamic part of the prompt: cursor, what the current row already holds, last entries."""
    row = script.rows[ctx.row] if ctx.row is not None and ctx.row < len(script.rows) else None
    by = script.by_key()
    row_filled = None
    if row:
        parts = []
        for k in row["cells"]:
            c = by[k]
            v = eff(k)
            if v and v.strip():
                parts.append(f"{c['column']}={v.strip()}")
        row_filled = ", ".join(parts) or None
    return {"row_id": row["id"] if row else None, "board": ctx.board, "row_filled": row_filled, "recent": recent[-6:],
            "batch": getattr(ctx, "mode", "board") == "row"}


def resolve(raw: dict, transcript: str, script, ctx: Context, eff, recent_numbers: list[Decimal] | None = None) -> dict:
    """Validate `raw` (the model's JSON). Returns the per-update trace, accepted cells, rejections and corrections."""
    res = {"ok": False, "command": None, "cells": [], "rejected": [], "corrections": [], "unplaced_notes": [],
           "checks": [], "row": None, "board": None, "skips": []}
    cmd = raw.get("command") if isinstance(raw, dict) else None
    updates = raw.get("updates") if isinstance(raw, dict) and isinstance(raw.get("updates"), list) else []
    if cmd and cmd != "none":
        if updates:
            res["rejected"].append({"update": None, "reason": f"command {cmd} together with updates; command ignored"})
        elif cmd in ("accept", "reject", "undo", "skip", "next", "previous", "repeat"):
            res["command"] = "back" if cmd == "previous" else cmd
            res["ok"] = True
            return res
    nb = len(script.boards)
    sp_boards, sp_all = spoken_boards(transcript, nb)
    sp_nums = spoken_numbers(transcript)
    same = bool(SAME_WORDS & set(grammar._tokens(transcript)[0]))
    ol_said = bool(OL_RE.search(transcript))
    licensed_boards: set[int] | None
    if sp_all:
        licensed_boards = set(range(1, nb + 1)) | sp_boards
    elif sp_boards:
        licensed_boards = sp_boards
    elif ctx.board:
        licensed_boards = {int(ctx.board[1:])}
    else:
        licensed_boards = None                 # single-board / Entry rows only
    row_mode = getattr(ctx, "mode", "board") == "row"
    implicit_utt = row_mode and not sp_boards and not sp_all     # no board named at all: boards are assigned in row order
    chain_prev: dict[int, str | None] = {}                       # row index -> last board handed out in this utterance
    chain_taken: dict[int, set] = {}
    by = script.by_key()
    rows_touched: dict[int, dict] = {}         # row index -> {"set": {col: text}, "fail": {board: reason}, "pass": bool, "fail_said": set}

    def reject(i, rid, board, reason):
        res["rejected"].append({"update": i, "id": rid, "board": board, "reason": reason})

    for i, u in enumerate(updates):
        if not isinstance(u, dict):
            reject(i, None, None, "update is not an object")
            continue
        rid = (u.get("id") or "").strip()
        if rid:
            rows = validate.rows_for_id(script, rid, ctx)
            if not rows:
                reject(i, rid, None, f"ID {rid} is not on the sheet")
                continue
            if len(rows) > 1:
                reject(i, rid, None, f"ID {rid} is ambiguous; move the cursor to the right table first")
                continue
            ri = rows[0]
        elif row_mode and ctx.recent_row is not None and (u.get("kind") or "none") == "none":
            ri = ctx.recent_row                  # a bare verdict or note belongs to the test that just completed
        elif ctx.row is not None:
            ri = ctx.row
        else:
            reject(i, None, None, "no ID and no current row")
            continue
        row = script.rows[ri]
        rc = [by[k] for k in row["cells"]]
        bcols = {c["board"]: c for c in rc if c["kind"] == "board"}
        entry = next((c for c in rc if c["kind"] == "entry"), None)
        kind = u.get("kind") or "none"
        row_modes = {c["unit_spec"]["mode"] for c in rc if c["kind"] in ("board", "entry")}
        if row_modes == {"yn"} and kind == "ok":
            kind = "yes"                       # "cleared", "OK" on a Y/N row mean yes
        elif row_modes == {"ok_text"} and kind == "yes":
            kind = "ok"
        rt = rows_touched.setdefault(ri, {"set": {}, "fail": {}, "pass": False, "fail_said": set(), "cells": {}})
        # ---- boards
        asked = [b for b in (u.get("boards") or []) if isinstance(b, int)]
        if implicit_utt and bcols and kind in ("number", "ol", "ok", "yes", "no", "text"):
            asked = []              # no board was spoken: whatever the model put in boards is a guess; the row order decides
        names: list[str] = []
        ok_boards = True
        for b in asked:
            nm = f"B{b}"
            if nm not in script.boards or nm not in bcols:
                reject(i, row["id"], nm, f"board {nm} does not exist on this row")
                ok_boards = False
            elif licensed_boards is not None and b not in licensed_boards:
                reject(i, row["id"], nm, f"board {nm} was not mentioned in the transcript")
                ok_boards = False
            else:
                names.append(nm)
        implicit_u = False
        if implicit_utt and bcols and kind in ("number", "ol", "ok", "yes", "no", "text") and (not asked or ok_boards):
            # row mode, no board spoken: the first value goes to the current board, the next to the board after it, and so on
            taken = chain_taken.setdefault(ri, set())
            if ri not in chain_prev:
                nb = ctx.board if (ri == ctx.row and ctx.board in bcols) else ctx.next_board(ri, None, ())
            else:
                nb = ctx.next_board(ri, chain_prev[ri], taken)
            if nb is None or nb in taken:
                reject(i, row["id"], None, "no open board left in this row for this value")
                continue
            chain_prev[ri] = nb
            taken.add(nb)
            names = [nb]
            implicit_u = True
        elif not asked and kind != "none":
            if entry and not bcols:
                names = ["Entry"]
            elif ctx.board and ctx.board in bcols and (licensed_boards is None or int(ctx.board[1:]) in licensed_boards):
                names = [ctx.board]
            elif len(bcols) == 1:
                names = list(bcols)
            else:
                reject(i, row["id"], None, "which board?")
                continue
        if kind != "none" and not names:
            if ok_boards:
                reject(i, row["id"], None, "no board for this value")
            continue
        names = sorted(dict.fromkeys(names), key=lambda n: (n == "Entry", n))
        summary = {"update": i, "id": row["id"], "boards": names, "kind": kind}
        skip_note = None
        if kind == "skip":
            for nm in names:
                if nm != "Entry":
                    res["skips"].append({"row": ri, "board": nm, "reason": (u.get("note") or u.get("text") or "").strip()})
            skip_note = "skipped" + (", " + (u.get("note") or u.get("text") or "").strip() if (u.get("note") or u.get("text") or "").strip() else "")
        # ---- value
        text_val: str | None = None
        vd = None
        correction = None
        if kind == "number":
            num = _dec(u.get("number"))
            if num is None:
                reject(i, row["id"], None, f"number {u.get('number')!r} is not a plain decimal")
                continue
            allowed = _scaled(sp_nums)
            if same:
                for k in row["cells"]:
                    m = re.match(r"^\s*(%s)" % un._NUM, eff(k) or "")
                    if m and by[k]["kind"] in ("board", "entry"):
                        allowed |= _scaled({Decimal(m.group(1))})
                for d in recent_numbers or []:
                    allowed |= _scaled({d})
            if num.normalize() not in allowed:
                reject(i, row["id"], None, f"number {u.get('number')} is not in the transcript")
                continue
            sym = None
            if u.get("unit"):
                if u["unit"] not in llm.UNIT_FROM_LLM:
                    reject(i, row["id"], None, f"unit {u['unit']!r} not recognised")
                    continue
                sym = llm.UNIT_FROM_LLM[u["unit"]]
            heard = _heard_symbol(u.get("unit_heard") or "") if (u.get("unit_heard") or "").lower().strip() in transcript.lower() else None
            spec = (bcols[names[0]] if names[0] in bcols else entry)["unit_spec"]
            dim = spec["dimension"]
            if heard and dim and heard in un.SYMBOL_DIM:
                # the word the technician said decides the prefix; the row decides the dimension (Whisper slips are corrected here)
                target = heard if un.SYMBOL_DIM[heard] == dim else _slip(heard, dim)
                if target and target != sym:
                    if un.SYMBOL_DIM[heard] != dim:
                        correction = f"{row['id']}: heard {u.get('unit_heard')} -> {target} (row unit {spec['raw']})"
                    sym = target
            if sym and dim and un.SYMBOL_DIM.get(sym) != dim:
                fixed = _slip(sym, dim)
                if fixed is None:
                    reject(i, row["id"], None, f"unit {sym} is not compatible with {spec['raw']}")
                    continue
                correction = f"{row['id']}: heard {u.get('unit_heard') or sym} -> {fixed} (row unit {spec['raw']})"
                sym = fixed
            elif sym and heard and dim and un.SYMBOL_DIM.get(heard) != dim and not correction:
                correction = f"{row['id']}: heard {u.get('unit_heard')} -> {sym} (row unit {spec['raw']})"
            vd = {"kind": "number", "number": un.fmt_decimal(num), "unit": sym, "prefix": None}
            base_sym = sym or spec["canonical"]
            if dim in CAPS and base_sym in un.SYMBOL_DIM:
                mag = abs(un.convert(num, base_sym, BASE_OF.get(dim, "%")))
                if mag > CAPS[dim]:
                    reject(i, row["id"], None, f"{num} {base_sym} is not a sane value for a {dim} row")
                    continue
            if correction:
                res["corrections"].append(correction)
        elif kind == "ol":
            if not ol_said:
                reject(i, row["id"], None, "OL was not said")
                continue
            vd = {"kind": "ol"}
        elif kind == "ok":
            vd = {"kind": "ok"}
        elif kind in ("yes", "no"):
            c0 = (bcols[names[0]] if names and names[0] in bcols else entry)
            rep = c0["unit_spec"]["repeat"] if c0 else 1
            vd = {"kind": "yn", "values": ["Y" if kind == "yes" else "N"] * rep}
        elif kind == "text":
            t = grammar.strip_board_scope((u.get("text") or "").strip())
            if not t:
                reject(i, row["id"], None, "text value is empty")
                continue
            vd = {"kind": "text", "text": t[:160]}
        # ---- cells for the value
        if vd:
            for nm in names:
                c = bcols.get(nm) or entry
                text, err = validate.format_value(vd, c["unit_spec"])
                if err:
                    reject(i, row["id"], nm, err)
                    continue
                flag = un.check_value(text, un.UnitSpec(**{k: c["unit_spec"][k] for k in ("raw", "mode", "dimension", "allowed", "canonical", "repeat")}), c["limits"], c["expected"])
                verdict, why = criterion(text, c)
                rt["set"][c["column"]] = text
                rt["cells"][c["column"]] = c
                if verdict == "fail" and nm != "Entry":
                    rt["fail"][int(nm[1:])] = why
                res["cells"].append({"cell": c["key"], "id": row["id"], "board": nm, "column": c["column"], "value": text, "flag": flag,
                                     "criterion": verdict, "why": why, "overwrites": (c["current"] if c["current"].strip() else None),
                                     "correction": correction, "update": i, "implicit": implicit_u,
                                     "conversion": (f"{vd['number']} {vd.get('unit') or ''}".strip() + f" -> {text}") if vd["kind"] == "number" else None})
        # ---- verdict
        v = u.get("verdict")
        if v == "pass" and not PASS_RE.search(transcript):
            res["corrections"].append(f"{row['id']}: model gave verdict pass but no pass word was said; ignored")
            v = "none"
        elif v == "fail" and not FAIL_RE.search(transcript):
            res["corrections"].append(f"{row['id']}: model gave verdict fail but no fail word was said; ignored")
            v = "none"
        if v == "pass":
            rt["pass"] = True
        elif v == "fail":
            for nm in (names or [f"B{b}" for b in asked if f"B{b}" in bcols]):
                if nm != "Entry":
                    rt["fail_said"].add(int(nm[1:]))
            if not asked and not names:
                rt.setdefault("fail_all", True)
        # ---- note
        note = (skip_note if skip_note is not None else (u.get("note") or "").strip())
        if note and skip_note is None and _redundant_note(note, vd, res["cells"], row["id"]):
            note = ""
        if note:
            nt = next((c for c in rc if c["kind"] == "notes"), None)
            tags = ", ".join(names) if names else (ctx.board or "")
            line = (f"{tags}: {note}" if tags else note)
            if nt is None:
                res["unplaced_notes"].append({"id": row["id"], "text": line, "reason": f"row {row['id']} has no Notes column"})
            else:
                cur = eff(nt["key"]).strip()
                val = line if not cur else (cur if line in cur else f"{cur}; {line}")
                if val != cur:
                    res["cells"].append({"cell": nt["key"], "id": row["id"], "board": None, "column": nt["column"], "value": val, "flag": None,
                                         "criterion": None, "why": "", "overwrites": nt["current"] if nt["current"].strip() else None,
                                         "correction": None, "update": i, "conversion": None})
        res["checks"].append(summary)
        res["row"] = ri
        if names and names[0] != "Entry":
            res["board"] = names[0] if len(names) == 1 else res["board"]
    _finish_rows(script, rows_touched, eff, res)
    res["ok"] = bool(res["cells"]) or bool(res["skips"])
    return res


def _finish_rows(script, rows_touched: dict, eff, res: dict) -> None:
    """Pass/fail per touched row, computed from the Expected column; the spoken verdict can add a P or a fail but never overrule the numbers."""
    by = script.by_key()
    for ri, rt in rows_touched.items():
        row = script.rows[ri]
        rc = [by[k] for k in row["cells"]]
        pf = next((c for c in rc if c["kind"] == "passfail"), None)
        # failing boards also from values already in the row
        fails = dict(rt["fail"])
        for c in rc:
            if c["kind"] == "board" and c["column"] not in rt["set"]:
                cur = (eff(c["key"]) or "").strip()
                if cur:
                    ver, why = criterion(cur, c)
                    if ver == "fail":
                        fails[int(c["board"][1:])] = why
        said_fail = rt["fail_said"]
        if pf is None:
            if fails or said_fail or rt["pass"]:
                res["rejected"].append({"update": None, "id": row["id"], "board": None, "reason": f"row {row['id']} has no Pass/fail column"})
            continue
        bad = set(fails)
        for b in said_fail:
            c = rt["cells"].get(f"B{b}")
            ver = criterion(rt["set"][f"B{b}"], c)[0] if c is not None else "unknown"
            if ver == "ok":
                res["corrections"].append(f"{row['id']}: said fail for B{b} but {rt['set'][f'B{b}']} meets the criterion; not marked")
            else:
                bad.add(b)
        cur = (eff(pf["key"]) or "").strip()
        cur_f = {int(x) for x in re.findall(r"\d", cur[1:])} if cur[:1].upper() == "F" else set()
        stale = set()
        for b in sorted(cur_f):                  # a board corrected to a passing value no longer fails
            cb = next((c for c in rc if c["kind"] == "board" and c["board"] == f"B{b}"), None)
            v = (rt["set"].get(f"B{b}") or eff(cb["key"]) or "").strip() if cb else ""
            if v and criterion(v, cb)[0] == "ok" and b not in said_fail:
                stale.add(b)
        if stale:
            cur_f -= stale
            res["corrections"].append(f"{row['id']}: " + ", ".join(f"B{b}" for b in sorted(stale)) + f" now meets the criterion; Pass/fail {cur} recomputed")
        new = None
        if bad:
            allb = sorted(bad | cur_f)
            new = "F" + ",".join(str(b) for b in allb)
            if rt["pass"]:
                res["corrections"].append(f"{row['id']}: said pass but " + "; ".join(f"B{b} {fails[b]}" for b in sorted(bad) if b in fails) + " -> marked fail")
        elif rt["pass"] and not cur_f and cur.upper() != "F":
            new = "P"
        elif rt["pass"] and (cur_f or cur.upper() == "F"):
            res["corrections"].append(f"{row['id']}: pass said but the row is already {cur}; Pass/fail left as it is")
        elif rt.get("fail_all") and not bad:
            new = "F"
        elif stale and cur_f:
            new = "F" + ",".join(str(b) for b in sorted(cur_f))
        if new and new != cur:
            why = "; ".join(f"B{b}: {fails[b]}" for b in sorted(bad) if b in fails)
            res["cells"].append({"cell": pf["key"], "id": row["id"], "board": None, "column": pf["column"], "value": new,
                                 "flag": ("computed from Expected: " + why) if why else None, "criterion": "fail" if new.startswith("F") else "ok",
                                 "why": why, "overwrites": pf["current"] if pf["current"].strip() else None, "correction": None,
                                 "update": None, "conversion": None})


def derive_for_values(script, targets: list[dict], eff) -> dict:
    """Fast-path companion: the grammar parsed plain values; compute Pass/fail from the Expected column for the rows they touched."""
    by = script.by_key()
    rows_touched: dict[int, dict] = {}
    for t in targets:
        c = by.get(t["cell"])
        if not c or c["kind"] not in ("board", "entry"):
            continue
        ri = next(i for i, r in enumerate(script.rows) if t["cell"] in r["cells"])
        rt = rows_touched.setdefault(ri, {"set": {}, "fail": {}, "pass": False, "fail_said": set(), "cells": {}})
        rt["set"][c["column"]] = t["value"]
        rt["cells"][c["column"]] = c
        ver, why = criterion(t["value"], c)
        if ver == "fail" and c["kind"] == "board":
            rt["fail"][int(c["board"][1:])] = why
    res = {"cells": [], "corrections": [], "rejected": []}
    _finish_rows(script, rows_touched, eff, res)
    return res
