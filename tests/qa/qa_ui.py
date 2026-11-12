"""QA UI pass in Playwright WebKit against staging 8903: card 5 at two sizes, question panel, step 4 plan review, two tabs,
narrow window. Screenshots to /private/tmp/claude-502/qa-ai/shots/. Usage: qa_ui.py <phase>  (card | plan | narrow | all)"""
import json, sys, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

import os
URL = os.environ.get("QA_URL", "http://127.0.0.1:8920")
OUT = Path(os.environ.get("QA_SHOTS", "/private/tmp/claude-502/pbai_shots")); OUT.mkdir(exist_ok=True, parents=True)
AI = {"X-Polybench-Client": "assistant", "X-Polybench-Assistant": "qa-ui", "X-Polybench-Session": "ui1", "Content-Type": "application/json"}
phase = sys.argv[1] if len(sys.argv) > 1 else "all"


def post(path, body, ai=True):
    r = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST", headers=AI if ai else {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def goto_step(page, step):
    page.evaluate(f"() => {{ ui.step = '{step}'; render(); }}")
    page.wait_for_timeout(600)


with sync_playwright() as p:
    b = p.webkit.launch()
    notes = []
    for w, h in ((1500, 900), (1100, 800)):
        ctx = b.new_context(viewport={"width": w, "height": h})
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.goto(URL); page.wait_for_timeout(1500)
        if phase in ("card", "all"):
            goto_step(page, "project")
            card = page.locator("#aicard")
            notes.append(f"{w}x{h} card5 present={card.count()} text_has_privacy={'leaves this Mac' in (card.inner_text() if card.count() else '')}")
            if card.count():
                card.scroll_into_view_if_needed(); page.wait_for_timeout(300)
                bb = card.bounding_box(); notes.append(f"  card bbox={bb}; page scrollWidth={page.evaluate('document.documentElement.scrollWidth')} viewport={w}")
                pre = page.locator("#aic-claude"); notes.append(f"  claude cmd: {pre.inner_text()[:160]}")
            page.screenshot(path=str(OUT / f"card5_{w}x{h}.png"), full_page=False)
            # question panel: ask through the assistant identity, then screenshot the workspace on unpowered
            code, r = post("/api/ai/ask", {"text": f"QA question at {w}: which meter range did you use on U01?", "options": ["200 kOhm", "2 MOhm", "auto"], "board": "S01", "test_id": "U01"})
            page.wait_for_timeout(1200)
            goto_step(page, "unpowered")
            q = page.locator("#aiq"); notes.append(f"  question panel present={q.count()} (ask -> {code})")
            page.screenshot(path=str(OUT / f"question_{w}x{h}.png"))
            # answer with the option button, then check the question is gone
            if q.count():
                page.locator("#aiq [data-aiq-opt]").first.click(); page.wait_for_timeout(1200)
                notes.append(f"  after answering: panel present={page.locator('#aiq').count()}")
        if phase in ("plan", "all"):
            goto_step(page, "testplan")
            page.wait_for_timeout(1500)
            prv = page.locator("#prv"); notes.append(f"{w}x{h} step4 draft panel present={prv.count()}; approve enabled={(not page.locator('[data-pr=approve]').is_disabled()) if prv.count() else None}")
            if prv.count():
                prv.scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / f"plan_review_{w}x{h}.png"))
        notes.append(f"  js errors: {errs[:5]}")
        ctx.close()
    if phase in ("narrow", "all"):
        ctx = b.new_context(viewport={"width": 760, "height": 700}); page = ctx.new_page(); page.goto(URL); page.wait_for_timeout(1200)
        goto_step(page, "project"); page.locator("#aicard").scroll_into_view_if_needed(); page.wait_for_timeout(300)
        notes.append(f"760 wide: scrollWidth={page.evaluate('document.documentElement.scrollWidth')}")
        page.screenshot(path=str(OUT / "card5_760.png")); ctx.close()
    if phase in ("tabs", "all"):
        # two tabs: both on step 4 with a draft; approve in tab A, then press approve in tab B (stale hash) -> expect a toast error, no second approval
        ctx = b.new_context(viewport={"width": 1500, "height": 900}); A = ctx.new_page(); B = ctx.new_page()
        for pg in (A, B):
            pg.goto(URL); pg.wait_for_timeout(1200); goto_step(pg, "testplan"); pg.wait_for_timeout(1500)
        notes.append(f"tabs: A panel={A.locator('#prv').count()} B panel={B.locator('#prv').count()}")
        if A.locator("#prv").count() and not A.locator("[data-pr=approve]").is_disabled():
            A.locator("[data-pr=approve]").click(); A.wait_for_timeout(500)
            A.screenshot(path=str(OUT / "tabs_A_dialog.png"))
            A.locator("dialog input, .dlg input, input[name=operator]").first.fill("Pat"); A.keyboard.press("Enter"); A.wait_for_timeout(1500)
            notes.append(f"  A toast: {A.locator('.toast, #toast').all_inner_texts()[:2]}")
            A.screenshot(path=str(OUT / "tabs_A_after.png"))
            # B still shows the old panel? press approve there
            B.wait_for_timeout(500)
            notes.append(f"  B panel still shown={B.locator('#prv').count()} approve disabled={B.locator('[data-pr=approve]').is_disabled() if B.locator('[data-pr=approve]').count() else 'n/a'}")
            if B.locator("[data-pr=approve]").count() and not B.locator("[data-pr=approve]").is_disabled():
                B.locator("[data-pr=approve]").click(); B.wait_for_timeout(500)
                B.locator("dialog input, .dlg input, input[name=operator]").first.fill("Pat"); B.keyboard.press("Enter"); B.wait_for_timeout(1500)
                notes.append(f"  B toast: {B.locator('.toast, #toast').all_inner_texts()[:2]}")
            B.screenshot(path=str(OUT / "tabs_B_after.png"))
        ctx.close()
    b.close()
print("\n".join(notes))
