"""Guided section by voice, end to end in Playwright WebKit: guide me, ready, the reading spoken, next step, the hold window, a stop, a
refusal, the panel at 390 px in dark mode. Simulated instruments, no microphone, no sound (the voice text path types the utterance).

    python tests/e2e/voice_guide_walk.py [shots_dir] [chromium|webkit]       (port: PB_E2E_PORT, default 8932; never 8750)

The Powered step is replaced by tests/voice/fixtures/guided_powered.yaml (a three-step "Rails in band" section); its hold window is
shortened to 20 s so the warning and the lapse can be walked.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8932"))
BASE = f"http://127.0.0.1:{PORT}/"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=30))
    except urllib.error.HTTPError as e:
        return {"_http": e.code}


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


def main(shots: Path, browser_name: str):
    assert PORT != 8750, "8750 is the live server"
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-guide-e2e-"))
    proto = tmp / "proto"
    shutil.copytree(ROOT / "src" / "polybench" / "examples" / "demo", proto)
    txt = (ROOT / "tests" / "voice" / "fixtures" / "guided_powered.yaml").read_text() % {"read": "demo.measure"}
    (proto / "powered.yaml").write_text(txt.replace("hold_s: 60", "hold_s: 20"))
    cfgp = tmp / "voice.json"
    cfgp.write_text(json.dumps({"tts_backend": "log", "grace_s": 0, "llm_enabled": False, "stt_backend": "sidecar"}))
    env = {**os.environ, "POLYBENCH_VOICE_TEST": "1", "POLYBENCH_VOICE_CONFIG": str(cfgp), "PYTHONPATH": os.environ.get("PYTHONPATH") or str(ROOT / "src")}
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(proto / "demo.pcbv.yaml"), "--simulate",
                            "--data-dir", str(tmp / "data"), "--batch", "GUIDE", "--boards", "3", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    errors: list[str] = []
    try:
        check("server up", until(lambda: get("api/state"), 30))
        for b in ("S01", "S02"):
            post("api/cmd/override_gate", {"board": b, "step": "powered", "reason": "walk"})
        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "fonts.g" not in m.text and "ERR_" not in m.text else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="P02"]')
            page.click('button[data-act="btab"][data-id="voice"]')
            page.wait_for_selector("#vlist")
            post("api/voice/enable", {"on": True})
            until(lambda: not page.locator("#pttbar").get_attribute("hidden"), 5)

            def say(text):
                n = len(get("api/state")["voice"])
                page.fill("#vtext", text)
                page.press("#vtext", "Enter")
                until(lambda: len(get("api/state")["voice"]) > n, 10)
                time.sleep(0.3)

            def entries():
                return get("api/state")["voice"]

            def last():
                return [e for e in entries() if e["source"] != "system"][-1]

            def spoken(needle, t=15):
                return until(lambda: any(needle in (e.get("reply") or "") for e in entries()), t) is not None

            def panel():
                return page.inner_text("#gpanel") if page.locator("#gpanel").count() else ""

            page.screenshot(path=str(shots / "01_section_with_guide_button.png"))
            check("the current section's Guide button is in the run bar and the left panel starts nothing", page.locator("#runbar .guidebtn").count() == 1 and page.locator("#left .guidebtn").count() == 0)

            say("guide me")
            check("guide me announces step 1 with the intro",
                  last()["reply"] == "Rails in band, board S1, three steps. Step 1 of 3: Turn on 12 V. This turns on 12 volts, 100 milliamp limit. Say ready.", last()["reply"])
            page.wait_for_selector("#gpanel")
            check("panel shows step 1 of 3 and the text", "Step 1 of 3" in panel() and "Turn on 12 V" in panel() and "100 milliamp" in panel(), panel())
            check("status line shows Guide: step 1 of 3", "Guide: step 1 of 3" in page.inner_text("#ptt-hint"), page.inner_text("#ptt-hint"))
            page.screenshot(path=str(shots / "02_guide_step1_announced.png"))

            say("ready")
            check("ready runs step 1 with no second confirm", last()["reply"] == "Step 1: running Turn on 12 V." and get("api/voice/status")["bench"]["confirm"] is None, last()["reply"])
            check("hold window announced", spoken("Hold window 20 seconds."))
            time.sleep(0.6)
            check("panel counts the hold down", "Hold window:" in panel(), panel())
            page.screenshot(path=str(shots / "03_hold_window_running.png"))

            say("next step")
            check("next step announces step 2 with the probe", last()["reply"] == "Step 2 of 3: Read 5V. Probe 5V at TP3. Say ready.", last()["reply"])
            say("ready")
            check("the reading is spoken with unit and verdict", spoken("5V rail: 5.0") and spoken(", pass. Say next step."))
            time.sleep(0.5)
            check("panel shows the last reading", "Last reading: 5V rail:" in panel(), panel())
            page.screenshot(path=str(shots / "04_reading_spoken_step2.png"))

            # the panel is keyboard reachable: Tab to Next, Enter
            page.focus('#gpanel [data-vo="gnext"]')
            page.keyboard.press("Enter")
            until(lambda: "Step 3 of 3" in panel(), 8)
            check("panel Next (keyboard) goes to step 3", "Step 3 of 3" in panel() and "Read 5V_AN" in panel(), panel())
            page.focus('#gpanel [data-vo="gready"]')
            page.keyboard.press("Enter")
            check("panel Ready (keyboard) runs step 3 and the reading is spoken", spoken("5V_AN rail:"))
            check("the 15 second warning was spoken", spoken("15 seconds left on the hold.", 12))
            check("the lapse is spoken with the redo offer", spoken("The hold lapsed and the output is off. Say redo step 1.", 20))
            time.sleep(0.5)
            page.screenshot(path=str(shots / "05_hold_lapsed_step3_read.png"))

            say("next step")
            check("section done with the counts", last()["reply"].startswith("Section done. 2 pass.") and "next section" in last()["reply"], last()["reply"])
            page.screenshot(path=str(shots / "06_section_done.png"))
            say("end guide")
            check("end guide closes the panel", last()["reply"] == "Guide ended." and page.locator("#gpanel").count() == 0)

            say("guide long capture")
            say("ready")
            check("long capture runs", until(lambda: get("api/state")["action"] is not None, 10) is not None)
            say("stop")
            check("stop acts at once", last()["reply"] == "Stopping." and last()["bench"]["cancel_ms"] < 500, str(last()))
            check("walk is paused and says so", spoken("Stopped. Guide paused on step 1. Say ready to run it again.", 25))
            time.sleep(0.5)
            check("panel shows paused", "paused" in panel(), panel())
            page.screenshot(path=str(shots / "07_stop_pauses_walk.png"))
            say("end guide")

            say("board S3")
            say("guide me")
            check("a gated board is refused with the reason", last()["reply"].startswith("Refused: step is gated") and page.locator("#gpanel").count() == 0, last()["reply"])
            page.screenshot(path=str(shots / "08_gated_refused.png"))

            say("guide bench checks")
            check("gated board also refuses a section by title", last()["outcome"] == "rejected")

            # 390 px, dark: fail and review readings on S2
            say("board S2")
            page.set_viewport_size({"width": 390, "height": 844})
            page.click('button[data-act="theme"]')
            say("guide rails in band")
            say("skip step")
            say("ready")
            check("fail reading spoken with the limit", spoken("5V rail: 4.5 volts, fail, below 4.85."))
            time.sleep(0.5)
            page.screenshot(path=str(shots / "09_narrow_dark_fail.png"))
            check("390 px: the panel fits the viewport", page.evaluate("(() => { const r = document.getElementById('gpanel').getBoundingClientRect(); return r.left >= 0 && r.right <= window.innerWidth + 1 && document.getElementById('gpanel').scrollWidth <= document.getElementById('gpanel').clientWidth + 1; })()"))
            say("next step")
            say("ready")
            check("review reading spoken", spoken("5V_AN rail: 5.2 volts, review, outside the guess band."))
            time.sleep(0.5)
            page.screenshot(path=str(shots / "10_narrow_dark_review.png"))
            say("next step")
            check("summary counts to review", last()["reply"].startswith("Section done. 0 pass, 2 to review."), last()["reply"])
            page.screenshot(path=str(shots / "11_narrow_dark_done.png"))

            check("every voice entry carries source and transcript", all("source" in e and "transcript" in e for e in entries()))
            check("no page errors", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
    bad = [n for n, ok in checks if not ok]
    print(f"{len(checks) - len(bad)}/{len(checks)} checks passed; screenshots in {shots}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PB_E2E_SHOTS") or tempfile.mkdtemp(prefix="pb-guide-shots-")),
                  sys.argv[2] if len(sys.argv) > 2 else "webkit"))
