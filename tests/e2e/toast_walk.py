"""Playwright WebKit walk for the advance() toast (D1: step change clears it; D2: 'settled' only when it is true). Own port, never 8750.

    .venv/bin/python tests/e2e/toast_walk.py [shots_dir]      (port: PB_E2E_PORT, default 8902)
HOST-ONLY: simulator.
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
PORT = int(os.environ.get("PB_E2E_PORT", "8902"))
assert PORT != 8750, "8750 is the live bench server"
BASE = f"http://127.0.0.1:{PORT}/"
res: list[bool] = []


def check(name, ok, extra=""):
    res.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def until(fn, timeout=15.0):
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


def main(shots: Path):
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-toast-e2e-"))
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"),
                            "--simulate", "--data-dir", str(tmp / "data"), "--batch", "TOAST", "--boards", "2", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    print("server pid", srv.pid, flush=True)
    try:
        check("server up", until(lambda: get("api/state"), 30))
        with sync_playwright() as pw:
            br = pw.webkit.launch()
            page = br.new_context(viewport={"width": 1400, "height": 900}).new_page()
            page.goto(BASE); page.wait_for_selector("#timeline .tl")
            toast = lambda: page.evaluate("(() => { const t = document.querySelector('#toast'); return t && t.style.display !== 'none' ? t.textContent : '' })()")

            def typed(vals, delay=0.45):
                for v in vals:
                    page.keyboard.type(v); page.keyboard.press("Enter"); time.sleep(delay)

            # D2 first: Unpowered is gated on Visual; override S01 only, so S02 stay gated.
            page.click('button.tl[data-id="unpowered"]'); page.wait_for_selector(".trow")
            page.click('.bchip[data-id="S01"]'); time.sleep(0.3)
            page.click("text=Override with a reason"); page.fill(".dlg input", "Test run: no photo"); page.click(".dlg button[type=submit]"); time.sleep(0.5)
            page.click('.trow[data-id="U01"]')
            typed(["12.5", "5", "3", "y", "clean board"])
            t = until(lambda: toast())
            page.screenshot(path=str(shots / "d2_gated_toast.png"))
            check("D2 gated boards: names the gate, does not say settled", t and "Nothing more to enter here" in t and "1 board waits on the decision gate" in t and "settled" not in t, repr(t))
            check("D2 toast carries the open cells' reason with no 'settled' claim", t and "Everything" not in t, repr(t))

            # D1: complete Visual, the 'settled' toast shows; then a step change clears it.
            page.click('button.tl[data-id="visual"]'); page.wait_for_selector('.trow[data-id="V01"]')
            for b in ("S01", "S02"):
                page.click(f'.bchip[data-id="{b}"]'); page.click('.trow[data-id="V01"]')
                typed(["y", "y", "y", "clean"], 0.4)
            t = until(lambda: toast())
            check("D1 all settled: the settled toast shows on Visual", t == "Everything in this step is settled for all boards.", repr(t))
            page.click('button.tl[data-id="unpowered"]'); page.wait_for_selector(".trow"); time.sleep(0.15)
            page.screenshot(path=str(shots / "d1_after_step_change.png"))
            check("D1 step change clears the old step's toast", toast() == "", repr(toast()))

            # D3: the Voice settings dialog keeps its Model select inside the dialog at both viewports (long model name forced in).
            for (w, h) in ((1440, 900), (900, 650)):
                page.set_viewport_size({"width": w, "height": h}); time.sleep(0.3)
                page.evaluate("ui.bottomOpen = true; ui.bottomTab = 'voice'; render()")
                page.click("text=Voice >> nth=-1") if not page.locator("button:has-text('Settings')").count() else None
                page.click("button:has-text('Settings')"); page.wait_for_selector("#vs-model"); time.sleep(1.2)
                page.evaluate("document.querySelector('#vs-model').innerHTML = '<option>' + 'a-very-long-model-name-with-quantisation-and-suffix-'.repeat(4) + '</option>'")
                time.sleep(0.2)
                r = page.evaluate("(() => { const d = document.querySelector('.vodlg').getBoundingClientRect(), s = document.querySelector('#vs-model').getBoundingClientRect(); return [d.left, d.right, s.left, s.right] })()")
                page.screenshot(path=str(shots / f"d3_voice_settings_{w}x{h}.png"))
                check(f"D3 model select inside the dialog at {w}x{h}", r[2] >= r[0] - 0.5 and r[3] <= r[1] + 0.5, str(r))
                page.click("button:has-text('Cancel')"); time.sleep(0.2)
            br.close()
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
    ok = all(res)
    print("ALL PASS" if ok else "FAILURES", len(res))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="pb-toast-shots-"))))
