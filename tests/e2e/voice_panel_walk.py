"""UIR-13: the Voice panel, all four on/off combinations and every control, in Playwright WebKit at 900 px. No microphone
(POLYBENCH_VOICE_TEST=1 swaps it for a fake), no sound (tts_backend log), the voice text path only.

    python tests/e2e/voice_panel_walk.py [shots_dir]        (port: PB_E2E_PORT, default 8904; never 8750)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import opauth  # noqa: F401  (operator session cookie)
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8904"))
BASE = f"http://127.0.0.1:{PORT}/"
PROTOCOL = ROOT / "tests" / "e2e" / "fixtures" / "voice_demo" / "voice_demo.pcbv.yaml"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=30))
    except urllib.error.HTTPError as e:
        return {"_http": e.code}


def until(fn, timeout=10.0, step=0.15):
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


def main(shots: Path):
    assert PORT != 8750, "8750 is the live server"
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-voice-panel-"))
    cfgp = tmp / "voice.json"
    cfgp.write_text(json.dumps({"tts_backend": "log", "grace_s": 0, "llm_enabled": False, "stt_backend": "sidecar"}))
    env = {**os.environ, "POLYBENCH_VOICE_TEST": "1", "POLYBENCH_VOICE_CONFIG": str(cfgp), "PYTHONPATH": os.environ.get("PYTHONPATH") or str(ROOT / "src")}
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(PROTOCOL), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "DEMO-PANEL", "--boards", "4", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    errors: list[str] = []
    st = lambda: get("api/voice/status")
    try:
        check("server up", until(lambda: get("api/state"), 30))
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            page = browser.new_context(viewport={"width": 900, "height": 1000}).new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="V01"]')
            page.click('button[data-act="btab"][data-id="voice"]')
            page.wait_for_selector("#vlist")
            vis = lambda sel: page.locator(sel).is_visible()
            kids = lambda: {"voice_name": vis("#vk-out"), "talk": vis("#vo-talk"), "hf": vis("#vo-hf"), "ack": vis("#vo-ack"), "settings": vis("#vo-settings")}
            check("exactly two top-level switches (the third, acknowledgements, is a child of Voice input)",
                  page.locator("#vg-out > button[role=switch]").count() == 1 and page.locator("#vg-in > button[role=switch]").count() == 1
                  and page.locator("#vtool > button[role=switch]").count() == 0)
            check("labels: Voice readouts, Voice input, Voice acknowledgements",
                  page.inner_text("#vo-readouts").strip() == "Voice readouts" and page.inner_text("#vo-power").strip() == "Voice input"
                  and page.inner_text("#vo-ack").strip() == "Voice acknowledgements")
            check("no 'Mute replies' anywhere", "Mute replies" not in page.inner_text("#bottom") and page.locator("#vo-mute").count() == 0)

            # combination 1: readouts on (default), input off
            page.wait_for_function("document.getElementById('vo-voice').textContent !== ''")
            k = kids()
            check("readouts on, input off: voice name and Settings shown, input children hidden",
                  k == {"voice_name": True, "talk": False, "hf": False, "ack": False, "settings": True}, str(k))
            page.screenshot(path=str(shots / "vo-1-readouts-on-input-off.png"))

            # combination 2: both on
            page.click("#vo-power")
            check("Voice input switch turns the server's voice on", until(lambda: st()["enabled"] is True))
            page.wait_for_selector("#vo-talk", state="visible")
            k = kids()
            check("both on: every child shown", all(k.values()), str(k))
            check("switch states reflect on", page.get_attribute("#vo-readouts", "aria-checked") == "true" and page.get_attribute("#vo-power", "aria-checked") == "true")
            page.screenshot(path=str(shots / "vo-2-both-on.png"))

            # every child control of Voice input
            page.click("#vo-hf")
            check("Hands-free switches the server's hands-free on", until(lambda: st()["handsfree"] is True))
            page.click("#vo-hf")
            check("Hands-free again switches it off", until(lambda: st()["handsfree"] is False))
            box = page.locator("#vo-talk").bounding_box()
            page.mouse.move(box["x"] + 5, box["y"] + 5)
            page.mouse.down()
            check("Hold to talk starts recording on the server", until(lambda: st()["recording"] is True))
            page.mouse.up()
            check("releasing Hold to talk stops recording", until(lambda: st()["recording"] is False))
            check("acknowledgements default on", get("api/voice/status")["config"]["tts_ack"] is True and page.get_attribute("#vo-ack", "aria-checked") == "true")
            page.click("#vo-ack")
            check("Voice acknowledgements off writes tts_ack false", until(lambda: st()["config"]["tts_ack"] is False and page.get_attribute("#vo-ack", "aria-checked") == "false"))
            page.screenshot(path=str(shots / "vo-3-both-on-acks-off.png"))
            page.click("#vo-ack")
            check("Voice acknowledgements on writes tts_ack true", until(lambda: st()["config"]["tts_ack"] is True))

            # text box (voice text path)
            page.fill("#vtext", "S1 0.2 volts")
            page.press("#vtext", "Enter")
            check("typed utterance goes through the text path and is listed", until(lambda: page.locator("#vlist .vrow").count() >= 1) and "0.2" in page.inner_text("#vlist"))

            # Settings
            page.click("#vo-settings")
            page.wait_for_selector("#vs-rate")
            page.screenshot(path=str(shots / "vo-4-settings-dialog.png"))
            check("Readout wording in Settings", "Readout voice" in page.inner_text(".vodlg") and "Reply voice" not in page.inner_text(".vodlg"))
            page.fill("#vs-rate", "180")
            page.click("#vs-save")
            check("Settings Save writes the rate", until(lambda: st()["config"]["tts_rate"] == 180))

            # combination 3: readouts off, input on
            page.click("#vo-readouts")
            check("Voice readouts switch off writes tts_enabled false", until(lambda: st()["config"]["tts_enabled"] is False and st()["muted_tts"] is True))
            k = kids()
            check("readouts off, input on: voice name hidden, input children shown, Settings still reachable",
                  k == {"voice_name": False, "talk": True, "hf": True, "ack": True, "settings": True}, str(k))
            page.screenshot(path=str(shots / "vo-5-readouts-off-input-on.png"))

            # combination 4: both off
            page.click("#vo-power")
            check("Voice input off turns the server's voice off", until(lambda: st()["enabled"] is False))
            page.wait_for_selector("#vo-talk", state="hidden")
            k = kids()
            check("both off: no child controls", not any(k.values()), str(k))
            check("both off: the text box stays", vis("#vtext"))
            page.screenshot(path=str(shots / "vo-6-both-off.png"))

            page.click("#vo-readouts")
            check("Voice readouts on writes tts_enabled true", until(lambda: st()["config"]["tts_enabled"] is True))
            check("horizontal scroll absent", not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
            browser.close()
        check("no page errors", not errors, str(errors)[:300])
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="vo-shots-"))))
