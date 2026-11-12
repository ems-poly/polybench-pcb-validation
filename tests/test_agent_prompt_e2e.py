"""UIR-46: the Copy agent prompt button on design step 4, in Playwright WebKit. HOST-ONLY. Own server on a free port (PB_AP_PORT overrides), DEMO protocol, simulated bench.
Screenshots go to PB_SHOTS (default: a temp folder)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from helpers import DEMO, ROOT, free_port

PORT = free_port("PB_AP_PORT")       # a free port per run; PB_AP_PORT overrides
BASE = f"http://127.0.0.1:{PORT}/"
PLAN = (ROOT / "tests" / "data" / "DEMO_TEST_PLAN.md").read_text(encoding="utf-8")


def until(fn, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.2)
    return None


def wait_up(fn, timeout):
    """Like until(), but an AssertionError from fn (the server has exited) fails at once instead of being retried."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if fn():
                return True
        except AssertionError:
            raise
        except Exception:
            pass
        time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def shots(tmp_path_factory):
    d = Path(os.environ.get("PB_SHOTS") or tmp_path_factory.mktemp("shots"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    assert PORT != 8750
    tmp = tmp_path_factory.mktemp("ap")
    proj = tmp / "proj"
    shutil.copytree(DEMO, proj)
    (proj / "PLAN.md").write_text(PLAN, encoding="utf-8")
    top = proj / "demo.pcbv.yaml"
    top.write_text(top.read_text(encoding="utf-8") + "\ntest_plan: {file: PLAN.md, generate: true}\n", encoding="utf-8")
    env = {**os.environ, "POLYBENCH_OP_TOKEN": "ap-test-token"}
    env.pop("POLYBENCH_AI_PERMISSIONS", None)
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(top), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "AP-E2E", "--boards", "2", "--no-browser", "--port", str(PORT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        def up():
            assert srv.poll() is None, "the server exited during startup:\n" + (srv.stdout.read() or b"").decode(errors="replace")[-2000:]
            return urllib.request.urlopen(BASE + "api/state", timeout=5).status == 200
        assert wait_up(up, 30), "server did not start"
        with sync_playwright() as pw:
            try:
                b = pw.webkit.launch()
            except Exception as e:
                pytest.skip(f"WebKit unavailable: {e}")
            ctx = b.new_context(viewport={"width": 1280, "height": 900})
            p = ctx.new_page()
            p.errors = []
            p.on("pageerror", lambda e: p.errors.append(str(e)))
            p.goto(BASE)
            p.wait_for_selector("#timeline .tl")
            p.click('button.tl[data-id="testplan"]')
            p.wait_for_selector("#tp-agentprompt")
            yield p
            b.close()
    finally:
        srv.terminate()                                    # our own PID only
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()


def show_facts(page):
    """Scroll the prompt box to the project facts block so the screenshot shows it."""
    page.evaluate("""() => { const t = document.getElementById('tp-prompt-text'); t.rows = 22; t.scrollTop = 0;
        const i = t.value.indexOf('## Project facts'); const lines = t.value.slice(0, i).split('\\n').length; t.scrollTop = (lines - 1) * 16; }""")


def test_p1_button_on_step_4_copies_and_shows_the_prompt_no_ecad(page, shots):
    assert page.locator("#tp-agentprompt").inner_text() == "Copy agent prompt"
    page.screenshot(path=str(shots / "01_step4_button.png"))
    page.click("#tp-agentprompt")
    page.wait_for_selector("#tp-prompt-text")
    text = page.input_value("#tp-prompt-text")
    assert "DEMO-1" in text and "MISSING (no ECAD document is linked yet" in text and "ASK the operator" in text
    assert "PLAN.draft.md" in text and "write_plan_draft" in text
    try:                                                    # the clipboard itself, where WebKit lets a page read it
        clip = page.evaluate("navigator.clipboard.readText()")
    except Exception:
        clip = None                                         # reading is blocked in headless WebKit; the panel text is the evidence then
    print("clipboard readback:", "blocked" if clip is None else "match" if clip == text else "MISMATCH")
    assert clip is None or clip == text
    assert page.locator("#tp-prompt b").first.inner_text() in ("Copied to the clipboard", "Could not copy automatically: select the text and copy it")
    show_facts(page)
    page.screenshot(path=str(shots / "02_prompt_no_ecad.png"))
    assert not page.errors


def test_p4_prompt_after_ecad_is_linked_in_step_1(page, shots):
    r = page.evaluate("""async () => { const p = await (await fetch('/api/project/probe')).json();
        const r = await fetch('/api/cmd/project_select', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({fingerprint: p.fingerprint})});
        return r.status; }""")
    assert r == 200
    page.click("#tp-prompt button[data-tp=promptclose]")
    page.click("#tp-agentprompt")
    page.wait_for_selector("#tp-prompt-text")
    text = page.input_value("#tp-prompt-text")
    assert "document: " in text and "MISSING (no ECAD" not in text and "get_ecad" in text
    show_facts(page)
    page.screenshot(path=str(shots / "03_prompt_ecad_linked.png"))
    assert not page.errors


def test_close_hides_the_panel(page):
    page.click("#tp-prompt button[data-tp=promptclose]")
    assert page.locator("#tp-prompt").count() == 0
