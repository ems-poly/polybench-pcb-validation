"""UI review lane PJ walk in WebKit: ECAD setup as one box (UIR-14), BOM standard/advanced (UIR-15), AI card collapsed/open (UIR-16).

    PB_E2E_PORT=8907 .venv/bin/python tests/e2e/uir_pj_walk.py [shots_dir]

DEMO protocol in a scratch folder; --simulate; POLYBENCH_CONFIG_DIR in scratch; the link is made unreachable with the simulator's
POLYBENCH_SIM_ECAD_DOWN flag file. HOST-ONLY.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright  # noqa: E402

import project_page_walk as W  # noqa: E402

ck = W.check


def main():
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/private/tmp/claude-502/pbshots-pj")
    shots.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PB_E2E_PORT", "8907"))
    s = W.make_scratch()
    flag = s / "ecad_down"
    flag.write_text("down")
    os.environ["POLYBENCH_SIM_ECAD_DOWN"] = str(flag)
    srv = W.Server(port, s, s / "demo" / "demo.pcbv.yaml", s / "proj", "B1")
    errors = []
    try:
        with sync_playwright() as p:
            br = p.webkit.launch()
            pg = br.new_context(viewport={"width": 1500, "height": 900}).new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            P = W.Page(pg, shots)
            pg.goto(srv.base)
            pg.wait_for_selector("#pjc-folder")
            pg.wait_for_selector("#pjc-link .vd")
            order = pg.evaluate("[...document.querySelectorAll('.pjcard')].map(e => e.dataset.card).join(',')")
            ck("R14-1 order of the cards: folder, ecad, bom, ai", order == "folder,ecad,bom,ai", order)
            ck("R14-1 box order: platform dropdown, link, document",
               pg.evaluate("[...document.querySelectorAll('#pjc-ecad .pjsec')].map(e => e.id).join(',')") == "pjc-plat,pjc-link,pjc-doc")
            ck("R14-1 platform is a dropdown with Fusion selected", pg.eval_on_selector("#pjplatsel", "e => e.tagName + ':' + e.selectedOptions[0].textContent") == "SELECT:Fusion")
            ck("R14-1 Fusion unreachable", "NOT REACHABLE" in pg.inner_text("#pjc-link"))
            ck("R14-2 help collapsed by default (no steps, no 'Fusion is not reachable' callout)",
               pg.locator("#pjc-ecad .steps").count() == 0 and pg.locator("#pjc-link .pjhelp [aria-expanded=false]").count() == 1)
            ck("R14 Continue without ECAD still offered", pg.locator("[data-act=pjskip]").count() == 1)
            P.snap("a_fusion_unreachable_help_collapsed", "#pjc-ecad")
            pg.click("[data-act=pjhelp]")
            pg.wait_for_selector("#pjc-ecad .steps")
            ck("R14-2 Help connecting opens on click", pg.locator("#pjc-ecad .steps li").count() >= 5)
            P.snap("b_fusion_unreachable_help_open", "#pjc-ecad")
            pg.click("[data-act=pjhelp]")
            ck("R14-2 and closes again", pg.locator("#pjc-ecad .steps").count() == 0)

            flag.unlink()
            pg.click("#pjc-link [data-act=pjcheck]")
            pg.wait_for_selector("#pjc-link .vd.pass")
            ck("R14-1 Fusion connected (simulator)", "CONNECTED" in pg.inner_text("#pjc-link") and "SIMULATED" in pg.inner_text("#pjc-link"))
            ck("R14-2 help still collapsed when connected", pg.locator("#pjc-ecad .steps").count() == 0)
            pg.wait_for_selector("#pjc-doc [data-act=pjuse]")
            P.snap("c_fusion_connected", "#pjc-ecad")
            left = lambda: pg.inner_text("#left")
            ck("R14-4 left checklist: one 'ECAD setup' item, no separate platform/document ticks", "ECAD setup" in left() and "ECAD platform" not in left() and "Document selected" not in left(), left())
            ck("R14-4 left checklist: ECAD setup is open (box says TO DO)", pg.locator("#left .trow .ck-open >> xpath=../.. >> text=ECAD setup").count() + pg.locator("#left .trow:has-text('ECAD setup') .ck-open").count() >= 1 and pg.locator("#pjc-ecad .pjst.todo").count() == 1)
            P.snap("m_checklist_todo", "#left")
            pg.screenshot(path=str(shots / "m_checklist_todo_page.png"))
            pg.click("#pjc-doc [data-act=pjuse]")
            pg.wait_for_function("S && S.project && S.project.select !== null")
            pg.wait_for_timeout(300)
            ck("R14-1 document in the same box, summary after Use", "fingerprint" in pg.inner_text("#pjc-doc") and pg.locator("#pjc-ecad .pjst.done").count() == 1)
            ck("R14-4 left checklist: ECAD setup ticked once the box is DONE", pg.locator("#left .trow:has-text('ECAD setup') .ck-done").count() == 1)
            pg.screenshot(path=str(shots / "n_checklist_done_page.png"))
            P.snap("d_fusion_document_selected", "#pjc-ecad")

            # UIR-15: BOM card, Advanced off then on
            ck("R15-1 Advanced is off", not pg.is_checked("#adv"))
            pg.wait_for_selector("#pjc-bom #bomline")
            t = pg.inner_text("#pjc-bom")
            ck("R15-1 one summary line 'N of N fitted parts match the board, M DNF'", "fitted parts match the board" in t and "DNF" in t, t)
            ck("R15-1 no file table, no Matched/DNF rows, no DNF chips, no Replace/Remove; the problem (missing in BOM, DEMO) is shown",
               pg.locator("#pjc-bom .facts, #pjc-bom .ccrow.ok, #pjc-bom .dnfc, #pjc-bom [data-act=bomreplace]").count() == 0
               and pg.locator("#pjc-bom .ccrow.warn").count() == 1, t)
            ck("R15-1 Accept the cross-check is there", pg.locator("#pjc-bom [data-act=pjaccept]").count() == 1)
            P.snap("e_bom_standard", "#pjc-bom")
            pg.check("#adv")
            pg.wait_for_selector("#pjc-bom .facts")
            ck("R15 Advanced shows the file table, the cross-check rows, DNF", pg.locator("#pjc-bom .facts").count() == 1 and pg.locator("#pjc-bom .ccrow").count() >= 3
               and pg.locator("#pjc-bom [data-act=bomreplace]").count() == 1)
            P.snap("f_bom_advanced", "#pjc-bom")
            pg.uncheck("#adv")

            # UIR-16: AI card
            pg.wait_for_selector("#aicard")
            ck("R16-1 AI card collapsed by default to one line with the permission mode", pg.locator("#aicard .pjb").count() == 0 and "Permissions: propose" in pg.inner_text("#aicard"), pg.inner_text("#aicard"))
            ck("R16-1 collapsed card is short", pg.eval_on_selector("#aicard", "e => e.getBoundingClientRect().height") < 90)
            P.snap("g_ai_collapsed", "#aicard")
            pg.click("#aicard [data-act=pjtoggle]")
            pg.wait_for_selector("#aicard .pjb")
            ck("R16-2 opened: three parts", pg.evaluate("[...document.querySelectorAll('#aicard .aipart')].map(e => e.id).join(',')") == "ai-conn,ai-setup,ai-activity")
            ck("R16 no on/off selector (deferred)", pg.locator("#aicard input[type=checkbox], #aicard [role=switch], #aicard select").count() == 0)
            P.snap("h_ai_open", "#aicard")
            pg.click("#aicard [data-act=pjtoggle]")

            # KC-03: switch the platform with the dropdown
            pg.select_option("#pjplatsel", "kicad")
            pg.wait_for_selector("#pjc-doc .pjcleared", timeout=15000)
            t = pg.inner_text("#pjc-doc")
            ck("R14-3 switching clears the document and asks to confirm again (KC-03)", "Switched to KiCad" in t and "was cleared" in t, t[:300])
            sv = srv.get("api/state")
            ck("R14-3 server: KiCad saved, selection cleared", sv["workspace"]["ecad"]["saved"] == "kicad" and sv["project"]["select"] is None)
            pg.wait_for_selector("#pjc-link .vd.pass")
            ck("R14-1 KiCad connected (simulator)", "KiCad" in pg.inner_text("#pjc-link"))
            P.snap("i_kicad_connected_cleared", "#pjc-ecad")
            flag.write_text("down")
            pg.click("#pjc-link [data-act=pjcheck]")
            pg.wait_for_selector("#pjc-link .vd.fail")
            ck("R14-1 KiCad unreachable", "NOT REACHABLE" in pg.inner_text("#pjc-link") and pg.locator("#pjc-ecad .steps").count() == 0)
            P.snap("j_kicad_unreachable", "#pjc-ecad")
            pg.click("[data-act=pjhelp]")
            pg.wait_for_selector("#pjc-ecad .steps")
            ck("R14-2 KiCad help text has no Fusion wording", "Fusion" not in pg.inner_text("#pjc-ecad .steps"))
            P.snap("k_kicad_unreachable_help_open", "#pjc-ecad")
            # continue without ECAD stays
            pg.click("[data-act=pjskip]")
            pg.fill(".dlg input[name=reason]", "No ECAD today")
            pg.click(".dlg button[type=submit]")
            pg.wait_for_function("S && S.project && S.project.ecad_skipped !== null")
            pg.wait_for_timeout(300)
            ck("R14 Continue without ECAD: box skipped with the reason, BOM line shown", "No ECAD today" in pg.inner_text("#pjc-ecad") and pg.locator("#pjc-bom #bomline").count() == 1)
            P.snap("l_continue_without_ecad", ".pjpane")
            ck("no page errors", not errors, "; ".join(errors[:5]))
            br.close()
    finally:
        srv.stop()
    bad = [n for n, ok in W.checks if not ok]
    print(f"{len(W.checks) - len(bad)}/{len(W.checks)} passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
