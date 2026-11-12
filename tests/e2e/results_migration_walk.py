"""Playwright WebKit: a batch whose journal predates auto-record (pending instrument proposals) opens, shows them as pending, and the one-time
button records them as results. Own port (never 8750). HOST-ONLY: simulator.

    python tests/e2e/results_migration_walk.py [shots_dir] [chromium|webkit]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opauth  # noqa: F401,E402

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8915"))
BASE = f"http://127.0.0.1:{PORT}/"
ok_all = True


def check(name, ok, extra=""):
    global ok_all
    ok_all &= bool(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def main(shots: Path, browser_name: str):
    assert PORT != 8750
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-mig-"))
    sys.path.insert(0, str(ROOT / "src"))
    from polybench.engine import Engine
    from polybench.protocol import load
    from polybench.store import Journal
    proto_path = ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"
    (tmp / "data" / "OLD").mkdir(parents=True)
    j = Journal(tmp / "data" / "OLD" / "journal.jsonl")
    e = Engine(load(proto_path), j, "tester")
    e.create_batch("OLD", ["S01", "S02", "S03"], "S", True, "DEMO-1")
    e.override_gate("S01", "powered", "walk")
    for tid, v in (("P03", "3.3"), ("P04", "31")):                    # what an earlier version left as proposals
        e.add_proposal("S01", tid, v, plugin="demo.measure", run=1, simulate=True, record="propose")
    j.close()
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(proto_path), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "OLD", "--no-browser", "--port", str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    try:
        for _ in range(100):
            try:
                get("api/state"); break
            except Exception:
                time.sleep(0.3)
        st = get("api/state")
        check("old journal loads with two pending proposals", len(st["cells"]["S01|P03"]["proposals"]) == 1 and st["pending_instrument"] == {"S01": 2})
        with sync_playwright() as pw:
            br = getattr(pw, browser_name).launch()
            page = br.new_context(viewport={"width": 1400, "height": 900}).new_page()
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="P04"]')
            page.wait_for_selector("#propbar")
            check("they are acceptable by hand (the box holds the proposal, Accept for review present)", page.locator("button[data-act=entrygo], button[data-act=entryconfirm]").count() >= 1 and page.input_value("#val") != "", str([page.locator("button[data-act=entrygo]").count(), page.input_value("#val"), page.evaluate("document.querySelector('#val').dataset.ctx")]))
            check("the one-time button offers to record them", "Record 2 instrument readings" in page.inner_text("#propbar"))
            page.screenshot(path=str(shots / f"{browser_name}_20_migration_pending.png"))
            page.click('#propbar button[data-act=recordpending]')
            until = time.time() + 15
            while time.time() < until and get("api/state")["pending_instrument"]:
                time.sleep(0.2)
            st = get("api/state")
            check("after the click both are results (auto) and none is pending", st["cells"]["S01|P04"]["status"] == "accepted" and st["cells"]["S01|P04"]["auto"] and not st["cells"]["S01|P03"]["proposals"])
            check("P04 above its guess limit is now in the Anomalies strip", [a["test_id"] for a in st["anomalies"]] == ["P04"])
            time.sleep(0.6)
            page.screenshot(path=str(shots / f"{browser_name}_21_migration_recorded.png"))
            br.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            pass
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()), sys.argv[2] if len(sys.argv) > 2 else "webkit"))
