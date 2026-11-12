"""Second half of build_layout.py: read pads and nets from the KiCad board and write polarity.json and netlist.json,
then add the polarity spec, the netlist and the fine-pitch IC list to the derived protocol. HOST-ONLY."""
import json, math, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "kicad" / "yd-esp32-s3" / "tools"))
import sexp                      # the redraw's own s-expression reader

MEDIUM_PREFIXES = ("C_1206", "D_SOD", "SOT-", "QFN", "SW_", "LED_WS")   # KiCad package-name prefixes the layout treats as medium parts

# Parts with a polarity mark the check reads. Basis: the redraw's schematic (YD-ESP32-S3 SCH V1.4) and the usual package marking.
# The datasheet of the diode (marking S4, value D26) is not identified (redraw TBR T9) and the CH343P datasheet page was not read for this file:
# both entries say so and the result stays a review aid (UNVALIDATED against the datasheets).
BAND = {"D1": "cathode band at the pad 1 (K) end", "D2": "cathode band at the pad 1 (K) end", "D3": "cathode band at the pad 1 (K) end"}
CORNER = {"U2": "pin 1 dot at the corner of pad 1"}
TALL = ['USB1', 'USB2', 'U4', 'BOOT', 'RST', 'C3', 'C4', 'U1', 'U3']
ANTENNA_MM = 6.24   # the photographed board runs 63.39 mm with the antenna tongue, the layout outline 57.15 mm (redraw PROVENANCE, maker's drawing)
DET_BAND = {"method": "ridge", "length_mm": 2.7, "width_mm": 1.6, "band_width_mm": 0.35, "zone_mm": [0.45, 1.15], "view_half_mm": 2.2,
            "row_fraction": 0.40, "sigma_mm": 0.03, "flank_mm": [0.10, 0.40], "locate_mm": 1.2}
DET_CORNER = {"size_mm": 3.0, "sigma_mm": [0.10, 0.22], "view_half_mm": 2.4, "corner_fraction": 0.45, "locate_mm": 1.2}


def _node(n, name):
    return [c for c in n if isinstance(c, list) and c and c[0] == name]


def read_pcb(pcb):
    """{ref: {'x','y','angle','pads':[{name,x,y,net}]}} in board mm, y up (as kicad-cli's position file), angle CCW degrees."""
    tree = sexp.parse(Path(pcb).read_text())
    out = {}
    for fp in _node(tree, "footprint"):
        ref = next(str(p[2]) for p in _node(fp, "property") if p[1] == "Reference")
        at = _node(fp, "at")[0]
        fx, fy, fa = float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0
        c, s = math.cos(math.radians(fa)), math.sin(math.radians(fa))
        pads = []
        for pad in _node(fp, "pad"):
            pa = _node(pad, "at")[0]
            px, py = float(pa[1]), float(pa[2])
            gx, gy = fx + px * c + py * s, fy - px * s + py * c       # file frame is y down; angle is CCW on screen
            net = _node(pad, "net")
            pads.append({"name": str(pad[1]), "x": round(gx, 4), "y": round(-gy, 4), "net": str(net[0][-1]) if net and len(net[0]) > 1 else ""})
        out[ref] = {"x": round(fx, 4), "y": round(-fy, 4), "angle": fa, "pads": pads}
    return out


def side_of(dx, dy):
    return ("right" if dx > 0 else "left") if abs(dx) >= abs(dy) else ("up" if dy > 0 else "down")


def quadrant_of(dx, dy):
    return ("N" if dy > 0 else "S") + ("E" if dx > 0 else "W")


def rot_quadrant(q, angle):
    from polybench.aoi.polarity import QUAD_VEC
    x, y = QUAD_VEC[q]
    for _ in range(int(round(angle / 90)) % 4):
        x, y = -y, x
    return quadrant_of(x, y)


