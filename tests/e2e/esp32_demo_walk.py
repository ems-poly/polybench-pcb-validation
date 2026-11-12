"""Playwright WebKit walk of the public ESP32-S3-DevKitC-1 demo with --simulate, on its own port (never 8750).

    .venv/bin/python tests/e2e/esp32_demo_walk.py [shots_dir] [chromium|webkit]     (port: PB_E2E_PORT, default 8901; viewport PB_E2E_W x PB_E2E_H, default 1400x900)

Walks: visual checklist, plan-generated unpowered step with the decision gate sending one board to Tier 2, powered readings,
then Program and function with the three plug-ins (flash, identify, serial self-test with the operator's LED answer; clean results record automatically), the held
board, and the report. HOST-ONLY: every action runs in the simulator.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8901"))
assert PORT != 8750, "8750 is the live bench server"
BASE = f"http://127.0.0.1:{PORT}/"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def post(name, body):
    req = urllib.request.Request(BASE + "api/cmd/" + name, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return {"http": e.code, **json.loads(e.read() or b"{}")}


def cell(b, t):
    c = get("api/state")["cells"].get(f"{b}|{t}")
    return bool(c and c.get("status") == "accepted")


def recorded_count():
    return sum(1 for c in get("api/state")["cells"].values() if c.get("status") == "accepted")


def until(fn, timeout=20.0, step=0.15):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def main(shots: Path, browser_name: str):
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-esp32-e2e-"))
    errors: list[str] = []
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(ROOT / "examples/esp32-s3-board/devkitc-1.pcbv.yaml"),
                            "--simulate", "--data-dir", str(tmp / "data"), "--batch", "ESP32-DEMO", "--boards", "3", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    try:
        check("server up", until(lambda: get("api/state"), 30))
        def shot(page, n):
            # a toast raised by the last action would sit over the view being documented: let it expire first (<= 5.2 s)
            try:
                page.wait_for_function("() => { const t = document.querySelector('#toast'); return !t || t.style.display === 'none' }", timeout=6000)
            except Exception:
                pass
            page.screenshot(path=str(shots / f"{browser_name}_{n}.png"))
        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            page = browser.new_context(viewport={"width": int(os.environ.get("PB_E2E_W", "1400")), "height": int(os.environ.get("PB_E2E_H", "900"))}).new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("response", lambda r: errors.append(f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")

            # The walk follows examples/esp32-s3-board/README.md literally: only typing and clicking, no API calls.
            # (The Project step shows the simulated ECAD bridge's document names, so it is not screenshotted.)
            def typed(values, delay=0.45):
                for v in values:
                    page.keyboard.type(v); page.keyboard.press("Enter"); time.sleep(delay)

            # ---- 1. Visual: y + Enter for each of the 12 tests (board by board)
            page.click('button.tl[data-id="visual"]')
            page.wait_for_selector('.trow[data-id="V01"]')
            page.click('.bchip[data-id="D01"]'); page.click('.trow[data-id="V01"]')
            shot(page, "01_visual")
            # one y at a time, each waited for: at a board boundary the app moves the cursor and a key typed too early is lost
            for i in range(12):
                for attempt in (1, 2, 3):
                    n = recorded_count()
                    page.keyboard.type("y"); page.keyboard.press("Enter")
                    if until(lambda: recorded_count() > n, 5):
                        break
                    print(f"  visual y #{i + 1}: not recorded, attempt {attempt}", flush=True)
            check("visual: 12 of 12 recorded by typing", until(lambda: all(cell(b, t) for b in ("D01", "D02", "D03") for t in ("V01", "V02", "V03", "V04"))))
            shot(page, "02_visual_done")

            # ---- 2. Unpowered, from the plan: click D01 and U01 first, then the step goes test by test across the boards
            page.click('button.tl[data-id="unpowered"]')
            page.wait_for_selector(".tier-h")
            check("unpowered is generated from the plan (Tier 1, Tier 2)", len(page.locator(".tier-h").all_inner_texts()) == 2)
            page.click('.bchip[data-id="D01"]'); page.click('.trow[data-id="U01"]')
            shot(page, "03_unpowered_plan")
            typed(["12", "11", "14"])             # U01 on D01, D02, D03
            typed(["3.2", "3.0"])                 # U02 on D01, D02
            typed(["0.3"])                        # D03: below the guess limit, so Enter does not accept it
            check("0.3 kOhm asks for review ('Accept for review' button)", page.get_by_role("button", name="Accept for review").is_visible())
            shot(page, "03b_out_of_band")
            page.get_by_role("button", name="Accept for review").click(); time.sleep(0.6)
            typed(["Fluke 117"] * 3)              # U03
            check("unpowered: 9 of 9 recorded by typing", until(lambda: all(cell(b, t) for b in ("D01", "D02", "D03") for t in ("U01", "U02", "U03"))))
            check("D03 U02 is the typed 0.3", get("api/state")["cells"]["D03|U02"]["value"] in (0.3, "0.3"), str(get("api/state")["cells"]["D03|U02"]))
            page.click('.trow.gate')
            page.wait_for_selector("table.gatet")
            rows = {tr.locator("td").nth(0).inner_text(): tr.inner_text() for tr in page.locator("table.gatet tr").all()[1:]}
            check("gate sends D03 to Tier 2 and clears D01, D02", "TIER 2" in rows["D03"] and "SKIPPED" in rows["D01"] and "SKIPPED" in rows["D02"], str(rows))
            shot(page, "04_gate")
            page.click('table.gatet button[data-act=gateacc]')
            check("gate Accept journals the Tier 2 assignment", until(lambda: "D03|unpowered" in get("api/state")["tier2"]))

            # ---- 3. Powered: D01 and D02 typed, D03 is held
            page.click('button.tl[data-id="powered"]')
            page.wait_for_selector('.trow[data-id="P01"]')
            for b, vals in (("D01", ["62", "3.31", "4.98", "y"]), ("D02", ["58", "3.30", "5.02", "y"])):
                page.click(f'.bchip[data-id="{b}"]'); page.click('.trow[data-id="P01"]')
                typed(vals)
            shot(page, "05_powered")
            check("powered: D01 and D02 recorded by typing", until(lambda: all(cell(b, t) for b in ("D01", "D02") for t in ("P01", "P02", "P03", "P04"))))
            page.click('.bchip[data-id="D03"]')
            check("D03 is held by the power interlock", until(lambda: "interlock" in page.inner_text("#centre").lower()), page.inner_text("#centre")[:200])
            shot(page, "06_interlock_held")

            # ---- Program and function on D01
            page.click('button.tl[data-id="function"]')
            page.wait_for_selector('button[data-act=run]')
            page.click('.bchip[data-id="D01"]')
            page.click('button[data-act=run][data-action=flash]')
            page.wait_for_selector("#runok")
            shot(page, "07_flash_prompt")
            page.click("#runok")
            # Clean action results are recorded automatically ("recorded automatically" on the test): there is nothing to accept.
            check("flash: F01 and F02 recorded automatically", until(lambda: cell("D01", "F01") and cell("D01", "F02"), 20))
            check("flash: no proposal bar and no 'Accept all clean' button", page.locator('[data-act=acceptall]').count() == 0 and page.locator("#propbar").count() == 0)
            shot(page, "08_flash_recorded")
            # the Run button belongs to the test in focus: click the first identify row, then run it
            page.click('.trow[data-id="F03"]')
            until(lambda: page.locator('button[data-act=run][data-action=identify]:not([disabled])').count() == 1)
            page.click('button[data-act=run][data-action=identify]')
            until(lambda: "run 2 ok" in page.inner_text("body"), 20)
            check("identify: F03, F05, F06 recorded automatically", until(lambda: all(cell("D01", t) for t in ("F03", "F05", "F06")), 20))
            check("identify: no 'Accept all clean' button after clicking F03", page.locator('[data-act=acceptall]').count() == 0)
            shot(page, "09_identify_recorded")
            page.click('.trow[data-id="F07"]')
            until(lambda: page.locator('button[data-act=run][data-action=selftest]:not([disabled])').count() == 1)
            page.click('button[data-act=run][data-action=selftest]')
            page.wait_for_selector('.dlg [data-opt="Yes"]', timeout=20000)
            check("operator is asked about the LED", "RGB LED" in page.inner_text(".dlg"))
            shot(page, "10_led_question")
            page.click('.dlg [data-opt="Yes"]')
            check("self-test run finished", until(lambda: "run 3 ok" in page.inner_text("body"), 20))
            check("self-test: no 'Accept all clean' button after clicking F07", page.locator('[data-act=acceptall]').count() == 0)
            shot(page, "11_selftest_recorded")
            check("all D01 function tests recorded without a click", until(lambda: all(cell("D01", t) for t in ("F01", "F03", "F05", "F07", "F09", "F10")), 20))
            shot(page, "12_function_done")

            # ---- D02 through the API-driven actions is not needed; report for what exists
            page.click('button.tl[data-id="report"]')
            page.wait_for_selector("#centre")
            time.sleep(1.0)
            shot(page, "13_report")
            check("report shows the simulated label", "SIMULATED" in page.inner_text("body").upper())
            browser.close()
        # Enter on an out-of-band value is answered 409 by the server (it wants "Accept for review"): exactly one, expected
        expected = [e for e in errors if "409 POST" in e and "/api/cmd/entry" in e or "status of 409" in e]
        other = [e for e in errors if e not in expected]
        check("exactly one expected 409 (out-of-band Enter) and no other console or page errors", len(expected) == 2 and not other, "; ".join(errors[:4]))
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PB_E2E_SHOTS") or tempfile.mkdtemp(prefix="pb-esp32-shots-")), sys.argv[2] if len(sys.argv) > 2 else "webkit"))
