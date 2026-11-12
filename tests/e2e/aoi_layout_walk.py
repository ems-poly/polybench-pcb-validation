"""Playwright WebKit walk of the LAYOUT-mode AOI on the YD-ESP32-S3 example (two photos D01 clean, D02 with four planted defects), own port (never 8750).

    .venv/bin/python tests/e2e/aoi_layout_walk.py [shots_dir] [webkit|chromium]   (port: PB_E2E_PORT, default 8980; viewport PB_E2E_W x PB_E2E_H, default 1500x900)

No photo is dragged or chosen: the protocol names them (`aoi.photos`, journal `assigned_by: protocol`). The walk opens the Visual step, checks both tiles carry a
photo, runs AOI, opens the review, and saves the run's candidate list as `<shots>/candidates.json` (used by the acceptance report). HOST-ONLY: two photos, not a
live camera; the analysis runs for real, nothing is a board test.
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
PORT = int(os.environ.get("PB_E2E_PORT", "8980"))
assert PORT != 8750, "8750 is the live bench server"
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
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-aoilayout-e2e-"))
    data = tmp / "data"
    w, h = int(os.environ.get("PB_E2E_W", "1500")), int(os.environ.get("PB_E2E_H", "900"))
    tag = f"{browser_name}_{w}x{h}"
    errors: list[str] = []
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(EX / "yd-esp32-s3.pcbv.yaml"), "--simulate",
                            "--data-dir", str(data), "--batch", "YD-AOI", "--boards", "2", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    try:
        check("server up", until(lambda: get("api/state"), 30))
        s = get("api/state")
        vis = next(x for x in s["steps"] if x["kind"] == "visual")
        check("boards D01 and D02", [b["label"] for b in s["boards"]] == ["D01", "D02"])
        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            page = browser.new_context(viewport={"width": w, "height": h}).new_page()
            page.on("pageerror", lambda e: errors.append(str(e)) if "access control checks" not in str(e) else None)
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "access control checks" not in m.text else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click(f'button.tl[data-id="{vis["id"]}"]')
            page.wait_for_selector("#aoitiles .aoi-tile")
            page.wait_for_selector("[data-tile=D02] img", timeout=20000)
            page.wait_for_timeout(800)
            ph = get("api/state")["aoi"]["photos"]
            check("both photos are in the batch with no drag and drop", sorted((p["board"], p["side"]) for p in ph) == [("D01", "top"), ("D02", "top")], str(ph)[:300])
            check("journal: assigned_by protocol", all(p.get("assigned_by") == "protocol" for p in ph))
            check("no Experimental diff badge in layout mode", "experimental" not in page.inner_text(".aoibar").lower())
            page.screenshot(path=str(shots / f"{tag}_1_tiles_both_photos.png"))
            stored = [p for p in data.rglob("aoi/photos/*") if p.is_file()]
            check("stored photos have no EXIF", len(stored) == 2 and all(b"Exif" not in p.read_bytes()[:4096] for p in stored))
            page.reload()
            page.wait_for_selector("#timeline .tl")
            page.click(f'button.tl[data-id="{vis["id"]}"]')
            page.wait_for_selector("[data-tile=D02] img", timeout=20000)
            check("a reload does not load them twice", len(get("api/state")["aoi"]["photos"]) == 2)
            page.evaluate("fetch('/api/cmd/override_gate', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({board: 'D01', step: '%s', reason: 'e2e: Assembly not walked'})})" % vis["id"])

            page.click('button[data-aoi="run"]')
            run = until(lambda: get("api/state")["aoi"]["run"], 170)
            check("layout run recorded", bool(run) and run.get("mode") in (None, "layout") and not run.get("synthetic"), str(run)[:300])
            if not run:
                return 1
            a = get("api/state")["aoi"]
            (shots / "candidates.json").write_text(json.dumps({"counts": a["counts"], "per_board": a["per_board"], "items": a["items"], "notes": run.get("notes"), "dir": run.get("dir")}, indent=1), encoding="utf-8")
            page.wait_for_timeout(800)
            page.screenshot(path=str(shots / f"{tag}_2_after_run.png"))
            page.click('button[data-aoi="mode"][data-m="review"]')
            fr = page.frame_locator("#aoiframe")
            fr.locator("#img").wait_for(state="visible", timeout=30000)
            page.wait_for_timeout(1200)
            page.screenshot(path=str(shots / f"{tag}_3_review_first.png"))
            page.click(f'button.tl[data-id="{vis["id"]}"]')
            page.wait_for_timeout(600)
            page.screenshot(path=str(shots / f"{tag}_4_visual_step_after_run.png"))
            over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            check("no horizontal scroll", over <= 0, str(over))
            browser.close()
        check("no page or console errors", not errors, "; ".join(errors)[:300])
        ev = [json.loads(l) for l in next(data.rglob("journal.jsonl")).read_text(encoding="utf-8").splitlines()]
        (shots / "journal_aoi.jsonl").write_text("\n".join(json.dumps(e) for e in ev if e["type"].startswith("aoi_")), encoding="utf-8")
        for p in data.rglob("evidence/D0*/*.jpg"):
            dst = shots / "evidence" / p.parent.name
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy(p, dst / p.name)
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
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/aoilayout_shots"), sys.argv[2] if len(sys.argv) > 2 else "webkit"))
