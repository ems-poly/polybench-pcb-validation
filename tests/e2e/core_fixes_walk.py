"""WebKit proof of two core UI fixes (C9, C10), on the DEMO protocol with --simulate, on its own port (never 8750).

    .venv/bin/python tests/e2e/core_fixes_walk.py [shots_dir] [chromium|webkit]    (port: PB_E2E_PORT, default 9064)

C9  an action asks the operator a question: the dialog carries its own Abort button, it is visible and not covered, and one click
    cancels the run and calls the safe state (the veil hides the run bar's Abort, so the dialog must hold one).
C10 a REVIEW entry: the toast names the button that exists ("Accept for review"), and the toast never covers that button, at
    1440x900 and at 900x650. The button then records the value.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "9064"))
BASE = f"http://127.0.0.1:{PORT}/"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def start(data):
    log = open(os.environ.get("PB_SERVER_LOG", os.devnull), "ab")
    p = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"),
                          "--simulate", "--data-dir", str(data), "--batch", "cf", "--no-browser", "--port", str(PORT)], stdout=log, stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + "api/state", timeout=1).read()
            return p
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not start")


def rect(pg, sel):
    return pg.evaluate("(s)=>{const e=document.querySelector(s); if(!e) return null; const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}}", sel)


def overlap(a, b):
    return not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


def top_is(pg, sel):
    """The element at the centre of `sel` is `sel` itself (nothing covers it)."""
    return pg.evaluate("(s)=>{const e=document.querySelector(s); const r=e.getBoundingClientRect(); const t=document.elementFromPoint(r.x+r.width/2, r.y+r.height/2); return !!t && (t===e || e.contains(t))}", sel)


def walk(pw, browser, shots, size):
    w, h = size
    tag = f"{browser}_{w}x{h}"
    br = getattr(pw, browser).launch()
    pg = br.new_page(viewport={"width": w, "height": h})
    errors = []
    pg.on("pageerror", lambda e: "access control checks" not in str(e) and errors.append(str(e)))      # WebKit reports a beacon cut by a reload this way
    pg.on("response", lambda r: r.status >= 500 and errors.append(f"HTTP {r.status} {r.url}"))
    pg.goto(BASE)
    pg.wait_for_selector("#timeline .tl")
    pg.click("#timeline .tl[data-id='setup']")
    pg.fill("#op", "Pat")
    pg.keyboard.press("Tab")
    pg.locator(".serial").nth(0).fill("DEMO-A-001")
    pg.keyboard.press("Tab")

    # the unpowered step is gated behind assembly and visual: open it the way an operator does, with a recorded override
    import json
    for step in ("unpowered", "characterisation"):
        req = urllib.request.Request(BASE + "api/cmd/override_gate", data=json.dumps({"board": "S01", "step": step, "reason": "walk: gate opened"}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
    pg.reload()
    pg.wait_for_selector("#timeline .tl")

    # ---- C10: REVIEW toast and the button it names
    pg.click("#timeline .tl[data-id='unpowered']")
    pg.wait_for_timeout(300)
    pg.click(".trow:has-text('5V to GND')")
    pg.locator("#val").focus()
    pg.keyboard.type("1")
    pg.keyboard.press("Enter")
    pg.wait_for_selector("#toast", state="visible", timeout=5000)
    pg.wait_for_selector("button[data-act=entryconfirm]", timeout=5000)
    txt = pg.inner_text("#toast")
    check(f"C10 {tag}: the toast names the button that exists", "Accept for review" in txt and "Record FAIL" not in txt, txt)
    btn_text = pg.inner_text("button[data-act=entryconfirm]")
    check(f"C10 {tag}: that button's label is on screen", btn_text.strip() == "Accept for review", btn_text)
    tr, br_ = rect(pg, "#toast"), rect(pg, "button[data-act=entryconfirm]")
    check(f"C10 {tag}: the toast does not overlap the button", not overlap(tr, br_), f"{tr} {br_}")
    check(f"C10 {tag}: nothing covers the button", top_is(pg, "button[data-act=entryconfirm]"))
    pg.screenshot(path=str(shots / f"c10_review_toast_{tag}.png"))
    pg.click("button[data-act=entryconfirm]")
    pg.wait_for_timeout(500)
    cells = json.load(urllib.request.urlopen(BASE + "api/state"))["cells"]
    c = next((v for k, v in cells.items() if k.startswith("S01|") and v.get("verdict") == "review"), None)
    check(f"C10 {tag}: the button records the REVIEW entry", c is not None and c["status"] == "accepted", str(c))

    # ---- C9: the ask dialog holds an Abort
    pg.click("#timeline .tl[data-id='characterisation']")
    pg.wait_for_timeout(300)
    pg.click("button:has-text('Capture noise at 2 mV/V')")
    pg.wait_for_selector("#runok", timeout=8000)
    pg.click("#runok")
    pg.wait_for_selector(".dlg [data-opt]", timeout=8000)
    pg.wait_for_selector(".dlg [data-abort]", timeout=3000)
    check(f"C9 {tag}: Abort is inside the ask dialog", pg.locator(".dlg [data-abort]").count() == 1)
    ar = rect(pg, ".dlg [data-abort]")
    check(f"C9 {tag}: the Abort button is on screen", ar and 0 <= ar["x"] and ar["x"] + ar["w"] <= w and 0 <= ar["y"] and ar["y"] + ar["h"] <= h, str(ar))
    check(f"C9 {tag}: nothing covers it", top_is(pg, ".dlg [data-abort]"))
    pg.screenshot(path=str(shots / f"c9_ask_dialog_abort_{tag}.png"))
    pg.click(".dlg [data-abort]")
    pg.wait_for_function("!document.querySelector('.dlg')", timeout=5000)
    pg.wait_for_function("consoleLines.some(l=>/^--- run \\d+ cancelled/.test(l.text))", timeout=15000)
    pg.wait_for_function("consoleLines.some(l=>/^safe state/.test(l.text))", timeout=15000)
    st = json.load(urllib.request.urlopen(BASE + "api/state"))
    check(f"C9 {tag}: no action is running any more", st["action"] is None)
    log = pg.evaluate("consoleLines.map(l=>l.text).join('\\n')")
    check(f"C9 {tag}: cancelled and safe state called, no error line", "cancelled" in log and "safe state" in log and "--- run" in log and not any(
        ln.startswith("--- run") and "error" in ln for ln in log.splitlines()), log[-300:])
    pg.wait_for_timeout(400)
    pg.screenshot(path=str(shots / f"c9_after_abort_{tag}.png"))
    check(f"C9 {tag}: no page errors, no HTTP 5xx", not errors, "; ".join(errors)[:300])
    br.close()


def main(shots: Path, browser: str):
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        for size in ((1440, 900), (900, 650)):
            data = Path(tempfile.mkdtemp(prefix="pbcf-"))
            srv = start(data)                       # a fresh batch for each size
            try:
                walk(pw, browser, shots, size)
            finally:
                srv.terminate()
                try:
                    srv.wait(timeout=5)
                except Exception:
                    srv.kill()
                shutil.rmtree(data, ignore_errors=True)
    bad = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/screens/core_fixes", sys.argv[2] if len(sys.argv) > 2 else "webkit"))
