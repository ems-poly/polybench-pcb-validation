"""A fake of KiCad's API server at the level of kipy's client: `send(command, response_type)` answers with real protobuf
messages, so kipy's own `KiCad` and `Board` classes, and this package's reader, run unchanged on top of it (KC-37).

It records every command it receives. Behaviours for the off-nominal tests: `busy` (AS_BUSY), `no_board`, `delay` (seconds per
request), `hang` (a request that never answers: a receive timeout after `delay`), `version`. It holds a selection that
`operator_select()` can change like a hand selection. It never touches a socket or a file.
"""
from __future__ import annotations

import threading
import time

from .model import BoardSnapshot, FootprintInfo


class FakeKiCadClient:
    def __init__(self, snap: BoardSnapshot, version=(10, 0, 7, "10.0.7"), with_schematic: bool = False):
        self.snap = snap
        self.version = version
        self.with_schematic = with_schematic
        self.sent: list = []                 # command messages, in order
        self.actions: list[str] = []         # RunAction names received
        self.known_actions = {"common.Control.zoomFitSelection", "common.Control.centerSelection", "pcbnew.EditorControl.highlightNetSelection",
                              "pcbnew.EditorControl.clearHighlight", "pcbnew.InteractiveSelection.SelectOnSchematic"}
        self.selection: list[str] = []       # KIIDs
        self.busy = False
        self.no_board = False
        self.delay = 0.0
        self.hang = False
        self.modified = False                # nothing here ever sets it (TBR-9 stand-in)
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._by_id = {}
        for f in snap.footprints:
            self._by_id[f.kiid] = ("footprint", f, None)
            for p in f.pads:
                self._by_id[p.kiid] = ("pad", f, p)

    @property
    def names(self) -> list[str]:
        return [type(c).__name__ for c in self.sent]

    def operator_select(self, ids):
        self.selection = list(ids)

    # ---- protobuf builders
    def _fp_proto(self, f: FootprintInfo):
        from google.protobuf.any_pb2 import Any
        from kipy.proto.board import board_types_pb2 as bt
        m = bt.FootprintInstance()
        m.id.value = f.kiid
        m.position.x_nm, m.position.y_nm = round(f.x * 1e6), round(f.y * 1e6)
        m.orientation.value_degrees = f.angle
        m.layer = bt.BoardLayer.BL_B_Cu if f.bottom else bt.BoardLayer.BL_F_Cu
        m.reference_field.text.text.text = f.ref
        m.value_field.text.text.text = f.value
        m.attributes.exclude_from_bill_of_materials = f.exclude_from_bom
        m.attributes.not_in_schematic = f.not_in_schematic
        if ":" in f.lib_id:
            m.definition.id.library_nickname, m.definition.id.entry_name = f.lib_id.split(":", 1)
        for p in f.pads:
            a = Any()
            a.Pack(self._pad_proto(p))
            m.definition.items.append(a)
        return m

    def _pad_proto(self, p):
        from kipy.proto.board import board_types_pb2 as bt
        m = bt.Pad()
        m.id.value = p.kiid
        m.number = p.number
        m.net.name = p.net
        m.position.x_nm, m.position.y_nm = round(p.x * 1e6), round(p.y * 1e6)
        m.type = {"smd": bt.PadType.PT_SMD, "pth": bt.PadType.PT_PTH, "npth": bt.PadType.PT_NPTH}.get(p.kind, bt.PadType.PT_SMD)
        lay = m.pad_stack.copper_layers.add()
        lay.layer = bt.BoardLayer.BL_F_Cu
        lay.shape = bt.PadStackShape.PSS_RECTANGLE
        lay.size.x_nm, lay.size.y_nm = round(p.w * 1e6), round(p.h * 1e6)
        m.pad_stack.angle.value_degrees = p.angle
        return m

    def _sel_items(self, resp):
        from google.protobuf.any_pb2 import Any
        for i in self.selection:
            kind, f, p = self._by_id[i]
            a = Any()
            a.Pack(self._fp_proto(f) if kind == "footprint" else self._pad_proto(p))
            resp.items.append(a)

    # ---- the server
    def send(self, command, response_type):
        from google.protobuf.any_pb2 import Any
        from kipy.errors import ApiError, ConnectionError
        from kipy.proto.common import ApiStatusCode
        from kipy.proto.common.types import base_types_pb2 as bt0
        name = type(command).__name__
        with self._lock:
            self.sent.append(command)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.hang:
                raise ConnectionError("Error receiving reply from KiCad: Timed out")
            if self.busy:
                raise ApiError("KiCad is busy", code=ApiStatusCode.AS_BUSY)
            r = response_type()
            if name == "GetVersion":
                r.version.major, r.version.minor, r.version.patch, r.version.full_version = self.version
            elif name == "GetOpenDocuments":
                d = self.snap.document
                if command.type == bt0.DocumentType.DOCTYPE_PCB and not self.no_board:
                    s = r.documents.add()
                    s.type, s.board_filename = bt0.DocumentType.DOCTYPE_PCB, d.board_filename
                    s.project.name, s.project.path = d.project_name, d.project_path
                elif command.type == bt0.DocumentType.DOCTYPE_SCHEMATIC and self.with_schematic:
                    s = r.documents.add()
                    s.type, s.board_filename = bt0.DocumentType.DOCTYPE_SCHEMATIC, d.board_filename.replace(".kicad_pcb", ".kicad_sch")
                elif command.type not in (bt0.DocumentType.DOCTYPE_PCB, bt0.DocumentType.DOCTYPE_SCHEMATIC):
                    raise ApiError("no handler available for request of type GetOpenDocuments", code=ApiStatusCode.AS_UNHANDLED)
            elif name == "GetItems":
                from kipy.proto.common.types import KiCadObjectType as K
                for t in command.types:
                    if t == K.KOT_PCB_FOOTPRINT:
                        for f in self.snap.footprints:
                            a = Any()
                            a.Pack(self._fp_proto(f))
                            r.items.append(a)
                    elif t == K.KOT_PCB_PAD:
                        for f in self.snap.footprints:
                            for p in f.pads:
                                a = Any()
                                a.Pack(self._pad_proto(p))
                                r.items.append(a)
                r.status = 1
            elif name == "GetNets":
                for n in self.snap.nets:
                    r.nets.add().name = n
            elif name == "GetSelection":
                self._sel_items(r)
            elif name == "AddToSelection":
                for k in command.items:
                    if k.value in self._by_id and k.value not in self.selection:
                        self.selection.append(k.value)
                # KiCad (measured live, 17 Oct 2026): the pads of a selected footprint are not kept in the selection
                fps = {i for i in self.selection if self._by_id[i][0] == "footprint"}
                self.selection = [i for i in self.selection if self._by_id[i][0] == "footprint" or self._by_id[i][1].kiid not in fps]
                self._sel_items(r)
            elif name == "RemoveFromSelection":
                self.selection = [i for i in self.selection if i not in {k.value for k in command.items}]
                self._sel_items(r)
            elif name == "ClearSelection":
                self.selection = []
            elif name == "RunAction":
                self.actions.append(command.action)
                r.status = 1 if command.action in self.known_actions else 2          # RAS_OK / RAS_INVALID
            elif name == "Ping":
                pass
            else:
                raise ApiError(f"fake server: unhandled {name}", code=ApiStatusCode.AS_UNHANDLED)
            return r
        finally:
            with self._lock:
                self.active -= 1
