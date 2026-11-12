"""UIR-01 walk (DEMO protocol, --simulate, Playwright WebKit): typed and clicked, one screenshot per sequence state.

Run:  .venv/bin/python tests/e2e/uir01_walk.py [shots_dir]      (port PB_E2E_PORT, default 8901; never 8750). Stops only the servers it started.
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

import opauth  # noqa: F401
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8901"))
assert PORT != 8750, "8750 is the live bench server"
BASE = f"http://127.0.0.1:{PORT}/"
EX = ROOT / "tests/data/demo_automated"
SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="uir01-shots-"))
checks = []


def check(name, ok):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name, flush=True)


def start(data, proto):
    exe = Path(os.environ.get("PB_VENV", ROOT / ".venv")) / "bin" / "polybench"
    p = subprocess.Popen([str(exe), "--protocol", str(proto), "--simulate", "--data-dir", str(data), "--batch", "w", "--no-browser", "--port", str(PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "api/session", timeout=1)
            return p
        except Exception:
            time.sleep(0.25)
    p.terminate()
    raise SystemExit("server did not start")


def variant(tmp, name, missing_pcan=False, flaky=None, slow=False):
    d = Path(tmp) / name
    shutil.copytree(EX, d)
    f = d / "function.yaml"
    t = f.read_text(encoding="utf-8")
    if missing_pcan:
        t = t.replace("        bitrate: 1000000\n", "        bitrate: 1000000\n        sim: {status: missing, detail: \"PCAN-USB not on USB (PEAK 0c72)\", hint: \"plug in the PCAN-USB\"}\n", 1)
    if flaky:
        t = t.replace("        plugin: openocd.flash\n        final: true\n        needs: [probe]\n        params: {image_from: final_images, map: {image_sha256: A90}}",
                      f"        plugin: demo.fail\n        final: true\n        needs: [probe]\n        params: {{until_file: {flaky}, propose: {{A90: abc}}}}")
    if slow:
        t = t.replace("plugin: openocd.flash\n        needs: [probe]\n        params: {image: images/selftest.hex, map: {image_sha256: A10}}",
                      "plugin: demo.flash\n        needs: [probe]\n        params: {}")
    f.write_text(t, encoding="utf-8")
    return d / "demo_automated.pcbv.yaml"


def shot(pg, name):
    pg.wait_for_timeout(300)
    pg.screenshot(path=str(SHOTS / f"{name}.png"))


def journal(data):
    out = []
    for ln in (Path(data) / "w" / "journal.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def seqev(data, phase):
    return [e["payload"] for e in journal(data) if e["type"] == "sequence" and e["payload"].get("phase") == phase]


def opened(pg):
    pg.goto(BASE)
    pg.wait_for_selector("#timeline")
    pg.click('.tl:has-text("Program and function")')
    pg.wait_for_selector(".stages")


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="uir01-")
    probe_ok = Path(tmp) / "probe_ok"
    with sync_playwright() as pw:
        br = pw.webkit.launch()
        # ---- 1. R1 + R2: a failing final flash, retried alone; then a second board reuses the equipment check
        data = Path(tmp) / "d1"
        srv = start(data, variant(tmp, "p1", flaky=probe_ok))
        try:
            pg = br.new_page(viewport={"width": 1440, "height": 1000})
            opened(pg)
            shot(pg, "01-idle")
            pg.click("[data-act=autorun]")
            pg.wait_for_selector("[data-act=autobench][data-do=confirm]")
            pg.click("[data-act=autobench][data-do=confirm]")
            pg.wait_for_selector(".askbig", timeout=20000)
            shot(pg, "03-waiting-on-question")
            pg.fill("#autoans", "y")
            pg.keyboard.press("Enter")
            pg.wait_for_selector(".verdict", timeout=30000)
            shot(pg, "04-verdict-ready")
            pg.click("[data-act=autoproceed]")
            pg.wait_for_selector("[data-act=autoretryfinal]", timeout=20000)
            shot(pg, "05-final-failed")
            check("failed-final box: one-line message, raw error only under details", "The final flash failed:" in pg.inner_text(".verdict") and not pg.is_visible(".verdict pre"))
            att = pg.evaluate("S.sequences['S01|function'].attempt")
            starts = len(seqev(data, "start"))
            check("final failed: Retry final flash and Retest are both on screen", pg.is_visible("[data-act=autoretryfinal]") and pg.is_visible("[data-act=autoretest]"))
            pg.click("[data-act=autoretryfinal]")
            for _ in range(60):
                if len(seqev(data, "final")) >= 2:
                    break
                time.sleep(0.2)
            pg.wait_for_selector("[data-act=autoretryfinal]")
            check("retry that fails again stays on the same record", len(seqev(data, "final")) == 2)
            shot(pg, "06-final-failed-again")
            probe_ok.write_text("x", encoding="utf-8")
            pg.click("[data-act=autoretryfinal]")
            pg.wait_for_selector(".imgrec", timeout=20000)
            pg.wait_for_function("S.sequences['S01|function'].final.state === 'ok'")
            pg.wait_for_timeout(300)
            shot(pg, "07-final-ok")
            check("R1: no new start, same attempt after two retries", len(seqev(data, "start")) == starts and pg.evaluate("S.sequences['S01|function'].attempt") == att)
            check("R1: three final events carry try_no 1,2,3", [e.get("try_no") for e in seqev(data, "final")] == [1, 2, 3])
            check("R1: final-ok state has no Retry button", not pg.is_visible("[data-act=autoretryfinal]"))
            # second board: the check is reused
            pg.click('.bchip:has-text("S02")')
            pg.wait_for_selector("[data-act=autocheck]")
            shot(pg, "08a-idle-second-board-after-successful-retry-check-restored")
            check("R2: a successful retry restores the kept check (second board shows it, reused)", pg.inner_text("[data-act=autocheck]") == "Check again" and "reused" in pg.inner_text(".stages").lower())
            pg.click("[data-act=autocheck]")
            pg.wait_for_function("!document.querySelector('.stages').innerText.toLowerCase().includes('reused: checked')")
            shot(pg, "08-idle-second-board-fresh-check-not-called-reused")
            check("wording: a check pressed on this board is not called reused", "reused" not in pg.inner_text(".stages").lower().replace("reused for", ""))
            pg.click("[data-act=autorun]")
            pg.wait_for_selector(".askbig", timeout=20000)
            check("R2: second sequence went straight to the question (no bench ask)", len(seqev(data, "bench")) == 2 and "trigger" not in seqev(data, "bench")[1])
            shot(pg, "09-second-board-waiting-reused")
            pg.fill("#autoans", "y")
            pg.keyboard.press("Enter")
            pg.wait_for_selector(".verdict", timeout=30000)
            pg.click("[data-act=autoproceed]")
            pg.wait_for_selector(".imgrec", timeout=20000)
            pg.click('.bchip:has-text("S03")')
            pg.wait_for_selector("[data-act=autocheck]")
            pg.click("[data-act=autocheck]")
            pg.wait_for_timeout(800)
            check("R2: Check again before a run keeps the pass", "Check again" in pg.inner_text(".benchbar"))
        finally:
            srv.terminate()
            srv.wait(5)
        # ---- 2. R3: missing PCAN asks once; and the 'Skip and start' button works
        data = Path(tmp) / "d2"
        srv = start(data, variant(tmp, "p2", missing_pcan=True))
        try:
            pg = br.new_page(viewport={"width": 1440, "height": 1000})
            opened(pg)
            pg.click("[data-act=autorun]")
            pg.wait_for_selector("[data-act=autoskipstart]")
            check("label shows (default) once", "(default) (default)" not in pg.inner_text("body"))
            pg.wait_for_timeout(4000)
            n = len(seqev(data, "bench"))
            voice = [e for e in journal(data) if e["type"] == "voice"]
            check("R3: 4 s with no input: exactly one bench check recorded", n == 1)
            shot(pg, "10-bench-missing-asks-once")
            pg.click("[data-act=autobench][data-do=recheck]")
            pg.wait_for_function("document.querySelector('.benchbar') && true")
            pg.wait_for_timeout(1200)
            check("R3: Check again pressed once: exactly one more check", len(seqev(data, "bench")) == 2)
            pg.click("[data-act=autoskipstart]")
            pg.wait_for_selector(".dlg input[name=reason]")
            shot(pg, "11a-skip-and-start-asks-a-reason")
            pg.fill(".dlg input[name=reason]", "no adapter on this bench today")
            pg.keyboard.press("Enter")
            pg.wait_for_selector(".askbig", timeout=20000)
            check("R4: 'Skip ... and start' works (was not wired)", True)
            shot(pg, "11-after-skip-and-start")
        finally:
            srv.terminate()
            srv.wait(5)
        # ---- 3. running state
        data = Path(tmp) / "d3"
        srv = start(data, variant(tmp, "p3", slow=True))
        try:
            pg = br.new_page(viewport={"width": 1440, "height": 1000})
            opened(pg)
            pg.click("[data-act=autorun]")
            pg.wait_for_selector("[data-act=autobench][data-do=confirm]")
            pg.click("[data-act=autobench][data-do=confirm]")
            pg.wait_for_selector(".stage.st-running", timeout=20000)
            shot(pg, "02-running")
            check("running: Abort is the only action, Run is gone", pg.is_visible("[data-act=abort]") and not pg.is_visible("[data-act=autorun]"))
            pg.click("[data-act=abort]")
            pg.wait_for_timeout(1500)
        finally:
            srv.terminate()
            srv.wait(5)
    bad = [n for n, ok in checks if not ok]
    print(f"{len(checks) - len(bad)}/{len(checks)} checks; screenshots in {SHOTS}")
    sys.exit(1 if bad else 0)


main()
