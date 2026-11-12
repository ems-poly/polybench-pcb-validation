"""Playwright WebKit walk of the instrument-result flow in the packaged demo (--simulate), on its own port (never 8750).

    python tests/e2e/results_walk.py [shots_dir] [chromium|webkit]       (port: PB_E2E_PORT, default 8912)

Asserts, without ever clicking Accept: an action's readings are recorded as results; an out-of-band reading raises the Anomalies strip with
its reason; a table result renders with its out-of-band rows and plot; Mark fail, Accept as-is with a note and Retest work; an action the
protocol opts out of (record: propose) still shows "Proposed" with Accept and Reject. Also 390 px and dark mode. HOST-ONLY: simulator.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opauth  # noqa: F401,E402  (operator session cookie, tests/e2e/opauth.py)

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8912"))
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


def main(shots: Path, browser_name: str):
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-results-e2e-"))
    errors: list[str] = []
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"),
                            "--simulate", "--data-dir", str(tmp / "data"), "--batch", "RESULTS", "--boards", "3", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    try:
        check("server up", until(lambda: get("api/state"), 30))
        for b in ("S01", "S02", "S03"):
            post("override_gate", {"board": b, "step": "powered", "reason": "walk: skip the unpowered step"})

        def shot(page, n):
            try:
                page.wait_for_function("() => { const t = document.querySelector('#toast'); return !t || t.style.display === 'none' }", timeout=6000)
            except Exception:
                pass
            page.evaluate("document.querySelectorAll('*').forEach(e => { if (e.scrollLeft) e.scrollLeft = 0 }); window.scrollTo(0, window.scrollY)")
            time.sleep(0.4)           # the flash has faded
            page.screenshot(path=str(shots / f"{browser_name}_{n}.png"))

        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click('button.tl[data-id="powered"]')
            page.wait_for_selector('.trow[data-id="P01"]')
            page.click('.trow[data-id="P01"]')
            shot(page, "01_before_run")

            # ---- S01: clean readings, recorded by the action with no click on Accept
            page.click('button[data-act="run"][data-action="read_rails"]')
            check("S01: P01..P04 recorded by the action alone", until(lambda: all((cell("S01", t) or {}).get("status") == "accepted" for t in ("P01", "P02", "P03", "P04"))))
            c = cell("S01", "P01")
            check("S01 P01 is an auto-recorded instrument result", c["auto"] and c["source"]["kind"] == "action" and c["source"].get("input_method") == "instrument", str(c))
            check("no pending proposal anywhere on S01", not any(x["proposals"] for k, x in get("api/state")["cells"].items() if k.startswith("S01|")))
            page.click('.trow[data-id="P01"]')
            page.wait_for_selector("#expl")
            check("19 Oct (PW-3): the per-board results block is replaced by the collapsed explorer", page.locator("#resbox").count() == 0 and "measurements (" in page.inner_text("#expl summary").lower())
            check("the single box holds the recorded P01 value with its chip and instrument tag", page.input_value("#val") == cell("S01", "P01")["value"] and page.locator("#boxnote .vd.pass").count() == 1 and "demo" in page.inner_text("#srctag").lower())
            check("no Accept button is offered for a recorded instrument value", page.get_by_role("button", name="Accept", exact=True).count() == 0)
            shot(page, "02_s01_recorded")

            # ---- S02: P04 is above its guess limit -> REVIEW, listed in the strip with the reason
            page.click('.bchip[data-id="S02"]')
            page.click('button[data-act="run"][data-action="read_rails"]')
            check("S02: P04 recorded as REVIEW without Accept", until(lambda: (cell("S02", "P04") or {}).get("verdict") == "review"))
            until(lambda: page.locator("#anoms .anrow").count() >= 1)
            txt = page.inner_text("#anoms")
            check("Anomalies strip names P04 with its reason", "P04" in txt and "outside" in txt and "guess limit" in txt, txt)
            check("strip count badge shows 1", page.locator("#anoms h3 .no").inner_text().strip() == "1")
            shot(page, "03_s02_anomaly_review")

            # ---- S03: P02 above the hard limit -> FAIL; sweep table out of band
            page.click('.bchip[data-id="S03"]')
            page.click('button[data-act="run"][data-action="read_rails"]')
            check("S03: P02 recorded as FAIL", until(lambda: (cell("S03", "P02") or {}).get("verdict") == "fail"))
            until(lambda: get("api/state").get("action") is None)
            page.click('button[data-act="run"][data-action="load_sweep"]')
            check("S03: sweep recorded as a table", until(lambda: isinstance((cell("S03", "P05") or {}).get("value"), dict)))
            c5 = cell("S03", "P05")
            check("table cell verdict is the worst row (FAIL) with per-row verdicts", c5["verdict"] == "fail" and [r["verdict"] for r in c5["row_verdicts"]].count("fail") >= 1, str(c5["row_verdicts"]))
            until(lambda: "P05" in page.inner_text("#anoms"))
            page.click('#anoms .anjump[data-id="P05"]')
            page.wait_for_selector(".recbox .rtable")
            check("table cell renders rows and a plot", page.locator(".recbox .rtable tbody tr").count() == 5 and page.locator(".recbox svg.tplot").count() == 1)
            check("out-of-band rows are marked", page.locator(".recbox .rtable td.out").count() >= 1)
            shot(page, "04_s03_table_fail")
            page.click('.trow[data-id="P02"]')
            shot(page, "05_s03_anomalies_fail")

            # ---- anomalies inform, they do not gate: no acknowledgement exists, and the operator can simply move on
            page.click('.bchip[data-id="S02"]')
            page.click('#anoms button[data-act="an-note"]')
            page.wait_for_selector("#notes-root, .ncside, .dlg, textarea", timeout=8000)
            page.keyboard.press("Escape")
            check("no Accept / Mark fail / Accept as-is control exists on an anomaly", page.locator("#anoms button", has_text="Accept").count() == 0 and page.locator("#anoms button", has_text="Mark fail").count() == 0)
            check("an unacknowledged anomaly does not block the next board", page.locator('.bchip[data-id="S03"]').is_enabled() and not [e for e in get("api/journal?limit=5000") if e["type"] in ("disposition",)])
            events = get("api/journal?limit=5000")
            check("the journal holds auto accept events", sum(1 for e in events if e["type"] == "accept" and e["payload"].get("auto")) >= 9)
            check("no 'accepted by operator' event was journalled for any auto result", all(e["payload"].get("auto") or not e["payload"].get("proposal_id") or e["payload"]["source"].get("kind") != "action" for e in events if e["type"] == "accept"))

            # ---- re-running an action over a recorded value replaces THAT cell in the same attempt; the rest of the step keeps its results
            page.click('.bchip[data-id="S01"]')
            before = cell("S01", "P01")
            n_rt = len([e for e in get("api/journal?limit=5000") if e["type"] == "retest_open"])
            page.click('button[data-act="run"][data-action="read_rails"]')
            until(lambda: get("api/state").get("action") is None and len((cell("S01", "P01") or {}).get("edits") or []) >= 1)
            now = cell("S01", "P01")
            check("a second run over a recorded value stays in the same attempt", now["attempt"] == before["attempt"] == 1)
            check("the earlier value is in the cell history", [h["value"] for h in now["edits"]] == [before["value"]], str(now["edits"]))
            check("no retest_open was journalled", len([e for e in get("api/journal?limit=5000") if e["type"] == "retest_open"]) == n_rt)
            check("the other tests of the step keep their results", all(cell("S01", t)["status"] == "accepted" and cell("S01", t)["attempt"] == 1 for t in ("P01", "P02", "P03", "P04")))
            page.click('.trow[data-id="P01"]')
            shot(page, "06_s01_remeasured_history")

            # ---- record: propose keeps the old behaviour
            page.click('button.tl[data-id="function"]')
            page.wait_for_selector('.trow[data-id="F03"]')
            for b in ("S01", "S02", "S03"):
                post("override_gate", {"board": b, "step": "function", "reason": "walk"})
            page.click('.bchip[data-id="S01"]'); page.click('.trow[data-id="F03"]')
            page.click('button[data-act="run"][data-action="heartbeat"]')
            check("record: propose leaves a pending proposal", until(lambda: (cell("S01", "F03") or {}).get("proposals") and cell("S01", "F03")["status"] == "empty"))
            page.wait_for_selector("#boxnote button[data-act=rejectprop]")
            check("the proposal is in the single box, marked proposed, with Accept and Reject", "proposed" in page.inner_text("#boxnote").lower() and page.input_value("#val") == cell("S01", "F03")["proposals"][0]["value"] and page.locator("button[data-act=entrygo]").count() == 1 and page.locator(".propbox").count() == 0)
            shot(page, "08_pending_proposal")
            page.click("button[data-act=entrygo]")
            check("Accept turns the proposal into a result", until(lambda: (cell("S01", "F03") or {}).get("status") == "accepted"))

            # ---- narrow and dark
            narrow = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
            narrow.on("pageerror", lambda e: errors.append(str(e)))
            narrow.goto(BASE)
            narrow.wait_for_selector("#timeline .tl")
            narrow.click('button.tl[data-id="powered"]')
            narrow.click('.bchip[data-id="S03"]')
            narrow.wait_for_selector("#anoms")
            check("390 px: no horizontal page scroll", narrow.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"),
                  str(narrow.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")))
            shot(narrow, "09_390px_anomalies")
            narrow.click('#anoms .anjump[data-id="P05"]')
            narrow.wait_for_selector(".recbox .rtable")
            narrow.evaluate("document.querySelector('.recbox').scrollIntoView()")
            shot(narrow, "10_390px_table")
            narrow.click('.bchip[data-id="S01"]')
            narrow.evaluate("(document.querySelector('#expl') || document.querySelector('#resbox')).scrollIntoView()")
            shot(narrow, "11_390px_results")
            narrow.get_by_role("button", name="Dark").click()
            narrow.click('.bchip[data-id="S03"]')
            narrow.evaluate("window.scrollTo(0,0); document.querySelector('#centre').scrollTop = 0")
            shot(narrow, "12_dark_390px")
            page.get_by_role("button", name="Dark").click()
            page.click('button.tl[data-id="powered"]')
            page.click('.bchip[data-id="S03"]')
            page.click('.trow[data-id="P05"]')
            time.sleep(0.4)
            check("dark theme is on", page.evaluate("document.documentElement.dataset.theme") == "dark")
            shot(page, "13_dark_table")
            # ---- Redo on a test with an action: the action runs again for this board, the cell is replaced in the same attempt
            page.click('.bchip[data-id="S02"]')
            page.click('.trow[data-id="P04"]')
            page.wait_for_selector('#anoms button[data-act="an-retest"]')
            others = {t: cell("S02", t)["value"] for t in ("P01", "P02", "P03")}
            n_rt = len([e for e in get("api/journal?limit=5000") if e["type"] == "retest_open"])
            check("Redo's tooltip names the action", "again on S02" in page.locator('#anoms button[data-act="an-retest"]').first.get_attribute("title"))
            shot(page, "14_redo_before")
            page.click('#anoms button[data-act="an-retest"] >> nth=0')
            check("Redo re-ran the action: P04 has a history entry, same attempt", until(lambda: len((cell("S02", "P04") or {}).get("edits") or []) >= 1 and cell("S02", "P04")["attempt"] == 1))
            until(lambda: get("api/state").get("action") is None)
            check("Redo opened no retest and left P01..P03 as they were", len([e for e in get("api/journal?limit=5000") if e["type"] == "retest_open"]) == n_rt
                  and all(cell("S02", t)["attempt"] == 1 and cell("S02", t)["status"] == "accepted" for t in others))
            shot(page, "15_redo_after_action")

            # ---- Redo on a typed test (no action): the entry box clears, the next value replaces the cell
            post("override_gate", {"board": "S03", "step": "unpowered", "reason": "walk: skip the visual step"})
            page.click('button.tl[data-id="unpowered"]')
            page.click('.bchip[data-id="S03"]')
            check("typed U02 below its guess limit is recorded for review", post("entry", {"board": "S03", "test_id": "U02", "value": "1.5", "confirm": True}).get("ok"))
            page.click('.trow[data-id="U02"]')
            page.wait_for_selector('#anoms button[data-act="an-retest"]')
            check("typed test: Redo says it clears the entry box", "Clear the entry box" in page.locator('#anoms button[data-act="an-retest"]').first.get_attribute("title"))
            shot(page, "16_redo_typed_before")
            page.click('#anoms button[data-act="an-retest"] >> nth=0')
            page.wait_for_selector("#val")
            check("Redo cleared the entry box and focused it", page.input_value("#val") == "" and page.evaluate("document.activeElement && document.activeElement.id") == "val")
            shot(page, "17_redo_typed_cleared")
            page.fill("#val", "3.2")
            page.keyboard.press("Enter")
            check("the typed value replaced the cell in the same attempt, history kept", until(lambda: (cell("S03", "U02") or {}).get("value") in ("3.2", 3.2) and [h["value"] for h in cell("S03", "U02")["edits"]] in (["1.5"], [1.5])), str(cell("S03", "U02")))
            check("typed Redo opened no retest", cell("S03", "U02")["attempt"] == 1 and len([e for e in get("api/journal?limit=5000") if e["type"] == "retest_open"]) == n_rt)
            page.click('.trow[data-id="U02"]')
            shot(page, "18_redo_typed_after")
            check("no page errors or HTTP errors", not errors, "; ".join(errors[:4]))
            browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            pass
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="pb-results-shots-")),
                  sys.argv[2] if len(sys.argv) > 2 else "webkit"))
