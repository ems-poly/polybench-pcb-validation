"""Read-only parser of a .kicad_pcb file into the model. It exists for two jobs: the expected values of the live acceptance
run (reference list, pad counts per net, pad positions), and a synthetic-from-file fake in the tests. The file is opened for
reading only. Nothing from a client project is copied into the repository: the path is given at run time.

KiCad's s-expression file: footprints carry `(at x y [angle])`, `(layer ...)`, `(property "Reference" ...)`, `(attr ...)` and pads
`(pad "N" smd rect (at x y [rot]) (size w h) (net "NAME"))`. Pad positions are footprint-local; the board position is the footprint
position plus the pad offset rotated by the footprint angle (KiCad: y down, positive angle counter-clockwise on screen).
"""
from __future__ import annotations

import math
import re
import uuid
from pathlib import Path

from .model import BoardSnapshot, DocumentInfo, FootprintInfo, PadInfo

_TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse_sexpr(text: str):
    stack: list[list] = [[]]
    for m in _TOK.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            node = stack.pop()
            stack[-1].append(node)
        elif t.startswith('"'):
            stack[-1].append(("s", t[1:-1].replace('\\"', '"').replace("\\\\", "\\")))
        else:
            stack[-1].append(t)
    return stack[0]


def _name(x) -> str:
    return x[1] if isinstance(x, tuple) else str(x)


def _find(node, key):
    return next((c for c in node if isinstance(c, list) and c and c[0] == key), None)


def _all(node, key):
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def _num(x, d=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def rot(x: float, y: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return (x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a))


def _collect_nets(node, out: set):
    if isinstance(node, list):
        if len(node) == 2 and node[0] == "net" and isinstance(node[1], tuple):
            out.add(node[1][1])
        for c in node:
            _collect_nets(c, out)


def load(path: str | Path) -> BoardSnapshot:
    p = Path(path)
    tree = parse_sexpr(p.read_text(encoding="utf-8"))
    root = tree[0]
    fps = []
    for fp in _all(root, "footprint"):
        at = _find(fp, "at") or ["at", "0", "0"]
        fx, fy = _num(at[1]), _num(at[2])
        fa = _num(at[3]) if len(at) > 3 else 0.0
        layer = _name(_find(fp, "layer")[1]) if _find(fp, "layer") else "F.Cu"
        ref = value = ""
        for pr in _all(fp, "property"):
            if _name(pr[1]) == "Reference":
                ref = _name(pr[2])
            elif _name(pr[1]) == "Value":
                value = _name(pr[2])
        attr = _find(fp, "attr")
        flags = {str(a) for a in (attr[1:] if attr else [])}
        info = FootprintInfo(ref=ref, kiid=_name(_find(fp, "uuid")[1]) if _find(fp, "uuid") else str(uuid.uuid5(uuid.NAMESPACE_URL, "fp:" + ref)),
                             x=fx, y=fy, angle=fa, layer=layer, value=value, lib_id=_name(fp[1]),
                             exclude_from_bom="exclude_from_bom" in flags, not_in_schematic="board_only" in flags)
        for pad in _all(fp, "pad"):
            pat = _find(pad, "at") or ["at", "0", "0"]
            dx, dy = rot(_num(pat[1]), _num(pat[2]), fa)
            sz = _find(pad, "size") or ["size", "0", "0"]
            net = _find(pad, "net")
            kind = {"smd": "smd", "thru_hole": "pth", "np_thru_hole": "npth"}.get(str(pad[2]), "")
            info.pads.append(PadInfo(number=_name(pad[1]), net=_name(net[1]) if net else "",
                                     kiid=_name(_find(pad, "uuid")[1]) if _find(pad, "uuid") else str(uuid.uuid5(uuid.NAMESPACE_URL, "pad:" + ref + _name(pad[1]))),
                                     x=fx + dx, y=fy + dy, w=_num(sz[1]), h=_num(sz[2]), angle=_num(pat[3]) if len(pat) > 3 else 0.0,
                                     shape=str(pad[3]) if len(pad) > 3 else "", kind=kind, parent=ref))
        fps.append(info)
    nets: set[str] = set()
    _collect_nets(root, nets)
    doc = DocumentInfo(p.name, p.stem, str(p.parent))
    return BoardSnapshot(doc, fps, sorted(nets - {""}))
