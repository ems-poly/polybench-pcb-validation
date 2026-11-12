"""Project page (step 1) walk in WebKit: project folder, ECAD platform, link check, document, BOM, AI card (Pat, 17 Oct).

    .venv/bin/python tests/e2e/project_page_walk.py [--part 1|2|all] [shots_dir]

Every state is screenshotted at 1500x900 and 1100x800 into <shots_dir>/<state>_<w>.png (default /private/tmp/claude-502/pbshots,
never committed). DEMO protocol only, copied to a scratch folder; the server runs --simulate with POLYBENCH_NO_REVEAL=1 and
POLYBENCH_CONFIG_DIR=<scratch>/cfg, so nothing is opened in Finder and nothing is written to ~/.config/polybench.

Part 1: (a) adopted project folder, (e) Fusion, (h) "+ Request another", (i) document table chips, (g) platform switch clears the
document, (f) KiCad, (k) BOM loaded, (l) replace with assembly events asks first, (m) continue without ECAD, (n) AI card.
Part 2: (b) New project form with the folder browser, (c) new project created, (j) BOM empty, (k2) BOM loaded with +,
(d) Open a legacy folder: adopted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "src" / "polybench" / "examples" / "demo"
SIZES = ((1500, 900), (1100, 800))
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


class Server:
    def __init__(self, port, scratch, protocol, data_dir, batch):
        assert int(port) != 8750, "8750 is the live bench server"
        self.base = f"http://127.0.0.1:{port}/"
        env = {**os.environ, "POLYBENCH_NO_REVEAL": "1", "POLYBENCH_CONFIG_DIR": str(scratch / "cfg")}
        env.pop("POLYBENCH_ECAD", None)
        self.log = open(scratch / f"server_{port}.log", "w")
        self.p = subprocess.Popen([str(ROOT / ".venv" / "bin" / "polybench"), "--protocol", str(protocol), "--simulate", "--data-dir",
                                   str(data_dir), "--batch", batch, "--no-browser", "--port", str(port)],
                                  stdout=self.log, stderr=subprocess.STDOUT, env=env)
        want = str(Path(data_dir).resolve())
        for _ in range(80):
            if self.p.poll() is not None:
                break
            try:
                if self.get("api/workspace")["project"]["dir"] == want:       # our server, not another one on this port
                    return
            except Exception:
                pass
            time.sleep(0.25)
        self.stop()
        raise RuntimeError("server did not start: " + (scratch / f"server_{port}.log").read_text()[-800:])

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path, timeout=20))

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=20))

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.log.close()


def make_scratch() -> Path:
    s = Path(tempfile.mkdtemp(prefix="pbpp-"))
    (s / "cfg").mkdir()
    shutil.copytree(DEMO, s / "demo")
    shutil.copytree(DEMO, s / "nobom")
    a = (s / "nobom" / "assembly.yaml").read_text()           # strip the bom: list (DEMO-derived, made at runtime, never committed)
    head, _, rest = a.partition("\nbom:\n")
    tail = [ln for ln in rest.split("\n") if ln and not ln.startswith("  ")]
    (s / "nobom" / "assembly.yaml").write_text(head + "\nbom: []\n" + "\n".join(tail) + ("\n" if tail else ""))
    boms = s / "boms"
    boms.mkdir()
    (boms / "alt_bom.yaml").write_text("bom:\n" + rest.split("\n\n")[0].rstrip() + "\n")    # the DEMO bom list as a BOM file
    return s


class Page:
    def __init__(self, pg, shots):
        self.pg, self.shots = pg, shots

    def wait_js(self, expr, timeout=12000):
        self.pg.wait_for_function(expr, timeout=timeout)

    def text(self, sel="#centre"):
        return self.pg.inner_text(sel)

    def show(self, sel):
        self.pg.evaluate("s => { const e = document.querySelector(s); if (e) e.scrollIntoView({block: 'start'}); }", sel)

    def noscroll(self, label):
        r = self.pg.evaluate("""() => ({doc: document.documentElement.scrollWidth - window.innerWidth,
                                centre: document.querySelector('#centre').scrollWidth - document.querySelector('#centre').clientWidth})""")
        check(f"no horizontal scroll at {label}", all(v <= 1 for v in r.values()), str(r))

    def snap(self, state, sel=None, modal=False):
        """Screenshot the state at both widths; at 1100 px also check there is no horizontal scroll."""
        for w, h in SIZES:
            self.pg.set_viewport_size({"width": w, "height": h})
            self.pg.wait_for_timeout(250)
            self.pg.evaluate("() => { const t = document.getElementById('toast'); if (t) t.style.display = 'none'; }")
            if sel and not modal:
                self.show(sel)
                self.pg.wait_for_timeout(100)
            self.pg.screenshot(path=str(self.shots / f"{state}_{w}.png"))
            if w == 1100:
                self.noscroll(f"1100 px, {state}")
        self.pg.set_viewport_size({"width": 1500, "height": 900})
        self.pg.wait_for_timeout(150)

    def click(self, sel):
        self.pg.click(sel)
        self.pg.wait_for_timeout(200)

    def open_card(self, card):
        b = self.pg.locator(f"#pjc-{card} [data-act=pjtoggle][data-open='0']")
        if b.count():
            b.first.click()
            self.pg.wait_for_timeout(150)

    def pick_platform(self, pid):
        """The platform is a dropdown in the ECAD setup box (UIR-14)."""
        self.pg.select_option("#pjplatsel", pid)
        self.pg.wait_for_timeout(200)

    def platform_shown(self):
        return self.pg.eval_on_selector("#pjplatsel", "e => e.selectedOptions[0].textContent")

    def chips_one_line(self, label):
        r = self.pg.evaluate("""() => [...document.querySelectorAll('#pjc-doc .pjt .vd')].map(e => {
            const cs = getComputedStyle(e); const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.4;
            return {t: e.textContent, h: e.getBoundingClientRect().height, lh}; })""")
        ok = bool(r) and all(x["h"] < 1.6 * x["lh"] + 4 for x in r)      # +4: 1 px padding and 1 px border, top and bottom
        check(f"STATE chips are one line at {label}: " + ", ".join(f"{x['t']} {x['h']:.0f}px" for x in r), ok, str(r))
        return r


def part1(br, shots, scratch, port):
    srv = Server(port, scratch, scratch / "demo" / "demo.pcbv.yaml", scratch / "proj1", "B1")
    errors: list[str] = []
    try:
        ctx = br.new_context(viewport={"width": 1500, "height": 900})
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        P = Page(pg, shots)
        pg.goto(srv.base)
        pg.wait_for_selector("#pjc-folder")
        pg.check("#adv")             # the paths, the BOM file table and the cross-check rows are Advanced details (UIR-15, QW)
        pg.wait_for_timeout(300)
        # (a) existing project, adopted DEMO data folder
        txt = P.text()
        txt_flat = txt.replace("\n", "")            # a path is two spans (folder part, file name): innerText puts a break between them
        s = srv.get("api/state")["workspace"]
        check("(a) card 0: project, batch, journal, settings and protocol paths shown",
              all(p in txt_flat for p in (s["project"]["dir"], s["batch"]["dir"], s["batch"]["journal"], s["project"]["settings_file"])), txt[:600])
        check("(a) adopted in place is shown", "adopted in place" in txt)
        check("(a) Copy and Show in Finder on every path", pg.locator("#pjc-folder [data-act=pjcopy]").count() == 5
              and pg.locator("#pjc-folder [data-act=pjreveal]").count() == 5)
        check("(a) New project and Open project buttons", "New project" in txt and "Open project" in txt)
        check("(a) the order of the cards is 0 folder, 1 ECAD setup (platform, link, document), 2 BOM, 3 AI",
              pg.evaluate("[...document.querySelectorAll('.pjcard')].map(e => e.dataset.card).join(',')") == "folder,ecad,bom,ai"
              and pg.evaluate("[...document.querySelectorAll('#pjc-ecad .pjsec')].map(e => e.id).join(',')") == "pjc-plat,pjc-link,pjc-doc")
        pg.click("#pjc-folder [data-act=pjreveal] >> nth=0")
        pg.wait_for_timeout(300)
        check("(a) Show in Finder is stubbed in test mode (nothing opened)", "test mode" in pg.inner_text("#toast"), pg.inner_text("#toast"))
        P.snap("a_project_folder", ".pjpane")

        # (e) ECAD platform Fusion (saved): expand card 1
        P.open_card("plat")
        pg.wait_for_selector("#pjplatsel")
        check("(e) the ECAD setup box shows Fusion as the chosen platform (box still TO DO: no document yet)", P.platform_shown() == "Fusion"
              and pg.locator("#pjc-ecad .pjst.todo").count() == 1)
        P.snap("e_platform_fusion", "#pjc-plat")

        # (h) + Request another
        P.click("#pjc-plat [data-act=pjreqopen]")
        pg.fill("#pjreq", "Altium Designer")
        P.click("#pjc-plat [data-act=pjreqsave]")
        P.wait_js("document.querySelector('#pjc-plat .pjokline') !== null")
        t = pg.inner_text("#pjc-plat")
        req_file = scratch / "cfg" / "ecad_requests.jsonl"
        check("(h) request saved to the local file, the page says nothing is sent", "nothing is sent" in t and str(req_file) in t
              and "Altium Designer" in req_file.read_text(), t)
        check("(h) earlier requests are listed", "Requested before" in t and "Altium Designer" in t)
        P.snap("h_request_another", "#pjc-plat")

        # (i) document table: ACTIVE, then SELECTED
        P.wait_js("document.querySelector('#pjc-doc .pjt .vd') !== null")
        P.chips_one_line("1500 px (ACTIVE)")
        pg.click("#pjc-doc [data-act=pjuse]")
        P.wait_js("S && S.project && S.project.select !== null")
        pg.wait_for_timeout(300)
        P.open_card("doc")
        pg.wait_for_selector("#pjc-doc .pjt .vd >> text=SELECTED")
        check("(i) ECAD setup done after Use this document (platform, link, document); ACTIVE and SELECTED chips", pg.locator("#pjc-ecad .pjst.done").count() == 1
              and "ACTIVE" in pg.inner_text("#pjc-doc") and "SELECTED" in pg.inner_text("#pjc-doc"))
        for w, h in SIZES:
            pg.set_viewport_size({"width": w, "height": h})
            pg.wait_for_timeout(250)
            P.chips_one_line(f"{w} px (ACTIVE + SELECTED)")
        P.snap("i_document_chips", "#pjc-doc")

        # (g) platform switch after a document was selected: the document is cleared
        P.open_card("plat")
        P.pick_platform("kicad")
        pg.wait_for_selector("#pjc-doc .pjcleared", timeout=15000)
        t = pg.inner_text("#pjc-doc")
        check("(g) callout: switched to KiCad, the Fusion document was cleared, confirm again",
              "Switched to KiCad" in t and "selected in Fusion" in t and "confirm the KiCad document again" in t, t[:400])
        s = srv.get("api/state")
        check("(g) server: KiCad saved, the selection cleared", s["workspace"]["ecad"]["saved"] == "kicad" and s["project"]["select"] is None)
        P.wait_js("document.querySelector('#pjc-link .vd.pass') !== null", 15000)
        P.snap("g_platform_switch_cleared", "#pjc-link")

        # (f) KiCad selected (simulated KiCad bridge)
        P.open_card("plat")
        check("(f) KiCad is the chosen platform", P.platform_shown() == "KiCad")
        P.open_card("link")
        check("(f) the link check uses the simulated KiCad", "KiCad (simulated)" in pg.inner_text("#pjc-link"), pg.inner_text("#pjc-link")[:300])
        P.snap("f_platform_kicad", "#pjc-plat")

        # (k) BOM loaded (from the protocol)
        pg.click("#pjc-doc [data-act=pjuse]")
        P.wait_js("S && S.project && S.project.select !== null")
        check("(g) the cleared callout goes once a document is selected", pg.locator(".pjcleared").count() == 0)
        pg.wait_for_selector("#pjc-bom .facts")
        t = pg.inner_text("#pjc-bom")
        check("(k) BOM facts: file, source, size, lines, designators, fitted/DNF", "assembly.yaml" in t and "from the protocol" in t
              and "kB" in t and "9 BOM lines" in t and "18 designators" in t and "16 fitted" in t and "2 do-not-fit" in t, t[:500])
        check("(k) Remove is disabled for a protocol BOM and says why", pg.locator("#pjc-bom button[disabled]", has_text="Remove").count() == 1
              and "part of the protocol file" in t)
        check("(k) cross-check sits in card 4 under its own heading", "cross-check with the selected document" in t.lower() and "Matched" in t)
        P.snap("k_bom_loaded", "#pjc-bom")

        # (l) replace with assembly events: confirm first
        srv.post("api/cmd/placed", {"board": "S01", "designators": ["U1"], "on": True})
        pg.wait_for_timeout(400)
        P.click("#pjc-bom [data-act=bomreplace]")
        pg.wait_for_selector(".fsb")
        check("(l) Replace opens the file browser in file mode", "Choose a BOM file" in pg.inner_text(".fsb"))
        pg.fill("#fsbpath", str(scratch / "boms"))
        P.click(".fsb [data-act=fsbgo2]")
        pg.wait_for_selector(".fsb .fsbe.file >> text=alt_bom.yaml")
        P.click(".fsb .fsbe.file [data-act=fsbpick]")
        pg.wait_for_selector(".dlg h2 >> text=Replace the BOM")
        t = pg.inner_text(".dlg")
        check("(l) confirm step: n assembly events stay in the journal, replace anyway?", "assembly event" in t and "stay in the journal" in t
              and "Replace anyway" in t, t)
        P.snap("l_bom_replace_confirm", modal=True)
        P.click(".dlg button[type=submit]")
        P.wait_js("S && S.project_bom && S.project_bom.source === 'settings'")
        pg.wait_for_timeout(300)
        t = pg.inner_text("#pjc-bom")
        check("(l) after confirm: BOM chosen for this project, Remove enabled", "alt_bom.yaml" in t and "chosen for this project" in t
              and pg.locator("#pjc-bom [data-act=bomremove]:not([disabled])").count() == 1, t[:300])
        P.snap("l2_bom_replaced", "#pjc-bom")

        # (m) continue without ECAD
        P.click("[data-act=pjskip]")
        pg.fill(".dlg input[name=reason]", "Bench laptop without ECAD today")
        P.click(".dlg button[type=submit]")
        P.wait_js("S && S.project && S.project.ecad_skipped !== null")
        pg.wait_for_timeout(300)
        check("(m) the ECAD setup box is skipped with the reason, Link ECAD again offered", pg.locator("#pjc-ecad.st-skip").count() == 1
              and "Bench laptop without ECAD today" in P.text() and pg.locator("[data-act=pjunskip]").count() == 1)
        check("(m) card 4 usable (not greyed)", pg.locator("#pjc-bom fieldset[disabled]").count() == 0 and pg.locator("#pjc-bom.st-wait").count() == 0)
        check("(m) step complete callout", "Project step complete" in P.text())
        P.snap("m_continue_without_ecad", "#pjskipnote")

        # (n) AI card default text
        t = pg.inner_text("#aicard")
        check("(n) AI card (the connector's real card) in slot 5, not connected", "NOT CONNECTED" in t and "optional, not needed to continue" in t, t[:300])
        check("(n) AI card has no done marker and gates nothing", pg.locator("#aicard .pjst.done, #aicard .pjst.todo").count() == 0)
        P.snap("n_ai_card", "#aicard")
        check("part 1: no page errors", not errors, "; ".join(errors[:5]))
        ctx.close()
    finally:
        srv.stop()


def part2(br, shots, scratch, port):
    srv = Server(port, scratch, scratch / "demo" / "demo.pcbv.yaml", scratch / "proj1", "B1")
    errors: list[str] = []
    try:
        ctx = br.new_context(viewport={"width": 1500, "height": 900})
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        P = Page(pg, shots)
        pg.goto(srv.base)
        pg.wait_for_selector("#pjc-folder")
        pg.check("#adv")
        pg.wait_for_timeout(300)
        # legacy folder for (d): a batch folder with a journal, no settings file
        (scratch / "legacy" / "LB1").mkdir(parents=True)
        shutil.copy(scratch / "proj1" / "B1" / "journal.jsonl", scratch / "legacy" / "LB1" / "journal.jsonl")

        # (b) New project form, folder browser open, inspect message
        P.click("[data-act=pjnew]")
        newdir = scratch / "proj2"
        pg.fill("#pjn-dir", str(newdir))
        pg.wait_for_selector("#pjn-insp >> text=Will be created")
        P.click("[data-act=pjbrowse][data-target='new.dir']")
        pg.wait_for_selector(".fsb .fsbe")
        t = pg.inner_text(".fsb")
        check("(b) folder browser: roots, up, entries; the project folder is badged", "Up" in t and "Home" in t and "proj1" in t
              and pg.locator(".fsb .fsbe:has-text('proj1') .fsbk.project").count() == 1, t[:400])
        check("(b) inspect message under the field: will be created", "Will be created" in pg.inner_text("#pjn-insp"))
        pg.fill("#fsbsub", "proj2")
        P.snap("b_new_project_browser", modal=True)
        P.click(".fsb [data-act=fsbuse]")
        check("(b) Use this folder fills the field", pg.input_value("#pjn-dir") == str(newdir.resolve()) or pg.input_value("#pjn-dir") == str(newdir),
              pg.input_value("#pjn-dir"))
        pg.fill("#pjn-dir", str(scratch / "proj1"))
        pg.wait_for_selector("#pjn-insp >> text=Already holds a project")
        check("(b) an existing project folder is refused: create disabled", pg.locator("[data-act=pjnewgo][disabled]").count() == 1)
        P.snap("b2_new_project_exists", "#pjnewform")
        pg.fill("#pjn-dir", str(newdir))
        pg.wait_for_selector("#pjn-insp >> text=Will be created")
        pg.fill("#pjn-name", "DEMO new project")
        pg.fill("#pjn-proto", str(scratch / "nobom" / "demo.pcbv.yaml"))
        pg.fill("#pjn-batch", "NB1")
        pg.fill("#pjn-boards", "3")
        P.click("[data-act=pjnewgo]")
        P.wait_js(f"S && S.workspace && S.workspace.project.dir === {json.dumps(str(newdir.resolve()))}", 15000)
        pg.wait_for_timeout(500)
        # (c) new project created
        t = pg.inner_text("#pjc-folder")
        check("(c) card 0 shows the new folder, the batch and the settings file", str(newdir.resolve()) in t and "NB1" in t
              and "DEMO new project" in t and (newdir / "polybench-project.json").is_file(), t[:500])
        check("(c) the form is closed and the page re-probed", pg.locator("#pjnewform").count() == 0)
        check("(c) 3 boards in the new batch", len(srv.get("api/state")["boards"]) == 3)
        P.snap("c_new_project_created", "#pjc-folder")

        # (j) BOM empty (protocol without a bom: list): confirm the document so card 4 is usable
        pg.wait_for_selector("#pjc-doc [data-act=pjuse]", timeout=15000)
        pg.click("#pjc-doc [data-act=pjuse]")
        P.wait_js("S && S.project && S.project.select !== null")
        pg.wait_for_selector("#pjc-bom .bomplus")
        t = pg.inner_text("#pjc-bom")
        check("(j) BOM empty: + button and the drop area text", "Drop a BOM file here or press + to choose one" in t and ".csv or .xlsx" in t, t[:300])
        check("(j) card 4 not done", pg.locator("#pjc-bom .pjst.todo").count() == 1)
        P.snap("j_bom_empty", "#pjc-bom")

        # (k2) load with + via the file browser
        P.click("#pjc-bom .bomplus")
        pg.wait_for_selector(".fsb")
        pg.fill("#fsbpath", str(scratch / "boms"))
        P.click(".fsb [data-act=fsbgo2]")
        pg.wait_for_selector(".fsb .fsbe.file >> text=alt_bom.yaml")
        P.snap("k2_bom_choose_file", modal=True)
        P.click(".fsb .fsbe.file [data-act=fsbpick]")
        P.wait_js("S && S.project_bom && S.project_bom.source === 'settings'")
        pg.wait_for_timeout(300)
        t = pg.inner_text("#pjc-bom")
        check("(k2) BOM loaded with +: chosen for this project, no confirm (no assembly events)", "alt_bom.yaml" in t
              and "chosen for this project" in t and pg.locator(".dlg").count() == 0, t[:300])
        P.snap("k2_bom_loaded", "#pjc-bom")
        # drop: a browser drop gives a name and a size; the page asks the server where the file is (one match: loaded)
        body = (scratch / "boms" / "alt_bom.yaml").read_text()
        (newdir / "drop_bom.yaml").write_text(body)
        dt = pg.evaluate_handle("""t => { const d = new DataTransfer(); d.items.add(new File([t], 'drop_bom.yaml', {type: 'application/x-yaml'})); return d; }""", body)
        pg.dispatch_event("#bomdrop", "drop", {"dataTransfer": dt})
        P.wait_js("S && S.project_bom && S.project_bom.name === 'drop_bom.yaml'")
        pg.wait_for_timeout(300)
        check("(k2) dropped file found by name and size in the project folder and loaded", "drop_bom.yaml" in pg.inner_text("#pjc-bom"))
        P.snap("k3_bom_dropped", "#pjc-bom")

        # (d) Open a legacy folder -> adopted
        P.click("[data-act=pjopen]")
        t = pg.inner_text("#pjopenform")
        check("(d) recent projects listed in the Open form", "proj1" in t, t[:400])
        pg.fill("#pjo-dir", str(scratch / "legacy"))
        pg.wait_for_selector("#pjo-insp >> text=Adopt this folder")
        pg.fill("#pjo-proto", str(scratch / "demo" / "demo.pcbv.yaml"))       # D2: adopting a legacy folder names its protocol
        pg.wait_for_timeout(200)
        t = pg.inner_text("#pjopenform")
        check("(d) legacy: adopt label, the batch picker lists LB1, button says Adopt and open",
              "a settings file is written next to the batch folders; nothing is moved" in t and "LB1" in t and "Adopt and open" in t, t[:600])
        P.snap("d_open_legacy_form", "#pjopenform")
        P.click("[data-act=pjopengo]")
        P.wait_js(f"S && S.workspace && S.workspace.project.dir === {json.dumps(str((scratch / 'legacy').resolve()))}", 15000)
        pg.wait_for_timeout(500)
        t = pg.inner_text("#pjc-folder")
        check("(d) adopted: settings file written, batch LB1 open, badge shown", (scratch / "legacy" / "polybench-project.json").is_file()
              and "LB1" in t and "adopted in place" in t, t[:500])
        P.snap("d_legacy_adopted", "#pjc-folder")
        check("part 2: no page errors", not errors, "; ".join(errors[:5]))
        ctx.close()
    finally:
        srv.stop()


def main():
    args = [a for a in sys.argv[1:]]
    part = "all"
    if "--part" in args:
        i = args.index("--part")
        part = args[i + 1]
        del args[i:i + 2]
    shots = Path(args[0]) if args else Path("/private/tmp/claude-502/pbshots")
    shots.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PB_E2E_PORT", "8931"))
    with sync_playwright() as p:
        br = p.webkit.launch()
        try:
            if part in ("1", "all"):
                part1(br, shots, make_scratch(), port)
            if part in ("2", "all"):
                part2(br, shots, make_scratch(), port + 1)
        finally:
            br.close()
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed; screenshots in {shots}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
