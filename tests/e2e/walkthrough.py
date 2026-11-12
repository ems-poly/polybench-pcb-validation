"""Playwright click-through of the demo protocol with --simulate. Saves light and dark screenshots of every step.

Run:  .venv/bin/python tests/e2e/walkthrough.py [shots_dir]
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
U03_TITLE = "3V3 to GND"
PORT = int(os.environ.get("PB_E2E_PORT", "8793"))
BASE = f"http://127.0.0.1:{PORT}/"
checks: list[tuple[str, bool]] = []


def check(name, ok):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name, flush=True)


def start_server(data_dir):
    exe = Path(os.environ.get("PB_VENV", ROOT / ".venv")) / "bin" / "polybench"      # PB_VENV: prove a bare or partial install
    log = open(os.environ.get("PB_SERVER_LOG", os.devnull), "ab")                     # PB_SERVER_LOG: keep the server log to grep for 500s
    p = subprocess.Popen([str(exe), "--protocol", str(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"), "--simulate", "--data-dir", str(data_dir),
                          "--batch", "e2e", "--no-browser", "--port", str(PORT)], stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "api/state", timeout=1).read()
            return p
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not start")


def state():
    return json.load(urllib.request.urlopen(BASE + "api/state"))


def main(shots: Path):
    shots.mkdir(parents=True, exist_ok=True)
    for f in shots.glob("*.png"):
        f.unlink()
    data = Path(tempfile.mkdtemp(prefix="pbe2e-"))
    srv = start_server(data)
    n = [0]
    pgref = []
    try:
        with sync_playwright() as pw:
            br = getattr(pw, os.environ.get("PB_BROWSER", "chromium")).launch()
            pg = br.new_page(viewport={"width": int(os.environ.get("PB_E2E_WIDTH", "1440")), "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("response", lambda r: r.status >= 500 and errors.append(f"HTTP {r.status} {r.url}"))
            pgref.append(pg)
            pg.goto(BASE)
            pg.wait_for_selector("#timeline .tl")
            pg.click("#timeline .tl[data-id='setup']")      # a batch without a project opens on step 1; these walks start at batch setup
            try:

                def snap(name):
                    n[0] += 1
                    pg.wait_for_timeout(350)
                    pg.screenshot(path=str(shots / f"{n[0]:02d}_{name}_light.png"))
                    pg.evaluate("document.documentElement.dataset.theme='dark'")
                    pg.wait_for_timeout(200)
                    pg.screenshot(path=str(shots / f"{n[0]:02d}_{name}_dark.png"))
                    pg.evaluate("document.documentElement.dataset.theme='light'")
                    pg.wait_for_timeout(150)

                def ended():
                    return pg.evaluate("consoleLines.filter(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text)).length")

                def run_wait(label, timeout=20000):
                    before = ended()
                    pg.click(f"button:has-text('{label}')")
                    pg.wait_for_function(f"consoleLines.filter(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text)).length>{before}", timeout=timeout)
                    pg.wait_for_timeout(500)

                def board(label):
                    # a click that lands during a redraw is lost: click, then wait until the page has taken the board
                    for _ in range(5):
                        pg.click(f".bchip:has-text('{label}')")
                        try:
                            pg.wait_for_function(f"(document.querySelector('.bchip.cur')||{{innerText:''}}).innerText.trim().startsWith('{label}')", timeout=1500)
                            return
                        except Exception:
                            pass
                    raise RuntimeError("board chip " + label + " did not take")

                def step(i):
                    ids = {"Assembly aid": "assembly", "Visual": "visual", "Unpowered testing": "unpowered", "Powered testing": "powered",
                           "Program and function": "function", "Characterisation": "characterisation", "Report": "report"}
                    pg.click(f"#timeline .tl[data-id='{ids[i]}']")
                    pg.wait_for_timeout(250)

                # 1 setup
                pg.fill("#op", "Pat")
                pg.keyboard.press("Tab")
                for i in range(1, 11):
                    pg.locator(".serial").nth(i - 1).fill(f"DEMO-A-{i:03d}")
                    pg.keyboard.press("Tab")
                pg.wait_for_timeout(300)
                check("setup: 10 serials stored", sum(1 for b in state()["boards"] if b["serial"]) == 10)
                snap("setup")

                # 2 assembly: scan
                step("Assembly aid")
                pg.click("#asmbegin")      # UIR-20: the start page
                pg.fill("#scan", "595-DEMOMCU32")
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(500)
                txt = pg.inner_text("#ectext")
                check("assembly: scan selects line 1 and simulated ECAD highlights U1", "selected 1: U1" in txt and "DEMOMCU-32" in pg.inner_text("#scanmsg"))
                snap("assembly_scan")
                pg.fill("#scan", "C3")
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(400)
                check("assembly: designator scan selects the capacitor line", "selected 6: C1 C2 C3" in pg.inner_text("#ectext"))
                pg.fill("#scan", "")
                # Enter places the selected line, then waits for the next scan; Enter again (nothing selected) takes the next to-do line.
                # So a line costs two Enters: place, then next. Press until S01 is done (bounded).
                for _ in range(30):
                    if next(x for x in state()["steps"] if x["id"] == "assembly")["per_board"]["S01"]["state"] == "done":
                        break
                    pg.locator("#scan").focus()
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(250)
                s = state()
                a = next(x for x in s["steps"] if x["id"] == "assembly")
                check("assembly: S01 fully placed with Enter (DNF line skipped)", a["per_board"]["S01"]["state"] == "done")
                snap("assembly_done")

                # 3 visual: S01 all yes by keyboard
                step("Visual")
                for _ in range(3):
                    pg.keyboard.type("y")
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(250)
                pg.keyboard.type("1")
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(300)
                check("visual: keyboard entry settles S01", next(x for x in state()["steps"] if x["id"] == "visual")["per_board"]["S01"]["state"] == "done")
                # S02 FAIL accept
                board("S02")
                pg.wait_for_timeout(200)
                pg.locator("#val").focus()
                pg.keyboard.type("n")
                pg.wait_for_timeout(300)
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(300)
                c = state()["cells"].get("S02|V01")
                check("visual: Enter does not record a FAIL", c is None or c["status"] != "accepted")
                snap("visual_fail_pending")
                pg.click("text=Record FAIL")
                pg.wait_for_timeout(400)
                c = state()["cells"].get("S02|V01")
                check("visual: FAIL recorded by its own button", c and c["verdict"] == "fail")
                # finish S02 visual then retest
                pg.keyboard.press("Escape")
                for k in ("y", "y", "1"):
                    pg.locator("#val").focus()
                    pg.keyboard.type(k)
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(250)
                board("S02")
                pg.wait_for_timeout(200)
                pg.click("text=Send S02 back for retest")
                pg.fill("input[name=cause]", "bridge on U1 pin 5")
                pg.fill("input[name=fix]", "reflowed with flux")
                pg.click(".dlg button[type=submit]")
                pg.wait_for_timeout(400)
                st = state()
                check("retest: attempt 2 opened with cause", st["retests"] and st["retests"][0]["attempt"] == 2 and st["retests"][0]["board"] == "S02")
                check("retest: earlier FAIL kept as history", st["cells"].get("S02|V01", {}).get("prev_attempts") and st["cells"]["S02|V01"]["prev_attempts"][0]["verdict"] == "fail")
                snap("visual_retest")
                for k in ("y", "y", "y", "1"):
                    pg.locator("#val").focus()
                    pg.keyboard.type(k)
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(250)
                # S03 visual via keyboard too (so S03 can progress)
                board("S03")
                for k in ("y", "y", "y", "1"):
                    pg.locator("#val").focus()
                    pg.keyboard.type(k)
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(250)

                # 4 unpowered: S01 values, S03 stop
                step("Unpowered testing")
                board("S01")
                # typed batch entry: Shift+Enter on one test applies to every open, empty board (S01-S03; S04-S10 are gated)
                first_t = "U03"
                pg.click(".trow:has-text('%s')" % U03_TITLE)
                pg.locator("#val").focus()
                pg.keyboard.type("0.8")
                pg.wait_for_timeout(300)
                check("batch: hint shown under the box", "Shift+Enter: all empty boards" in pg.locator("#batchhint").inner_text())
                pg.keyboard.press("Shift+Enter")
                pg.wait_for_timeout(600)
                c = state()["cells"]
                check("batch: Shift+Enter filled the test on S01 S02 S03 only",
                      all(c.get(f"{b}|{first_t}", {}).get("value") == "0.8" for b in ("S01", "S02", "S03")) and f"S04|{first_t}" not in c)
                check("batch: toast names the boards", "S01 S02 S03" in pg.locator("#toast").inner_text())
                pg.click(".trow:has-text('VIN to GND')")
                for v in ("12.5", "4.1", "y", "ok board"):
                    pg.locator("#val").focus()
                    pg.keyboard.type(v)
                    pg.wait_for_timeout(150)
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(300)
                st = state()
                c = st["cells"]
                check("unpowered: guess below limit is not a FAIL (U02 ok), TBR gives no verdict (U03)", c["S01|U03"]["verdict"] == "none")
                snap("unpowered_entered")
                board("S03")
                pg.click(".trow:has-text('VIN to GND')")
                pg.locator("#val").focus()
                pg.keyboard.type("0.4")
                pg.wait_for_timeout(300)
                snap("unpowered_stop_pending")
                pg.click("button[data-act=entryconfirm]")
                pg.wait_for_timeout(500)
                check("unpowered: stop rule trips and blocks the board", any(not s["ack"] for s in state()["stops"]))
                snap("unpowered_stop")
                pg.click("text=Acknowledge stop with a reason")
                pg.fill("input[name=reason]", "U2 reflowed, re-measure")
                pg.click(".dlg button[type=submit]")
                pg.wait_for_timeout(400)
                pg.click("text=Send S03 back for retest")
                pg.fill("input[name=cause]", "U2 bridged, reflowed")
                pg.click(".dlg button[type=submit]")
                pg.wait_for_timeout(400)
                for v in ("11", "3", "0.9", "y", "x"):
                    pg.locator("#val").focus()
                    pg.keyboard.type(v)
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(300)
                pg.keyboard.press("Escape")

                # 5 powered: action with live console and proposals, S01
                step("Powered testing")
                board("S01")
                pg.click("button:has-text('Read rails and current')")
                pg.wait_for_selector("#bbody .cl.proposal", timeout=10000)
                snap("powered_running")
                pg.wait_for_function("document.querySelectorAll('#bbody .cl.proposal').length>=4", timeout=15000)
                pg.wait_for_timeout(600)
                check("action: live console shows proposals", pg.locator("#bbody .cl.proposal").count() >= 4)
                pg.click("text=Accept all clean for S01")
                pg.wait_for_timeout(500)
                pw_ = next(x for x in state()["steps"] if x["id"] == "powered")["per_board"]["S01"]
                check("proposals: accept all clean settles S01 powered", pw_["state"] == "done")
                snap("powered_accepted")
                # S03: forced FAIL proposal on 5V rail
                board("S03")
                run_wait("Read rails and current")
                pg.click(".trow:has-text('5V rail')")
                pg.wait_for_timeout(200)
                snap("powered_fail_proposal")
                pg.click(".prop button:has-text('Record FAIL')")
                pg.wait_for_timeout(500)
                check("proposal FAIL needs its own button and is recorded", state()["cells"]["S03|P02"]["verdict"] == "fail")

                # 6 function: flash on S01, abort test
                step("Program and function")
                board("S01")
                e0 = ended()
                pg.click("button:has-text('Flash bring-up image')")
                pg.wait_for_selector(".btabs .prog", timeout=5000)
                pg.wait_for_timeout(500)
                snap("function_flash_running")
                pg.wait_for_function(f"consoleLines.filter(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text)).length>{e0}", timeout=20000)
                pg.wait_for_timeout(500)
                snap("function_flash")
                pg.click(".trow:has-text('Device ID')")
                pg.wait_for_timeout(300)
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(300)
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(300)
                run_wait("Read heartbeat over CAN")
                pg.click(".trow:has-text('Heartbeat rate')")
                pg.locator("#val").focus()
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(300)
                pg.click(".trow:has-text('Firmware revision')")
                pg.locator("#val").focus()
                pg.keyboard.type("2")
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(300)
                pg.click("button:has-text('Long capture')")
                pg.wait_for_selector(".btabs .danger", timeout=5000)
                pg.wait_for_timeout(800)
                pg.click(".btabs .danger")
                pg.wait_for_function("!document.querySelector('.btabs .danger')", timeout=15000)
                pg.wait_for_timeout(600)
                check("abort: cancel, plugin stops, safe state called", "safe state (demo.slow)" in pg.inner_text("#bbody"))
                snap("function_aborted")
                pg.click("button:has-text('Flash with a missing programmer')")
                pg.wait_for_function("!document.querySelector('.btabs .danger') && document.body.innerText.includes('programmer not found')", timeout=15000)
                check("action error is shown, app keeps working", "ERROR" in pg.inner_text("#bbody"))

                # 7 characterisation: ask and progress
                fn = next(x for x in state()["steps"] if x["id"] == "function")["per_board"]["S01"]
                check("function: S01 settled", fn["state"] == "done")
                if fn["state"] != "done":
                    print("TOAST:", pg.inner_text("#toast"))
                    print({k: (v["value"], v["status"], len(v["proposals"])) for k, v in state()["cells"].items() if k.startswith("S01|F")})
                step("Characterisation")
                pg.click("button:has-text('Capture noise at 2 mV/V')")
                pg.wait_for_selector("#runok")
                check("prompt: operator confirmation shown before the child starts", state()["action"] is None)
                snap("characterisation_prompt")
                pg.click("#runok")
                pg.wait_for_selector(".dlg [data-opt]", timeout=8000)
                snap("characterisation_ask")
                e1 = ended()
                pg.keyboard.press("1")
                pg.wait_for_selector(".btabs .prog", timeout=5000)
                pg.wait_for_timeout(500)
                snap("characterisation_progress")
                pg.wait_for_function(f"consoleLines.filter(l=>/^--- run \\d+ (ok|error|cancelled)/.test(l.text)).length>{e1}", timeout=20000)
                pg.wait_for_timeout(700)
                snap("characterisation_stats")
                pg.click("text=Accept all clean for S01")
                pg.wait_for_timeout(500)
                cs = state()["cells"]
                check("prompt: confirmation logged", "operator confirmed on S01" in pg.evaluate("consoleLines.map(l=>l.text).join('\\n')"))
                check("characterisation: statistics landed in cells", all(f"S01|{t}" in cs and cs[f'S01|{t}']["status"] == "accepted" for t in ("C01", "C02", "C03")))

                # advanced grid + report
                pg.check("#adv")
                pg.wait_for_timeout(300)
                step("Unpowered testing")
                snap("advanced_grid")
                pg.uncheck("#adv")
                step("Report")
                pg.wait_for_selector("#repframe")
                pg.wait_for_timeout(800)
                snap("report")
                pg.click("text=Generate report files")
                pg.wait_for_timeout(600)
                check("report: files written", "Written to" in pg.inner_text("#repmsg"))
                html = urllib.request.urlopen(BASE + "api/report/preview").read().decode()
                check("report: retest highlighted, SIMULATED banner", "bridge on U1 pin 5" in html and "SIMULATED DATA" in html)
                pg.click("[data-act=btab][data-id=journal]")
                pg.wait_for_timeout(500)
                snap("journal_tab")
                check("no JavaScript errors", not errors)
                if errors:
                    print(errors)
                before = state()
            except Exception:
                pg.screenshot(path=str(shots / "FAILURE.png"))
                print({k: (v["value"], v["attempt"], v["status"]) for k, v in state()["cells"].items() if k.startswith("S03|U")})
                raise
            br.close()
        # kill -9 mid-batch and restart
        os.kill(srv.pid, signal.SIGKILL)
        srv.wait()
        srv = start_server(data)
        after = state()
        keys = ("cells", "placed", "retests", "stops", "boards")
        check("kill -9 and restart: all state identical", all(json.dumps(before[k], sort_keys=True) == json.dumps(after[k], sort_keys=True) for k in keys))
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": int(os.environ.get("PB_E2E_WIDTH", "1440")), "height": 900})
            pg.goto(BASE)
            pg.wait_for_selector("#timeline .tl")
            pg.click("#timeline .tl[data-id='powered']")
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(shots / "99_after_restart_light.png"))
            br.close()
    except Exception:
        try:
            pgref[0].screenshot(path=str(shots / "FAILURE.png"))
        except Exception:
            pass
        raise
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
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/screens/dv"))
