"""Screenshots of the D3 panel placement and the UX 2 approve dialog (Playwright WebKit) against a staging server (QA_URL, default 8920).
Needs a pending proposal and an open question; this script creates both through the assistant routes. Usage: ui_fixes.py <shots dir>"""
import json, os, sys, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = os.environ.get("QA_URL", "http://127.0.0.1:8920")
PROJ = Path(os.environ["QA_PROJ"])
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
AI = {"X-Polybench-Client": "assistant", "X-Polybench-Assistant": "ui", "X-Polybench-Session": "ui1", "Content-Type": "application/json"}


def post(path, body):
    r = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST", headers=AI)
    try:
        return json.loads(urllib.request.urlopen(r, timeout=20).read())
    except urllib.error.HTTPError as e:
        return {"http": e.code, **json.loads(e.read() or b"{}")}


plan = (PROJ / "PLAN.md").read_text()
new = plan.replace("| U05 | R3 across its pads |", "| U07 | R3 across its pads, fresh |")
print("draft", post("/api/ai/plan_draft", {"text": new, "summary": "replace U05 by U07"}).get("ok"))
print("propose", post("/api/ai/propose", {"board": "S01", "test_id": "U02", "value": "2.4", "reason": "operator read it aloud"}))
print("ask", post("/api/ai/ask", {"text": "Which meter range did you use on U02?", "options": ["200 kOhm", "2 MOhm", "auto"], "board": "S01", "test_id": "U02"}).get("qid"))

with sync_playwright() as p:
    b = p.webkit.launch()
    for w, h in ((1500, 900), (1100, 800)):
        ctx = b.new_context(viewport={"width": w, "height": h}); page = ctx.new_page(); errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(URL); page.wait_for_timeout(1500)
        page.evaluate("() => { ui.step = 'unpowered'; ui.board = 'S01'; ui.test = 'U02'; render(); }"); page.wait_for_timeout(1000)
        q = page.locator("#aiq"); qb = q.bounding_box()
        btns = page.locator("#centre button:visible", has_text="Accept")
        rej = page.locator("#centre button:visible", has_text="Reject")
        print(w, h, "question panel", qb, "accept buttons", btns.count(), "reject", rej.count())
        for loc, nm in ((btns, "accept"), (rej, "reject")):
            if loc.count():
                bb = loc.first.bounding_box()
                over = not (bb["y"] + bb["height"] <= qb["y"] or bb["y"] >= qb["y"] + qb["height"] or bb["x"] + bb["width"] <= qb["x"] or bb["x"] >= qb["x"] + qb["width"])
                top = page.evaluate("([x,y]) => { const e = document.elementFromPoint(x,y); return e ? (e.closest('button') ? e.closest('button').textContent.trim() : e.tagName + '#' + e.id) : null }", [bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2])
                print("  ", nm, bb, "overlaps panel:", over, "element at its centre:", top, "in viewport:", bb["y"] + bb["height"] <= h)
        print("  scrollWidth", page.evaluate("document.documentElement.scrollWidth"))
        page.screenshot(path=str(OUT / f"panel_{w}x{h}.png"))
        if (w, h) == (1500, 900):
            page.evaluate("() => { ui.step = 'testplan'; render(); }"); page.wait_for_timeout(2000)
            page.locator("[data-pr=approve]").scroll_into_view_if_needed(); page.screenshot(path=str(OUT / "plan_review_draft.png"))
            page.locator("[data-pr=approve]").click(); page.wait_for_timeout(500)
            page.screenshot(path=str(OUT / "approve_dialog.png"))
            print("  dialog text:", page.locator(".dlg p").inner_text())
            page.locator(".dlg [data-cancel]").click()
            page.evaluate("() => { ui.step = 'project'; render(); }"); page.wait_for_timeout(800)
            page.locator("#aicard").scroll_into_view_if_needed(); page.screenshot(path=str(OUT / "card5_trial.png"))
        print("  js errors", errs)
        ctx.close()
    b.close()
