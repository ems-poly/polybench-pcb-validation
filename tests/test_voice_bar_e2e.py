"""UIR-13 round 2, Playwright WebKit, own server on port 8909 (PB_E2E_PORT; never 8750), no sound, no microphone. HOST-ONLY.
Bar shows the configured key; the input warning shows only with Voice input on, as separate lines; the state says 'not installed' when speech to
text is missing; a push-to-talk click never leaves 'Transcribing' (fake microphone: the usual result; no microphone: gives up at once)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from helpers import ROOT

PORT = int(os.environ.get("PB_E2E_PORT", "8909"))
BASE = f"http://127.0.0.1:{PORT}/"
PROTOCOL = ROOT / "tests" / "e2e" / "fixtures" / "voice_demo" / "voice_demo.pcbv.yaml"
sys.path.insert(0, str(ROOT / "tests" / "e2e"))
import opauth  # noqa: E402,F401


def serve(tmp, fake_mic, key=None):
    cfg = {"tts_backend": "log", "llm_enabled": False, "grace_s": 0, "stt_backend": "whisper"}
    if key:
        cfg["ptt_key"] = key
    (tmp / "voice.json").write_text(json.dumps(cfg))
    env = {**os.environ, "POLYBENCH_VOICE_CONFIG": str(tmp / "voice.json"), "PYTHONPATH": str(ROOT / "src")}
    if fake_mic:
        env["POLYBENCH_VOICE_TEST"] = "1"
    else:
        env.pop("POLYBENCH_VOICE_TEST", None)
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(PROTOCOL), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "DEMO-BAR", "--boards", "4", "--no-browser", "--port", str(PORT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "api/state", timeout=2)
            return srv
        except Exception:
            time.sleep(0.3)
    srv.kill()
    raise RuntimeError("server did not start")


def open_voice(pw, on):
    page = pw.webkit.launch().new_context(viewport={"width": 900, "height": 1000}).new_page()
    page.goto(BASE)
    page.wait_for_selector("#timeline .tl")
    page.click('button.tl[data-id="powered"]')
    page.click('.trow[data-id="V01"]')
    page.click('button[data-act="btab"][data-id="voice"]')
    page.wait_for_selector("#vlist")
    if on:
        page.click("#vo-power")
        page.wait_for_selector("#vo-talk", state="visible")
        page.wait_for_timeout(700)
    return page


def stt_ok():
    return json.load(urllib.request.urlopen(BASE + "api/voice/status"))["stt"]["ok"]


def test_bar_key_warning_and_state(tmp_path):
    srv = serve(tmp_path, True)
    try:
        with sync_playwright() as pw:
            page = open_voice(pw, False)
            ok = stt_ok()
            page.screenshot(path=str(tmp_path / "off.png"))
            assert page.inner_text("#vissue").strip() == "" or ok is True                 # input off: no speech-to-text warning
            page.click("#vo-power")
            page.wait_for_selector("#vo-talk", state="visible")
            page.wait_for_timeout(700)
            assert page.inner_text("#ptt-label") == "Hold ` to talk" and "`" in page.inner_text("#ptt-hint")
            assert "Space" not in page.inner_text("#ptt-label")
            if not ok:
                assert page.inner_text("#vo-state") == "not installed"
                lines = [l for l in page.text_content("#vissue").split("\n") if l.strip()]
                assert page.evaluate("getComputedStyle(document.getElementById('vissue')).whiteSpace") == "pre-line"
                assert len(lines) == 3, lines
                assert lines[0].startswith("Speech to text not installed") and lines[1].startswith("Install: ") and lines[2].startswith("Microphone permission")
    finally:
        srv.terminate(); srv.wait(10)


def test_the_bar_follows_the_configured_key(tmp_path):
    srv = serve(tmp_path, True, key="KeyT")
    try:
        with sync_playwright() as pw:
            page = open_voice(pw, True)
            assert page.inner_text("#ptt-label") == "Hold T to talk"
    finally:
        srv.terminate(); srv.wait(10)


@pytest.mark.parametrize("fake_mic", [True, False])
def test_a_push_to_talk_click_never_sticks_on_transcribing(tmp_path, fake_mic):
    if fake_mic:
        pytest.importorskip("numpy", reason="the fake microphone needs the voice extra (numpy)")
    srv = serve(tmp_path, fake_mic)
    try:
        with sync_playwright() as pw:
            page = open_voice(pw, True)
            for sel in ("#vo-talk", "#ptt-bar"):
                page.click(sel)
                page.wait_for_function("document.getElementById('ptt-label').textContent !== 'Transcribing…'", timeout=5000)
            page.keyboard.down("`"); page.keyboard.up("`")                            # a zero-length press
            page.wait_for_function("document.getElementById('ptt-label').textContent !== 'Transcribing…'", timeout=5000)
    finally:
        srv.terminate(); srv.wait(10)
