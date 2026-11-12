"""kipy objects -> model. The only module that reads attributes of kipy's classes, so a change in kipy touches one file.

Every attribute read is guarded: a field kipy or KiCad does not provide becomes a default, never an exception that hides the
other footprints. Units: kipy gives nanometres; the model is millimetres.
"""
from __future__ import annotations

from .model import BoardSnapshot, DocumentInfo, FootprintInfo, PadInfo, ShapeInfo

NM = 1e-6


def _try(fn, default=None):
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _mm(v) -> float:
    return float(v) * NM


def _layer_name(kipy_layer) -> str:
    """BoardLayer value -> 'F.Cu' / 'B.Cu' / 'F.SilkS' ... (KiCad's own names, from the enum name BL_F_Cu)."""
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer
        name = BoardLayer.Name(kipy_layer)
    except Exception:
        return str(kipy_layer)
    name = name[3:] if name.startswith("BL_") else name
    return {"F_Cu": "F.Cu", "B_Cu": "B.Cu", "F_SilkS": "F.SilkS", "B_SilkS": "B.SilkS", "F_Fab": "F.Fab", "B_Fab": "B.Fab",
            "F_CrtYd": "F.CrtYd", "B_CrtYd": "B.CrtYd"}.get(name, name.replace("_", "."))


def document_info(doc) -> DocumentInfo:
    proj = _try(lambda: doc.project)
    return DocumentInfo(board_filename=str(_try(lambda: doc.board_filename, "")),
                        project_name=str(_try(lambda: proj.name, "")), project_path=str(_try(lambda: proj.path, "")))


def pad_info(p, parent_ref: str = "") -> PadInfo:
    layers = _try(lambda: list(p.padstack.copper_layers), [])
    first = layers[0] if layers else None
    size = _try(lambda: first.size)
    shape = _try(lambda: first.shape)
    try:
        from kipy.proto.board.board_types_pb2 import PadStackShape, PadType
        shape_s = PadStackShape.Name(shape).replace("PSS_", "").lower() if shape is not None else ""
        kind = {"PT_SMD": "smd", "PT_PTH": "pth", "PT_NPTH": "npth"}.get(PadType.Name(_try(lambda: p.pad_type, 0)), "")
    except Exception:
        shape_s, kind = "", ""
    parent = _try(lambda: p.parent)
    return PadInfo(number=str(_try(lambda: p.number, "")), net=str(_try(lambda: p.net.name, "")),
                   kiid=str(_try(lambda: p.id.value, "")),
                   x=_mm(_try(lambda: p.position.x, 0)), y=_mm(_try(lambda: p.position.y, 0)),
                   w=_mm(_try(lambda: size.x, 0)), h=_mm(_try(lambda: size.y, 0)),
                   angle=float(_try(lambda: p.padstack.angle.degrees, 0.0)), shape=shape_s, kind=kind,
                   parent=parent_ref)


def footprint_info(fp, with_shapes: bool = False) -> FootprintInfo:
    ref = str(_try(lambda: fp.reference_field.text.value, ""))
    lib = _try(lambda: fp.definition.id)
    lib_id = ""
    if lib is not None:
        lib_id = f"{_try(lambda: lib.library_nickname, '')}:{_try(lambda: lib.entry_name, '')}".strip(":")
    info = FootprintInfo(
        ref=ref, kiid=str(_try(lambda: fp.id.value, "")),
        x=_mm(_try(lambda: fp.position.x, 0)), y=_mm(_try(lambda: fp.position.y, 0)),
        angle=float(_try(lambda: fp.orientation.degrees, 0.0)),
        layer=_layer_name(_try(lambda: fp.layer, 0)) or "F.Cu",
        value=str(_try(lambda: fp.value_field.text.value, "")), lib_id=lib_id,
        exclude_from_bom=bool(_try(lambda: fp.attributes.exclude_from_bill_of_materials, False)),
        not_in_schematic=bool(_try(lambda: fp.attributes.not_in_schematic, False)))
    info.pads = [pad_info(p, ref) for p in _try(lambda: list(fp.definition.pads), [])]
    if with_shapes:
        info.shapes = shapes_info(fp)
    return info


def shapes_info(fp) -> list[ShapeInfo]:
    """Silkscreen and fab graphics of the footprint (TBR-10 spike). Segments, arcs, circles and rectangles; others are skipped."""
    from kipy.board_types import BoardArc, BoardCircle, BoardRectangle, BoardSegment, to_concrete_board_shape
    out: list[ShapeInfo] = []
    for sh in _try(lambda: list(fp.definition.shapes), []):
        layer = _layer_name(_try(lambda: sh.layer, 0))
        if "SilkS" not in layer and "Fab" not in layer:
            continue
        s = _try(lambda: to_concrete_board_shape(sh), sh)
        width = _mm(_try(lambda: s.attributes.stroke.width, 150000))

        def pt(v):
            return (_mm(v.x), _mm(v.y))
        if isinstance(s, BoardSegment):
            out.append(ShapeInfo("line", layer, [pt(s.start), pt(s.end)], width))
        elif isinstance(s, BoardArc):
            out.append(ShapeInfo("arc", layer, [pt(s.start), pt(s.mid), pt(s.end)], width))
        elif isinstance(s, BoardCircle):
            out.append(ShapeInfo("circle", layer, [pt(s.center), pt(s.radius_point)], width))
        elif isinstance(s, BoardRectangle):
            out.append(ShapeInfo("rect", layer, [pt(s.top_left), pt(s.bottom_right)], width))
    return out


def read_snapshot(kicad, board, doc_info: DocumentInfo | None = None, with_shapes: bool = False) -> BoardSnapshot:
    """Footprints (with their pads) and net names of the open board. Three read requests: GetItems(footprint), GetNets.
    Pads come with their footprint; Pad.parent (KiCad 10.0.6+) is not needed because the footprint carries them."""
    fps = [footprint_info(f, with_shapes) for f in board.get_footprints()]
    nets = sorted({str(_try(lambda n=n: n.name, "")) for n in board.get_nets()} - {""})
    return BoardSnapshot(document=doc_info or document_info(board.document), footprints=fps, nets=nets)


def read_selection(board) -> list[tuple[str, str]]:
    """[(kind, kiid)] of the PCB editor selection: kind is 'footprint', 'pad' or the class name of anything else."""
    out = []
    for it in board.get_selection():
        name = type(it).__name__
        kind = {"FootprintInstance": "footprint", "Pad": "pad"}.get(name, name)
        out.append((kind, str(_try(lambda it=it: it.id.value, ""))))
    return out
