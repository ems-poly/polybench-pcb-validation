"""Reading what a part-bag label says (the codes the camera or the keyboard wedge delivers).

Rules follow the Assembly Scanner (docs/CONTRACTS.md):
  * D2/D3: a label carries about eight barcodes. The Mouser part number is a 1D Code 128; the DataMatrix has no Mouser part
    number. We do not rely on label layout: every code is offered, the one that is a key of the BOM wins.
  * D7: the customer part number (Code 128 like 'DEMO1 R4,5', DataMatrix field `P`) is never used for matching. It is
    shortened by the buyer on purpose, so it is incomplete by design.
  * Codes are compared stripped, upper-cased, with inner whitespace collapsed.

A DataMatrix or QR on a distributor label is usually ISO/IEC 15434 (`[)>` envelope, `RS`/`GS` separators) with ANSI MH10.8.2
data identifiers. `1P` is the manufacturer part number and is offered as a candidate; `P` is the customer part number and is
dropped; `Q` is the quantity and is only reported.
"""
from __future__ import annotations

import re

RS, GS, EOT = "\x1e", "\x1d", "\x04"


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s).strip()).upper()


def parse_label(text: str) -> dict:
    """{'candidates': [...], 'fields': {...}, 'qty': int|None, 'envelope': bool}. Plain strings give themselves."""
    raw = str(text or "")
    if not raw.strip():
        return {"candidates": [], "fields": {}, "qty": None, "envelope": False}
    if not raw.lstrip().startswith("[)>"):
        return {"candidates": [raw.strip()], "fields": {}, "qty": None, "envelope": False}
    body = raw.strip().rstrip(EOT)
    parts = [p for p in re.split(f"[{RS}{GS}]", body) if p != ""]
    fields: dict[str, str] = {}
    for p in parts[1:]:          # parts[0] is '[)>' (format code 06 follows as its own part)
        m = re.match(r"^(\d{0,2}[A-Z])(.*)$", p)
        if m and m.group(2):
            fields.setdefault(m.group(1), m.group(2).strip())
    cands: list[str] = []
    mpn = fields.get("1P")
    if mpn:
        cands.append(mpn)
    # `P` (customer part number) is dropped on purpose (D7).
    qty = None
    q = fields.get("Q")
    if q and re.fullmatch(r"\d{1,7}", q):
        qty = int(q)
    return {"candidates": cands, "fields": {k: v for k, v in fields.items() if k != "P"}, "qty": qty, "envelope": True}


def expand(codes) -> tuple[list[str], int | None]:
    """All matching candidates from the decoded codes, in order, and a quantity if a label states one."""
    out: list[str] = []
    qty = None
    for c in codes:
        info = parse_label(c)
        qty = qty if qty is not None else info["qty"]
        for x in info["candidates"]:
            if x not in out:
                out.append(x)
        from polybench import label_readers      # vendor readers add their own part numbers (mpn, then the vendor's)
        got = label_readers.read_label(c)
        if got:
            qty = qty if qty is not None else got.get("qty")
            for x in (got.get("mpn"), got.get("vendor_pn")):
                if x and x not in out:
                    out.append(x)
    return out, qty
