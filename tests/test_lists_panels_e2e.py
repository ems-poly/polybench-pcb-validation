"""Lists and panels, UIR-17..21: collapsible group headings and the count bar, the Journal filters (no Assembly log tab), one Plugins
entry, the Assembly aid start page and the assembly check groups in build order. HOST-ONLY.
Own server on a free port (PB_LP_PORT overrides), DEMO protocol, simulated bench, Playwright WebKit (skipped without
playwright or WebKit). Screenshots go to PB_SHOTS (default: a temp folder)."""
from __future__ import annotations

import contextlib
import json
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

from helpers import DEMO, free_port

PORT = free_port("PB_LP_PORT")          # a free port per run: a fixed one is shared with parallel runs and answers for the wrong server
BASE = f"http://127.0.0.1:{PORT}/"
TOKEN = "lp-test-token"

# DEMO assembly check groups for UIR-21: two before placement, one after, one with no declared position.
ASM_GROUPS = """
    sections:
      - id: paste
        title: Paste print
        position: before_placement
        tests:
          - {id: A01, title: Stencil aligned, kind: yn}
          - {id: A02, title: Paste volume even, kind: yn}
      - id: prep
        title: Board prep
        position: before_placement
        tests:
          - {id: A03, title: Board clean and dry, kind: yn}
      - id: placecheck
        title: Placement check
        position: after_placement
        tests:
          - {id: A04, title: Polarity checked, kind: yn}
      - id: reflow
        title: Reflow and clean
        position: after_placement
        tests:
          - {id: A05, title: Profile run, kind: yn}
      - id: legacy
        title: Final look
        tests:
          - {id: A06, title: Visual check, kind: yn}
"""


# the same kinds of groups, declared in a different order in the file than they are drawn in
ASM_SCRAMBLED = """
    sections:
      - {id: post1, title: Placement check, position: after_placement, tests: [{id: B01, title: Polarity checked, kind: yn}]}
      - {id: paste, title: Paste print, position: before_placement, tests: [{id: B02, title: Stencil aligned, kind: yn}, {id: B03, title: Paste volume even, kind: yn}]}
      - {id: plain, title: Final look, tests: [{id: B04, title: Visual check, kind: yn}]}
      - {id: prep, title: Board prep, position: before_placement, tests: [{id: B05, title: Board clean, kind: yn}]}
"""


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


