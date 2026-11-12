"""Capture the documentation screenshots from Polybench run on the synthetic DEMO-1 board with --simulate.

    python site/tools/capture_screenshots.py --polybench <venv>/bin/polybench [--port 8781] [--out site/docs/assets/screens]

Starts its own server on its own port (never 8750) with a scratch data folder under /private/tmp, walks the steps with
Playwright and saves PNGs at 1440 x 900 (and 900 x 900 where the narrow layout matters). The camera is a folder holding
one generated barcode image, so no real camera is opened. Every page's text is checked for words that must not appear
in public documentation before the screenshot is saved; a hit stops the run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SITE = HERE.parent
SRC_PROTO = SITE / "examples" / "demo-docs"
SRC_DEMO = SITE.parent / "src" / "polybench" / "examples" / "demo"
# Scratch folder. Fixed and neutral on purpose: the Project page prints the data folder and protocol paths, and a screenshot must not
# show a user name or a checkout path. /tmp resolves to /private/tmp on macOS, which is what the page then prints.
DEMO_DIR = Path(os.environ.get("PB_DEMO_DIR", "/tmp/polybench-demo"))
PROTO = DEMO_DIR / "protocol" / "demo-docs.pcbv.yaml"
# Words that must never appear in a public screenshot (client, project and person names), comma separated in
# PB_DOCS_FORBIDDEN. They are kept out of this file on purpose. The run refuses to start without them.
FORBIDDEN = [w.strip() for w in os.environ.get("PB_DOCS_FORBIDDEN", "").split(",") if w.strip()]
BAD = re.compile("|".join(re.escape(w) for w in FORBIDDEN) or "(?!x)x", re.I)


def barcode_frames(folder: Path, code: str) -> None:
    import cv2
    import numpy as np
    import zxingcpp
    folder.mkdir(parents=True, exist_ok=True)
    img = np.array(zxingcpp.create_barcode(code, zxingcpp.BarcodeFormat.Code128).to_image(scale=4))
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    canvas = np.full((720, 1280, 3), 200, np.uint8)
    cv2.rectangle(canvas, (300, 220), (980, 520), (248, 248, 248), -1)
    canvas[300:440, 360:920] = cv2.cvtColor(cv2.resize(g, (560, 140), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR)
    cv2.putText(canvas, "DEMO PART BAG   " + code, (360, 285), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 40), 2)
    cv2.imwrite(str(folder / "bag.png"), canvas)
    (folder / "CURRENT").write_text("")


def board_photo(layout: dict, seed: int) -> bytes:
    """A synthetic top-side 'photo' drawn from the layout: red mask, grey parts, pale pads. Clearly not a real board."""
    import cv2
    import numpy as np
    ppmm, m = 30, 40
    w, h = 40 * ppmm + 2 * m, 30 * ppmm + 2 * m
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), (24, 22, 26), np.uint8)
    cv2.rectangle(img, (m, m), (w - m, h - m), (40, 40, 165), -1)
    for p in layout["parts"]:
        if not p.get("fitted", True):
            continue
        pts = np.int32([[m + x * ppmm, h - m - y * ppmm] for x, y in p["polygon"]])
        cv2.fillPoly(img, [pts], (55, 55, 60) if p["package"] not in ("TP",) else (170, 200, 215))
    img = cv2.GaussianBlur(img, (3, 3), 0)
    noise = rng.integers(0, 12, img.shape, dtype=np.uint8)
    img = cv2.add(img, noise)
    cv2.putText(img, f"SYNTHETIC DEMO-1 PHOTO {seed}", (m + 10, m + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (230, 230, 230), 2)
    return cv2.imencode(".jpg", img)[1].tobytes()


class Server:
    def __init__(self, exe, port, data, batch, frames, state, project=True):
        env = dict(os.environ, POLYBENCH_NO_REVEAL="1", POLYBENCH_DEMO_DIR=str(DEMO_DIR / "demo-project"), POLYBENCH_CONFIG_DIR=str(state / "cfg"), POLYBENCH_SCANNER_FAKE_FRAMES=str(frames), POLYBENCH_STATE_DIR=str(state), POLYBENCH_DATA=str(data), POLYBENCH_VOICE_CONFIG=str(state / "voice.json"),
                   ASCAN_CONFIG_DIR=str(state / "scanner"))     # keep the scanner settings of the real install untouched
        self.base = f"http://127.0.0.1:{port}/"
        cmd = ([exe, "--protocol", str(PROTO), "--batch", batch, "--data-dir", str(data), "--operator", "A. Engineer"] if project else [exe])   # no project: the welcome page
        self.p = subprocess.Popen(cmd + ["--simulate", "--port", str(port), "--no-browser"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "api/state", timeout=1).read()
                return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("server did not start: " + self.p.stdout.read(4000).decode(errors="replace"))

    def state(self):
        return json.load(urllib.request.urlopen(self.base + "api/state", timeout=10))

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--polybench", required=True, help="the polybench executable of a venv that has the [scanner,aoi] extras")
    ap.add_argument("--port", type=int, default=8781)
    ap.add_argument("--out", default=str(SITE / "docs" / "assets" / "screens"))
    ap.add_argument("--only", default="main,photos,welcome", help="comma-separated walks to run: main, photos, welcome (the first-run page and the Plugins tab)")
    a = ap.parse_args()
    if not FORBIDDEN:
        sys.exit("set PB_DOCS_FORBIDDEN to the comma-separated names that must not appear in a public screenshot")
    if a.port == 8750:
        sys.exit("port 8750 is the operator's live server; choose another")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = DEMO_DIR
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    (work / "protocol").mkdir()
    shutil.copytree(SRC_DEMO, work / "protocol" / "demo")
    shutil.copytree(SRC_PROTO / "aoi", work / "protocol" / "aoi")
    shutil.copy(SRC_PROTO / "DEMO-1_Test_Plan.md", work / "protocol" / "DEMO-1_Test_Plan.md")
    text = (SRC_PROTO / "demo-docs.pcbv.yaml").read_text().replace("../../../src/polybench/examples/demo/", "demo/")
    PROTO.write_text(text)
    frames, state, data = work / "frames", work / "state", work / "data"
    if {x.strip() for x in a.only.split(",")} & {"main", "photos"}:      # the scanner walks need cv2 and zxing-cpp; the welcome walk does not
        barcode_frames(frames, "595-DEMOMCU32")
    else:
        frames.mkdir(parents=True, exist_ok=True)
    (state / "cfg").mkdir(parents=True, exist_ok=True)
    # voice: replies go to the log, never the speaker; the LLM, if used, is the local Ollama
    (state / "voice.json").write_text(json.dumps({"tts_backend": "log"}))
    saved: list[str] = []

    def run_batch(batch, walk, project=True):
        srv = Server(a.polybench, a.port, data, batch, frames, state, project)
        try:
            with sync_playwright() as pw:
                br = pw.chromium.launch()
                pg = br.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto(srv.base)
                pg.wait_for_selector("#timeline .tl" if project else "#welcome")

                def snap(name, width=1440, height=900, full=False, clip=None):
                    if pg.viewport_size["width"] != width or pg.viewport_size["height"] != height:
                        pg.set_viewport_size({"width": width, "height": height})
                    pg.wait_for_timeout(450)
                    try:                      # a toast raised by the last action would sit over the view: let it expire first
                        pg.wait_for_function("() => { const t = document.querySelector('#toast'); return !t || t.style.display === 'none' }", timeout=6000)
                    except Exception:
                        pass
                    txt = pg.evaluate("document.body.innerText + [...document.querySelectorAll('iframe')].map(f => { try { return f.contentDocument.body.innerText } catch (e) { return '' } }).join(' ')")
                    hit = BAD.findall(txt)
                    if hit:
                        ctx = [txt[max(0, m.start() - 60):m.end() + 40].replace("\n", " ") for m in BAD.finditer(txt)][:3]
                        raise SystemExit(f"{name}: forbidden words on the page: {sorted(set(hit))}; context: {ctx}")
                    p = out / f"{name}.png"
                    pg.screenshot(path=str(p), full_page=full, clip=clip)
                    saved.append(p.name)
                    print("saved", p.name, flush=True)

                try:
                    walk(pg, srv, snap)
                except Exception:
                    if os.environ.get("PB_DEBUG_SHOT"):
                        pg.screenshot(path=os.environ["PB_DEBUG_SHOT"])
                    raise
                if errors:
                    print("page errors:", errors)
                br.close()
        finally:
            srv.stop()

    def ended(pg):
        return pg.evaluate("consoleLines.filter(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text)).length")

    def run_wait(pg, label, timeout=30000):
        before = ended(pg)
        pg.click(f"button:has-text('{label}')")
        pg.wait_for_function(f"consoleLines.filter(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text)).length>{before}", timeout=timeout)
        pg.wait_for_timeout(500)

    def step(pg, sid):
        pg.click(f"#timeline .tl[data-id='{sid}']")
        pg.wait_for_timeout(350)

    def confirm(pg):
        """An out-of-band reading is only recorded after the confirm panel: press its Accept / Record button."""
        b = pg.locator("button:has-text('Accept for review'), button:has-text('Accept FAIL'), button:has-text('Record FAIL')")
        if b.count():
            b.first.click()
            pg.wait_for_timeout(400)

    def type_enter(pg, values):
        """Type each value and press Enter; a value prefixed with '!' is out of band and is confirmed on the confirm panel (FAIL or review)."""
        for v in values:
            pg.locator("#val").focus()
            pg.keyboard.type(v.lstrip("!"))
            pg.wait_for_timeout(120)
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(300)
            if v.startswith("!"):
                confirm(pg)

    def board(pg, label):
        """Select a board. Unpowered is gated on Visual; this demo walk does not photograph every board, so it overrides with a reason."""
        pg.click(f".bchip:has-text('{label}')")
        pg.wait_for_timeout(250)
        if pg.locator("text=Override with a reason").count():
            pg.click("text=Override with a reason")
            pg.fill(".dlg input", "Documentation run: no photo taken for this board")
            pg.click(".dlg button[type=submit]")
            pg.wait_for_timeout(400)

    def bottom(pg, open_):
        pg.evaluate(f"ui.bottomOpen = {'true' if open_ else 'false'}; render()")
        pg.wait_for_timeout(200)

    # ------------------------------------------------------------------ main walk
    def walk_main(pg, srv, snap):
        # 1 Project
        pg.wait_for_selector("#pjc-folder")
        pg.wait_for_timeout(1500)
        pg.evaluate("document.querySelector('#pjc-plat').scrollIntoView({block: 'start'})")
        snap("01_project_link")
        pg.click("#pjc-doc [data-act=pjuse]")
        pg.wait_for_timeout(900)
        if pg.locator("#ccnote").count():
            pg.fill("#ccnote", "TP99 is a fixture test point, not in the BOM")
        pg.evaluate("document.querySelector('#pjc-bom').scrollIntoView({block: 'start'})")
        snap("01_project_crosscheck")
        pg.click("[data-act=pjaccept]")
        pg.wait_for_timeout(500)
        # 4 Test plan
        step(pg, "testplan")
        snap("04_testplan")
        pg.evaluate("""(() => { const h = [...document.querySelectorAll('#centre h1, #centre h2, #centre h3, #centre h4, #centre b, #centre div')]
                       .find(e => e.textContent.trim() === 'Coverage'); if (h) h.scrollIntoView({block: 'start'}); })()""")
        snap("04_testplan_coverage")
        # 5 Batch setup
        step(pg, "setup")
        for i in range(1, 11):
            pg.locator(".serial").nth(i - 1).fill(f"DEMO-A-{i:03d}")
            pg.keyboard.press("Tab")
        pg.wait_for_timeout(300)
        snap("05_batch_setup")
        # 6 Assembly aid
        step(pg, "assembly")
        pg.click("#asmbegin")      # UIR-20: the start page
        (frames / "CURRENT").write_text("bag.png")
        pg.wait_for_timeout(3500)
        snap("06_assembly_scan")
        snap("06_assembly_scan_900", width=900)
        (frames / "CURRENT").write_text("")
        pg.set_viewport_size({"width": 1440, "height": 900})
        pg.locator("#scan").fill("D1")
        pg.press("#scan", "Enter")
        pg.wait_for_timeout(900)
        pg.evaluate("document.querySelector('#centre').scrollTop = 520")
        snap("06_assembly_polarity")
        pg.locator("#scan").fill("C3")
        pg.press("#scan", "Enter")
        pg.wait_for_timeout(500)
        pg.fill("#scan", "")
        for _ in range(9):
            pg.locator("#scan").focus()
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(250)
        pg.evaluate("document.querySelector('#centre').scrollTop = 520")
        snap("06_assembly_progress")
        # notes
        pg.click('button[data-nt="add"]')
        pg.wait_for_timeout(300)
        pg.fill("#nd-text", "The 0402 decoupling pads are short for hand soldering")
        pg.select_option("#nd-cat", "design issue")
        pg.wait_for_timeout(200)
        if pg.locator("#nd-fix").is_visible():
            pg.fill("#nd-fix", "Lengthen the 0402 pads by 0.1 mm")
        snap("13_note_dialog")
        pg.click("#nd-save")
        pg.wait_for_timeout(300)
        # 7 Visual: AOI only (photos, run, issue review, checklist); there is no Yes/No checklist in this step
        step(pg, "visual")
        pg.wait_for_selector('button[data-aoi="run"]')
        bottom(pg, False)
        pg.click('button[data-aoi="run"]')
        pg.wait_for_timeout(6000)
        snap("07_aoi_run")
        bottom(pg, False)
        pg.click('button[data-aoi="mode"][data-m="review"]')
        pg.wait_for_selector("#aoiframe")
        pg.wait_for_timeout(2500)
        frame = next(f for f in pg.frames if f.url.endswith("/aoi/review/"))
        frame.evaluate("decide('rejected')"); pg.wait_for_timeout(400)
        frame.evaluate("go(idx+1)"); pg.wait_for_timeout(300)
        frame.evaluate("decide('rejected')"); pg.wait_for_timeout(400)
        frame.evaluate("go(idx+1)"); pg.wait_for_timeout(300)
        frame.evaluate("decide('unsure')"); pg.wait_for_timeout(400)
        frame.evaluate("go(idx+1)"); pg.wait_for_timeout(800)
        snap("07_aoi_review")
        pg.set_viewport_size({"width": 900, "height": 900})       # narrow window: the checklist is a drawer behind the Checklist chip
        pg.wait_for_timeout(500)
        if pg.locator('button[data-aoi="ck"]').count() and not pg.evaluate("ui.aoiDrawer"):
            pg.click('button[data-aoi="ck"]')
            pg.wait_for_timeout(600)
        snap("07_aoi_checklist_900", width=900)
        pg.set_viewport_size({"width": 1440, "height": 900})
        # 8 Unpowered: the tests are generated from the plan (Tier 1, gate, Tier 2)
        step(pg, "unpowered")
        bottom(pg, False)
        board(pg, "S01")
        snap("08_unpowered_tiers")
        if pg.locator("#advmode").count() and pg.is_checked("#advmode"):
            pg.uncheck("#advmode")                    # walk test by test on one board for the screenshots
        pg.click(".trow[data-id='U01']")
        type_enter(pg, ["12.5", "!1.6", "1.2", "y", "clean board"])
        pg.click(".trow[data-id='U02']")
        pg.wait_for_timeout(300)
        snap("08_unpowered_guess_review")
        board(pg, "S03")
        pg.click(".trow[data-id='U01']")
        pg.locator("#val").focus()
        pg.keyboard.type("0.4")
        pg.wait_for_timeout(300)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(400)
        confirm(pg)
        pg.wait_for_timeout(300)
        snap("08_unpowered_stop")
        pg.click("text=Acknowledge stop with a reason")
        pg.fill("input[name=reason]", "U3 bridged, reflow and re-measure")
        pg.click(".dlg button[type=submit]")
        pg.wait_for_timeout(400)
        board(pg, "S02")
        pg.click(".trow[data-id='U01']")
        type_enter(pg, ["11", "3", "1.2", "y", "ok"])
        # voice: a typed utterance goes through the same path as speech (no microphone is opened)
        board(pg, "S04")
        pg.click(".trow[data-id='U01']")
        pg.click("[data-act=btab][data-id=voice]")
        pg.wait_for_timeout(800)
        for utt in ("S4 11800 ohms", "accept"):
            pg.fill("#vtext", utt)
            pg.press("#vtext", "Enter")
            pg.wait_for_timeout(2500)
        snap("14_voice_tab")
        if pg.locator("[data-vo=settings]").count():
            pg.click("[data-vo=settings]")
            pg.wait_for_timeout(1500)
            snap("14_voice_settings")
            pg.keyboard.press("Escape")
            pg.evaluate("document.getElementById('dialog-root').innerHTML=''")
        bottom(pg, False)
        board(pg, "S04")
        pg.click(".trow[data-id='U02']")
        type_enter(pg, ["3.2", "1.3", "y", "ok"])
        if pg.locator(".trow[data-id='U01'] .c").count() == 0:
            pass
        # decision gate
        pg.click(".trow.gate")
        pg.wait_for_timeout(500)
        snap("08_decision_gate")
        pg.click("[data-act=gateacc][data-b='S01']")
        pg.wait_for_timeout(600)
        pg.click(".trow[data-id='U10']")
        board(pg, "S01")
        pg.click(".trow[data-id='U10']")
        pg.wait_for_timeout(300)
        snap("08_tier2")
        type_enter(pg, ["0.05", "1.4", "y"])
        board(pg, "S01")
        pg.click(".trow[data-id='U01']")
        pg.click("text=Send S01 back for retest")
        pg.fill("input[name=cause]", "C2 shorted the 5V rail")
        if pg.locator("input[name=fix]").count():
            pg.fill("input[name=fix]", "C2 replaced")
        pg.click(".dlg button[type=submit]")
        pg.wait_for_timeout(400)
        pg.click(".trow[data-id='U01']")
        type_enter(pg, ["12.4", "3.1", "1.2", "y", "after rework"])
        # 9 Powered
        step(pg, "powered")
        bottom(pg, False)
        pg.click(".bchip:has-text('S01')")
        pg.wait_for_timeout(300)
        if pg.locator("text=Override with a reason").count():
            snap("09_power_interlock")
            pg.click("text=Override with a reason")
            pg.fill(".veil input", "Tier 2 done and Tier 1 repeated; documentation run")
            pg.click(".veil button.primary")
            pg.wait_for_timeout(400)
        pg.click("button:has-text('Read rails and current')")
        pg.wait_for_function("document.querySelectorAll('#bbody .cl.proposal').length>=4", timeout=20000)
        pg.wait_for_timeout(800)
        snap("09_powered_proposals")
        pg.click("text=Accept all clean for S01")
        pg.wait_for_timeout(500)
        pg.click(".bchip:has-text('S04')")
        run_wait(pg, "Read rails and current")
        pg.click("text=Accept all clean for S04")
        pg.wait_for_timeout(400)
        pg.click(".bchip:has-text('S02')")
        run_wait(pg, "Read rails and current")
        pg.click(".trow[data-id='P02']")
        pg.wait_for_timeout(300)
        pg.click("[data-act=btab][data-id=controls]")
        pg.wait_for_timeout(300)
        snap("09_powered_controls")
        # 10 Program and function
        step(pg, "function")
        pg.click(".bchip:has-text('S01')")
        e0 = ended(pg)
        pg.click("button:has-text('Flash bring-up image')")
        pg.wait_for_selector(".btabs .prog", timeout=8000)
        pg.wait_for_timeout(500)
        snap("10_function_flash_running")
        pg.wait_for_function(f"consoleLines.filter(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text)).length>{e0}", timeout=30000)
        pg.wait_for_timeout(500)
        pg.click(".trow:has-text('Device ID')")
        snap("10_function_proposal")
        pg.locator("#val").focus()
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(300)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(300)
        run_wait(pg, "Read heartbeat over CAN")
        pg.click(".trow:has-text('Heartbeat rate')")
        pg.locator("#val").focus()
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(300)
        pg.click(".trow:has-text('Firmware revision')")
        type_enter(pg, ["2"])
        pg.click("button:has-text('Long capture')")
        pg.wait_for_selector(".btabs .danger", timeout=8000)
        pg.wait_for_timeout(900)
        pg.click(".btabs .danger")
        pg.wait_for_function("!document.querySelector('.btabs .danger')", timeout=20000)
        pg.wait_for_timeout(600)
        snap("10_function_aborted")
        # 11 Characterisation
        step(pg, "characterisation")
        pg.click("button:has-text('Capture noise at 2 mV/V')")
        pg.wait_for_selector("#runok")
        snap("11_characterisation_prompt")
        pg.click("#runok")
        pg.wait_for_selector(".dlg [data-opt]", timeout=10000)
        snap("11_characterisation_ask")
        e1 = ended(pg)
        pg.keyboard.press("1")
        pg.wait_for_function(f"consoleLines.filter(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text)).length>{e1}", timeout=30000)
        pg.wait_for_timeout(700)
        pg.locator("#val").fill("")                   # the answer key may land in the box after the dialog closes
        pg.click(".trow[data-id='C01']")
        snap("11_characterisation_stats")
        pg.click("text=Accept all clean for S01")
        pg.wait_for_timeout(400)
        # journal, advanced grid, help
        pg.click("[data-act=btab][data-id=journal]")
        pg.wait_for_timeout(500)
        snap("13_journal_tab")
        pg.check("#adv")
        step(pg, "unpowered")
        bottom(pg, False)
        pg.evaluate("document.querySelector('#centre').scrollTop = 1e6")
        snap("03_advanced_grid")
        pg.uncheck("#adv")
        pg.click("[data-act=help]")
        pg.wait_for_timeout(300)
        snap("03_keyboard_help")
        pg.keyboard.press("Escape")
        pg.evaluate("document.getElementById('dialog-root').innerHTML=''")
        # 12 Report
        step(pg, "report")
        bottom(pg, False)
        pg.wait_for_selector("#repframe")
        pg.wait_for_timeout(1200)
        snap("12_report")
        pg.click("text=Generate report files")
        pg.wait_for_timeout(700)
        snap("12_report_generated")
        # overview in dark theme
        step(pg, "unpowered")
        bottom(pg, False)
        pg.click(".bchip:has-text('S01')")
        pg.click(".trow:has-text('5V to GND')")
        pg.evaluate("ui.theme = 'dark'; render()")
        snap("00_overview_dark")
        pg.evaluate("ui.theme = 'light'; render()")
        snap("00_overview")

    # ------------------------------------------------------------------ AOI photo tiles (separate batch: photos make the real pipeline run)
    def walk_photos(pg, srv, snap):
        layout = json.loads((PROTO.parent / "aoi" / "layout.json").read_text())
        step(pg, "setup")
        step(pg, "visual")
        pg.click('button[data-aoi="mode"][data-m="aoi"]')
        pg.wait_for_timeout(500)
        for i, b in enumerate(("S01", "S02", "S03", "S05"), 1):
            p = work / f"DEMO-1-{b}-top.jpg"
            p.write_bytes(board_photo(layout, i))
            with pg.expect_file_chooser() as fc:
                pg.click(f"[data-tile={b}]")
            fc.value.set_files(str(p))
            pg.wait_for_timeout(700)
        snap("07_aoi_photo_tiles")

    def walk_welcome(pg, srv, snap):
        """First run (no project): the welcome page; then the packaged demo and the Plugins tab with a result in it."""
        pg.wait_for_selector("#welcome")
        snap("00_welcome")
        pg.click("[data-act=demoopen]")
        pg.wait_for_selector(".badge.demo")
        pg.wait_for_timeout(600)
        # the powered step is gated behind the unpowered tests: the demo overrides it with a reason, then one demo action runs
        pg.evaluate("""async () => { const post = (u, b) => fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
            await post('/api/cmd/override_gate', {board: 'S01', step: 'powered', reason: 'Documentation run'});
            await post('/api/action/run', {step: 'powered', section: 'first_power', action: 'read_rails', board: 'S01', confirmed: true}); }""")
        pg.wait_for_timeout(2500)
        step(pg, "powered")
        pg.click("#bottom .btab-plugins")
        pg.wait_for_selector(".pl-tab")
        pg.wait_for_timeout(600)
        pg.evaluate("document.querySelector('#bbody').scrollTop = 0")
        snap("00_plugins")

    only = {x.strip() for x in a.only.split(",")}
    if "main" in only:
        run_batch("docs-demo", walk_main)
    if "photos" in only:
        run_batch("docs-photos", walk_photos)
    if "welcome" in only:
        run_batch("docs-welcome", walk_welcome, project=False)
    # 256-colour palette: UI screenshots stay sharp at about a third of the size
    from PIL import Image
    for name in saved:
        p = out / name
        Image.open(p).convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(p, optimize=True)
    shutil.rmtree(work, ignore_errors=True)
    print(f"{len(saved)} screenshots in {out}; scratch folder {work} removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
