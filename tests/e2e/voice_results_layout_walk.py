"""The Guide button, the guide panel and the anomalies strip together in the step view (voice-guided + instrument-results), Playwright WebKit.
Packaged demo, --simulate, voice text path; no microphone, no sound, no instruments. S03 reads a failing 5V rail and a failing sweep, so
the anomalies strip is populated while the guide panel is open. 1100 px and 390 px, light and dark.

    python tests/e2e/voice_results_layout_walk.py [shots_dir] [chromium|webkit]       (port: PB_E2E_PORT, default 8942; never 8750)
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

import opauth  # noqa: F401  (operator session cookie)
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8942"))
assert PORT != 8750, "8750 is the live bench server"
BASE = f"http://127.0.0.1:{PORT}/"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


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


OVERLAP = """(sels) => { const rs = sels.map(s => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return r.width ? r : null; });
  const out = []; for (let i = 0; i < rs.length; i++) for (let j = i + 1; j < rs.length; j++) { const a = rs[i], b = rs[j]; if (!a || !b) continue;
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) out.push(sels[i] + ' x ' + sels[j]); } return out; }"""


def main(shots: Path, browser_name: str):
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-vrl-e2e-"))
    cfgp = tmp / "voice.json"
    cfgp.write_text(json.dumps({"tts_backend": "log", "grace_s": 0, "llm_enabled": False, "stt_backend": "sidecar"}))
    env = {**os.environ, "POLYBENCH_VOICE_TEST": "1", "POLYBENCH_VOICE_CONFIG": str(cfgp), "PYTHONPATH": os.environ.get("PYTHONPATH") or str(ROOT / "src")}
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"),
                            "--simulate", "--data-dir", str(tmp / "data"), "--batch", "LAYOUT", "--boards", "3", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT, env=env)
    errors: list[str] = []
    try:
        check("server up", until(lambda: get("api/state"), 30))
        for b in ("S01", "S02", "S03"):
            post("api/cmd/override_gate", {"board": b, "step": "powered", "reason": "walk"})
        post("api/voice/enable", {"on": True})
        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            for width, name in ((1100, "1100"), (390, "390")):
                if width == 390:
                    post("api/voice/text", {"text": "end guide", "context": {"step": "powered", "test_id": "P01", "board": "S03"}})     # the 1100 px guide would float over the new page
                ctx = browser.new_context(viewport={"width": width, "height": 900 if width > 700 else 844})
                page = ctx.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "fonts.g" not in m.text and "ERR_" not in m.text else None)
                page.goto(BASE)
                page.wait_for_selector("#timeline .tl")
                page.click('button.tl[data-id="powered"]')
                page.click('.bchip[data-id="S03"]')
                if width == 1100:
                    for act in ("read_rails", "load_sweep"):
                        page.click(f'button[data-act="run"][data-action="{act}"]')
                        until(lambda: not get("api/state").get("action"), 30)
                    check("anomalies strip lists the S03 failures", until(lambda: page.locator("#anoms .anjump").count() >= 2))
                else:
                    page.wait_for_selector("#anoms .anjump")
                if width == 390 and page.locator("#runbar .guidebtn:visible").count() == 0:
                    page.click("text=»")            # at 390 px the section list starts collapsed: open it
                    time.sleep(0.4)
                check(f"{name}: the Guide button is in the run bar at the top of the work area, inside the viewport",
                      page.evaluate("(() => { const b = document.querySelector('#runbar .guidebtn'); if (!b) return false; const r = b.getBoundingClientRect(); if (!r.width) return false; const h = b.closest('#runbar'); return r.left >= 0 && r.right <= window.innerWidth + 1 && h.scrollWidth <= h.clientWidth + 1; })()"))
                check(f"{name}: no horizontal page scroll", page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"))
                page.screenshot(path=str(shots / f"{name}_light_1_guide_button_and_anomalies.png"))
                page.click('#runbar .guidebtn')
                check(f"{name}: the panel opens", until(lambda: page.locator("#gpanel").count() and "Step 1" in page.inner_text("#gpanel"), 10))
                page.evaluate("window.scrollTo(0,0); const c = document.querySelector('#centre'); if (c) c.scrollTop = 0")
                time.sleep(0.4)
                ov = page.evaluate(OVERLAP, ["#gpanel", "#anoms"])
                if width == 1100:
                    check(f"{name}: guide panel and anomalies strip do not overlap (top of page)", not ov, str(ov))
                page.screenshot(path=str(shots / f"{name}_light_2_panel_with_anomalies.png"))
                if width == 1100:
                    page.get_by_role("button", name="Dark").click()
                    time.sleep(0.4)
                    check("dark theme is on", page.evaluate("document.documentElement.dataset.theme") == "dark")
                else:
                    page.get_by_role("button", name="Dark").click()
                    time.sleep(0.4)
                ov = page.evaluate(OVERLAP, ["#gpanel", "#anoms"])
                if width == 1100:
                    check(f"{name} dark: no overlap", not ov, str(ov))
                page.screenshot(path=str(shots / f"{name}_dark_3_panel_with_anomalies.png"))
                # the anomalies strip stays reachable: its Redo button is clickable while the panel is open (not covered)
                covered = page.evaluate("""(() => { const b = document.querySelector('#anoms button'); if (!b) return 'none'; b.scrollIntoView({block:'center'});
                    const r = b.getBoundingClientRect(); const e = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return b.contains(e) || e === b ? '' : (e ? e.id || e.className : 'null'); })()""")
                check(f"{name}: a strip button is not covered by the panel after scrolling it into view", covered == "", str(covered))
                time.sleep(0.3)
                page.screenshot(path=str(shots / f"{name}_dark_4_strip_scrolled.png"))
                ctx.close()
            browser.close()
        check("no page or console errors", not errors, "; ".join(errors[:3]))
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "shots"), sys.argv[2] if len(sys.argv) > 2 else "webkit"))
