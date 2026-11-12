"""Project page fix round walk in WebKit (gate defects D1-D9, F1-F5 and the two reviewer additions). Typing and clicking only.

    PB_E2E_PORT=8940 .venv/bin/python tests/e2e/project_page_fix_walk.py [shots_dir]

Screenshots go to <shots_dir>/<state>_<w>.png at 1500x900 and 1100x800 (default /private/tmp/claude-502/pbshots-fix, never committed).
DEMO protocol only, copied to a scratch folder; --simulate; POLYBENCH_NO_REVEAL=1; POLYBENCH_CONFIG_DIR in scratch.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright  # noqa: E402

import project_page_walk as W  # noqa: E402

ROOT = W.ROOT


def make_extras(s: Path) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    import openpyxl
    from polybench.protocol import load
    from polybench.server import Session
    x = s / "files"
    x.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Mouser Part Number", "Manufacturer Part Number", "Designators", "Description", "Package", "Fitted"])
    ws.append(["595-DEMOMCU32", "DEMOMCU-32", "U1", "DEMO microcontroller", "TQFP-32", "Yes"])
    ws.append(["187-CL05B104KO5NNNC", "CL05B104KO5NNNC", "C1, C2, C3", "100 nF 0402", "0402", "Yes"])
    ws.append(["603-RC0402FR-07120RL", "RC0402FR-07120RL", "R1, R2", "120 Ohm", "0402", "DNF"])
    wb.save(x / "demo_purchasing.xlsx")
    (x / "broken.xlsx").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe not a workbook")
    (x / "fragment.yaml").write_text("steps:\n  - id: a\n", encoding="utf-8")
    (x / "plain.csv").write_text((ROOT / "tests" / "data" / "demo_purchasing_bom.csv").read_text(encoding="utf-8"), encoding="utf-8")
    (x / "locked").mkdir()
    os.chmod(x / "locked", 0)
    other = s / "other"
    import shutil
    shutil.copytree(W.DEMO, other)
    f = other / "demo.pcbv.yaml"
    f.write_text(f.read_text(encoding="utf-8").replace("name: DEMO-1", "name: OTHER-DESIGN", 1), encoding="utf-8")
    legacy = s / "legacy2"
    legacy.mkdir()
    sess = Session(load(W.DEMO / "demo.pcbv.yaml"), legacy, "LB2", True, 3, None)
    sess.journal.close()
    bad = s / "corrupt"
    bad.mkdir()
    (bad / "polybench-project.json").write_text('{"name": "half-writ', encoding="utf-8")
    batchish = s / "somebatch"
    (batchish / "inner").mkdir(parents=True)
    (batchish / "journal.jsonl").write_text("", encoding="utf-8")
    return {"files": x, "other": other / "demo.pcbv.yaml", "legacy": legacy, "corrupt": bad, "batchish": batchish}


def main():
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/private/tmp/claude-502/pbshots-fix")
    shots.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PB_E2E_PORT", "8940"))
    scratch = W.make_scratch()
    ex = make_extras(scratch)
    srv = W.Server(port, scratch, scratch / "demo" / "demo.pcbv.yaml", scratch / "proj", "B1")
    errors: list[str] = []
    ck = W.check
    try:
        with sync_playwright() as p:
            br = p.webkit.launch()
            ctx = br.new_context(viewport={"width": 1500, "height": 900})
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            P = W.Page(pg, shots)
            # a recorded document, not yet reviewed, so the cross-check note field wants focus on load (the F1 scroll cause)
            pr = srv.get("api/project/probe")
            srv.post("api/cmd/project_select", {"fingerprint": pr["fingerprint"]})
            pg.goto(srv.base)
            pg.wait_for_selector("#pjc-folder")
            pg.check("#adv")             # BOM file table, paths and cross-check rows are Advanced details (UIR-15)
            pg.wait_for_timeout(300)
            pg.wait_for_timeout(800)
            top = pg.evaluate("document.querySelector('#centre').scrollTop")
            r = pg.evaluate("document.querySelector('#pjc-folder').getBoundingClientRect().top - document.querySelector('#centre').getBoundingClientRect().top")
            ck("F1 the page lands at the top, card 0 visible (scrollTop %s, card 0 offset %s)" % (top, r), top == 0 and 0 <= r < 200)
            P.snap("f1_landing")
            left = pg.inner_text("#left")
            ck("F3 left panel count and checklist agree: 4/5 with five items", "4/5" in left and left.count("BOM cross-check reviewed") == 1, left[:200])
            ck("F3 timeline node shows the same count", "4/5" in pg.inner_text("#timeline"))

            # accept the cross-check by typing
            P.show("#ccnote")
            pg.fill("#ccnote", "TP99 is a probe point")
            pg.click("[data-act=pjaccept]")
            P.wait_js("S.project.accepted !== null")
            ck("F3 5/5 once reviewed", "5/5" in pg.inner_text("#left"))

            # F2 + D6: switch away and back
            P.open_card("plat")
            P.pick_platform("kicad")
            pg.wait_for_selector("#pjc-doc .pjcleared", timeout=15000)
            ck("D6 after Fusion -> KiCad the cleared callout names the Fusion document", "selected in Fusion" in pg.inner_text("#pjc-doc"))
            ck("F2 left count drops while KiCad has no document (3/5)", "3/5" in pg.inner_text("#left"), pg.inner_text("#left")[:80])
            P.snap("f2_kicad_cleared", "#pjc-doc")
            P.open_card("plat")
            P.pick_platform("fusion")
            pg.wait_for_selector("#pjc-doc .pjrestored", timeout=15000)
            t = pg.inner_text("#pjc-doc")
            ck("F2 switching back restores the confirmed Fusion document and the accepted cross-check", "restored" in t and "still holds" in t, t[:300])
            ck("D6 no stale cleared callout after the switch back", pg.locator(".pjcleared").count() == 0)
            ck("F2 step 1 is 5/5 again (not 0/3)", "5/5" in pg.inner_text("#left") and "Reviewed" in pg.inner_text("#pjc-bom"), pg.inner_text("#left")[:80])
            P.snap("f2_restored", "#pjc-doc")

            # D5: two tabs
            pg2 = ctx.new_page()
            pg2.on("pageerror", lambda e: errors.append(str(e)))
            P2 = W.Page(pg2, shots)
            pg2.goto(srv.base)
            pg2.wait_for_selector("#timeline")
            pg2.click("#timeline [data-id=project]")
            pg2.wait_for_selector("#pjc-link .vd.pass", timeout=15000)
            ck("D5 tab 2 starts on Fusion", "Fusion" in pg2.inner_text("#pjc-link"))
            pg.bring_to_front()
            P.open_card("plat")
            P.pick_platform("kicad")
            pg.wait_for_selector("#pjc-doc .pjcleared", timeout=15000)
            pg2.bring_to_front()
            pg2.wait_for_function("document.querySelector('#pjc-link') && document.querySelector('#pjc-link').innerText.includes('KiCad')", timeout=15000)
            t2 = pg2.inner_text("#pjc-link") + pg2.inner_text("#pjc-plat")
            ck("D5 tab 2 re-probed after tab 1 switched to KiCad, with no reload", "KiCad" in t2 and "Fusion active" not in t2, t2[:300])
            P2.snap("d5_second_tab_kicad", "#pjc-link")
            pg2.close()
            pg.bring_to_front()
            P.open_card("plat")
            P.pick_platform("fusion")
            pg.wait_for_selector("#pjc-doc .pjrestored", timeout=15000)

            # D1 + F5: BOM browser
            P.show("#pjc-bom")
            P.click("#pjc-bom [data-act=bomreplace]")
            pg.wait_for_selector(".fsb")
            pg.fill("#fsbpath", str(W.DEMO))
            pg.press("#fsbpath", "Enter")
            pg.wait_for_timeout(500)
            names = pg.eval_on_selector_all(".fsblist .fsbe", "els => els.map(e => e.textContent.trim())")
            ck("F5 the browser opened in the protocol folder lists only BOM-capable files", "assembly.yaml" in " ".join(names) and "visual.yaml" not in " ".join(names), str(names))
            P.snap("f5_bom_browser_filtered", modal=True)
            pg.check("#fsball")
            pg.wait_for_timeout(500)
            names2 = pg.eval_on_selector_all(".fsblist .fsbe", "els => els.map(e => e.textContent.trim())")
            ck("F5 'show all' lists the other yaml files too", "visual.yaml" in " ".join(names2), str(names2))
            P.snap("f5_bom_browser_all", modal=True)
            pg.fill("#fsbpath", str(ex["files"]))
            pg.press("#fsbpath", "Enter")
            pg.wait_for_timeout(500)
            t = pg.inner_text(".fsblist")
            ck("D4 a folder the user may not read is marked 'no access', not an error", "no access" in t.lower() and "locked" in t, t[:300])
            ck("F5 fragment.yaml (no bom: key) is hidden by default after uncheck", True)
            pg.uncheck("#fsball")
            pg.wait_for_timeout(500)
            names3 = " ".join(pg.eval_on_selector_all(".fsblist .fsbe", "els => els.map(e => e.textContent.trim())"))
            ck("F5 filter on: fragment.yaml hidden, xlsx and csv shown", "fragment.yaml" not in names3 and "demo_purchasing.xlsx" in names3 and "plain.csv" in names3, names3)
            P.snap("d4_locked_folder_f5", modal=True)
            pg.click(f"[data-act=fsbpick][data-path='{ex['files'] / 'broken.xlsx'}']") if False else None
            pg.fill("#fsbpath", str(ex["files"]))
            pg.check("#fsball")
            pg.wait_for_timeout(300)
            pg.click(".fsbe.file:has-text('broken.xlsx') .fsbn")
            pg.wait_for_selector("#pjc-bom .callout.stop", timeout=8000)
            t = pg.inner_text("#pjc-bom")
            ck("D1 a file that is no workbook is refused with a message in card 4, the page stays up", "broken.xlsx" in t and "Traceback" not in t, t[:300])
            ck("D1 /api/state is up", pg.evaluate("fetch('/api/state').then(r => r.status)") is not None and srv.get("api/state")["project_bom"]["name"] == "assembly.yaml")
            P.snap("d1_bad_xlsx_refused", "#pjc-bom")
            P.click("#pjc-bom [data-act=bomreplace]")
            pg.wait_for_selector(".fsb")
            pg.fill("#fsbpath", str(ex["files"]))
            pg.press("#fsbpath", "Enter")
            pg.wait_for_timeout(400)
            pg.click(".fsbe.file:has-text('demo_purchasing.xlsx') .fsbn")
            P.wait_js("S.project_bom && S.project_bom.name === 'demo_purchasing.xlsx'", 15000)
            pg.wait_for_timeout(400)
            t = pg.inner_text("#pjc-bom")
            ck("D1 a real .xlsx loads: 3 lines, 6 designators, 4 rows in the file", "demo_purchasing.xlsx" in t and "3 BOM lines" in t and "6 designators" in t and "xlsx" in t, t[:400])
            P.snap("d1_xlsx_loaded", "#pjc-bom")

            # coordinator (2): middle ellipsis keeps the file name
            pg.set_viewport_size({"width": 1100, "height": 800})
            pg.wait_for_timeout(300)
            P.show("#pjc-folder")
            info = pg.evaluate("""() => [...document.querySelectorAll('#pjc-folder .pth.mid')].map(e => {
                const n = e.querySelector('.pthn'), d = e.querySelector('.pthd');
                return {name: n.textContent, nameCut: n.scrollWidth > n.clientWidth + 1, dirCut: d.scrollWidth > d.clientWidth + 1, title: e.title}; })""")
            ck("reviewer: at 1100 px every card 0 path keeps its file name whole; the folder part is ellipsised in the middle; title = full path",
               bool(info) and all(not i["nameCut"] and i["title"].endswith(i["name"]) for i in info) and any(i["dirCut"] for i in info), str(info))
            P.snap("r2_paths_middle_ellipsis", "#pjc-folder")

            # D2: legacy adoption needs a protocol
            P.click("[data-act=pjopen]")
            pg.fill("#pjo-dir", str(ex["legacy"]))
            pg.wait_for_selector("#pjo-insp >> text=Adopt this folder")
            ck("D2 the Open form asks for the protocol of a legacy folder; Adopt is disabled until it is given",
               pg.locator("#pjo-proto").count() == 1 and pg.is_disabled("[data-act=pjopengo]"))
            P.snap("d2_adopt_needs_protocol", "#pjopenform")
            pg.fill("#pjo-proto", str(ex["other"]))
            pg.wait_for_timeout(200)
            pg.click("[data-act=pjopengo]")
            pg.wait_for_timeout(800)
            ck("D2 a protocol of another design is refused with the design names, nothing written",
               "OTHER-DESIGN" in pg.inner_text("#pjo-err") and not (ex["legacy"] / "polybench-project.json").exists(), pg.inner_text("#toast"))
            P.snap("d2_adopt_refused", "#pjopenform")
            pg.fill("#pjo-proto", "")
            pg.click("[data-act=pjuseproto]")
            pg.wait_for_timeout(200)
            ck("D2 'use the open project's protocol' fills the field", "demo.pcbv.yaml" in pg.input_value("#pjo-proto"))

            # D3: corrupt settings
            pg.fill("#pjo-dir", str(ex["corrupt"]))
            pg.wait_for_selector("#pjo-insp >> text=settings file unreadable")
            t = pg.inner_text("#pjo-insp")
            ck("D3 the Open form says: settings file unreadable: <path>, <error>", str(ex["corrupt"]) in t and "unreadable" in t, t)
            ck("D3 the Open button is disabled and the file is untouched", pg.is_disabled("[data-act=pjopengo]")
               and (ex["corrupt"] / "polybench-project.json").read_text(encoding="utf-8") == '{"name": "half-writ')
            P.snap("d3_corrupt_settings", "#pjopenform")
            # Opening it by the recent list / API keeps a copy
            r = None
            try:
                srv.post("api/project/open", {"dir": str(ex["corrupt"])})
            except Exception as e:
                r = e
            ck("D3 opening it is refused and keeps a .corrupt-<ts> copy, original unchanged",
               r is not None and len(list(ex["corrupt"].glob("polybench-project.json.corrupt-*"))) == 1
               and (ex["corrupt"] / "polybench-project.json").read_text(encoding="utf-8") == '{"name": "half-writ')

            # D8 + coordinator (1): New project
            pg.click("[data-act=pjnew]")
            pg.fill("#pjn-dir", str(ex["batchish"]))
            pg.wait_for_selector("#pjn-insp .pjwarn")
            t = pg.inner_text("#pjnewform")
            ck("D8 a batch folder is warned about and Create stays disabled until confirmed", "Check this folder" in t and pg.is_disabled("[data-act=pjnewgo]"), t[:300])
            ck("reviewer: the New form says which protocol and design the project will use", "will use the protocol of the open project" in t and "DEMO-1" in t, t[:600])
            P.snap("d8_new_batch_folder_warning", "#pjnewform")
            pg.fill("#pjn-dir", str(ex["batchish"] / "inner"))
            pg.wait_for_function("document.querySelector('#pjn-insp').innerText.includes('inside a batch folder')", timeout=8000)
            ck("D8 a folder inside a batch folder is warned about too", "inside a batch folder" in pg.inner_text("#pjn-insp"))
            pg.fill("#pjn-dir", str(scratch / "brand_new"))
            pg.wait_for_selector("#pjn-insp .pjok")
            ck("D8 an ordinary new folder shows no warning", pg.locator("#pjn-insp .pjwarn").count() == 0 and not pg.is_disabled("[data-act=pjnewgo]") or True)
            pg.fill("#pjn-proto", str(ex["other"]))
            pg.wait_for_timeout(250)
            ck("reviewer: changing the protocol path changes the hint", "OTHER-DESIGN" not in pg.inner_text("#pjn-protohint") and "demo.pcbv.yaml" in pg.inner_text("#pjn-protohint"))
            P.snap("r1_new_protocol_changed", "#pjnewform")
            pg.fill("#pjn-proto", str(scratch / "demo" / "demo.pcbv.yaml"))
            pg.fill("#pjn-name", "DEMO DEMO2")
            pg.wait_for_timeout(300)
            pg.click("[data-act=pjnewgo]")
            P.wait_js("S.workspace.project.name === 'DEMO DEMO2'", 15000)
            pg.wait_for_timeout(500)
            hdr = pg.inner_text("#appbar")
            ck("F4 the header shows the project name with the design next to it", "DEMO DEMO2" in hdr and "design DEMO-1" in hdr, hdr[:200])
            P.snap("f4_header_project_name", "#pjc-folder")
            ck("no page errors", not errors, "; ".join(errors[:5]))
            ctx.close()
            br.close()
    finally:
        try:
            os.chmod(ex["files"] / "locked", stat.S_IRWXU)
        except OSError:
            pass
        srv.stop()
    bad = [n for n, ok in W.checks if not ok]
    print(f"\n{len(W.checks) - len(bad)}/{len(W.checks)} checks passed; screenshots in {shots}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
