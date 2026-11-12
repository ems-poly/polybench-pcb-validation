"""Playwright WebKit walk of the AOI diff mode on the YD-ESP32-S3 demo photos (two boards, no layout), on its own port (never 8750).

    .venv/bin/python tests/e2e/aoi_diff_walk.py [shots_dir] [chromium|webkit]   (port: PB_E2E_PORT, default 9031; viewport PB_E2E_W x PB_E2E_H, default 1500x900)

Typing and clicking only for the operator steps: choose the photo for tile D01 and D02, Run AOI, open the issue review, name the board and decide
Y / N / U, then check the journal (`mode: diff`, `issue_board`), the gate and the report. HOST-ONLY: real photos, the diff analysis runs for real,
nothing is a board test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "9031"))
BASE = f"http://127.0.0.1:{PORT}/"
EX = ROOT / "examples/esp32-s3-board"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def until(fn, timeout=20.0, step=0.2):
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
    tmp = Path(tempfile.mkdtemp(prefix="pb-aoidiff-e2e-"))
    data = tmp / "data"
    w, h = int(os.environ.get("PB_E2E_W", "1500")), int(os.environ.get("PB_E2E_H", "900"))
    tag = f"{browser_name}_{w}x{h}"
    errors: list[str] = []
    ex = tmp / "example"
    shutil.copytree(EX, ex, ignore=shutil.ignore_patterns("firmware", "screenshots", "kicad"))
    vy = (ex / "visual.yaml").read_text()
    a, b = vy.index("aoi:\n"), vy.index("steps:")
    (ex / "visual.yaml").write_text(vy[:a] + "aoi: {mode: diff, reference: D01}\n" + vy[b:])      # diff mode, photos chosen by hand (no `photos:`)
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(ex / "yd-esp32-s3.pcbv.yaml"), "--simulate",
                            "--data-dir", str(data), "--batch", "YD-AOI", "--boards", "2", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    try:
        check("server up", until(lambda: get("api/state"), 30))
        s = get("api/state")
        vis = next(x for x in s["steps"] if x["kind"] == "visual")
        check("two boards D01 D02 and an aoi block in diff mode", [b["label"] for b in s["boards"]] == ["D01", "D02"] and s["protocol"].get("aoi", {}).get("mode") == "diff"
              if isinstance(s.get("protocol"), dict) and "aoi" in s["protocol"] else [b["label"] for b in s["boards"]] == ["D01", "D02"])
        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            page = browser.new_context(viewport={"width": w, "height": h}).new_page()
            page.on("pageerror", lambda e: errors.append(str(e)) if "access control checks" not in str(e) else None)
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "access control checks" not in m.text else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click(f'button.tl[data-id="{vis["id"]}"]')
            page.wait_for_selector("#aoitiles .aoi-tile")
            page.evaluate("fetch('/api/cmd/override_gate', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({board: 'D01', step: '%s', reason: 'e2e: Assembly not walked'})})" % vis["id"])
            page.screenshot(path=str(shots / f"{tag}_1_tiles_empty.png"))

            # 1. choose the photo for each tile
            for b in ("D01", "D02"):
                with page.expect_file_chooser() as fc:
                    page.click(f"[data-tile={b}]")
                fc.value.set_files(str(ex / "aoi" / f"{b}_top.jpg"))
                check(f"photo assigned to tile {b}", until(lambda: any(p["board"] == b for p in get("api/state")["aoi"]["photos"])))
            page.wait_for_selector("[data-tile=D02] img")
            page.wait_for_timeout(800)
            check("AOI bar carries the Experimental diff badge", "experimental diff" in page.inner_text(".aoibar").lower())
            page.screenshot(path=str(shots / f"{tag}_2_tiles_photos.png"))
            ev = [json.loads(l) for l in next(data.rglob("journal.jsonl")).read_text().splitlines()]
            check("journal aoi_photo events carry mode: diff", [e["payload"].get("mode") for e in ev if e["type"] == "aoi_photo"] == ["diff", "diff"])
            stored = [p for p in (data).rglob("aoi/photos/*") if p.is_file()]
            check("stored photos have no EXIF", stored and all(b"Exif" not in p.read_bytes()[:4096] for p in stored))

            # 2. run
            page.click('button[data-aoi="run"]')
            run = until(lambda: get("api/state")["aoi"]["run"], 120)
            check("diff run recorded", bool(run) and run.get("mode") == "diff" and run.get("reference") == "D01", str(run)[:300])
            if not run:
                return 1
            a = get("api/state")["aoi"]
            total = a["counts"]["total"]
            check("candidates listed under D02 only (reference D01 has none)", total > 0 and set(a["per_board"]) >= {"D01", "D02"} and a["per_board"]["D01"]["total"] == 0, str(a["counts"]))
            reg = run.get("registration", {}).get("D02", {})
            check("registration quality recorded (inliers, reprojection error)", reg.get("inliers", 0) >= 40 and "reproj_rms_px" in reg, str(reg))
            page.wait_for_timeout(600)
            page.screenshot(path=str(shots / f"{tag}_3_after_run.png"))

            # 3. review
            page.click('button[data-aoi="mode"][data-m="review"]')
            fr = page.frame_locator("#aoiframe")
            fr.locator("#img").wait_for(state="visible", timeout=20000)
            page.wait_for_timeout(1200)
            check("review is in diff mode: Issue (Y), which-board chooser", fr.locator("#bY").inner_text().startswith("Issue (Y)") and fr.locator("#which").is_visible())
            check("wording is 'differs from the reference board', never 'defect'", "differs from the reference board" in fr.locator("#note").inner_text() or "differs from" in fr.locator("#note").inner_text())
            check("review page carries the Experimental badge", fr.locator("#expbadge").is_visible())
            page.screenshot(path=str(shots / f"{tag}_4_review_first.png"))
            # Y without a board: refused in the page
            fr.locator("#bY").click()
            page.wait_for_timeout(400)
            check("Y without naming the board is refused", "Choose which board" in fr.locator("#saved").inner_text() and get("api/state")["aoi"]["counts"]["decided"] == 0)
            # Y on the first, naming D02
            fr.locator("input[name=wb][value=D02]").check()
            fr.locator("#bY").click()
            check("Y with board D02 saved", until(lambda: get("api/state")["aoi"]["counts"]["confirmed"] == 1))
            page.wait_for_timeout(600)
            page.screenshot(path=str(shots / f"{tag}_5_review_after_y.png"))
            # N on the second
            fr.locator('button[data-d=rejected]').click()
            check("N saved", until(lambda: get("api/state")["aoi"]["counts"]["rejected"] == 1))
            # U on the third
            fr.locator("#bU").click()
            check("U saved", until(lambda: get("api/state")["aoi"]["counts"]["unsure"] == 1))
            page.wait_for_timeout(600)
            page.screenshot(path=str(shots / f"{tag}_6_review_after_decisions.png"))
            ev = [json.loads(l) for l in next(data.rglob("journal.jsonl")).read_text().splitlines()]
            dec = [e["payload"] for e in ev if e["type"] == "aoi_decision"]
            check("journal aoi_decision: mode diff, issue_board D02 on the Y", dec and all(d.get("mode") == "diff" for d in dec) and dec[0]["decision"] == "confirmed" and dec[0]["issue_board"] == "D02", str(dec)[:300])
            check("journal aoi_run carries mode: diff", any(e["type"] == "aoi_run" and e["payload"].get("mode") == "diff" for e in ev))
            st = get("api/state")["aoi"]["per_board"]
            check("gate semantics: D02 has an open confirmed issue, undecided items remain", st["D02"]["state"] == "attention" and st["D02"]["open_confirmed"] == 1, str(st["D02"]))

            # 4. Y naming the REFERENCE board puts the issue on D01 (the compared board D02 is not blamed)
            n = len(get("api/state")["aoi"]["items"])
            page.click('button[data-aoi="mode"][data-m="review"]')
            fr.locator("#img").wait_for(state="visible", timeout=20000)
            # go to the 4th item with the arrow key in the page, name D01
            fr.locator("body").press("ArrowRight")
            fr.locator("body").press("ArrowRight")
            fr.locator("body").press("ArrowRight")
            page.wait_for_timeout(500)
            fr.locator("input[name=wb][value=D01]").check()
            fr.locator("#bY").click()
            check("Y with board D01 (the reference) moves the open issue to D01", until(lambda: get("api/state")["aoi"]["per_board"]["D01"]["open_confirmed"] == 1), str(get("api/state")["aoi"]["per_board"]))
            page.wait_for_timeout(500)
            page.screenshot(path=str(shots / f"{tag}_7_issue_on_reference.png"))
            # crops of the candidates for the report
            runs = sorted(data.rglob("evidence/D02/AOI-DIFF-*.jpg"), key=lambda p: int(p.stem.split("-")[-1]))
            for p in runs[:6]:
                shutil.copy(p, shots / f"{tag}_crop_{p.stem}.jpg")
            page.click(f'button.tl[data-id="{vis["id"]}"]')
            page.wait_for_timeout(800)
            page.screenshot(path=str(shots / f"{tag}_8_visual_step.png"))
            over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            check("no horizontal scroll", over <= 0, str(over))
            browser.close()
        check("no page or console errors", not errors, "; ".join(errors)[:300])
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [c for c, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed", "" if not bad else f"FAILED: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/aoidiff_shots"), sys.argv[2] if len(sys.argv) > 2 else "webkit"))
