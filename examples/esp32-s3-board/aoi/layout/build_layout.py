#!/usr/bin/env python3
"""Rebuild the AOI layout inputs of the YD-ESP32-S3 example from the KiCad redraw (HOST-ONLY).

    python build_layout.py            # needs kicad-cli (KiCad 10) and the aoi extra; writes into this folder

1. kicad-cli: pick-and-place (front, back) and the F.Paste and Edge.Cuts Gerbers of ../../kicad/yd-esp32-s3/.
2. They are packed as `yd-esp32-s3_cam.zip` under the names the AOI layout guard reads (PnP_*_front.txt, PnP_*_back.txt,
   solderpaste_top.gbr, profile.gbr). This is NOT a released CAM package: nothing was made from it, it only gives the layout
   guard and the pad reader a file to check against (docs/AOI.md, "YD example").
3. `polybench aoi-derive` (src/polybench/aoi/derive.py) writes layout.json, aoi_protocol.json, aoi.yaml, GAPS.txt.
4. polarity.json (polarity spec) and the protocol additions (fine-pitch ICs, polarity spec) come from polarity_spec.py.
"""
import json, os, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EX = HERE.parent.parent
PCB = EX / "kicad" / "yd-esp32-s3" / "yd-esp32-s3.kicad_pcb"
DESIGN = "YD-ESP32-S3"
KICAD_CLI = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli") or "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"


def run(*a):
    subprocess.run([KICAD_CLI, *map(str, a)], check=True, capture_output=True, timeout=60)


def pnp_text(path):
    """kicad-cli ascii position file -> the tab separated PnP the guard reads: name x y angle value package."""
    out = []
    for line in Path(path).read_text().splitlines():
        if not line or line.startswith(("#", "##")):
            continue
        ref, val, pkg, x, y, rot, side = line.split()
        out.append("\t".join((ref, x, y, rot, val, pkg)))
    return "\n".join(["Designator\tX\tY\tRotation\tValue\tPackage"] + out) + "\n"


def stable(text):
    """Drop the lines that carry the export time, so that the same board gives the same zip."""
    return "".join(l for l in text.splitlines(True) if "CreationDate" not in l and "Created by" not in l)


def main():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        run("pcb", "export", "pos", "--format", "ascii", "--units", "mm", "--side", "front", "-o", t / "f.txt", PCB)
        run("pcb", "export", "pos", "--format", "ascii", "--units", "mm", "--side", "back", "-o", t / "b.txt", PCB)
        run("pcb", "export", "gerbers", "-l", "F.Paste,F.Mask,Edge.Cuts", "--no-x2", "-o", t / "g", PCB)
        zp = HERE / "yd-esp32-s3_cam.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in (("Assembly/PnP_yd-esp32-s3_front.txt", pnp_text(t / "f.txt")), ("Assembly/PnP_yd-esp32-s3_back.txt", pnp_text(t / "b.txt")),
                               ("Gerber/solderpaste_top.gbr", stable(next((t / "g").glob("*F_Paste.gtp")).read_text())),
                               ("Gerber/soldermask_top.gbr", stable(next((t / "g").glob("*F_Mask.gts")).read_text())),
                               ("Gerber/profile.gbr", stable(next((t / "g").glob("*Edge_Cuts.gm1")).read_text()))):
                zi = zipfile.ZipInfo(name, (2026, 11, 10, 0, 0, 0))      # fixed time: the zip is reproducible
                z.writestr(zi, data)
    from polybench.aoi import derive
    import polarity_spec
    derive.EXTRA_MEDIUM_PREFIXES = polarity_spec.MEDIUM_PREFIXES
    res = derive.derive(zp, EX / "assembly.yaml", DESIGN, HERE)
    polarity_spec.finish(HERE, PCB, zp)
    print(json.dumps({k: v for k, v in res.items() if k != "gaps"}))


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
