"""KiCad 10 ECAD link for Polybench (KICAD_PLUGIN_REQUIREMENTS.md).

Read-only toward KiCad: footprints, pads and nets are read, and the PCB editor selection is set and cleared. That is the whole
feature. The transport is KiCad's IPC API (kicad-python / kipy over a local nng socket, optional extra `kicad`).

Layers, bottom to top:
  model      plain data (BoardSnapshot, FootprintInfo, PadInfo), no KiCad import
  settings   socket discovery and KiCad's own settings file (read only)
  guard      the command allow-list in front of every request (KC-33) and the command log
  link       one connection, one worker thread, hard time budget per call (KC-24, KC-27-KC-30)
  reader     kipy objects -> model (the only place that touches kipy attributes)
  select     name resolution (aliases, references, nets, pins) and selection sets, shared by both seams
  bridge     EcadBridge `kicad` (step 1 and the plain highlight)
  client     KicadScannerClient with the FusionClient interface (test highlight, Assembly aid, polarity card)
  fakeclient a protobuf-level fake of KiCad's API server (tests, --simulate)
  pcbfile    read-only parser of a .kicad_pcb (expected values for tests and the acceptance run)
"""
from __future__ import annotations

PROGRAM = "KiCad"
EOF_NOTE = ""
