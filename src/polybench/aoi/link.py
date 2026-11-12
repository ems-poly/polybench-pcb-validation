"""Glue between the ported AOI pipeline and Polybench: photo naming, candidate -> visual test mapping,
and the state derived from the journal events `aoi_photo`, `aoi_photo_drop`, `aoi_run`, `aoi_decision`.

Nothing here touches images. AOI never writes a result into a test by itself: an operator decision does.
"""
from __future__ import annotations

import re
from pathlib import Path

PHOTO_SUFFIXES = {".heic", ".heif", ".jpg", ".jpeg", ".png"}
SIDE = re.compile(r"(?<![A-Za-z])(top|bottom|bot)(?![A-Za-z])", re.I)

# Words of a visual test title that say which defect kind it is about. A test may override with `aoi: [kind, ...]`.
KIND_WORDS = {
    "BRIDGE": r"bridg|short|solder balls",
    "MISSING": r"present|missing|complete|fitted|empty|none missing",
    "LIFT": r"flat|level|tombstone|float|lift|wett|fillet|square|tilt",
    "POL": r"pin ?1|polarity|orientation|cathode|band|triangle|chamfer|rotated|upright|marking",
    "ANOM": r".",
}
KIND_WORDS["POLSYS"] = KIND_WORDS["POL"]
KIND_WORDS["PART"] = KIND_WORDS["ANOM"]


def board_from_name(name: str, labels: list[str], prefix: str = "S") -> str | None:
    """`IMG_5990-B3.HEIC` or `x_S03_top.jpg` -> the batch label. B and the batch prefix both name a board
    number; a label such as `S03` or `B3` that exists in the batch is matched literally first."""
    stem = Path(name).stem
    for lab in sorted(labels, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(lab)}(?![0-9])", stem, re.I):
            return lab
    m = re.search(rf"(?<![A-Za-z0-9])(?:[B{re.escape(prefix)}]|board[_ -]?)(\d{{1,2}})(?![0-9])", stem, re.I)
    if m:
        n = int(m.group(1))
        for lab in labels:
            if re.fullmatch(r"[A-Za-z]*0*" + str(n), lab):
                return lab
    return None


def side_from_name(name: str) -> str:
    m = SIDE.search(Path(name).stem)
    return "bottom" if m and m.group(1).lower() in ("bottom", "bot") else "top"


def designators_of(result_id: str) -> list[str]:
    """AOI-LIFT-C4 -> [C4]; AOI-BRIDGE-IC4-6-7 -> [IC4]; AOI-BRIDGE-C1.2-C2.1 -> [C1, C2]; AOI-POL-SYS-D3 -> [D3]."""
    parts = result_id.split("-")
    if len(parts) < 3:
        return [parts[1]] if len(parts) == 2 else []
    kind = parts[1]
    if kind == "DIFF":
        return []
    if kind == "POL":
        return [parts[-1]]
    if kind == "BRIDGE":
        body = result_id.split("-", 2)[2]
        if "." in body:
            return [x.split(".")[0] for x in body.split("-") if "." in x]
        return [body.rsplit("-", 2)[0]]
    return [parts[2]]


def tests_for(steps: list[dict], kind: str, designators: list[str]) -> list[str]:
    """Visual tests that cover this candidate: the test names one of the designators in `highlight` and its title
    (or an explicit `aoi:` list) matches the defect kind. Falls back to every test that names the designator."""
    found, fallback = [], []
    for s in steps:
        if s.get("kind") != "visual":
            continue
        for sec in s.get("sections", []):
            for t in sec.get("tests", []):
                if not set(designators) & set(t.get("highlight") or []):
                    continue
                fallback.append(t["id"])
                tagged = t.get("aoi")
                if tagged is not None:
                    if kind in tagged:
                        found.append(t["id"])
                elif re.search(KIND_WORDS.get(kind, "."), t["title"], re.I):
                    found.append(t["id"])
    return found or fallback
