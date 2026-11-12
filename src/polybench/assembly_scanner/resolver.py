"""Resolve the codes decoded from one label against the BOM (docs/CONTRACTS.md, D3).

The scanner returns every barcode on the label (PO, line, P/N, cust P/N, qty,
COO, invoice, routing). We keep the code that is a key in the BOM index; we do
not rely on label layout. The 'Cust P/N' barcode (e.g. 'DEMO1 R4,5') is never
used for anything (D7: not designators, not matching, not a cross-check);
the user shortens it in the cart, so it is incomplete by design. The BOM file
is the authority.
"""
from __future__ import annotations

from polybench.assembly_scanner.bom import MOUSER_PN_SHAPE
from polybench.assembly_scanner.models import Bom, Resolution, Status, normalize_pn


def resolve(codes: list[str], bom: Bom) -> Resolution:
    raw = tuple(codes)
    idx = bom.by_mouser
    normed = [normalize_pn(c) for c in codes if c is not None]

    # First code (in scanner order) that is a BOM key wins.
    # A label carries one Mouser P/N, so >1 distinct key is not expected.
    for pn in normed:
        if pn in idx:
            lines = tuple(idx[pn])
            seen: set[str] = set()
            desigs = tuple(d for l in lines if l.fitted for d in l.designators
                           if not (d in seen or seen.add(d)))
            status = Status.MATCH if desigs else Status.MATCH_NO_PLACEMENT
            return Resolution(status, pn, lines, desigs, raw)

    for pn in normed:
        if MOUSER_PN_SHAPE.match(pn):
            return Resolution(Status.NOT_IN_BOM, pn, (), (), raw)

    return Resolution(Status.NO_CANDIDATE, None, (), (), raw)
