"""First-run welcome page, demo project and Plugins tab: a walk in Playwright WebKit. Typing and clicking only.

    PB_E2E_PORT=8960 .venv/bin/python tests/e2e/welcome_walk.py [shots_dir]

Server A starts on a fresh HOME and config folder (first run), B likewise. Both run --simulate so the Project page's ECAD link check
talks to the simulated bridge, never to a real ECAD program. Everything lives in a scratch folder; the demo protocol is copied there.
Screenshots go to <shots_dir>/<state>.png (default /private/tmp/claude-502/pbshots-welcome, never committed). Exit code 1 on a failed check.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "src" / "polybench" / "examples" / "demo"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class Server:
    def __init__(self, scratch: Path, port: int, recent: list | None = None):
        home = scratch / "home"
        home.mkdir()
        if recent is not None:                 # a recent list written before the first start
            (home / "cfg").mkdir()
            (home / "cfg" / "recent.json").write_text(json.dumps({"schema": 1, "projects": recent}), encoding="utf-8")
        env = dict(os.environ, HOME=str(home), POLYBENCH_CONFIG_DIR=str(home / "cfg"), POLYBENCH_DATA=str(scratch / "data"),
                   POLYBENCH_DEMO_DIR=str(scratch / "demo-project"), POLYBENCH_NO_REVEAL="1", POLYBENCH_STATE_DIR=str(scratch / "state"))
        exe = str(Path(sys.executable).parent / "polybench")
        self.log = open(scratch / f"server_{port}.log", "w", encoding="utf-8")
        self.p = subprocess.Popen([exe, "--simulate", "--port", str(port), "--no-browser"], env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.base = f"http://127.0.0.1:{port}/"
        import time
        for _ in range(60):
            try:
                urllib.request.urlopen(self.base + "api/state", timeout=2).read()
                return
            except OSError:
                time.sleep(0.25)
        self.stop()
        raise RuntimeError("server did not start: " + (scratch / f"server_{port}.log").read_text(encoding="utf-8")[-600:])

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path, timeout=20))

    def stop(self):
        self.p.terminate()                 # our own PID only
        try:
            self.p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.log.close()


class Walk:
    def __init__(self, browser, srv, shots: Path, theme="light", size=(1100, 800)):
        self.srv, self.shots = srv, shots
        self.pg = browser.new_page(viewport={"width": size[0], "height": size[1]})
        self.errors: list[str] = []
        self.pg.on("pageerror", lambda e: self.errors.append("pageerror: " + str(e)))
        self.pg.on("response", lambda r: self.errors.append(f"http {r.status} {r.request.method} {r.url}") if r.status in (409, 500) else None)
        self.pg.on("console", lambda m: self.errors.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
        self.pg.add_init_script(f"try{{localStorage.setItem('pbdv.theme','\"{theme}\"')}}catch(e){{}}")

    def snap(self, name):
        self.pg.wait_for_timeout(300)
        self.pg.evaluate("() => { const t = document.getElementById('toast'); if (t) t.style.display = 'none'; }")
        self.pg.screenshot(path=str(self.shots / f"{name}.png"))
        print("shot", name)

    def sized(self, w, h):
        self.pg.set_viewport_size({"width": w, "height": h})
        self.pg.wait_for_timeout(250)

    def no_hscroll(self, label):
        d = self.pg.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        check(f"no horizontal page scroll: {label}", d <= 1, str(d))

    def text(self, sel="body"):
        return self.pg.inner_text(sel)


def main() -> int:
    port = int(os.environ.get("PB_E2E_PORT", "8960"))
    assert port != 8750, "8750 is the live bench server"
    shots = Path(sys.argv[1] if len(sys.argv) > 1 else "/private/tmp/claude-502/pbshots-welcome")
    shots.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="pbwel-"))
    shutil.copytree(DEMO, scratch / "proto")
    sa = sb = sc = None
    try:
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            # ---------------- A: first run, welcome, New project form, create, demo from a project, plugins, back
            (scratch / "a").mkdir()
            sa = Server(scratch / "a", port)
            w = Walk(browser, sa, shots)
            w.pg.goto(sa.base)
            w.pg.wait_for_selector("#welcome")
            t = w.text("#centre")
            check("W1 first run opens the welcome page, no error", "No project open" in w.text("#appbar") and "+ New project" in t)
            tl = t.lower()
            order = [tl.index(x.lower()) for x in ("+ New project", "Open project", "Try the demo project", "How Polybench works")]
            check("W2/W3 order: New project, Open project, demo link, timeline", order == sorted(order), str(order))
            check("W2 no recent list on a fresh HOME", "recent projects" not in t.lower())
            check("W3 twelve stages, two flagged not built, one preview", w.pg.locator(".onb-st").count() == 12 and w.pg.locator(".onb-st.unbuilt").count() == 2 and w.pg.locator(".onb-st.preview").count() == 1)
            w.no_hscroll("welcome 1100")
            w.snap("01_welcome_light_1100")
            w.pg.evaluate("() => { document.querySelector('[data-act=theme]').click(); }")
            w.snap("02_welcome_dark_1100")
            w.sized(390, 844)
            w.no_hscroll("welcome 390 dark")
            w.snap("03_welcome_dark_390")
            w.pg.evaluate("() => { document.querySelector('[data-act=theme]').click(); }")
            w.snap("04_welcome_light_390")
            w.sized(1100, 800)
            w.pg.click("#welnew")
            w.pg.wait_for_selector("#pjnewform")
            check("W2 + New project opens the existing New project form", w.pg.locator("#pjn-dir").count() == 1)
            check("W2 Create is disabled until a protocol is named", w.pg.locator("[data-act=pjnewgo]").is_disabled())
            w.snap("05_new_project_form")
            w.pg.fill("#pjn-dir", str(scratch / "myproject"))
            w.pg.fill("#pjn-name", "My first project")
            w.pg.fill("#pjn-proto", str(scratch / "proto" / "demo.pcbv.yaml"))
            w.pg.wait_for_function("() => !document.querySelector('[data-act=pjnewgo]').disabled", timeout=8000)
            w.snap("06_new_project_filled")
            # Plugins tab on the welcome page (no project): no protocol uses them
            w.pg.click("#bottom .btab-plugins")
            w.pg.wait_for_selector(".pl-tab")
            check("P1 chip opens the Plugins tab on the welcome page", "No project is open" in w.text("#bbody"))
            w.snap("07_plugins_no_project")
            w.pg.click("[data-act=toggleBottom]")
            w.pg.click("[data-act=pjnewgo]")
            w.pg.wait_for_selector(".pjpane")
            check("W1 creating a project leaves the welcome page", w.pg.locator("#welcome").count() == 0 and "My first project" in w.text("#appbar"))
            check("W4 How Polybench works is collapsed on the Project page", w.pg.locator("#onbhow .onb-body").count() == 0)
            w.pg.wait_for_timeout(600)
            w.snap("08_project_page_how_collapsed")
            w.pg.click("[data-act=onbtoggle]")
            w.pg.wait_for_selector("#onbhow .onb-body")
            w.pg.evaluate("() => document.getElementById('onbhow').scrollIntoView({block: 'start'})")
            check("W4 the same timeline opens on the Project page", w.pg.locator("#onbhow .onb-st").count() == 12)
            w.snap("09_project_page_how_open")
            # demo from a project: SIMULATED visible, own folder, back to my project
            before = (scratch / "a" / "data").exists() and any((scratch / "a" / "data").rglob("*"))
            w.pg.evaluate("() => document.querySelector('#centre').scrollTo(0, 0)")
            w.pg.click("[data-act=demoopen]")
            w.pg.wait_for_selector(".badge.demo")
            w.pg.wait_for_timeout(500)
            bar = w.text("#appbar")
            check("W5 demo shows SIMULATED and DEMO PROJECT and a way back", "SIMULATED" in bar and "DEMO PROJECT" in bar and "Back to my project" in bar, bar)
            st = sa.get("api/state")
            check("W5 demo folder is its own and simulated", str(scratch / "a" / "demo-project") in st["meta"]["dir"] and st["origin"] == "simulated")
            w.snap("10_demo_project_page")
            w.pg.click('[data-act="step"][data-id="powered"]')
            w.pg.wait_for_timeout(400)
            check("W5 SIMULATED badge stays on another step", "SIMULATED" in w.text("#appbar") and "Back to my project" in w.text("#appbar"))
            w.snap("11_demo_powered_step")
            # run a demo action so the Plugins tab has a last result
            # the powered step is gated behind the unpowered tests; the demo (simulated, own journal) overrides the gate with a reason
            w.pg.evaluate("""async () => { const post = (u, b) => fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
                await post('/api/cmd/override_gate', {board: 'S01', step: 'powered', reason: 'demo walk'});
                await post('/api/action/run', {step: 'powered', section: 'first_power', action: 'read_rails', board: 'S01', confirmed: true}); }""")
            w.pg.wait_for_timeout(2500)
            w.pg.click("#bottom .btab-plugins")
            w.pg.wait_for_selector(".pl-tab")
            w.pg.wait_for_timeout(500)
            body = w.text("#bbody")
            check("P2 columns", all(c in body for c in ("PLUGIN", "SOURCE", "STATUS", "USED BY THIS PROTOCOL", "LAST RESULT")) or all(c.lower() in body.lower() for c in ("Plugin", "Source", "Status", "Used by this protocol", "Last result")))
            check("P2 demo.measure is used and has a result", w.pg.locator("tr.pl-r:has-text('demo.measure') .vd").count() == 1, w.text("tr.pl-r:has-text('demo.measure')"))
            if "missing extra" in body:          # only in an install without every extra (the full venv has none to show)
                check("P2 a missing extra shows the pip command", 'pip install "polybench-pcb-validation[' in body)
            check("P2 not-run plugin reads not run", "not run" in body)
            w.pg.evaluate("() => { const b = document.querySelector('#bbody'); b.scrollTop = 0; }")
            w.snap("12_plugins_demo_loaded")
            w.pg.click("tr.pl-r:has-text('demo.measure') [data-act=plset]")
            w.pg.wait_for_selector(".pl-set")
            check("P3 settings are shown read-only with their file", "powered.yaml" in w.text(".pl-set") and "values" in w.text(".pl-set") and w.pg.locator(".pl-set input, .pl-set textarea").count() == 0)
            w.pg.evaluate("() => { const r = document.querySelector('.pl-set'); r.scrollIntoView({block: 'center'}); }")
            w.snap("13_plugins_settings_open")
            w.pg.click("[data-act=toggleBottom]")
            w.pg.click("[data-act=democlose]")
            w.pg.wait_for_function("() => !document.querySelector('.badge.demo')", timeout=8000)
            w.pg.wait_for_timeout(600)
            check("W5 back to my project: project name, no demo badge", "My first project" in w.text("#appbar") and "DEMO PROJECT" not in w.text("#appbar"))
            w.snap("14_back_to_my_project")
            after = any((scratch / "a" / "data").rglob("*")) if (scratch / "a" / "data").exists() else False
            check("W5 nothing written to the data folder by the demo", before == after)
            check("no JS errors in walk A", not w.errors, "; ".join(w.errors[:5]))
            # reload: the project is what `polybench` reopens next time
            w.pg.reload()
            w.pg.wait_for_selector(".pjpane")
            check("W1 reload keeps the project", "My first project" in w.text("#appbar"))
            # ---------------- B: fresh again, welcome -> demo -> back to welcome
            (scratch / "b").mkdir()
            sb = Server(scratch / "b", port + 1)
            w2 = Walk(browser, sb, shots)
            w2.pg.goto(sb.base)
            w2.pg.wait_for_selector("#welcome")
            w2.pg.click("[data-act=demoopen]")
            w2.pg.wait_for_selector(".badge.demo")
            w2.pg.wait_for_timeout(500)
            bar = w2.text("#appbar")
            check("W5 demo from welcome: SIMULATED, DEMO PROJECT, Back to welcome", "SIMULATED" in bar and "Back to welcome" in bar, bar)
            w2.snap("15_demo_from_welcome")
            w2.pg.click("[data-act=democlose]")
            w2.pg.wait_for_selector("#welcome")
            check("W5 back lands on the welcome page", "No project open" in w2.text("#appbar"))
            check("W5 demo is not in the recent list", "recent projects" not in w2.text("#centre").lower())
            w2.snap("16_back_to_welcome")
            check("no JS errors in walk B", not w2.errors, "; ".join(w2.errors[:5]))
            # ---------------- C: a recent list whose project folder is gone: welcome page, entry marked missing, removable
            (scratch / "c").mkdir()
            sc = Server(scratch / "c", port + 2, recent=[{"dir": str(scratch / "c" / "gone"), "name": "Old board", "batch": "B1", "opened": "2026-10-01T09:00:00"}])
            w3 = Walk(browser, sc, shots)
            w3.pg.goto(sc.base)
            w3.pg.wait_for_selector("#welcome")
            check("W1 a recent list with no usable project starts on the welcome page", "No project open" in w3.text("#appbar"))
            check("W2 recent projects are listed, a missing one is marked", "recent projects" in w3.text("#centre").lower() and "missing" in w3.text(".wel-recm"))
            w3.snap("17_welcome_recent_missing")
            w3.pg.click("[data-act=pjrecrm]")
            w3.pg.wait_for_function("() => !document.querySelector('.wel-recm')", timeout=6000)
            check("W2 Remove from list clears it", "recent projects" not in w3.text("#centre").lower())
            # Open project... from the welcome page on a folder that holds a project (made beforehand with the library, simulated)
            sys.path.insert(0, str(ROOT / "src"))
            from polybench import workspace as wslib
            from polybench.protocol import load
            from polybench.server import Session
            existing = scratch / "c" / "existing"
            ws = wslib.Workspace.create(existing, "Existing project", str(scratch / "proto" / "demo.pcbv.yaml"), "fusion", "B1")
            Session(load(scratch / "proto" / "demo.pcbv.yaml"), ws.dir, "B1", True, 3, "fusion", "", workspace=ws).journal.close()
            w3.pg.click("[data-act=pjopen]")
            w3.pg.wait_for_selector("#pjopenform")
            w3.pg.fill("#pjo-dir", str(existing))
            w3.pg.wait_for_function("() => /Polybench project/.test(document.getElementById('pjo-insp').innerText)", timeout=8000)
            w3.snap("18_welcome_open_form")
            w3.pg.click("[data-act=pjopengo]")
            w3.pg.wait_for_selector(".pjpane")
            check("W2 Open project... opens an existing project from the welcome page", "Existing project" in w3.text("#appbar"))
            check("no JS errors in walk C", not w3.errors, "; ".join(w3.errors[:5]))
            browser.close()
    finally:
        for s in (sa, sb, sc):
            if s:
                s.stop()
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"{len(FAILS)} failed" if FAILS else "all checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
