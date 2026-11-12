"""Bench commands by voice, end to end in Playwright WebKit: board, start, confirm, stop, refusals, timeout. Simulated instruments, no microphone,
no sound (the voice text path types the utterance; the microphone is a fake recorder in test mode).

    python tests/e2e/voice_bench_walk.py [shots_dir] [chromium|webkit]       (port: PB_E2E_PORT, default 8931; never 8750)

The First power action is replaced by demo.slow (runs until cancelled) in a temporary copy of the demo protocol.
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
PORT = int(os.environ.get("PB_E2E_PORT", "8931"))
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
    tmp = Path(tempfile.mkdtemp(prefix="pb-bench-voice-e2e-"))
    proto = tmp / "proto"
    shutil.copytree(ROOT / "src" / "polybench" / "examples" / "demo", proto)
    p = proto / "powered.yaml"
    p.write_text(p.read_text().replace("plugin: demo.measure", "plugin: demo.slow"))
    cfgp = tmp / "voice.json"
    cfgp.write_text(json.dumps({"tts_backend": "log", "grace_s": 0, "llm_enabled": False, "stt_backend": "sidecar", "confirm_timeout_s": 4}))
    env = {**os.environ, "POLYBENCH_VOICE_TEST": "1", "POLYBENCH_VOICE_CONFIG": str(cfgp), "PYTHONPATH": os.environ.get("PYTHONPATH") or str(ROOT / "src")}
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(proto / "demo.pcbv.yaml"), "--simulate",
                            "--data-dir", str(tmp / "data"), "--batch", "BENCH-VOICE", "--boards", "3", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    errors: list[str] = []
    try:
        check("server up", until(lambda: get("api/state"), 30))
        for b in ("S01", "S02"):
            post("api/cmd/override_gate", {"board": b, "step": "powered", "reason": "walk"})
        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "fonts.g" not in m.text and "ERR_" not in m.text else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="P01"]')
            page.click('button[data-act="btab"][data-id="voice"]')
            page.wait_for_selector("#vlist")
            post("api/voice/enable", {"on": True})                       # fake recorder in test mode: no microphone is opened
            until(lambda: not page.locator("#pttbar").get_attribute("hidden"), 5)

            def say(text):
                page.fill("#vtext", text)
                page.press("#vtext", "Enter")
                until(lambda: any(e.get("transcript") == text for e in get("api/state")["voice"][-6:]), 10)
                time.sleep(0.4)

            def last():
                return [e for e in get("api/state")["voice"] if e["source"] != "system"][-1]

            say("board S2")
            check("board S2 selected, spoken Board S2", last()["reply"] == "Board S2." and get("api/voice/status")["context"]["board"] == "S02", str(last()))
            page.screenshot(path=str(shots / "01_board_selected.png"))
            check("status line shows the last command", "Last command: board S2" in page.inner_text("#ptt-hint"), page.inner_text("#ptt-hint"))

            say("board S9")
            check("unknown board refused, nothing changes", last()["reply"] == "No board S9." and get("api/voice/status")["context"]["board"] == "S02")

            say("start test")       # the demo's Powered step has two actions: a bare start asks which
            check("a bare start with two actions asks which", last()["reply"].startswith("More than one action here.") and get("api/state")["action"] is None, str(last()))
            say("run read rails")
            page.wait_for_selector("#vstrip")
            check("start reads back and waits (strip with countdown)", "Say confirm" in page.inner_text("#vstrip") and " s)" in page.inner_text("#vstrip") and get("api/state")["action"] is None,
                  page.inner_text("#vstrip"))
            page.screenshot(path=str(shots / "02_confirm_pending_countdown.png"))

            say("yes")
            check("a wrong word cancels the request", last()["reply"].startswith("Start cancelled.") and get("api/voice/status")["bench"]["confirm"] is None)
            page.screenshot(path=str(shots / "03_wrong_word_cancelled.png"))

            say("run read rails")
            until(lambda: get("api/voice/status")["bench"]["confirm"] is None, 8)
            time.sleep(0.5)
            check("no answer: times out, nothing started", get("api/state")["action"] is None and any(e["source"] == "timer" and e["reply"] == "No confirmation. Nothing started." for e in get("api/state")["voice"]))
            page.screenshot(path=str(shots / "04_timeout.png"))

            say("run read rails")
            say("confirm")
            check("confirm starts the action on S02", until(lambda: (get("api/state")["action"] or {}).get("board") == "S02", 10) is not None)
            time.sleep(0.8)
            page.screenshot(path=str(shots / "05_running_after_confirm.png"))
            check("status line shows the running action", "Running Read rails and current on S02" in page.inner_text("#ptt-hint"), page.inner_text("#ptt-hint"))

            say("run read rails")
            check("a start while one runs is refused", last()["reply"] == "Read rails and current is still running. Say stop to abort.", last()["reply"])
            page.screenshot(path=str(shots / "06_busy_refused.png"))

            say("stop test")
            check("stop aborts the run (no confirmation)", last()["reply"] == "Stopping." and last()["bench"]["cancel_ms"] < 500, str(last()))
            check("run ends", until(lambda: get("api/state")["action"] is None, 20) is not None)
            check("safe state ran (outputs off)", until(lambda: any("safe state (demo.slow)" in c["text"] and "outputs off" in c["text"] for c in get("api/console")), 5) is not None)
            time.sleep(0.8)
            page.screenshot(path=str(shots / "07_stopped.png"))

            say("board S3")
            say("run read rails")
            check("a gated board is refused with the reason", last()["reply"].startswith("Refused: step is gated") and get("api/state")["action"] is None, last()["reply"])
            page.screenshot(path=str(shots / "08_gated_refused.png"))

            say("board S2")
            say("next test")
            check("next test moves on", last()["intent"] == "next_test" and get("api/voice/status")["context"]["test_id"] == "P02", str(last()))
            page.screenshot(path=str(shots / "09_next_test.png"))
            # the journal has everything
            v = get("api/state")["voice"]
            check("every voice entry carries source and transcript", all("source" in e and "transcript" in e for e in v))
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
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PB_E2E_SHOTS") or tempfile.mkdtemp(prefix="pb-bench-voice-shots-")),
                  sys.argv[2] if len(sys.argv) > 2 else "webkit"))
