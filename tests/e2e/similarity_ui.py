"""'Not similar to the batch' highlight, end to end at 900 and 1440 px, light and dark, with the demo protocol (simulated, no hardware).

    .venv/bin/python tests/e2e/similarity_ui.py [shots_dir]        (port: PB_E2E_PORT, default 8796)

Four boards read U02 (kOhm) 3.0, 3.1, 3.0 and 7.0. Typing 7.0 on S04 (through the keyboard entry box) must add the amber note to the result line, an amber marker to S04's row in
the 'across the batch' table and a corner dot on S04's square in the left grid. No red, no dialog, the entry is still accepted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8796"))
BASE = f"http://127.0.0.1:{PORT}/"
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
fails: list[str] = []


def check(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)
    if not ok:
        fails.append(name)


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def until(fn, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.15)
    return None


def main():
    assert PORT != 8750
    tmp = Path(tempfile.mkdtemp(prefix="pb-sim-e2e-"))
    cfg = tmp / "voice.json"
    cfg.write_text(json.dumps({"enabled": False, "tts_backend": "log"}))
    env = {**os.environ, "POLYBENCH_VOICE_CONFIG": str(cfg), "POLYBENCH_VOICE_TEST": "1", "PYTHONPATH": os.environ.get("PYTHONPATH") or str(ROOT / "src")}
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"), "--simulate",
                            "--data-dir", str(tmp / "data"), "--batch", "SIM-E2E", "--boards", "4", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    errors: list[str] = []
    try:
        check("server up", until(lambda: urllib.request.urlopen(BASE + "api/state", timeout=5).status == 200, 40))
        for b in ("S01", "S02", "S03", "S04"):
            post("api/cmd/override_gate", {"board": b, "step": "unpowered", "reason": "e2e"})
        for b, v in (("S01", "3.0"), ("S02", "3.1"), ("S03", "3.0")):
            post("api/cmd/entry", {"board": b, "test_id": "U02", "value": v, "confirm": True})
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            first = True
            for width in (900, 1440):
                for theme in ("light", "dark"):
                    tag = f"{width}px {theme}"
                    ctx = browser.new_context(viewport={"width": width, "height": 1100}, color_scheme=theme)
                    page = ctx.new_page()
                    page.on("pageerror", lambda e: errors.append(str(e)) if "access control" not in str(e) else None)
                    page.goto(BASE)
                    page.wait_for_selector("#timeline .tl")
                    page.evaluate(f"() => localStorage.setItem('pbdv.theme', '\"{theme}\"')")
                    page.goto(BASE)
                    page.wait_for_selector("#timeline .tl")
                    page.click('button.tl[data-id="unpowered"]')
                    page.click('#left .trow[data-id="U02"]')
                    page.click('table.across tr[data-id="S04"]')
                    until(lambda: page.locator("#val").is_visible())
                    if first:
                        check(f"{tag}: nothing flagged with 3 readings", page.locator(".simnote").count() == 0)
                        check(f"{tag}: no note before Enter", "Not similar" not in page.inner_text("#centre"))
                        page.fill("#val", "7.0")
                        page.press("#val", "End")
                        until(lambda: "PASS" in page.inner_text("#entryctl") or "REVIEW" in page.inner_text("#entryctl"))
                        page.keyboard.press("Enter")
                        check(f"{tag}: Enter shows an amber toast", until(lambda: "Not similar to the batch" in page.inner_text("#toast") and page.evaluate("document.querySelector('#toast').className") == "warn"))
                        first = False
                        page.click('#left .trow[data-id="U02"]')
                        page.click('table.across tr[data-id="S04"]')
                    check(f"{tag}: across table marks S04", until(lambda: page.locator('table.across .simnote[data-sim="S04"]').count() == 1 or None))
                    txt = page.inner_text('table.across .simnote[data-sim="S04"]') if page.locator('table.across .simnote').count() else ""
                    check(f"{tag}: text names deviation, median and band", "≠ batch: 2.3× median (median 3.05 kΩ, band ±25 %)" in txt, txt)
                    check(f"{tag}: only S04 is marked", page.locator("table.across .simnote").count() == 1)
                    check(f"{tag}: result line has the note", "Not similar to the batch" in page.inner_text("#centre"))
                    check(f"{tag}: grid square of S04 has the corner dot", page.locator('#left .trow[data-id="U02"] .dot.sim').count() == 1)
                    col = page.evaluate("getComputedStyle(document.querySelector('.simmark')).backgroundColor")
                    rev = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--review').trim()")
                    check(f"{tag}: the marker is the --review amber ({col}, {rev})", col.startswith("rgb(") and "178" not in col.split(",")[0])
                    check(f"{tag}: entry stays accepted, no dialog", page.locator(".veil").count() == 0 and "7.0" in page.inner_text("#centre"))
                    ov = page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                    check(f"{tag}: no horizontal overflow", not ov)
                    if SHOTS:
                        SHOTS.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(SHOTS / f"similarity_{width}_{theme}.png"))
                    ctx.close()
            browser.close()
        check("no JavaScript errors", not errors, "; ".join(errors[:3]))
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
    print(f"{'FAILED: ' + str(fails) if fails else 'all checks passed'}")
    return 1 if fails else 0


sys.exit(main())