def post(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Cookie": "pb_op=" + TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read() or b"{}")}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


@contextlib.contextmanager
def server(tmp, boards=4, groups=False, name="b"):
    assert PORT != 8750
    proj = Path(tmp) / f"proj-{name}"
    shutil.copytree(DEMO, proj)
    if groups:
        a = proj / "assembly.yaml"
        a.write_text(a.read_text().replace("    title: Assembly aid\n", "    title: Assembly aid\n" + (ASM_GROUPS if groups is True else groups).lstrip("\n"), 1))
    env = {**os.environ, "POLYBENCH_OP_TOKEN": TOKEN}
    env.pop("POLYBENCH_AI_PERMISSIONS", None)
    (Path(tmp) / "frames").mkdir(exist_ok=True)
    env["POLYBENCH_SCANNER_FAKE_FRAMES"] = str(Path(tmp) / "frames")          # recorded frames: this test never opens a real camera
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(proj / "demo.pcbv.yaml"), "--simulate",
                            "--data-dir", str(Path(tmp) / f"data-{name}"), "--batch", "LP-E2E", "--boards", str(boards), "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        assert until(lambda: srv.poll() is None and urllib.request.urlopen(BASE + "api/state", timeout=5).status == 200, 30), "server did not start"
        yield proj
    finally:
        srv.terminate()                                    # our own PID only
        with contextlib.suppress(Exception):
            srv.wait(10)


@pytest.fixture(scope="module")
def shots(tmp_path_factory):
    d = Path(os.environ.get("PB_SHOTS") or tmp_path_factory.mktemp("shots"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="module")
def pw():
    with sync_playwright() as p:
        try:
            b = p.webkit.launch()
        except Exception as e:                             # WebKit not installed here
            pytest.skip(f"WebKit not available: {e}")
        yield b
        b.close()


@pytest.fixture
def page(pw):
    ctx = pw.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    ctx.close()


def open_step(pg, step):
    pg.goto(BASE)
    pg.wait_for_selector("#timeline [data-act=step]", timeout=15000)
    pg.click(f"#timeline [data-act=step][data-id={step}]")
    pg.wait_for_timeout(300)


def fill_unpowered(boards, passing=True, only=None):
    """Enter passing values for the Unpowered tests: every test of the given sections (default all) on the given boards."""
    for b in boards:
        post("api/cmd/override_gate", {"board": b, "step": "unpowered", "reason": "DEMO: visual not part of this check"})
    vals = {"U01": "50", "U02": "20", "U03": "10", "U04": "y", "U05": "ok"}
    for b in boards:
        for tid, v in vals.items():
            if only and tid not in only:
                continue
            r = post("api/cmd/entry", {"board": b, "test_id": tid, "value": v})
            assert r.get("ok"), r


def shot(pg, shots, name):
    pg.wait_for_timeout(250)
    pg.screenshot(path=str(shots / f"{name}.png"))


# ---------------------------------------------------------------- UIR-17: group headings

def head(pg, key):
    return pg.locator(f'#left .sec-h[data-k="{key}"]')


def rows(pg):
    return pg.locator("#left .trow[data-act=test]")


def cur_test(pg):
    return pg.evaluate("ui.test")


def test_uir17_r1_collapse_all_pass_current_open(tmp_path, page, shots):
    """R1: a fully passed group starts collapsed, the group holding the current test is open, a heading toggles (mouse and keyboard)."""
    with server(tmp_path, boards=4):
        fill_unpowered(["S01", "S02", "S03", "S04"], only={"U01", "U02", "U03"})
        open_step(page, "unpowered")
        rails, cont = head(page, "unpowered|rails"), head(page, "unpowered|continuity")
        assert rails.get_attribute("aria-expanded") == "false"                       # every board passed: starts folded
        assert cont.get_attribute("aria-expanded") == "true"                         # holds the current test (U04)
        assert [r.get_attribute("data-id") for r in rows(page).all()] == ["U04", "U05"]
        assert rails.locator(".dot.s-pass").count() == 4                             # name, count and one square per board
        assert "12/12" in rails.inner_text()
        shot(page, shots, "uir17_unpowered_4boards_collapsed")
        rails.click()                                                                 # mouse opens it
        assert rails.get_attribute("aria-expanded") == "true"
        assert [r.get_attribute("data-id") for r in rows(page).all()] == ["U01", "U02", "U03", "U04", "U05"]
        shot(page, shots, "uir17_unpowered_4boards_expanded")
        rails.focus()
        page.keyboard.press("Enter")                                                  # keyboard folds it again
        assert head(page, "unpowered|rails").get_attribute("aria-expanded") == "false"
        cont.click()                                                                  # the current group cannot be folded
        assert cont.get_attribute("aria-expanded") == "true"
        assert not page.errors or all("visibility" in e or "access control" in e for e in page.errors)


def test_uir17_r1_failed_group_stays_open(tmp_path, page):
    with server(tmp_path, boards=4):
        fill_unpowered(["S01", "S02", "S03"], only={"U01", "U02", "U03"})
        post("api/cmd/override_gate", {"board": "S04", "step": "unpowered", "reason": "DEMO"})
        post("api/cmd/entry", {"board": "S04", "test_id": "U01", "value": "5", "confirm": True})          # a FAIL on S04
        open_step(page, "unpowered")
        assert head(page, "unpowered|rails").get_attribute("aria-expanded") == "true"                     # not all passed


def test_uir17_r2_count_bar_above_12_boards(tmp_path, page, shots):
    """R2: above 12 boards the squares become one bar of pass, fail and open counts (simulated 24-board batch)."""
    with server(tmp_path, boards=24):
        boards = [f"S{i:02d}" for i in range(1, 25)]
        for b in boards:
            post("api/cmd/override_gate", {"board": b, "step": "unpowered", "reason": "DEMO"})
        for b in boards[:22]:
            for tid, v in {"U01": "50", "U02": "20", "U03": "10"}.items():
                assert post("api/cmd/entry", {"board": b, "test_id": tid, "value": v}).get("ok")
        assert post("api/cmd/entry", {"board": "S23", "test_id": "U01", "value": "5", "confirm": True}).get("ok")     # one FAIL
        open_step(page, "unpowered")
        rails = head(page, "unpowered|rails")
        assert rails.get_attribute("aria-expanded") == "true"                                              # a FAIL and an open board: not folded by itself
        rails.click()                                                                                       # fold it by hand
        rails = head(page, "unpowered|rails")
        assert rails.locator(".dot").count() == 0 and rails.locator(".cbar").count() == 1                 # a bar, not 24 squares
        assert rails.locator(".cbar").get_attribute("data-n") == "22/1/1"                                  # S01-S22 pass, S23 failed, S24 open
        shot(page, shots, "uir17_unpowered_24boards")
        rails.click()
        page.wait_for_timeout(200)
        assert page.locator("#left .trow[data-act=test] .cbar").count() == 5 and page.locator("#left .trow .dot").count() == 0
        assert page.locator('#left .trow[data-id=U01] .cbar').get_attribute("data-n") == "22/1/1"
        shot(page, shots, "uir17_unpowered_24boards_expanded")


def test_uir17_r3_keyboard_skips_folded_and_opens(tmp_path, page, shots):
    """R3: Up/Down skip rows of folded groups; with nothing showing in that direction the neighbour is taken and its group opens."""
    with server(tmp_path, boards=2, groups=True):
        open_step(page, "assembly")
        page.click("#asmbegin")
        page.click("#left .trow[data-id=A01]")
        assert cur_test(page) == "A01"
        head(page, "assembly|prep").click()
        head(page, "assembly|placecheck").click()
        assert head(page, "assembly|prep").get_attribute("aria-expanded") == "false"
        page.click("#val")
        seq = []
        for _ in range(4):
            page.keyboard.press("ArrowDown")
            seq.append(cur_test(page))
        assert seq == ["A02", "A05", "A06", "A06"], seq                               # A03 and A04 are folded: skipped
        for _ in range(2):
            page.keyboard.press("ArrowUp")
        assert cur_test(page) == "A02"                                                # A05 -> A02, A03/A04 skipped
        # fold the paste group's neighbours so nothing shows above A05, then Up steps into the folded group and opens it
        page.keyboard.press("ArrowDown")
        assert cur_test(page) == "A05"
        head(page, "assembly|paste").click()
        assert head(page, "assembly|paste").get_attribute("aria-expanded") == "false"
        page.keyboard.press("ArrowUp")
        assert cur_test(page) == "A04"                                                # nothing showing above: placecheck opens
        assert head(page, "assembly|placecheck").get_attribute("aria-expanded") == "true"
        assert page.locator("#left .trow[data-id=A04]").count() == 1
        page.keyboard.press("ArrowUp")
        assert cur_test(page) == "A03" and head(page, "assembly|prep").get_attribute("aria-expanded") == "true"


# ---------------------------------------------------------------- UIR-18: Assembly log folded into the Journal

def test_uir18_journal_filters_replace_assembly_log_tab(tmp_path, page, shots):
    """R1: no Assembly log tab; the Journal's Assembly filter shows the same events the tab showed (count compared with the state's log)."""
    with server(tmp_path, boards=4):
        assert post("api/cmd/place_batch", {"line": 4, "boards": ["S01", "S02"]}).get("ok")
        assert post("api/cmd/scan", {"code": "187-CL05B104KO5NNNC", "via": "typed"}).get("ok")
        assert post("api/cmd/scan", {"code": "NOT-A-PART", "via": "typed"}).get("ok") is not None
        assert post("api/cmd/auto_cancel", {"line": 4, "reason": "esc"}).get("ok")
        post("api/cmd/serial", {"board": "S01", "serial": "DEMO-001"})
        st = get("api/state")
        alog = st["alog"]
        assert len(alog) == 4 and {r["kind"] for r in alog} == {"place", "scan", "scan_miss", "auto_cancel"}
        open_step(page, "unpowered")
        tabs = page.locator("#bottom .btabs .btab").all_inner_texts()
        assert not any("Assembly log" in t for t in tabs), tabs
        page.click("#bottom [data-act=btab][data-id=journal]")
        page.click("#bottom [data-nt=view][data-v=events]")
        page.wait_for_selector("#jfilters .jchip")
        chips = {b.get_attribute("data-f"): b.inner_text() for b in page.locator("#jfilters .jchip").all()}
        assert list(chips) == ["all", "assembly", "results", "voice", "actions"]
        page.click('#jfilters [data-f=assembly]')
        n_asm = page.locator("#evlist .jr").count()
        assert n_asm == len(alog)                                                      # the same events the tab showed
        types = set(page.locator("#evlist .jr .ty").all_inner_texts())
        assert types <= {"placed", "placed_batch", "auto_cancel", "scan"} and "placed_batch" in types
        page.click('#jfilters [data-f=all]')
        assert page.locator("#evlist .jr").count() > n_asm                            # the serial event and the batch start are not assembly
        shot(page, shots, "uir18_journal_all")
        page.click('#jfilters [data-f=assembly]')
        shot(page, shots, "uir18_journal_assembly_filter")
        page.click('#jfilters [data-f=voice]')
        assert "No voice events" in page.inner_text("#evlist")
        # the Assembly aid's own box opens the Journal on the Assembly filter
        open_step(page, "assembly")
        page.click("#bottom [data-act=toggleBottom]") if page.locator("#bottom.collapsed").count() == 0 else None
        page.click("[data-act=asmjournal]")
        page.wait_for_selector("#jfilters .jchip.on[data-f=assembly]")
        assert page.locator("#evlist .jr").count() == len(alog)


# ---------------------------------------------------------------- UIR-19: one Plugins entry

def test_uir19_one_plugins_entry_with_count_and_colour(tmp_path, page, shots):
    """R1: one Plugins entry, 'Plugins N', amber when a used plugin is not loaded, red when one fails to import; it opens the Plugins tab."""
    with server(tmp_path, boards=2):
        open_step(page, "unpowered")
        bar = page.locator("#bottom .btabs")
        assert bar.locator(":scope > *", has_text="Plugins").count() == 1                 # not a tab and a chip
        assert page.locator("#bottom .pchip").count() == 0
        total = get("api/state")["plugins"]["total"]
        assert bar.locator(".btab-plugins").inner_text().strip() == f"Plugins {total}"
        assert "warn" not in bar.locator(".btab-plugins").get_attribute("class") and "bad" not in bar.locator(".btab-plugins").get_attribute("class")
        shot(page, shots, "uir19_bottom_bar_ok")
        for cls, plug in (("warn", {"attention": 1, "errors": 0}), ("bad", {"attention": 1, "errors": 1})):
            page.evaluate("p => { S.plugins = { ...S.plugins, ...p }; renderBottom(); }", plug)
            assert cls in page.locator("#bottom .btab-plugins").get_attribute("class").split()
            col = page.evaluate("getComputedStyle(document.querySelector('.btab-plugins')).color")
            assert col not in ("", None)
            shot(page, shots, f"uir19_bottom_bar_{'amber' if cls == 'warn' else 'red'}")
        page.click("#bottom .btab-plugins")
        page.wait_for_selector(".pl-tab")
        assert "on" in page.locator("#bottom .btab-plugins").get_attribute("class").split()
        shot(page, shots, "uir19_plugins_open")
        # the welcome page (no project) has the same single entry
        page.evaluate("S = null; renderBottomWelcome()")
        assert page.locator("#bottom .btabs .btab").count() == 1


# ---------------------------------------------------------------- UIR-20: Assembly aid start page

def journal_types():
    return [e["type"] for e in get("api/journal?limit=500")]


def test_uir20_start_page_skip_reverse_and_no_camera_before_assemble(tmp_path, page, shots):
    """R1: a batch with no placements opens on the start card. R2: skip and reverse are journaled, come back after a reload.
    R3: no /api/scanner request (so no camera, no stream) before Assemble, on the start page or the skipped page."""
    with server(tmp_path, boards=3):
        scanner_calls = []
        page.on("request", lambda r: scanner_calls.append(r.url) if "/api/scanner" in r.url else None)
        open_step(page, "assembly")
        page.wait_for_selector("#asmstart")
        txt = page.inner_text("#asmstart")
        assert "Scan a component bag and see where it goes on your board in Fusion. Needs a matched BOM." in txt      # ecadName(): the active program, Fusion by default (KC-43)
        assert page.locator("#asmstart svg.asmicon").count() == 1                                   # icon
        assert page.locator("#asmbegin").inner_text() == "Assemble" and "already built" in page.locator("#asmskip").inner_text()
        assert page.locator("#scanner, #scan").count() == 0                                         # no scanner page, no camera preview
        assert "press Assemble" in page.inner_text("#left")
        shot(page, shots, "uir20_assembly_start_page")
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")                       # the page-visibility hook stays quiet too
        page.wait_for_timeout(1500)                                                                  # the poll is 500 ms: it would have fired
        assert scanner_calls == [] and not get("api/state")["assembly"]["since"]
        # skip
        page.click("#asmskip")
        page.wait_for_selector("#asmunskip")
        assert "Skipped (boards already built)" in page.inner_text("#asmstart") and page.locator("#asmbegin").count() == 0
        assert journal_types().count("assembly_skip") == 1
        shot(page, shots, "uir20_assembly_skipped")
        # it survives a reload and a visit to another step (state rebuilds from the journal)
        page.reload()
        page.wait_for_selector("#timeline [data-act=step]")
        page.click("#timeline [data-act=step][data-id=unpowered]")
        page.click("#timeline [data-act=step][data-id=assembly]")
        page.wait_for_selector("#asmunskip")
        page.wait_for_timeout(1200)
        assert scanner_calls == []
        # change their mind
        page.click("#asmunskip")
        page.wait_for_selector("#scanner")
        assert journal_types().count("assembly_unskip") == 1 and journal_types().count("assembly_skip") == 1   # append-only: nothing rewritten
        until(lambda: scanner_calls)
        assert scanner_calls                                                                          # the scanner is used only now
        assert page.locator("#left .bomrow").count() >= 8
        shot(page, shots, "uir20_assembly_reversed")
        # a batch that already has a placement is never shown the start page
        assert post("api/cmd/place_batch", {"line": 4, "boards": ["S01"]}).get("ok")
        assert get("api/state")["assembly"]["mode"] == "assembling"


def test_uir20_assemble_button_starts_and_journals(tmp_path, page, shots):
    with server(tmp_path, boards=3):
        open_step(page, "assembly")
        page.click("#asmbegin")
        page.wait_for_selector("#scanner")
        assert journal_types().count("assembly_begin") == 1
        page.reload()
        page.wait_for_selector("#timeline [data-act=step]")
        page.click("#timeline [data-act=step][data-id=assembly]")
        page.wait_for_selector("#scanner")                                                           # not asked again
        assert page.locator("#asmstart").count() == 0


# ---------------------------------------------------------------- UIR-21: assembly check groups in build order

def left_headings(pg):
    return [t.strip() for t in pg.locator("#left .sec-h .sh-t").all_text_contents()]       # textContent: the CSS upper-cases what is drawn


def test_uir21_groups_in_build_order(tmp_path, page, shots):
    """R1: groups declared before placement sit above To do, the others below; R2: a group with no position stays below."""
    with server(tmp_path, boards=2, groups=True):
        open_step(page, "assembly")
        page.click("#asmbegin")
        page.wait_for_selector("#left .bomsec")
        assert left_headings(page) == ["Paste print", "Board prep", "To do", "Placement check", "Reflow and clean", "Final look"]
        y = {n: page.locator(f'#left .sec-h[data-k="assembly|{k}"]').bounding_box()["y"] for n, k in
             (("paste", "paste"), ("prep", "prep"), ("todo", "todo"), ("post", "placecheck"), ("reflow", "reflow"), ("legacy", "legacy"))}
        assert y["paste"] < y["prep"] < y["todo"] < y["post"] < y["reflow"] < y["legacy"]
        shot(page, shots, "uir21_assembly_list_build_order")
        page.click("#left .trow[data-id=A02]")                                                 # a paste check opens in the centre like any test
        assert "Paste volume even" in page.inner_text("#centre")
        shot(page, shots, "uir21_assembly_paste_check_open")


def test_uir21_keyboard_follows_the_drawn_order(tmp_path, page):
    """Up/Down walk the tests in the order the list draws them, not the order of the file."""
    with server(tmp_path, boards=2, groups=ASM_SCRAMBLED):
        open_step(page, "assembly")
        page.click("#asmbegin")
        page.wait_for_selector("#left .bomsec")
        assert left_headings(page) == ["Paste print", "Board prep", "To do", "Placement check", "Final look"]
        page.click("#left .trow[data-id=B02]")
        page.click("#val")
        seq = []
        for _ in range(4):
            page.keyboard.press("ArrowDown")
            seq.append(cur_test(page))
        assert seq == ["B03", "B05", "B01", "B04"], seq


def test_uir21_no_declared_groups_no_tips(tmp_path, page, shots):
    """R3: the DEMO protocol declares no assembly groups: the list is the BOM and nothing else."""
    with server(tmp_path, boards=2):
        open_step(page, "assembly")
        page.click("#asmbegin")
        page.wait_for_selector("#left .bomsec")
        assert left_headings(page) == ["To do"]
        assert page.locator("#left .trow[data-act=test]").count() == 0
        low = page.inner_text("#left").lower()
        assert "paste" not in low and "stencil" not in low and "reflow" not in low
        shot(page, shots, "uir21_assembly_list_no_groups")
