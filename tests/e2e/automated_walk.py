"""Playwright WebKit walk of the automated step (DEMO-AUTO, --simulate): saves a screenshot of every state, light and dark.

Run:  .venv/bin/python tests/e2e/automated_walk.py [shots_dir]      (port PB_E2E_PORT, default 8794; never 8750)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import opauth  # noqa: F401
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8794"))
BASE = f"http://127.0.0.1:{PORT}/"
EX = ROOT / "src/polybench/examples/demo_automated"
SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="auto-shots-"))
checks = []


def check(name, ok):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name, flush=True)


def start(data, proto):
    exe = Path(os.environ.get("PB_VENV", ROOT / ".venv")) / "bin" / "polybench"
    p = subprocess.Popen([str(exe), "--protocol", str(proto), "--simulate", "--data-dir", str(data), "--batch", "auto", "--no-browser", "--port", str(PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "api/session", timeout=1)
            return p
        except Exception:
            time.sleep(0.25)
    p.terminate()
    raise SystemExit("server did not start")


def variant(tmp, missing_pcan):
    d = Path(tmp) / "proto"
    shutil.copytree(EX, d)
    if missing_pcan:
        f = d / "function.yaml"
        t = f.read_text().replace("        bitrate: 1000000\n", "        bitrate: 1000000\n        sim: {status: missing, detail: \"PCAN-USB not on USB (PEAK 0c72)\", hint: \"plug in the PCAN-USB\"}\n", 1)
        f.write_text(t)
    return d / "demo_automated.pcbv.yaml"


def shot(pg, name):
    pg.wait_for_timeout(250)
    pg.screenshot(path=str(SHOTS / f"{name}.png"))


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="auto-e2e-")
    with sync_playwright() as pw:
        br = pw.webkit.launch()
        # 1. all green, then a missing PCAN, in two servers
        for theme in ("light", "dark"):
            srv = start(Path(tmp) / f"data-{theme}", variant(tmp + "/" + theme, False))
            try:
                pg = br.new_page(viewport={"width": 1440, "height": 1000})
                pg.goto(BASE)
                pg.wait_for_selector("#timeline")
                pg.evaluate(f"ui.theme='{theme}'")
                pg.click('.tl:has-text("Program and function")')
                pg.wait_for_selector(".stages")
                check(f"{theme}: opens on stage 1, equipment and connections", "Equipment and connections" in pg.inner_text(".stages"))
                shot(pg, f"{theme}-01-before-run")
                pg.click('details.edet >> nth=0 >> summary')
                shot(pg, f"{theme}-02-connection-details")
                pg.click('[data-act=autoskip][data-eq=pcan]')
                pg.fill('.dlg input[name=reason]', "no adapter on this bench today")
                pg.keyboard.press("Enter")
                shot(pg, f"{theme}-03-pcan-skipped")
                pg.click('[data-act=autounskip][data-eq=pcan]')
                pg.click('[data-act=autorun]')
                pg.wait_for_selector('[data-act=autobench][data-do=confirm]')
                check(f"{theme}: all green, Bench ready enabled", pg.is_enabled('[data-act=autobench][data-do=confirm]'))
                shot(pg, f"{theme}-04-bench-all-green")
                pg.click('[data-act=autobench][data-do=confirm]')
                pg.wait_for_selector(".askbig", timeout=20000)
                shot(pg, f"{theme}-05-operator-check")
                pg.click('[data-act=autoans][data-v=yes]')
                pg.wait_for_selector(".verdict", timeout=30000)
                shot(pg, f"{theme}-06-verdict-all-pass")
                check(f"{theme}: verdict says 8 passed", "8 passed, 0 failed" in pg.inner_text(".vsum"))
                pg.click('[data-act=autoproceed]')
                pg.wait_for_selector(".imgrec", timeout=20000)
                shot(pg, f"{theme}-07-accepted-final-image")
            finally:
                srv.terminate()
                srv.wait(5)
        # 2. missing PCAN
        srv = start(Path(tmp) / "data-miss", variant(tmp + "/miss", True))
        try:
            pg = br.new_page(viewport={"width": 1440, "height": 1000})
            pg.goto(BASE)
            pg.wait_for_selector("#timeline")
            pg.click('.tl:has-text("Program and function")')
            pg.click('[data-act=autorun]')
            pg.wait_for_selector('[data-act=autobench][data-do=confirm]')
            check("missing PCAN: Bench ready is disabled", pg.is_disabled('[data-act=autobench][data-do=confirm]'))
            shot(pg, "light-08-bench-pcan-missing")
            pg.click('[data-act=autoskip][data-eq=pcan]')
            pg.fill('.dlg input[name=reason]', "no adapter on this bench today")
            pg.keyboard.press("Enter")
            check("skipping the missing PCAN enables Bench ready", pg.is_enabled('[data-act=autobench][data-do=confirm]'))
            shot(pg, "light-09-bench-pcan-skipped")
            pg.click('[data-act=autobench][data-do=confirm]')
            pg.wait_for_selector(".askbig", timeout=20000)
            pg.click('[data-act=autoans][data-v=yes]')
            pg.wait_for_selector(".verdict", timeout=30000)
            shot(pg, "light-10-verdict-with-skipped")
        finally:
            srv.terminate()
            srv.wait(5)
    bad = [n for n, ok in checks if not ok]
    print(f"{len(checks) - len(bad)}/{len(checks)} checks; screenshots in {SHOTS}")
    sys.exit(1 if bad else 0)


main()
