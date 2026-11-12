"""Project page second fix round walk in WebKit (N4 batch path on its own line, N7 design in the New hint, N8 missing recent).

    PB_E2E_PORT=9020 .venv/bin/python tests/e2e/project_page_fix2_walk.py [shots_dir]

DEMO protocol only, in a scratch folder; --simulate; POLYBENCH_CONFIG_DIR in scratch.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright  # noqa: E402

import project_page_walk as W  # noqa: E402


def main():
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/private/tmp/claude-502/pbshots-fix2")
    shots.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PB_E2E_PORT", "9020"))
    s = W.make_scratch()
    other = s / "other"
    shutil.copytree(W.DEMO, other)
    f = other / "demo.pcbv.yaml"
    f.write_text(f.read_text().replace("name: DEMO-1", "name: OTHER-DESIGN", 1))
    ghost = s / "ghost"
    ghost.mkdir()
    (s / "cfg" / "recent.json").write_text(json.dumps({"schema": 1, "projects": [
        {"dir": str(ghost), "name": "Ghost project", "batch": "G1", "opened": "2026-10-01T10:00:00"}]}))
    shutil.rmtree(ghost)
    srv = W.Server(port, s, s / "demo" / "demo.pcbv.yaml", s / "proj", "B1")
    ck = W.check
    try:
        with sync_playwright() as p:
            br = p.webkit.launch()
            pg = br.new_context(viewport={"width": 1500, "height": 900}).new_page()
            P = W.Page(pg, shots)
            pg.goto(srv.base)
            pg.wait_for_selector("#timeline")
            pg.click("#timeline [data-id=project]")
            pg.wait_for_selector("#pjc-folder")
            pg.check("#adv")             # the batch folder row is an Advanced detail
            pg.wait_for_timeout(300)
            pg.wait_for_timeout(600)
            for w in (1500, 1100):
                pg.set_viewport_size({"width": w, "height": 900})
                pg.wait_for_timeout(250)
                row = pg.evaluate("""() => { const dd = [...document.querySelectorAll('#pjc-folder dd.pjpath')].find(d => d.innerText.includes('batch '));
                    const sub = dd.querySelector('.pjsub1'), p = dd.querySelector('.pth.mid');
                    const pd = p.querySelector('.pthd'), pn = p.querySelector('.pthn');
                    return {subTop: sub.getBoundingClientRect().top, pathBottom: p.getBoundingClientRect().bottom,
                            dirClipped: pd.scrollWidth > pd.clientWidth + 1, nameClipped: pn.scrollWidth > pn.clientWidth + 1, path: p.title}; }""")
                ck(f"N4 batch name is on its own line below the path at {w}", row["subTop"] >= row["pathBottom"] - 1, str(row))
                ck(f"N4 the batch file name is whole at {w}", not row["nameClipped"], str(row))
                P.show("#pjc-folder")
            P.snap("n4_batch_path_own_line", "#pjc-folder")
            pg.click("[data-act=pjnew]")
            pg.wait_for_selector("#pjn-protohint")
            pg.fill("#pjn-proto", str(other / "demo.pcbv.yaml"))
            pg.wait_for_function("document.querySelector('#pjn-protohint').innerText.includes('OTHER-DESIGN')", timeout=8000)
            t = pg.inner_text("#pjn-protohint")
            ck("N7 the hint names the typed protocol's design and the open one", "OTHER-DESIGN" in t and "DEMO-1" in t, t)
            P.snap("n7_new_protocol_design_hint", "#pjnewform")
            pg.click("[data-act=pjformclose]")
            pg.click("[data-act=pjopen]")
            pg.wait_for_selector("#pjopenform")
            t = pg.inner_text("#pjopenform")
            ck("N8 a recent project with no folder is marked missing and has Remove from list", "missing" in t and pg.locator("[data-act=pjrecrm]").count() == 1, t[:300])
            P.snap("n8_recent_missing", "#pjopenform")
            pg.click("[data-act=pjrecrm]")
            pg.wait_for_timeout(500)
            ck("N8 Remove from list drops it", pg.locator("[data-act=pjrecrm]").count() == 0)
            br.close()
    finally:
        srv.stop()
    bad = [n for n, ok in W.checks if not ok]
    print(f"\n{len(W.checks) - len(bad)}/{len(W.checks)} checks passed; screenshots in {shots}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
