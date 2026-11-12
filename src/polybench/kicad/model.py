"""Plain data read from KiCad (or from a .kicad_pcb file, or from the synthetic board). Millimetres, board frame of KiCad
(x right, y DOWN), degrees counter-clockwise as KiCad shows them. No KiCad import here."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PadInfo:
    number: str
    net: str = ""
    kiid: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    angle: float = 0.0
    shape: str = ""
    kind: str = ""            # smd | pth | npth | ""
    parent: str = ""          # reference of the footprint this pad belongs to


@dataclass
class ShapeInfo:
    """A graphic of the footprint on a silkscreen or fab layer: line, arc, circle, rect (board mm)."""
    kind: str
    layer: str
    pts: list[tuple[float, float]] = field(default_factory=list)   # line: 2 points. arc: start, mid, end. circle: centre, point on rim. rect: 2 corners
    width: float = 0.15


@dataclass
class FootprintInfo:
    ref: str
    kiid: str = ""
    x: float = 0.0
    y: float = 0.0
    angle: float = 0.0
    layer: str = "F.Cu"       # F.Cu | B.Cu
    value: str = ""
    lib_id: str = ""
    exclude_from_bom: bool = False
    not_in_schematic: bool = False
    pads: list[PadInfo] = field(default_factory=list)
    shapes: list[ShapeInfo] = field(default_factory=list)

    @property
    def bottom(self) -> bool:
        return self.layer.startswith("B")


@dataclass
class DocumentInfo:
    board_filename: str = ""
    project_name: str = ""
    project_path: str = ""

    @property
    def document_id(self) -> str:
        """KC-08: absolute project path plus board file name."""
        return f"{self.project_path.rstrip('/')}/{self.board_filename}" if self.project_path else self.board_filename


@dataclass
class BoardSnapshot:
    document: DocumentInfo
    footprints: list[FootprintInfo] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)

    def by_ref(self) -> dict[str, FootprintInfo]:
        return {f.ref.upper(): f for f in self.footprints if f.ref}

    def net_pads(self) -> dict[str, list[tuple[FootprintInfo, PadInfo]]]:
        out: dict[str, list] = {}
        for f in self.footprints:
            for p in f.pads:
                if p.net:
                    out.setdefault(p.net, []).append((f, p))
        return out
