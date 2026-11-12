"""Playwright WebKit walk of the powered step's single value box, Run in the main panel and Results explorer (PW-1..PW-3, PW-6), in a copy
of the packaged demo (--simulate) on its own port (never 8750).

    python tests/e2e/powered_walk.py [shots_dir] [chromium|webkit]       (port: PB_E2E_PORT, default 8914)

Walk: the Run button is in the main panel and the left panel has nothing startable; the action's reading is proposed into the single
box (source tag, verdict chip); typing overrides it ("edited (instrument said X)"), Enter accepts; the derived yes/no test arrives with
the run; the explorer is collapsed, opens, remembers its state over a reload; "Use this value" fills the box and Enter accepts it.
Light and dark. HOST-ONLY: simulator.
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
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opauth  # noqa: F401,E402

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8914"))
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


def until(fn, timeout=25.0, step=0.15):
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


def cell(b, t):
    return get("api/state")["cells"].get(f"{b}|{t}")


def make_protocol(tmp: Path) -> Path:
    d = tmp / "demo"
    shutil.copytree(ROOT / "src/polybench/examples/demo", d)
    f = d / "powered.yaml"
    s = f.read_text()
    s = s.replace("values: {P01: [22, 0.15, mA], P02: [5.0, 0.01, V], P03: [3.3, 0.01, V], P04: [14, 0.2, mA]}",
                  "values: {P01: [22, 0.15, mA], P02: [5.0, 0.01, V], P03: [3.3, 0.01, V], P04: [14, 0.2, mA]}\n              analyse: {trip_above: {P01: 40}}")
    s = s.replace("            proposes: [P01, P02, P03, P04]\n", "            record: propose\n            proposes: [P01, P02, P03, P04]\n"
                  "            derive:\n              - {test: P06, from: analysis.within_limit, map: {true: \"yes\", false: \"no\"}, note: \"no reading above its trip level\"}\n", 1)
    s = s.rstrip("\n") + "\n          - {id: P06, title: \"Current stayed under the limit, no trip\", kind: yn, limit: {class: record}, inputs: [keyboard, action]}\n"
    f.write_text(s)
    return d / "demo.pcbv.yaml"


def main(shots: Path, browser_name: str):
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-powered-e2e-"))
    proto = make_protocol(tmp)
    errors: list[str] = []
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(proto), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "POWERED", "--boards", "2", "--no-browser", "--port", str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    try:
        check("server up", until(lambda: get("api/state"), 30))
        for b in ("S01", "S02"):
            post("override_gate", {"board": b, "step": "powered", "reason": "walk: skip the unpowered step"})

        def shot(page, n):
            time.sleep(0.5)
            page.screenshot(path=str(shots / f"{browser_name}_{n}.png"))

        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)) if "/visibility" not in str(e) else None)
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "/visibility" not in m.text else None)   # the unload beacons of the reload step
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.evaluate("try { localStorage.removeItem('pb.exp.open') } catch (e) {}")
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="P01"]')
            page.wait_for_selector("#runbar")

            # PW-1: the Run button is in the main panel, nothing startable in the left panel
            check("PW-1 Run button is in the main panel", page.locator('#centre #runbar button.primary.huge[data-act="run"][data-action="read_rails"]').count() == 1)
            check("PW-1 the left panel has no run button and no Guide button", page.locator('#left [data-act="run"], #left [data-vo="guide"], #left .sec-a').count() == 0)
            check("PW-1 the Run button is the first thing in the work area", page.evaluate("document.querySelector('#centre .pane').firstElementChild.id") == "runbar")
            check("PW-1 the box is empty before the run", page.input_value("#val") == "")
            shot(page, "01_before_run")

            # PW-2: the proposal pre-fills the single box
            page.click('#runbar button[data-act="run"]')
            check("PW-1 Abort replaces Run while it runs", until(lambda: page.locator('#runbar button[data-act="abort"]').count() == 1 or (cell("S01", "P04") or {}).get("proposals")))
            check("action ran: P01 is a pending proposal, not a result", until(lambda: (cell("S01", "P01") or {}).get("proposals")) and cell("S01", "P01")["status"] == "empty")
            until(lambda: page.locator('#runbar button[data-act="run"]').count() == 1)
            until(lambda: page.input_value("#val") != "")
            prop = cell("S01", "P01")["proposals"][0]
            check("PW-2 the box is pre-filled with the instrument's value", page.input_value("#val") == prop["value"], page.input_value("#val"))
            tag = page.inner_text("#srctag").lower()
            check("PW-2 a source tag names the instrument and the run", "demo" in tag and "run 1" in tag, tag)
            check("PW-2 a verdict chip shows beside the box", page.locator("#boxnote .vd").count() == 1)
            check("PW-2 there is no second 'proposed' box for a plain value", page.locator(".propbox").count() == 0 and page.locator(".recbox").count() == 0)
            shot(page, "02_proposed_in_box")

            # overwrite, then accept
            page.fill("#val", "21.5")
            check("PW-2 typing marks the tag keyboard and says what the instrument said", until(lambda: page.inner_text("#srctag").lower() == "keyboard" and "overrides the instrument value" in page.inner_text("#boxnote")))
            shot(page, "03_overwritten")
            page.keyboard.press("Enter")
            check("PW-2 Enter recorded the typed value with source keyboard", until(lambda: (cell("S01", "P01") or {}).get("status") == "accepted") and cell("S01", "P01")["value"] == "21.5" and cell("S01", "P01")["source"]["kind"] == "keyboard")
            check("PW-2 the instrument value is in the history", [e["value"] for e in cell("S01", "P01")["edits"]] == [prop["value"]] and cell("S01", "P01")["edits"][0]["source"]["kind"] == "action")
            page.click('.trow[data-id="P01"]')
            page.wait_for_selector("#val")
            check("PW-2 the box shows the recorded value and 'edited (instrument said X)'", page.input_value("#val") == "21.5" and "instrument said" in page.inner_text("#boxnote"), page.inner_text("#boxnote"))
            shot(page, "04_accepted_edited")

            # Enter on the pre-filled box accepts the instrument's own proposal (P02)
            page.click('.trow[data-id="P02"]')
            page.wait_for_selector("#val")
            check("P02 pre-filled", page.input_value("#val") == cell("S01", "P02")["proposals"][0]["value"])
            page.keyboard.press("Enter") if page.evaluate("document.activeElement && document.activeElement.id") == "val" else (page.focus("#val"), page.keyboard.press("Enter"))
            check("Enter accepted the instrument proposal as it was (source action)", until(lambda: (cell("S01", "P02") or {}).get("status") == "accepted") and cell("S01", "P02")["source"]["kind"] == "action")

            # PW-6: P06 derived from the run's analysis
            page.click('.trow[data-id="P06"]')
            page.wait_for_selector("#val")
            p06 = cell("S01", "P06")
            check("PW-6 P06 was proposed from the analysis, not asked", p06 and (p06["proposals"] or p06["status"] == "accepted") and "within_limit" in (p06["proposals"][0]["raw"] if p06["proposals"] else p06["raw"]))
            check("PW-6 the box shows yes with the instrument tag", page.input_value("#val") == "yes" and "demo" in page.inner_text("#srctag").lower(), page.input_value("#val"))
            shot(page, "05_derived_p06")

            # PW-3: the explorer
            page.click('.trow[data-id="P01"]')
            page.wait_for_selector("#expl")
            check("PW-3 the old per-board results block is gone", page.locator("#resbox").count() == 0)
            check("PW-3 the explorer is collapsed by default, one line 'Measurements (N)'", not page.evaluate("document.querySelector('#expl').open") and "measurements (" in page.inner_text("#expl summary").lower(), page.inner_text("#expl summary"))
            until(lambda: "(…)" not in page.inner_text("#expl summary") and "(…)" not in page.inner_text("#expl summary"))
            page.click("#expl summary")
            page.wait_for_selector("#expl table.xtab")
            check("PW-3 the table lists time, action/run, channel, quantity, value, proposed to", all(h in page.inner_text("#expl thead") for h in ("Time", "Action / run", "Ch", "Quantity", "Value", "Proposed to")))
            check("PW-3 rows include samples, the derived answer and proposals", page.locator("#expl tr.k-sample").count() >= 4 and page.locator("#expl tr.k-derived").count() == 1 and page.locator("#expl tr.k-proposal").count() >= 3)
            shot(page, "06_explorer_open")
            check("PW-3 the open state is saved per step", "powered" in (page.evaluate("localStorage.getItem('pb.exp.open')") or ""))
            page.reload()
            page.wait_for_selector("#timeline .tl")
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="P01"]')
            page.wait_for_selector("#expl")
            check("PW-3 the explorer is still open after a reload", page.evaluate("document.querySelector('#expl').open"))

            # Use this value: the instrument's first P01 reading goes into the box as an override, Accept records it
            page.click('.trow[data-id="P01"]')
            page.wait_for_selector("#expl table.xtab")
            inst_val = [e for e in cell("S01", "P01")["edits"]]
            row = page.locator('#expl tr.k-sample', has_text="reading for P01").first
            sample_val = row.locator("td").nth(4).inner_text().split()[0]
            row.locator('button[data-act="expuse"]').click()
            check("PW-3 'Use this value' filled the box (still needs Accept)", until(lambda: page.input_value("#val") == sample_val) and cell("S01", "P01")["value"] == "21.5", f"{sample_val}")
            shot(page, "07_use_this_value")
            page.keyboard.press("Enter")
            check("PW-3 Accept recorded it as an override, history keeps the earlier values", until(lambda: cell("S01", "P01")["value"] == sample_val) and len(cell("S01", "P01")["edits"]) == 2, str(cell("S01", "P01")))

            # dark
            page.get_by_role("button", name="Dark").click()
            page.click('.trow[data-id="P01"]')
            page.wait_for_selector("#expl table.xtab")
            check("dark theme is on", page.evaluate("document.documentElement.dataset.theme") == "dark")
            shot(page, "08_dark_explorer")
            page.click('.bchip[data-id="S02"]')
            page.click('#runbar button[data-act="run"]')
            until(lambda: (cell("S02", "P01") or {}).get("proposals"))
            until(lambda: page.locator('#runbar button[data-act="run"]').count() == 1 and page.input_value("#val") != "")
            shot(page, "09_dark_proposed")

            # narrow
            narrow = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
            narrow.on("pageerror", lambda e: errors.append(str(e)) if "/visibility" not in str(e) else None)
            narrow.goto(BASE)
            narrow.wait_for_selector("#timeline .tl")
            narrow.click('button.tl[data-id="powered"]')
            narrow.wait_for_selector("#runbar")
            check("390 px: no horizontal page scroll", narrow.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"))
            shot(narrow, "10_390px")
            check("no page errors or HTTP errors", not errors, "; ".join(errors[:4]))
            browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="pb-powered-shots-")),
                  sys.argv[2] if len(sys.argv) > 2 else "webkit"))
