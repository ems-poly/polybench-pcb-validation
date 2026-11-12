# Ported from the earlier voice prototype prg/validate.py. Changes for Polybench: none.
"""Bind a parsed utterance to cells of the capture script and check it.

Everything the operator says (or the LLM returns) goes through `bind`. A result that does not
validate is rejected with a reason; nothing is ever coerced into a cell.
"""
from __future__ import annotations

from decimal import Decimal

from . import units as un
from .script import CaptureScript

BASE_SYMBOL = {"resistance": "Ω", "voltage": "V", "current": "A", "frequency": "Hz", "time": "s"}


class Context:
    """Operator context: current board and the row cursor (index into script.rows)."""

    def __init__(self):
        self.board: str | None = None
        self.row: int | None = None
        self.mode: str = "board"             # "row": the cursor is a row and `board` is the next board to fill (Session keeps it)
        self.recent_row: int | None = None   # row that auto-advanced after its last board; a bare pass/fail/note still belongs to it
        self.next_board = None               # row mode: callable(row_idx, start_board, taken) -> next unsettled board or None


def rows_for_id(script: CaptureScript, rid: str, ctx: Context) -> list[int]:
    idx = [i for i, r in enumerate(script.rows) if r["id"].lower() == rid.lower()]
    if len(idx) > 1 and ctx.row is not None:
        cur_table = script.rows[ctx.row]["table"]
        same = [i for i in idx if script.rows[i]["table"] == cur_table]
        if len(same) == 1:
            return same
    return idx


def row_by_number(script: CaptureScript, ctx: Context, n: int):
    """(row index, None) for the n-th row (1-based) of the cursor's table, or (None, reason)."""
    if ctx.row is None:
        return None, "row number needs a current table"
    tab = script.rows[ctx.row]["table"]
    in_tab = [i for i, r in enumerate(script.rows) if r["table"] == tab]
    if n > len(in_tab):
        return None, f"this table has only {len(in_tab)} rows"
    return in_tab[n - 1], None


def _board_names(boards, script: CaptureScript, row: dict) -> tuple[list[str] | None, str | None]:
    if boards == "all":
        names = [c["board"] for c in (script.by_key()[k] for k in row["cells"]) if c["kind"] in ("board", "entry")]
        return names, None
    if boards:
        names = [f"B{b}" for b in boards]
        for nm in names:
            if nm not in script.boards:
                return None, f"board {nm} does not exist (sheet has {', '.join(script.boards)})"
        return names, None
    return None, None


def format_number(vd: dict, spec: dict) -> tuple[str | None, str | None]:
    """Return (cell text, error) for a number value against a unit spec."""
    mode = spec["mode"]
    num = Decimal(vd["number"])
    sym = vd.get("unit")
    if sym is None and vd.get("prefix"):
        base = BASE_SYMBOL.get(spec["dimension"] or "")
        if base is None:
            return None, "a bare prefix needs a unit in the row"
        sym = vd["prefix"] + base
        if sym not in un.SYMBOL_DIM:
            return None, f"unit {sym} not understood"
    if mode == "count":
        if sym:
            return None, "this row is a count; no unit expected"
        if num != num.to_integral_value():
            return None, "a count must be a whole number"
        return str(int(num)), None
    if mode in ("yn", "hex"):
        return None, f"a number is not valid for a {spec['raw']} row"
    if mode == "ok_text":
        return None, "this row takes OK or a description, not a number"
    if mode in ("text", "free"):
        txt = vd["number"] + (f" {sym}" if sym else "")
        return txt, None
    # numeric with dimension
    if sym is None:
        if spec["canonical"]:
            return vd["number"], None
        return None, f"say the unit for this row ({' or '.join(spec['allowed'])})"
    if sym not in un.SYMBOL_DIM or un.SYMBOL_DIM[sym] != spec["dimension"]:
        return None, f"unit {sym} is not compatible with {spec['raw']}"
    if spec["canonical"]:
        if sym == spec["canonical"]:
            return vd["number"], None
        return un.fmt_decimal(un.convert(num, sym, spec["canonical"])), None
    # several allowed units
    if sym in spec["allowed"]:
        return f"{vd['number']} {sym}", None
    target = max(spec["allowed"], key=lambda s: un.DIMS[spec["dimension"]][s])
    return f"{un.fmt_decimal(un.convert(num, sym, target))} {target}", None


def format_value(v: dict, spec: dict) -> tuple[str | None, str | None]:
    k = v["kind"]
    mode = spec["mode"]
    if k == "number":
        return format_number(v, spec)
    if k == "ol":
        if mode == "number":
            return "OL", None
        return None, "OL is only valid in a numeric row"
    if k == "ok":
        if mode in ("ok_text", "text", "free"):
            return "OK", None
        return None, f"OK is not valid for a {spec['raw']} row"
    if k == "yn":
        if mode != "yn":
            return None, f"yes/no is not valid for a {spec['raw']} row"
        if len(v["values"]) != spec["repeat"]:
            return None, f"this row needs {spec['repeat']} yes/no answer(s), heard {len(v['values'])}"
        return " ".join(v["values"]), None
    if k == "hex":
        if mode in ("hex", "text", "free", "ok_text"):
            return v["text"], None
        return None, f"a hex value is not valid for a {spec['raw']} row"
    if k == "verdict":
        if mode in ("text", "free", "ok_text", "hex", "count"):
            return v["text"], None
        return None, f"a verdict word is not valid for a {spec['raw']} row"
    if k == "text":
        if mode in ("text", "ok_text", "free"):
            return v["text"], None
        return None, f"free text is not valid in a {spec['raw'] or 'numeric'} row; use note"
    return None, "unknown value kind"


