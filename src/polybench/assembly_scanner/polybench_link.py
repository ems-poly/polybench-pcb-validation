"""Glue between the Polybench batch and the embedded scanner: the BOM goes through the PoC importer, the service is built."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from polybench import scanner as bomcsv
from polybench.assembly_scanner.bom import load_bom
from polybench.assembly_scanner.fusion_client import FusionClient
from polybench.assembly_scanner.main import Settings
from polybench.assembly_scanner.models import normalize_pn
from polybench.assembly_scanner.service import AssemblyService


def load_batch_bom(bom_lines: list[dict], workdir: Path):
    """Polybench BOM lines -> the PoC's Bom, through its own CSV importer (designator ranges, DNF rows, multi-sheet rules).

    Returns (bom, line_map, pn_lines): line_map[line] = the PoC BomLines of that Polybench line; pn_lines[normalised Mouser
    P/N] = Polybench lines that carry it. The CSV lives in the system temp folder, never in the batch folder.
    """
    path = Path(workdir) / "Batch BOM.csv"
    bomcsv.export_bom_csv(bom_lines, path)
    bom = load_bom(str(path))
    by = bom.by_mouser
    line_map, pn_lines = {}, {}
    for ln in bom_lines:
        pn = normalize_pn(bomcsv.mouser_pn(ln) or ln.get("mpn", ""))
        got = tuple(by.get(pn, ()))
        if got:
            line_map[ln["line"]] = got
            pn_lines.setdefault(pn, []).append(ln["line"])
    return bom, line_map, pn_lines


def build_service(sess, on_fire=None, on_change=None) -> AssemblyService:
    """The service for one session. Real camera and real Fusion client unless the environment says otherwise:
      POLYBENCH_SCANNER_FAKE_FRAMES=<folder>   serve recorded frames (file named in <folder>/CURRENT) instead of the camera
      POLYBENCH_FUSION_URL=<url>               Fusion MCP endpoint (a stand-in server in tests)
      a --simulate batch without a URL         in-memory board built from the BOM
    """
    from polybench.assembly_scanner import camera as camera_mod
    from polybench.assembly_scanner import sim
    settings = Settings.load()
    frames = os.environ.get("POLYBENCH_SCANNER_FAKE_FRAMES")
    factory = (lambda i: sim.ImageCamera(i, frames)) if frames else camera_mod.Camera
    cams = (lambda: []) if frames else camera_mod.list_cameras
    url = os.environ.get("POLYBENCH_FUSION_URL")
    lines = sess.engine.state.bom_lines()
    bridge = getattr(sess, "bridge", None)
    if getattr(bridge, "name", "") == "kicad":       # --ecad kicad selects the bridge AND this client (KC-03); Fusion is never contacted
        from polybench.kicad.client import KicadScannerClient
        fusion = KicadScannerClient(link=bridge.link, simulate=bridge.simulate, bridge=bridge)
    elif url:
        fusion = FusionClient(url)
    elif sess.simulate:
        fusion = sim.SimulatedFusionClient([d for b in lines if not b.get("dnf") for d in b["designators"]])
    else:
        fusion = FusionClient(settings.fusion_url)
    svc = AssemblyService(settings, fusion, camera_factory=factory, on_fire=on_fire, on_change=on_change, camera_list=cams)
    svc.program = getattr(fusion, "program", "Fusion")
    work = Path(tempfile.gettempdir()) / ("polybench-scanner-" + re.sub(r"[^A-Za-z0-9_.-]", "_", sess.batch))
    try:
        bom, lmap, pmap = load_batch_bom(lines, work)
        svc.set_bom(bom, f"{sess.batch} BOM ({len(lines)} lines)", lmap, pmap)
    except Exception as e:
        svc.set_bom_error(f"cannot load the batch BOM into the scanner: {e}")
    return svc


def sync_project(sess, svc) -> bool:
    """Use step 1 Project's BOM and board when the session exposes them (sess.project_info() -> dict | None)."""
    getter = getattr(sess, "project_info", None)
    if not callable(getter):
        return False
    try:
        info = getter()
    except Exception:
        return False
    pj = getattr(getattr(getattr(sess, "engine", None), "state", None), "project", None) or {}
    if hasattr(svc, "set_ecad_off"):
        svc.set_ecad_off("skipped" if pj.get("skipped") else None)      # step 1 'Continue without ECAD' (journal: ecad_skipped)
    work = Path(tempfile.gettempdir()) / ("polybench-scanner-" + re.sub(r"[^A-Za-z0-9_.-]", "_", sess.batch))
    return svc.apply_project(info, lambda lines: load_batch_bom(lines, work))
