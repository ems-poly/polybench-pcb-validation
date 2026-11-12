"""A synthetic demo board for the simulator and the tests. Invented geometry; no client data. It has the parts the
requirements name (R9, C12, D1, IC1 with numbered pads, test pads) and the nets VIN, 3V3_A, 5V, GND, plus a generic two-pad
part for every other designator asked for."""
from __future__ import annotations

import uuid

from .model import BoardSnapshot, DocumentInfo, FootprintInfo, PadInfo, ShapeInfo

DOC = DocumentInfo("DEMO-1 (simulated).kicad_pcb", "DEMO-1 (simulated)", "/simulated")


def _id(s: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "polybench-sim." + s))


def _two_pad(ref, x, y, a=0.0, n1="N1", n2="GND", layer="F.Cu"):
    f = FootprintInfo(ref, _id(ref), x, y, a, layer, "", "sim:RESC1608X60")
    f.pads = [PadInfo("1", n1, _id(ref + ".1"), x - 0.8, y, 0.8, 1.0, a, "rectangle", "smd", ref),
              PadInfo("2", n2, _id(ref + ".2"), x + 0.8, y, 0.8, 1.0, a, "rectangle", "smd", ref)]
    f.shapes = [ShapeInfo("line", "F.SilkS", [(x - 0.85, y - 0.8), (x + 0.85, y - 0.8)], 0.127)]
    return f


def demo_board(designators=()) -> BoardSnapshot:
    fps = [_two_pad("R9", 157.56, 98.30, 180, "VREF", "3V3_A"), _two_pad("C12", 148.60, 110.79, 90, "3V3_A", "GND")]
    d1 = FootprintInfo("D1", _id("D1"), 146.50, 102.86, 0.0, "F.Cu", "", "sim:LED_RGB")
    d1.pads = [PadInfo(str(i + 1), n, _id(f"D1.{i + 1}"), 146.17 + (0.65 if i in (2, 3) else 0), 102.54 + (0.65 if i in (1, 2) else 0),
                       0.4, 0.4, 0.0, "rectangle", "smd", "D1") for i, n in enumerate(["N$1", "LED_G", "LED_B", "LED_R"])]
    d1.shapes = [ShapeInfo("circle", "F.SilkS", [(145.9, 102.3), (146.0, 102.3)], 0.12)]
    ic = FootprintInfo("IC1", _id("IC1"), 151.75, 100.49, 180.0, "F.Cu", "", "sim:TSSOP16")
    nets = ["ADC_RST", "GND", "GND", "TEMP_SENS", "3V3_A", "VIN", "VREF", "SENSE_P", "SENSE_N", "GND", "5V", "3V3_A", "SWCLK", "DOUT", "CS", "VIN"]
    ic.pads = [PadInfo(str(i + 1), nets[i], _id(f"IC1.{i + 1}"), 153.2 if i < 8 else 150.3, 101.24 - 0.5 * (i % 8), 0.9, 0.3, 0.0, "rectangle", "smd", "IC1") for i in range(16)]
    tp = [FootprintInfo(r, _id(r), 150.9 + i * 3, 107.7, 0.0, "B.Cu", "", "sim:TP") for i, r in enumerate(["TP1", "TP2"])]
    for f, n in zip(tp, ["SWCLK", "5V"]):
        f.pads = [PadInfo("TP", n, _id(f.ref + ".TP"), f.x, f.y, 1.3, 1.3, 0.0, "roundrect", "smd", f.ref)]
    fps += [d1, ic] + tp
    have = {f.ref for f in fps}
    for i, d in enumerate(sorted({str(x) for x in designators} - have)):
        fps.append(_two_pad(d, 130 + (i % 20) * 3.0, 90 + (i // 20) * 4.0, 0.0, f"N{i}", "GND"))
    nets_all = sorted({p.net for f in fps for p in f.pads if p.net})
    return BoardSnapshot(DocumentInfo(DOC.board_filename, DOC.project_name, DOC.project_path), fps, nets_all)