def say_unit(s: str | None) -> str:
    return (s or "").replace("Ω", "ohm").replace("µ", "micro ")


def bind(parse: dict, script: CaptureScript, ctx: Context) -> dict:
    """Return {'ok': True, 'proposals': [...], 'readback': str} or {'ok': False, 'reason': str}."""
    intent = parse.get("intent")
    if intent not in ("value", "pass", "fail", "note"):
        return {"ok": True, "proposals": [], "readback": ""}
    # --- row
    if parse.get("id_candidates"):
        return {"ok": False, "reason": "ambiguous ID: could be " + ", ".join(parse["id_candidates"][:6])}
    if parse.get("row_no"):
        ri, err = row_by_number(script, ctx, parse["row_no"])
        if err:
            return {"ok": False, "reason": err}
    elif parse.get("id"):
        rows = rows_for_id(script, parse["id"], ctx)
        if not rows:
            return {"ok": False, "reason": f"ID {parse['id']} is not on the sheet"}
        if len(rows) > 1:
            secs = "; ".join(f"{script.rows[i]['section']}" for i in rows)
            return {"ok": False, "reason": f"ID {parse['id']} is ambiguous ({secs}); move the cursor to the right table first"}
        ri = rows[0]
    elif ctx.mode == "row" and ctx.recent_row is not None and intent in ("pass", "fail", "note"):
        ri = ctx.recent_row
    elif ctx.row is not None:
        ri = ctx.row
    else:
        return {"ok": False, "reason": "no ID given and no current row"}
    row = script.rows[ri]
    cells = script.by_key()
    row_cells = [cells[k] for k in row["cells"]]
    # --- target cells
    if intent in ("pass", "fail"):
        pf = [c for c in row_cells if c["kind"] == "passfail"]
        if not pf:
            return {"ok": False, "reason": f"row {row['id']} has no Pass/fail column"}
        if intent == "pass":
            val = "P"
        else:
            bs = parse.get("boards")
            if bs and bs != "all":
                for b in bs:
                    if f"B{b}" not in script.boards:
                        return {"ok": False, "reason": f"board B{b} does not exist"}
                val = "F" + ",".join(str(b) for b in bs)
            else:
                val = "F"
        return _one(pf[0], val, row, f"{row['id']} {'pass' if intent == 'pass' else 'fail ' + val[1:]}", ri)
    if intent == "note":
        nt = [c for c in row_cells if c["kind"] == "notes"]
        if not nt:
            return {"ok": False, "reason": f"row {row['id']} has no Notes column"}
        board = None
        bs = parse.get("boards")
        if bs and bs != "all":
            board = f"B{bs[0]}"
        elif ctx.board:
            board = ctx.board
        text = parse["value"]["text"]
        if board:
            text = f"{board}: {text}"
        cur = nt[0]["current"].strip()
        val = f"{cur}; {text}" if cur else text
        return _one(nt[0], val, row, f"note {row['id']}: {text}", ri)
    # --- value
    names, err = _board_names(parse.get("boards"), script, row)
    if err:
        return {"ok": False, "reason": err}
    if names is None:
        entry = [c for c in row_cells if c["kind"] == "entry"]
        bcols = [c for c in row_cells if c["kind"] == "board"]
        if entry and not bcols:
            names = ["Entry"]
        elif not ctx.board:
            if len(bcols) == 1:
                names = [bcols[0]["board"]]
            else:
                return {"ok": False, "reason": "which board? say board and a number first"}
        else:
            names = [ctx.board]
    props, rb = [], []
    for nm in names:
        c = next((c for c in row_cells if c["kind"] in ("board", "entry") and c["board"] == nm), None)
        if c is None:
            return {"ok": False, "reason": f"row {row['id']} has no {nm} column"}
        text, e = format_value(parse["value"], c["unit_spec"])
        if e:
            return {"ok": False, "reason": f"{row['id']}: {e}"}
        from .units import check_value, UnitSpec
        spec = UnitSpec(**{k: c["unit_spec"][k] for k in ("raw", "mode", "dimension", "allowed", "canonical", "repeat")})
        flag = check_value(text, spec, c["limits"], c["expected"])
        props.append({"cell": c["key"], "value": text, "flag": flag, "board": nm, "id": row["id"],
                      "overwrites": c["current"] if c["current"].strip() else None})
        shown = text
        if parse["value"]["kind"] == "number" and c["unit_spec"]["canonical"] and text and text[-1].isdigit():
            shown = f"{text} {c['unit_spec']['canonical']}"
        rb.append(f"{nm} {row['id']} {say_unit(shown) if shown else ''}".strip())
    return {"ok": True, "proposals": props, "readback": ", ".join(rb), "row": ri,
            "board": names[0] if len(names) == 1 else None}


def _one(cell, val, row, rb, ri):
    return {"ok": True, "proposals": [{"cell": cell["key"], "value": val, "flag": None, "board": cell["board"],
                                       "id": row["id"], "overwrites": cell["current"] if cell["current"].strip() else None}],
            "readback": rb, "row": ri, "board": None}
