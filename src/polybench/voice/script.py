# Ported from the earlier voice prototype prg/script.py. Changes for Polybench: none.
"""Capture script: every fillable cell of a sheet, as JSON-serialisable dicts."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import sheet as sh
from . import units as un

BOARD_HDR = re.compile(r"^\s*(B\d+)\b", re.I)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "sheet"


def clean_id(s: str) -> str:
    return s.replace("`", "").strip()


@dataclass
class CaptureScript:
    sheet_path: str
    sha256: str
    cells: list[dict]
    boards: list[str]
    warnings: list[str] = field(default_factory=list)
    skipped_tables: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)      # ordered rows (cursor order)

    def to_json(self) -> dict:
        return {"sheet": self.sheet_path, "sha256": self.sha256, "boards": self.boards,
                "rows": self.rows, "cells": self.cells,
                "warnings": self.warnings, "skipped_tables": self.skipped_tables}

    def by_key(self) -> dict[str, dict]:
        return {c["key"]: c for c in self.cells}


def build(sheet: sh.Sheet) -> CaptureScript:
    cells, rows, boards = [], [], []
    warnings = list(sheet.warnings)
    skipped = []
    seen_slug: dict[str, int] = {}
    seen_keys: dict[str, int] = {}
    for t in sheet.tables:
        hdrs = [h.strip() for h in t.headers]
        low = [h.lower() for h in hdrs]
        id_col = next((i for i, h in enumerate(low) if h in ("id",)), None)
        board_cols = [(i, BOARD_HDR.match(h).group(1).upper()) for i, h in enumerate(hdrs) if BOARD_HDR.match(h)]
        entry_col = next((i for i, h in enumerate(low) if h in ("entry",)), None)
        pf_col = next((i for i, h in enumerate(low) if h.replace(" ", "") in ("pass/fail", "passfail")), None)
        notes_col = next((i for i, h in enumerate(low) if h in ("notes", "note")), None)
        if id_col is None or (not board_cols and pf_col is None and entry_col is None):
            if board_cols:
                skipped.append({"section": t.section, "headers": hdrs, "reason": "no ID column"})
            continue
        unit_col = next((i for i, h in enumerate(low) if h in ("unit", "units")), None)
        exp_col = next((i for i, h in enumerate(low) if h in ("expected", "expected value", "limit")), None)
        label_col = next((i for i, h in enumerate(low) if h in ("check", "item", "pad and net")), None)
        base = slug(t.section)
        n = seen_slug.get(base, 0)
        seen_slug[base] = n + 1
        tslug = base if n == 0 else f"{base}-{n + 1}"
        for r in t.rows:
            rid = clean_id(r.cells[id_col].text)
            if not rid:
                continue
            unit_txt = r.cells[unit_col].text if unit_col is not None else ""
            exp_txt = r.cells[exp_col].text if exp_col is not None else ""
            label = r.cells[label_col].text if label_col is not None else ""
            spec = un.classify_unit(unit_txt)
            lim = un.parse_limits(exp_txt, spec)
            row = {"table": t.index, "table_slug": tslug, "section": t.section, "id": rid, "label": label,
                   "unit": unit_txt, "expected": exp_txt, "line": r.line_no + 1, "cells": []}

            def add(col_idx, col_name, kind):
                c = r.cells[col_idx]
                key = f"{tslug}:{rid}:{col_name}"
                k = seen_keys.get(key, 0)
                seen_keys[key] = k + 1
                if k:
                    key = f"{key}#{k + 1}"
                d = {"key": key, "id": rid, "table": t.index, "section": t.section, "line": r.line_no + 1,
                     "column": col_name, "kind": kind, "board": col_name if kind in ("board", "entry") else None,
                     "label": label, "unit": unit_txt, "unit_spec": spec.as_dict(), "expected": exp_txt,
                     "limits": lim, "current": c.text, "start": c.start, "end": c.end}
                cells.append(d)
                row["cells"].append(key)
                if kind == "board" and col_name not in boards:
                    boards.append(col_name)
            for ci, b in board_cols:
                add(ci, b, "board")
            if entry_col is not None:
                add(entry_col, "Entry", "entry")
            if pf_col is not None:
                add(pf_col, "Pass/fail", "passfail")
            if notes_col is not None:
                add(notes_col, "Notes", "notes")
            rows.append(row)
    boards.sort(key=lambda b: int(b[1:]))
    # duplicate IDs
    count: dict[str, list[str]] = {}
    for r in rows:
        count.setdefault(r["id"], []).append(r["section"])
    for rid, secs in count.items():
        if len(secs) > 1:
            warnings.append(f"duplicate row ID {rid} in: {', '.join(secs)}; spoken use is ambiguous outside the cursor's table")
    return CaptureScript(sheet.path, sheet.sha256, cells, boards, warnings, skipped, rows)
