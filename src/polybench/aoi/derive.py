"""Derive the AOI inputs of a board from its released CAM package and the Polybench BOM (no ECAD program needed).

  layout     parts (x, y, angle, side, value, package) from the PnP files; per part the connected paste pads give the
             `inspection_polygon` (pads + 0.2 mm, as the PoC took it from Fusion); outline from profile.gbr
  protocol   the AOI protocol (classes, thresholds, one row per visual test)
  fragment   the `aoi:` block for the Polybench protocol

What cannot be derived stays out and is listed in `gaps` (nets, pin-1 pad positions, package outlines): AOI then does
not make the checks that depend on it.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import numpy as np
import yaml

from polybench.aoi import layout_guard, pads as padlib

# body size (length, width in mm) from the IPC-7351 package name, 0 deg orientation; only used to attach paste pads to parts
_IPC = re.compile(r"^(?:CAPC|RESC|DIOM|SODFL|SOD)?(\d{2})(\d{2})")
KICAD_CHIP_MM = {"0201": (.6, .3), "0402": (1.0, .5), "0603": (1.6, .8), "0805": (2.0, 1.25), "1206": (3.2, 1.6)}   # imperial code -> body (L, W) mm
KICAD_BODY_MM = {"USB_C_Receptacle": (8.94, 7.3), "ESP32-S3-WROOM-1": (18.0, 25.5), "SW_SPST_TS-1088": (5.1, 5.1), "D_SOD-123F": (2.7, 1.6), "SOT-23": (2.9, 1.3), "SOT-223": (6.5, 3.5)}                         # KiCad package-name prefix -> body (L, W) mm
EXTRA_BODY_MM: dict[str, tuple[float, float]] = {}      # a design's own package names -> body size; the core carries none
EXTRA_MEDIUM_PREFIXES: tuple[str, ...] = ()              # package-name prefixes the layout treats as medium parts, besides the built-in ones


def body_mm(package: str) -> tuple[float, float]:
    p = package.upper()
    m = re.match(r"^(?:QFN|SON|DFN)\d+P(\d{3,4})X(\d{3,4})", p)
    if m:
        return int(m.group(1)) / 100, int(m.group(2)) / 100
    m = re.match(r"^(CAPC|RESC)(\d{2})(\d{2})", p)
    if m:
        return int(m.group(2)) / 10, int(m.group(3)) / 10
    m = re.match(r"^(?:C|R|L|LED)_(\d{4})_\d{4}METRIC", p)          # KiCad chip footprints: C_0402_1005Metric
    if m and m.group(1) in KICAD_CHIP_MM:
        return KICAD_CHIP_MM[m.group(1)]
    m = re.search(r"(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)MM", p)               # KiCad names that carry the body: QFN-16-1EP_3x3mm_..., LED_WS2812B_PLCC4_5.0x5.0mm_...
    if m:
        return float(m.group(1)), float(m.group(2))
    for prefix, size in KICAD_BODY_MM.items():
        if p.startswith(prefix.upper()):
            return size
    table = {"0603": (1.6, .8), "0402": (1.0, .5), "0805": (2.0, 1.25), "SOD323FL": (1.7, 1.25)}
    return {**table, **EXTRA_BODY_MM}.get(p, (2.0, 2.0))


def read_outline(zip_path) -> list[dict]:
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if Path(n).name == "profile.gbr")
        text = z.read(name).decode("utf-8", "replace")
    fs = re.search(r"%FSLAX(\d)(\d)Y", text)
    div = 10 ** int(fs.group(2)) if fs else 1e4
    wires, cur = [], None
    for a, b, op in re.findall(r"X(-?\d+)Y(-?\d+)D0([12])\*", text):       # D02 moves, D01 draws: separate segments (KiCad) must not be chained
        p = (int(a) / div, int(b) / div)
        if op == "1" and cur is not None and cur != p:
            wires.append({"x1": cur[0], "y1": cur[1], "x2": p[0], "y2": p[1], "curve": 0})
        cur = p
    return wires


def derive(cam_zip, assembly_yaml, design: str, out_dir, dnf_extra=()):
    cam_zip, out_dir = Path(cam_zip), Path(out_dir)
    cam, digest = layout_guard.read_cam_pnp(cam_zip)
    bom = yaml.safe_load(Path(assembly_yaml).read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in bom.get("steps", [])}
    dnf = {d for line in bom["bom"] for d in line["designators"] if line.get("dnf")} | set(dnf_extra)
    pads = padlib.read_paste_pads(cam_zip)
    # nearest-part assignment: a paste pad belongs to the part whose rotated body box (+0.8 mm) holds it, nearest centre first
    owner: dict[str, list] = {n: [] for n in cam}
    for pad in pads:
        best = None
        for n, c in cam.items():
            if c["side"] != "top":
                continue
            L, W = body_mm(c["package"])
            a = np.radians(-c["angle"])
            dx, dy = pad["cx"] - c["x"], pad["cy"] - c["y"]
            u, v = dx * np.cos(a) - dy * np.sin(a), dx * np.sin(a) + dy * np.cos(a)
            if abs(u) <= L / 2 + .8 and abs(v) <= W / 2 + .8:
                d = float(np.hypot(dx, dy))
                if best is None or d < best[0]:
                    best = (d, n)
        if best:
            owner[best[1]].append(pad)
    bare = {d for line in bom["bom"] if line.get("dnf") for d in line["designators"] if not owner.get(d)}   # bare pads: no paste aperture
    parts, no_pads = [], []
    for n, c in sorted(cam.items()):
        L, W = body_mm(c["package"])
        a = np.radians(c["angle"])
        rot = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
        body = [list(np.array([c["x"], c["y"]]) + rot @ np.array(p)) for p in ((-L / 2, -W / 2), (L / 2, -W / 2), (L / 2, W / 2), (-L / 2, W / 2))]
        part = {"name": n, "value": c["value"], "x": c["x"], "y": c["y"], "angle": c["angle"], "side": c["side"],
                "fitted": n not in dnf, "package": c["package"], "polygon": [[round(float(x), 4), round(float(y), 4)] for x, y in body]}
        mine = owner.get(n) or []
        if mine and c["side"] == "top":
            xs = [x for p in mine for x, _ in (p["poly"] if "poly" in p else [(p["cx"] - p["w"] / 2, 0), (p["cx"] + p["w"] / 2, 0)])]
            ys = [y for p in mine for _, y in (p["poly"] if "poly" in p else [(0, p["cy"] - p["h"] / 2), (0, p["cy"] + p["h"] / 2)])]
            # rectangular pads: width/height are in the part's own frame for rotated parts, so take the larger half as a safe bound
            if any("poly" not in p and c["angle"] % 180 == 90 for p in mine):
                xs = [x for p in mine for x in (p["cx"] - p["h"] / 2, p["cx"] + p["h"] / 2)]
                ys = [y for p in mine for y in (p["cy"] - p["w"] / 2, p["cy"] + p["w"] / 2)]
            m = .2
            part["inspection_polygon"] = [[min(xs) - m, min(ys) - m], [max(xs) + m, min(ys) - m], [max(xs) + m, max(ys) + m], [min(xs) - m, max(ys) + m]]
            part["region_source"] = "connected_paste_pads_plus_0.2mm_margin"
        elif c["side"] == "top" and n not in bare:
            no_pads.append(n)
        parts.append(part)
    outline = read_outline(cam_zip)
    xs = [w[k] for w in outline for k in ("x1", "x2")]
    ys = [w[k] for w in outline for k in ("y1", "y2")]
    layout = {"board": design, "unit": "mm", "parts": parts, "holes": [], "outline": outline,
              "source": f"derived from {cam_zip.name} (sha256 {digest[:12]}): PnP + solderpaste_top + profile",
              "fingerprint": hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()}
    large = []      # the exposed-mask presence test is calibrated on one red-mask board only: no photo of this design exists yet
    ics = [n for n in cam if re.match(r"IC\d", n)]
    medium = ics + [n for n, c in cam.items() if c["package"].upper().startswith(("CAPC2012", "SOD", "C_1206", "D_SOD") + EXTRA_MEDIUM_PREFIXES) or n == "R7" or n == "R3"]
    visual = steps.get("visual", {})
    rows = []
    secs = visual.get("sections", [])
    arch = Path(assembly_yaml).parent / "Archive" / "visual_checklist.yaml"
    if not any(sec.get("tests") for sec in secs) and arch.is_file():
        # 16 Oct: Visual is AOI only and the manual checklist is archived; its parts and checks still define the AOI inspection rows
        secs = (yaml.safe_load(arch.read_text(encoding="utf-8")) or {}).get("sections", [])
    for sec in secs:
        for t in sec.get("tests", []):
            ds = [d for d in (t.get("highlight") or []) if d in cam and d not in bare]
            title = t["title"].lower()
            check = ("orientation_mark" if re.search(r"pin 1|polarity|cathode|chamfer|marking", title) else
                     "bridge_suspicion" if "bridge" in title else "watch_presence" if re.search(r"present|complete|empty|fitted", title) else "joint_review")
            rows.append({"id": t["id"], "title": t["title"], "stage": "visual", "check": check if ds else "none", "designators": ds,
                         "requires": ["top"] if ds else [], "ceiling": "review" if ds else "not_assessable",
                         "limits": "AOI supplies crops and flags candidates; it cannot establish joints, bridges, polarity or value. The operator's decision records the result."})
    protocol = {"schema": "polystage.aoi_protocol/0", "protocol": f"{design.lower()}-visual", "version": 1, "report_ref": "Polybench visual step",
                "layout": {"design": design, "cam_zip": str(cam_zip), "recorded": "layout.json"},
                "reference_board": "S01", "boards": {f"S{i:02d}": {"label": "", "expect_absent": []} for i in range(1, 11)},
                "dnf": sorted(n for n, c in cam.items() if c["value"].upper() == "DNF"), "optional": [], "watch_fitted": [],
                "non_parts": sorted(bare),
                "classes": {"large": {"ceiling": "review", "designators": large, "basis": "none: the exposed-mask presence test is not calibrated for this board's solder mask"},
                            "medium": {"ceiling": "review", "designators": sorted(set(medium)), "basis": "ICs and parts of 1.6 mm or more"},
                            "small": {"ceiling": "review", "designators": "*", "basis": "everything else"}},
                "thresholds": {"present_max_exposure": 0.15, "absent_min_exposure": 0.6},
                "defects": {"fine_pitch_ics": ics, "tall": [n for n, c in cam.items() if c["package"].startswith("74438")]},
                "rows": rows}
    gaps = ["no netlist: every close pad pair has an unknown net, same-net solder (for example GND to GND) is not filtered out",
            "no polarity data: pin-1 pad positions and the polarity marks are not in the CAM zip (needs the Fusion footprints); polarity is NOT assessed by AOI",
            "package outlines are estimated from the package name, pads from the paste layer: parts without a paste aperture get the body box only",
            "no photo of this design exists yet: the exposed-mask presence test is not calibrated, so large=[] and AOI proves no presence or absence",
            f"{len(no_pads)} top parts without a paste aperture: {', '.join(no_pads)}" if no_pads else "every top part has a paste aperture"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "layout.json").write_text(json.dumps(layout, indent=1), encoding="utf-8")
    (out_dir / "aoi_protocol.json").write_text(json.dumps(protocol, indent=1), encoding="utf-8")
    (out_dir / "aoi.yaml").write_text(
        f"# AOI configuration for the Visual step (derived {Path(cam_zip).name}; regenerate: polybench aoi-derive). Paths are relative to this file.\n"
        f"aoi:\n  protocol: aoi_protocol.json\n  layout: layout.json\n  cam_zip: {cam_zip}\n", encoding="utf-8")
    (out_dir / "GAPS.txt").write_text("\n".join(gaps) + "\n", encoding="utf-8")
    return {"parts": len(parts), "with_pads": sum(1 for p in parts if p.get("inspection_polygon")), "gaps": gaps, "rows": len(rows)}
