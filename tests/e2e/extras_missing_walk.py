"""WebKit walk of a BARE install (no optional extra): the features that need an extra say what to install, nothing shows a traceback, and the
server answers no 5xx. Runs the YD-ESP32-S3 demo (it has an AOI block) with --simulate on its own port (never 8750).

    PB_VENV=/path/to/venv-with-a-bare-install  python tests/e2e/extras_missing_walk.py [shots_dir]    (port: PB_E2E_PORT, default 9080)

The driver needs Playwright; the server is the venv in PB_VENV (default: this checkout's .venv).
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

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "9080"))
BASE = f"http://127.0.0.1:{PORT}/"
EX = ROOT / "examples" / "esp32-s3-board"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def main(shots: Path):
    shots.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="pbxm-"))
    logf = Path(os.environ.get("PB_SERVER_LOG", str(data / "server.log")))
    exe = Path(os.environ.get("PB_VENV", ROOT / ".venv")) / "bin" / "polybench"
    srv = subprocess.Popen([str(exe), "--protocol", str(EX / "yd-esp32-s3.pcbv.yaml"), "--simulate", "--data-dir", str(data / "d"), "--batch", "x", "--no-browser",
                            "--port", str(PORT)], stdout=open(logf, "ab"), stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(BASE + "api/state", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        errors: list[str] = []
        with sync_playwright() as pw:
            br = pw.webkit.launch()
            pg = br.new_page(viewport={"width": 1440, "height": 900})
            pg.on("pageerror", lambda e: "access control checks" not in str(e) and errors.append(str(e)))
            pg.on("response", lambda r: r.status >= 500 and errors.append(f"HTTP {r.status} {r.url}"))
            pg.goto(BASE)
            pg.wait_for_selector("#timeline .tl")
            ids = pg.evaluate("[...document.querySelectorAll('#timeline .tl[data-id]')].map(e=>e.dataset.id)")
            # every step opens
            for sid in ids:
                pg.click(f"#timeline .tl[data-id='{sid}']")
                pg.wait_for_timeout(250)
            check("every step opens on a bare install", not errors, "; ".join(errors)[:300])

            # Assembly aid: the camera pane says what is missing
            pg.click("#timeline .tl[data-id='assembly']")
            pg.click("#asmbegin")      # UIR-20: the start page
            pg.wait_for_timeout(1200)
            body = pg.inner_text("body")
            check("assembly: the camera says to install the scanner extra", "polybench-pcb-validation[scanner]" in body, body[:200])
            pg.screenshot(path=str(shots / "01_assembly_scanner_missing.png"))

            # Visual: AOI photo upload and Run AOI
            vis = next(i for i in ids if i == "visual")
            pg.click(f"#timeline .tl[data-id='{vis}']")
            pg.wait_for_selector("#aoifile", state="attached", timeout=8000)
            pg.set_input_files("#aoifile", str(EX / "aoi" / "D01_top.jpg"))
            pg.wait_for_function("document.querySelector('#aoiup') && /\\[aoi\\]/.test(document.querySelector('#aoiup').innerText)", timeout=8000)
            up = pg.inner_text("#aoiup")
            check("visual: a photo upload says to install the aoi extra, no traceback", "polybench-pcb-validation[aoi]" in up and "Traceback" not in up, up)
            pg.screenshot(path=str(shots / "02_visual_upload_aoi_missing.png"))
            pg.click("button[data-aoi=run]")
            pg.wait_for_function("consoleLines.some(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text))", timeout=15000)
            log = pg.evaluate("consoleLines.map(l=>l.text).join('\\n')")
            check("visual: Run AOI ends with the install text, not a traceback", "polybench-pcb-validation[aoi]" in log and "Traceback" not in log and "ModuleNotFoundError" not in log, log[-300:])
            pg.screenshot(path=str(shots / "03_visual_run_aoi_missing.png"))

            # Voice tab
            pg.click("[data-act=btab][data-id=voice]")
            pg.wait_for_selector("#vo-power")
            pg.click("#vo-power")                      # UIR-13: the input warnings (what to install) show only while Voice input is on
            pg.wait_for_timeout(1200)
            vt = pg.inner_text("body")
            check("voice: the tab says what to install, no traceback", "Traceback" not in vt and ("[voice]" in vt or "sounddevice" in vt or "not installed" in vt), vt[-300:])
            pg.screenshot(path=str(shots / "04_voice_missing.png"))
            check("no page errors, no HTTP 5xx during the walk", not errors, "; ".join(errors)[:300])
            br.close()
        logtxt = logf.read_text(errors="replace")
        check("server log: no Traceback, no 500", "Traceback" not in logtxt and " 500 " not in logtxt and "Internal Server Error" not in logtxt, logtxt[-400:])
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)
    bad = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/screens/extras_missing"))
