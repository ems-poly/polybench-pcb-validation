"""The batch BOM as a CSV in the layout the Assembly Scanner's importer reads (assembly_scanner/bom.py).

The embedded scanner loads the batch BOM through its own importer, so this file is the bridge: one row per Polybench BOM line,
Mouser P/N first. It is written to the system temp folder by assembly_scanner/polybench_link.py, not into the batch folder.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

HEADER = ["Mouser Part Number", "Manufacturer Part Number", "Designators", "Description", "Package", "Fitted"]   # app/bom.py synonyms
MOUSER_SHAPE = re.compile(r"^\d{2,3}-\S+$")


def mouser_pn(line: dict) -> str:
    for c in line.get("barcode", []):
        if MOUSER_SHAPE.match(str(c).strip()):
            return str(c).strip()
    return ""


def export_bom_csv(bom_lines: list[dict], path: Path) -> dict:
    """One row per BOM line. Lines without a Mouser P/N are written with the MPN (the scanner skips rows it cannot key)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows, nopn = 0, []
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for b in sorted(bom_lines, key=lambda x: (x.get("order", x["line"]), x["line"])):
            pn = mouser_pn(b)
            if not pn:
                nopn.append(b["line"])
            w.writerow([pn or b.get("mpn", ""), b.get("mpn", ""), ", ".join(b["designators"]),
                        " ".join(x for x in (b.get("value", ""), b.get("package", "")) if x) or b.get("mpn", ""),
                        b.get("package", ""), "DNF" if b.get("dnf") else "Fitted"])
            rows += 1
    return {"path": str(path), "rows": rows, "lines_without_mouser_pn": nopn}