def spec_for(parts):
    spec = {"schema": "polystage.aoi_polarity_spec/0", "min_decided": 2,
            "note": "min_decided is 2 here: the example has two photos. The default is 3 (docs/AOI.md, 'Polarity: min_decided').", "parts": {}}
    for d, why in BAND.items():
        p = parts[d]
        el = {"x": p["x"], "y": p["y"], "angle": p["angle"]}
        cath = next(q for q in p["pads"] if q["name"] == "1")
        side = side_of(cath["x"] - p["x"], cath["y"] - p["y"])
        spec["parts"][d] = {"kind": "band", "cathode_pad": "1", "cathode_net": cath["net"],
                            "footprint": {"element": el, "pads": p["pads"]},
                            "expected": {"side": side, "marking": "cathode band", "why": why + " (redraw schematic; marking S4 / D26 not identified, T9)"},
                            "datasheet": {"document": "none read for this file: package convention, band at the cathode", "page": None},
                            "detector": dict(DET_BAND)}
    for d, why in CORNER.items():
        p = parts[d]
        el = {"x": p["x"], "y": p["y"], "angle": p["angle"]}
        pin1 = next(q for q in p["pads"] if q["name"] == "1")
        quad = quadrant_of(pin1["x"] - p["x"], pin1["y"] - p["y"])
        # top view of a QFN with the part at angle 0: pad 1 on the left side, top end -> north-west corner
        spec["parts"][d] = {"kind": "corner_mark", "pin1_pad": "1",
                            "footprint": {"element": el, "pads": p["pads"]},
                            "expected": {"side": quad, "marking": "pin 1 dot", "why": why + " (QFN-16 3x3, redraw footprint; datasheet page not read, UNVALIDATED)"},
                            "datasheet": {"document": "none read for this file: package convention, dot at pin 1", "page": None, "pin1_corner_top_view": "NW"},
                            "detector": dict(DET_CORNER)}
    return spec


def netlist_for(layout, parts, cam_zip):
    """{'DES.n': net} where n is the pad's place in the order the AOI pad reader gives (assign_pads), matched to the KiCad pad by position."""
    from polybench.aoi import pads as padlib
    assigned = padlib.assign_pads(layout, padlib.read_paste_pads(cam_zip))
    nets = {}
    for d, pl in assigned.items():
        kp = parts[d]["pads"]
        for i, pad in enumerate(pl):
            best = min(kp, key=lambda k: (k["x"] - pad["cx"]) ** 2 + (k["y"] - pad["cy"]) ** 2)
            if (best["x"] - pad["cx"]) ** 2 + (best["y"] - pad["cy"]) ** 2 < 0.5 ** 2:
                nets[f"{d}.{i + 1}"] = best["net"]
    return {"schema": "polystage.aoi_netlist/0", "pads": nets}


def finish(out, pcb, cam_zip):
    out = Path(out)
    parts = read_pcb(pcb)
    layout = json.loads((out / "layout.json").read_text())
    (out / "polarity.json").write_text(json.dumps(spec_for(parts), indent=1))
    (out / "netlist.json").write_text(json.dumps(netlist_for(layout, parts, cam_zip), indent=0))
    pr = json.loads((out / "aoi_protocol.json").read_text())
    pr["defects"]["fine_pitch_ics"] = ["U2"]
    pr["defects"]["tall"] = TALL      # off the board plane: left out of the photo-to-photo fit
    pr["defects"]["netlist"] = "netlist.json"
    lim = "AOI supplies crops and flags candidates; the operator's decision records the result."
    top = [p["name"] for p in layout["parts"] if p["side"] == "top" and p.get("inspection_polygon")]
    pas = [d for d in top if d[0] in "CRD" and d != "D1" or d == "D1"]
    pr["rows"] = [
        {"id": "V01", "title": "Passives and diodes present, seated and soldered", "stage": "visual", "check": "joint_review",
         "designators": [d for d in top if d[0] in "CRD" or d in ("PWR", "RX", "TX")], "requires": ["top"], "ceiling": "review", "limits": lim},
        {"id": "V02", "title": "Polarity: diode cathode bands and CH343P pin 1", "stage": "visual", "check": "polarity_check",
         "designators": ["D1", "D2", "D3", "U2"], "requires": ["top"], "ceiling": "review",
         "limits": "Polarity is read from the marking in the photo against the redraw footprint; a result is withheld unless the detector's own 180 degree test holds. " + lim},
        {"id": "V03", "title": "No solder bridges at the CH343P and the diodes", "stage": "visual", "check": "bridge_suspicion",
         "designators": ["U2", "D1", "D2", "D3"], "requires": ["top"], "ceiling": "review", "limits": lim}]
    pr["registration"] = {"board_colour": "dark", "fit_without_tall": True, "overhang_mm": {"right": ANTENNA_MM}}
    pr["polarity"] = {"spec": "polarity.json"}
    pr["layout"]["cam_zip"] = "yd-esp32-s3_cam.zip"
    pr["boards"] = {f"D{i:02d}": {"label": "", "expect_absent": []} for i in (1, 2)}
    pr["reference_board"] = "D01"
    (out / "aoi_protocol.json").write_text(json.dumps(pr, indent=1))
    (out / "aoi.yaml").write_text("# AOI configuration (layout mode) for the YD-ESP32-S3 example. Paths are relative to this file. Regenerate: layout/build_layout.py\n"
                                  "aoi:\n  protocol: aoi_protocol.json\n  layout: layout.json\n  cam_zip: yd-esp32-s3_cam.zip\n")
