"""UI-1 and UI-4 (docs/CONTRACTS.md).

UI-4 is checked by inspecting the source: no pop-ups / modal dialogs / message boxes anywhere
in the embedded scanner package. There is no Qt left in it (the page shows every status as text); the check stays so a dialog
cannot creep back in.
UI-1 needs the HUD text the UI shows for a fired resolution. The function name is not in the
contract yet; `hud_text()` looks for one of HUD_CANDIDATES in app.state / app.ui.
"""
from __future__ import annotations

import re


from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "polybench" / "assembly_scanner"
from asc_pipeline import BAG_A, BAG_A_DESIG, BAG_TUNING, BAG_UNKNOWN, Pipeline, SpyFusion, run_stream

FORBIDDEN = re.compile(
    r"\b(Q(?!File)\w*Dialog|QMessageBox|QErrorMessage|QInputDialog|QProgressDialog)\b"
    r"|display (dialog|alert)"            # osascript pop-ups
    r"|\bNSAlert\b|\btkinter\.messagebox\b")


def test_ui4_no_popups_in_app_source():
    hits = []
    for py in sorted(PKG.rglob("*.py")):
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            m = FORBIDDEN.search(code)
            if m:
                hits.append(f"{py.relative_to(PKG)}:{n}: {m.group(0)}")
    assert not hits, "UI-4: pop-up/dialog use (only QFileDialog is allowed):\n" + "\n".join(hits)


def test_ui4_inspection_actually_scans_the_ui():
    """Guard against the test above passing vacuously before ui.py exists."""
    assert (PKG / "service.py").exists() and len(list(PKG.rglob("*.py"))) >= 10


HUD_CANDIDATES = ("hud_text", "hud_lines", "status_text", "format_status", "render_hud",
                  "hud_for")


def hud_text(resolution) -> str:
    """Per contract (docs/CONTRACTS.md, hud.py): hud_lines -> [(text, level)]."""
    from polybench.assembly_scanner.hud import hud_lines
    return "\n".join(text for text, _level in hud_lines(resolution))


def _fired(bom_a, bag, spy):
    pipe = Pipeline(bom_a, spy)
    run_stream(pipe, [(BAG_A, 1.0), (None, 1.0), (bag, 2.0)])
    return pipe, [r for r in pipe.fired if r is not None][-1]


def test_ui1_no_placement_hud_and_no_fusion_call(bom_a):
    spy = SpyFusion()
    pipe, res = _fired(bom_a, BAG_TUNING, spy)
    assert res.status.value == "MATCH_NO_PLACEMENT"
    assert spy.highlights == [BAG_A_DESIG] and spy.current == BAG_A_DESIG
    text = hud_text(res)
    assert "NO PLACEMENT FOUND" in text.upper()
    assert "920-DEMO-R560" in text


def test_not_in_bom_hud_text(bom_a):
    spy = SpyFusion()
    pipe, res = _fired(bom_a, BAG_UNKNOWN, spy)
    text = hud_text(res)
    assert "NOT IN CURRENT BOM" in text.upper() and "999-DEMO-NOTINBOM" in text
    assert spy.current == BAG_A_DESIG


def test_match_hud_text_lists_designators(bom_a):
    spy = SpyFusion()
    pipe, res = _fired(bom_a, BAG_A, spy)
    text = hud_text(pipe.fired[0])
    assert "901-DEMO-R2K2" in text and "R4" in text and "R5" in text
