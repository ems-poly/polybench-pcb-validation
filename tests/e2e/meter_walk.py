"""Playwright WebKit walk of the live multimeter card on a simulated meter (T6.8, R9, R10, R13), in a copy of the packaged demo
(--simulate) on its own port (never 8750).

    python tests/e2e/meter_walk.py [shots_dir] [webkit|chromium]       (port: PB_E2E_PORT, default 8921)

States shot: connected and live with a proposal, OL, not proposed with a reason (HOLD), quantity mismatch, auto-accept armed, auto-accepted
with Undo (focus moved on), undone, lost (cable unplugged), dark theme, a narrow window. HOST-ONLY: simulator.
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
PORT = int(os.environ.get("PB_E2E_PORT", "8921"))
assert PORT != 8750, "8750 is the live bench server"
BASE = f"http://127.0.0.1:{PORT}/"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return {"http": e.code, **json.loads(e.read() or b"{}")}


def until(fn, timeout=15.0, step=0.1):
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
    tmp = Path(tempfile.mkdtemp(prefix="pb-meter-e2e-"))
    shutil.copytree(ROOT / "src/polybench/examples/demo", tmp / "demo")
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(tmp / "demo/demo.pcbv.yaml"), "--simulate",
                            "--data-dir", str(tmp / "data"), "--batch", "METER", "--boards", "2", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    errors: list[str] = []
    try:
        check("server up", until(lambda: get("api/state"), 30))
        for b in ("S01", "S02"):
            post("api/cmd/override_gate", {"board": b, "step": "unpowered", "reason": "walk: skip the visual step"})
        scene = lambda **k: post("api/meter/sim", k)          # noqa: E731

        def shot(page, n):
            time.sleep(0.4)
            page.screenshot(path=str(shots / f"{browser_name}_{n}.png"))

        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "/visibility" not in m.text else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click('button.tl[data-id="unpowered"]')
            page.click('.trow[data-id="U01"]')
            page.wait_for_selector("#metercard")
            check("R9 meter card is on the test page", page.locator("#metercard").count() == 1)
            check("R9 connected, with the model", until(lambda: "Connected: UT161E" in page.inner_text("#mconn")), page.inner_text("#mconn"))
            check("R9 auto-accept is off by default", not page.is_checked('input[data-meter="auto"]'))

            # 1. live reading, proposal into U01 (a kOhm test), accepted like any proposal
            scene(scene="ohm", value=" 12.300")
            check("R9 live display shows the value and unit", until(lambda: "12.30" in page.inner_text("#mread") and "kΩ" in page.inner_text("#mread")), page.inner_text("#mread"))
            check("R9 the target test is named", until(lambda: "S01" in page.inner_text("#mstat") and "U01" in page.inner_text("#mstat")))
            check("R8 a proposal arrives on U01 with source meter", until(lambda: (cell("S01", "U01") or {}).get("proposals")) and cell("S01", "U01")["proposals"][0]["source"]["kind"] == "meter")
            check("R9 the card says it is proposed", until(lambda: "Proposed" in page.inner_text("#mstat")))
            check("R9 the existing box is pre-filled with the proposal", until(lambda: page.input_value("#val").startswith("12.3")), page.input_value("#val"))
            shot(page, "01_connected_live_proposed")
            page.focus("#val")
            page.keyboard.press("Enter")
            check("R9 Enter accepts it the usual way", until(lambda: (cell("S01", "U01") or {}).get("status") == "accepted") and cell("S01", "U01")["source"]["kind"] == "meter")

            # 2. OL
            scene(scene="ohm_ol")
            check("R4 OL is shown as OL", until(lambda: page.inner_text("#mread .mval").strip() == "OL"), page.inner_text("#mread"))
            check("R4 the card says why nothing is proposed", "overload" in page.inner_text("#mstat"))
            shot(page, "02_overload")

            # 3. HOLD: shown, not proposed, with the reason (a fresh test so nothing is pending)
            page.click('.trow[data-id="U02"]')
            page.wait_for_selector("#metercard")
            scene(scene="hold", mode=6, range_idx=1, value=" 12.300")
            check("R5 HOLD is shown as a flag", until(lambda: "HOLD" in page.inner_text("#mread")), page.inner_text("#mread"))
            check("R5 the reason is on the card", until(lambda: "HOLD is on" in page.inner_text("#mstat")), page.inner_text("#mstat"))
            check("R5 nothing was proposed to U02", not (cell("S01", "U02") or {}).get("proposals"))
            shot(page, "03_hold_not_proposed")

            # 4. quantity mismatch: meter on DC V, U02 wants kOhm
            scene(scene="dc_v")
            check("R6 mismatch is on the card", until(lambda: "resistance" in page.inner_text("#mstat") and "voltage" in page.inner_text("#mstat")), page.inner_text("#mstat"))
            check("R6 nothing proposed on a mismatch", not (cell("S01", "U02") or {}).get("proposals"))
            shot(page, "04_quantity_mismatch")

            # 5. auto-accept with Undo
            page.check('input[data-meter="auto"]')
            check("R10 the toggle is on", get("api/meter")["auto"]["on"] is True)
            scene(scene="ohm", value="  8.200")
            check("R10 'Accepting in' shows while armed", until(lambda: "Accepting in" in page.inner_text("#mstat"), 3), page.inner_text("#mstat"))
            shot(page, "05_auto_accept_armed")
            check("R10 the value is accepted after the delay", until(lambda: (cell("S01", "U02") or {}).get("status") == "accepted", 6) and cell("S01", "U02")["value"] == "8.2")
            check("R10 the journal says via meter-auto", [e for e in get("api/journal?limit=50") if e["type"] == "accept"][-1]["payload"]["via"] == "meter-auto")
            check("R10 focus moved to the next test", until(lambda: page.evaluate("ui.test") == "U03"), page.evaluate("ui.test"))
            check("R10 Undo is offered on the card", until(lambda: page.locator('button[data-meter="undo"]').count() == 1))
            shot(page, "06_auto_accepted_undo")
            page.click('button[data-meter="undo"]')
            check("R10 Undo empties the cell again", until(lambda: (cell("S01", "U02") or {}).get("status") != "accepted"))
            check("R10 and brings U02 back into focus", until(lambda: page.evaluate("ui.test") == "U02"))
            check("R10 the undone value is not proposed again", not until(lambda: (cell("S01", "U02") or {}).get("proposals"), 1.5))
            shot(page, "07_undone")

            # 6. lost
            scene(scene="unplugged")
            check("R12 lost is shown", until(lambda: "Lost" in page.inner_text("#mconn"), 4), page.inner_text("#mconn"))
            shot(page, "08_lost")
            scene(scene="plugged")
            check("R12 reconnects by itself", until(lambda: "Connected: UT161E" in page.inner_text("#mconn"), 6), page.inner_text("#mconn"))

            # 7. dark and narrow
            page.get_by_role("button", name="Dark").click()
            scene(scene="ohm", value=" 33.000")
            until(lambda: "33.0" in page.inner_text("#mread"))
            shot(page, "09_dark")
            page.set_viewport_size({"width": 760, "height": 900})
            time.sleep(0.5)
            check("no horizontal overflow at 760 px", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
            shot(page, "10_narrow")
            check("no page errors", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/meter_shots"), sys.argv[2] if len(sys.argv) > 2 else "webkit"))
