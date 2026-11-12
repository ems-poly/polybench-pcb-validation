"""Step 1 (Project): is the ECAD program linked, which document is this batch built from, does its part list agree with the BOM.

Pure functions; the server feeds them what the ECAD bridge read. Nothing here talks to an ECAD program.
"""
from __future__ import annotations

import hashlib


def fingerprint(names) -> str:
    """Short hash of the sorted part names. Three documents with one title differ in content, not in name."""
    return hashlib.sha1("\n".join(sorted(map(str, names))).encode()).hexdigest()[:8]


def crosscheck(bom_lines: list[dict], design_names) -> dict:
    """BOM designators against the designators of the selected document.

    matched            fitted in the BOM and present in the document
    missing_in_design  fitted in the BOM, absent from the document (nothing to highlight for them)
    missing_in_bom     in the document, on no BOM line (test points, fiducials, or a part the BOM forgot)
    dnf                BOM lines marked do-not-fit; `dnf_in_design` of them are drawn on the board (normal)
    """
    design = {str(n) for n in (design_names or [])}
    fit = [d for b in bom_lines if not b.get("dnf") for d in b["designators"]]
    dnf = [d for b in bom_lines if b.get("dnf") for d in b["designators"]]
    fitset, dnfset = set(fit), set(dnf)
    matched = sorted(fitset & design)
    missing_design = sorted(fitset - design)
    missing_bom = sorted(design - fitset - dnfset)
    return {
        "bom_total": len(fitset) + len(dnfset), "bom_fitted": len(fitset), "design_total": len(design),
        "matched": len(matched), "missing_in_design": missing_design, "missing_in_bom": missing_bom,
        "dnf": sorted(dnfset), "dnf_in_design": sorted(dnfset & design),
        "mismatches": len(missing_design) + len(missing_bom),
        "ok": not missing_design and not missing_bom,
    }
