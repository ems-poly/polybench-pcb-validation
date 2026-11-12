"""Test plan viewer, UIR-09..11: the contents list, folding, and the always-visible approval bar. HOST-ONLY.
Own server on port 8903 (PB_TP_PORT), DEMO protocol and the DEMO plan, simulated bench, Playwright WebKit (skipped without playwright or WebKit).
Screenshots go to PB_SHOTS (default: a temp folder)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from helpers import DEMO, ROOT

PORT = int(os.environ.get("PB_TP_PORT", "8903"))
BASE = f"http://127.0.0.1:{PORT}/"
TOKEN = "tp-test-token"
PLAN = (ROOT / "tests" / "data" / "DEMO_TEST_PLAN.md").read_text()
AI = {"X-Polybench-Client": "assistant", "X-Polybench-Assistant": "pytest", "X-Polybench-Session": "tp1", "Content-Type": "application/json"}


def expected_headings(md: str) -> list[tuple[int, str]]:
    """## and ### lines of the markdown outside code fences, in file order."""
    out, fence = [], False
    for ln in md.splitlines():
        if ln.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{2,3}) +(.+?) *#*$", ln)
        if m:
            out.append((len(m.group(1)), m.group(2)))
    return out


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


@pytest.fixture(scope="module")
def shots(tmp_path_factory):
    d = Path(os.environ.get("PB_SHOTS") or tmp_path_factory.mktemp("shots"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    assert PORT != 8750
    tmp = tmp_path_factory.mktemp("tp")
    proj = tmp / "proj"
    shutil.copytree(DEMO, proj)
    (proj / "PLAN.md").write_text(PLAN)
    top = proj / "demo.pcbv.yaml"
    top.write_text(top.read_text() + "\ntest_plan: {file: PLAN.md, generate: true}\n")
    env = {**os.environ, "POLYBENCH_OP_TOKEN": TOKEN}
    env.pop("POLYBENCH_AI_PERMISSIONS", None)
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(top), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "TP-E2E", "--boards", "2", "--no-browser", "--port", str(PORT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        assert until(lambda: urllib.request.urlopen(BASE + "api/state", timeout=5).status == 200, 30), "server did not start"
        yield proj
    finally:
        srv.terminate()                                    # our own PID only
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()


@pytest.fixture(scope="module")
def browser_page(server):
    with sync_playwright() as pw:
        try:
            b = pw.webkit.launch()
        except Exception as e:                             # WebKit not installed
            pytest.skip(f"WebKit unavailable: {e}")
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.goto(BASE)
        page.wait_for_selector("#timeline .tl")
        page.click('button.tl[data-id="testplan"]')
        page.wait_for_selector("#left .tocrow")
        yield page
        b.close()


def centre_scroll(page):
    return page.evaluate("document.getElementById('centre').scrollTop")


def in_view(page, sel):
    loc = page.locator(sel).first if isinstance(sel, str) else sel
    return loc.evaluate("""e => { const c = document.getElementById('centre').getBoundingClientRect(), r = e.getBoundingClientRect();
        return r.top >= c.top - 2 && r.bottom <= c.bottom + 2; }""")


# ---- UIR-09-R1: the heading list
def test_headings_unit(browser_page):
    """TP.headings on the server's html of the DEMO plan gives its ## and ### lines, in file order."""
    page = browser_page
    got = page.evaluate("""async () => { const r = await fetch('/api/testplan?i=0'); const d = await r.json(); return TP.headings(TP.sanitise(d.html)).map(h => [h.level, h.text]); }""")
    exp = expected_headings(PLAN)
    assert [tuple(g) for g in got] == exp
    assert len(exp) >= 8 and exp[0] == (2, "1. How it works")


def test_sidebar_lists_every_heading(browser_page, shots):
    page = browser_page
    rows = page.locator("#left .tocrow").evaluate_all("els => els.map(e => [e.classList.contains('l3') ? 3 : 2, e.textContent])")
    assert [tuple(r) for r in rows] == expected_headings(PLAN)
    page.screenshot(path=str(shots / "01_toc_sidebar.png"))


# ---- UIR-09-R2: click scrolls, current entry highlighted
def test_click_scrolls_and_highlights(browser_page, shots):
    page = browser_page
    assert centre_scroll(page) == 0 and page.locator("#left .tocrow.cur").inner_text() == "1. How it works"
    page.locator("#left .tocrow", has_text="Decision gate").click()
    assert until(lambda: page.locator("#left .tocrow.cur").inner_text() == "Decision gate")
    assert centre_scroll(page) > 100
    assert in_view(page, page.locator("#centre .tp-sec.l2", has_text="Decision gate").first.locator("> h2"))
    page.screenshot(path=str(shots / "02_click_scrolled_decision_gate.png"))
    page.locator("#left .tocrow", has_text="Pull-ups").click()
    assert until(lambda: page.locator("#left .tocrow.cur").inner_text() == "Pull-ups (R3, R4, R5)")
    # scrolling by hand moves the highlight
    page.evaluate("document.getElementById('centre').scrollTop = 0")
    assert until(lambda: page.locator("#left .tocrow.cur").inner_text() == "1. How it works")


# ---- UIR-10: folding
def test_fold_unfold_and_all(browser_page, shots):
    page = browser_page
    page.evaluate("document.getElementById('centre').scrollTop = 0")
    sec = page.locator("#centre .tp-sec.l3", has_text="Input and rails").first
    body = sec.locator("> .tp-sb")
    assert body.is_visible()
    sec.locator("> h3").click()                            # click on the heading folds
    assert not body.is_visible() and "folded" in sec.get_attribute("class")
    sec.locator("> h3 > .tp-tog").click()                  # and the arrow unfolds
    assert body.is_visible()
    page.click('[data-tp-all=fold]')
    assert page.locator("#centre .tp-sec:not(.folded)").count() == 0
    assert page.locator("#centre .tp-sec > h2").first.is_visible()      # headings stay, bodies go
    assert page.locator("#left .tocrow.cur").inner_text() == "1. How it works"          # folded headings do not confuse the highlight
    page.screenshot(path=str(shots / "03_collapse_all.png"))
    page.click('#centre .tp-sec.l2:has(> h2:text-is("Tier 1 — Unpowered")) > h2 > .tp-tog')   # unfold a parent: its subsections are still folded
    assert page.locator("#centre .tp-sec.l3.folded").count() >= 1
    page.click('[data-tp-all=unfold]')
    assert page.locator("#centre .tp-sec.folded").count() == 0
    page.screenshot(path=str(shots / "04_expand_all.png"))


def test_toc_click_unfolds_a_folded_target(browser_page):
    page = browser_page
    page.click('[data-tp-all=fold]')
    page.locator("#left .tocrow", has_text="Pull-ups").click()
    assert page.locator("#centre .tp-sec:has(> h3:has-text('Pull-ups'))").first.get_attribute("class").find("folded") < 0
    assert page.locator("#centre .tp-sec.l2:has(> h2:text-is('Tier 1 — Unpowered'))").get_attribute("class").find("folded") < 0     # its parent opened too
    page.click('[data-tp-all=unfold]')


def test_collapsed_state_survives_reload(browser_page, shots):
    page = browser_page
    page.click('[data-tp-all=unfold]')
    page.locator("#centre .tp-sec.l3", has_text="Pull-ups").first.locator("> h3").click()
    page.locator("#centre .tp-sec.l2", has_text="Decision gate").first.locator("> h2").click()
    folded = page.locator("#centre .tp-sec.folded").evaluate_all("els => els.map(e => e.dataset.tpsec)")
    assert len(folded) == 2
    page.reload()
    page.wait_for_selector("#timeline .tl")
    page.click('button.tl[data-id="testplan"]')
    page.wait_for_selector("#centre .tp-sec")
    assert page.locator("#centre .tp-sec.folded").evaluate_all("els => els.map(e => e.dataset.tpsec)") == folded
    page.locator("#centre .tp-sec.l3", has_text="Pull-ups").first.scroll_into_view_if_needed()
    page.evaluate("document.getElementById('centre').scrollTop -= 120")
    page.screenshot(path=str(shots / "05_after_reload_two_folded.png"))
    page.click('[data-tp-all=unfold]')


# ---- UIR-11: the bar
def test_bar_visible_at_the_fold_without_scrolling(browser_page, shots):
    page = browser_page
    page.evaluate("document.getElementById('centre').scrollTop = 0")
    bar = page.locator("#prbar")
    assert bar.is_visible() and in_view(page, "#prbar")
    assert "Continue to Batch setup" in bar.inner_text() and "Plan" in bar.inner_text()
    page.screenshot(path=str(shots / "06_bar_no_draft_top.png"))
    page.evaluate("document.getElementById('centre').scrollTop = 99999")
    assert in_view(page, "#prbar")
    page.screenshot(path=str(shots / "07_bar_no_draft_bottom.png"))
    page.evaluate("document.getElementById('centre').scrollTop = 0")


def test_continue_goes_to_batch_setup(browser_page, shots):
    page = browser_page
    page.click("#prnext")
    page.wait_for_selector("text=Batch TP-E2E")
    page.screenshot(path=str(shots / "08_continue_lands_on_batch_setup.png"))
    page.click('button.tl[data-id="testplan"]')
    page.wait_for_selector("#prbar")


def test_draft_state_bar_and_approve_stays_operator_only(browser_page, server, shots):
    page = browser_page
    new = PLAN.replace("| U06 | R4 across its pads |", "| U06 | R4 across its two pads |")
    req = urllib.request.Request(BASE + "api/ai/plan_draft", json.dumps({"text": new, "summary": "U06 wording"}).encode(), AI, method="POST")
    assert json.load(urllib.request.urlopen(req, timeout=30))["ok"]
    assert until(lambda: page.locator("#prapprove").count() == 1, 30)
    assert in_view(page, "#prbar") and "Draft waiting" in page.inner_text("#prbar") and "U06 wording" in page.inner_text("#prbar")
    page.screenshot(path=str(shots / "09_bar_draft_waiting.png"))
    page.click("#prapprove")                               # the same operator dialog as the panel's button
    page.wait_for_selector("text=Approve the AI draft")
    page.screenshot(path=str(shots / "10_bar_approve_dialog.png"))
    page.keyboard.press("Escape")
    # an assistant call to the same route is refused (the existing guard), and the master is unchanged
    st = json.load(urllib.request.urlopen(urllib.request.Request(BASE + "api/ai/plan_draft", headers=AI), timeout=30))
    req = urllib.request.Request(BASE + "api/ai/plan_approve", json.dumps({"hash": st["hash"], "operator": "x"}).encode(), AI, method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=30)
    assert ei.value.code == 403
    assert (server / "PLAN.md").read_text() == PLAN


def test_bar_at_900_wide(browser_page, shots):
    page = browser_page
    page.set_viewport_size({"width": 900, "height": 700})
    page.evaluate("document.getElementById('centre').scrollTop = 0")
    assert in_view(page, "#prbar")
    page.screenshot(path=str(shots / "11_bar_900x700.png"))
    page.set_viewport_size({"width": 1280, "height": 900})
    errs = [e for e in page.errors if "/visibility" not in e]       # the scanner/voice visibility fetches fail the WebKit access-control check on main too (not this lane)
    assert not errs, errs
