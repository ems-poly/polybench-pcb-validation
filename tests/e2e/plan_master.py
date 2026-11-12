"""Test plan as master, end to end at 900 px on its own port (never 8750).

    .venv/bin/python tests/e2e/plan_master.py [shots_dir] [chromium|webkit]    (port: PB_E2E_PORT, default 8796)

The demo protocol with `test_plan: {file: PLAN.md, generate: true}`. Checks: the step is rendered grouped by tier and section from the plan; loading a
test selects its parts, pins and nets in the simulated Fusion (and only then, once per test); what was not found is on the probe card; Tier 2 is n/a until
the Decision gate sends a board there; the gate proposes, Accept opens Tier 2; the power interlock holds the board; row mode in the unpowered step;
step 4 shows the coverage; regenerating after the plan lost a test keeps its results as retired.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8796"))
BASE = f"http://127.0.0.1:{PORT}/"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def post(name, body):
    req = urllib.request.Request(BASE + "api/cmd/" + name, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return {"http": e.code, **json.loads(e.read() or b"{}")}


def until(fn, timeout=20.0, step=0.15):
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


def main(shots, browser_name):
    assert PORT != 8750, "8750 is the live server"
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-plan-e2e-"))
    proj = tmp / "proj"
    shutil.copytree(ROOT / "src" / "polybench" / "examples" / "demo", proj)
    plan_text = (ROOT / "tests" / "data" / "DEMO_TEST_PLAN.md").read_text().replace("C3, U1.4", "C3, U1.9")        # U1.9 is not on the board
    plan_file = proj / "PLAN.md"
    plan_file.write_text(plan_text)
    top = proj / "demo.pcbv.yaml"
    top.write_text(top.read_text() + "\ntest_plan: {file: PLAN.md, generate: true}\n")
    netlist = tmp / "netlist.json"
    netlist.write_text(json.dumps({"5V": ["U1.3", "C1.1"], "GND": ["C2.2", "U1.2"]}))          # no 3V3 net: U02 shows it as not found
    log = tmp / "hl.log"
    env = {**os.environ, "POLYBENCH_SIM_NETLIST": str(netlist), "POLYBENCH_SIM_HIGHLIGHT_LOG": str(log)}
    nsel = lambda: len(log.read_text().splitlines()) if log.exists() else 0
    errors: list[str] = []
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(top), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "PLAN-E2E", "--boards", "4", "--no-browser", "--port", str(PORT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        check("server up", until(lambda: get("api/state"), 30))
        s = get("api/state")
        check("plan_loaded journalled at start with the plan hash", s["plan_loaded"] and s["plan_loaded"]["hash"] == s["plan"]["hash"] and s["plan"]["tests"] == 15,
              str(s.get("plan_loaded"))[:200])
        for b in ("S01", "S02", "S03", "S04"):
            post("override_gate", {"board": b, "step": "unpowered", "reason": "visual not under test here"})
        with sync_playwright() as pw:
            browser = getattr(pw, browser_name).launch()
            page = browser.new_context(viewport={"width": 900, "height": 1000}).new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            page.click('button.tl[data-id="unpowered"]')
            page.wait_for_selector(".tier-h")

            # ---- the step is the plan, grouped by tier and section
            tiers = page.locator(".tier-h").all_inner_texts()
            check("left panel: Tier 1 then Tier 2 headers", len(tiers) == 2 and tiers[0].lower().startswith("tier 1") and "deeper review" in tiers[1].lower(), str(tiers))
            heads = [h.strip().lower() for h in page.locator(".sec-h .sh-t").all_inner_texts()]
            check("sections are the plan's groups, in order", heads == ["input and rails", "pull-ups", "power diagnosis"], str(heads))
            check("group designators shown with the section", "J1 C1 C2 C3 U1" in page.locator(".sec-h .sh-d").first.inner_text())
            check("tests listed in plan order", page.locator(".trow[data-act=test]").evaluate_all("els => els.map(e => e.dataset.id)") == ["U01", "U02", "U03", "U04", "U05", "U06", "U20", "U21"])
            check("Tier 2 section is n/a for every board until sent", page.locator(".sec.cond .sec-h .c").first.inner_text() == "n/a")
            check("Tier 2 dots are n/a", page.locator('.trow[data-id="U20"] .dot.s-na').count() == 4)
            check("a decision gate row sits between Tier 1 and Tier 2", page.locator(".trow.gate").count() == 1)
            page.screenshot(path=str(shots / f"{browser_name}_1_step_by_tier.png"))

            # ---- test load selects the targets in Fusion (simulated), once
            check("U01 loaded: its targets were selected", until(lambda: nsel() == 1), str(nsel()))
            line = json.loads(log.read_text().splitlines()[0])
            check("parts, pin and nets of U01 sent", line["parts"] == ["C2", "U1"] and line["pins"] == ["U1.3"] and line["nets"] == ["5V", "GND"] and line["selected"] == ["C2", "U1"], str(line))
            card = until(lambda: page.locator(".probecard .pc-status.ok").count() == 1 and page.inner_text(".probecard .pc-status"))
            check("probe card says what was selected", card and "selected C2 U1" in card and "pins U1.3" in card and "nets 5V GND" in card, str(card))
            check("probe card shows the red and black pole", page.locator(".probecard .pole.red").count() == 1 and page.locator(".probecard .pole.blk").count() == 1)
            page.screenshot(path=str(shots / f"{browser_name}_2_probe_card.png"))
            page.click("#adv"); page.click("#adv"); page.click('button[data-act=theme]'); page.click('button[data-act=theme]')
            time.sleep(0.8)
            check("re-render does not send again (debounce)", nsel() == 1, str(nsel()))
            page.click('.trow[data-id="U02"]')
            check("U02 loaded: a second selection", until(lambda: nsel() == 2), str(nsel()))
            txt = until(lambda: "not found" in page.inner_text(".probecard .pc-status") and page.inner_text(".probecard .pc-status"))
            check("not-found net is on the probe card", txt and "nets 3V3" in txt, str(txt))
            page.click('.trow[data-id="U03"]')
            check("U03: pins list empty, still selected", until(lambda: nsel() == 3))
            page.click('.trow[data-id="U02"]')
            time.sleep(0.8)
            check("coming back to a test selects it again", nsel() == 4, str(nsel()))
            page.click('.probecard button[data-act=hl]')
            check("the card's button repeats it on demand", until(lambda: nsel() == 5), str(nsel()))
            # a pin that is not on the board
            page.click('.trow[data-id="U02"]')

            # ---- continue without ECAD: nothing is sent
            post("ecad_skip", {"reason": "Fusion is closed on this PC"})
            n0 = nsel()
            page.click('.trow[data-id="U05"]'); time.sleep(0.8)
            page.click('.trow[data-id="U01"]'); time.sleep(0.8)
            check("without ECAD nothing is sent to Fusion", nsel() == n0, f"{n0} -> {nsel()}")
            check("probe card says ECAD is off", "ECAD is off" in page.inner_text(".probecard .pc-status") or "off" in page.inner_text(".probecard .pc-status").lower(), page.inner_text(".probecard .pc-status"))
            post("ecad_skip_clear", {})

            # ---- row mode in the unpowered step: Enter goes to the next board, same test
            page.click('.trow[data-id="U01"]')
            page.click('.bchip[data-id="S01"]')
            page.fill("#val", "2.0"); page.press("#val", "Enter")
            check("unpowered is row mode: Enter moves to the next board, same test", until(lambda: page.evaluate("document.querySelector('#val').dataset.ctx") == "S02|U01"),
                  str(page.evaluate("document.querySelector('#val').dataset.ctx")))
            check("the switch is shown as set for this step", page.locator("#advmode").is_checked())
            page.screenshot(path=str(shots / f"{browser_name}_3_row_mode.png"))

            # ---- fill Tier 1 unpowered for the four boards; S02 has a guess limit missed (U01 0.5 kOhm) and S04 an outlier
            vals = {"U01": [2.0, 0.5, 2.1, 9.0], "U02": [3.0] * 4, "U03": [0.6] * 4, "U05": [10.0] * 4, "U06": [10.0] * 4}
            for tid, vs in vals.items():
                for b, v in zip(("S01", "S02", "S03", "S04"), vs):
                    post("entry", {"board": b, "test_id": tid, "value": str(v), "confirm": True, "replace": True})
            for b in ("S01", "S02", "S03", "S04"):
                post("entry", {"board": b, "test_id": "U04", "value": "Fluke 117"})
            r = post("entry", {"board": "S01", "test_id": "U20", "value": "x"})
            check("an entry in Tier 2 for a board that was not sent is refused", r.get("http") == 409 and "Tier 2" in r.get("error", ""), str(r))
            page.click('.trow.gate')
            page.wait_for_selector("table.gatet")
            rows = {tr.locator("td").nth(0).inner_text(): tr.inner_text() for tr in page.locator("table.gatet tr").all()[1:]}
            check("gate proposes Tier 2 for S02 with the reason", "TIER 2" in rows["S02"] and "U01" in rows["S02"], rows["S02"])
            check("gate: S01 and S03 clear, S04 outlier noted (powered next)", "TIER 2 SKIPPED" in rows["S01"] and "TIER 2 SKIPPED" in rows["S03"] and "OUTLIER" in rows["S04"], str(rows))
            page.screenshot(path=str(shots / f"{browser_name}_4_gate.png"))
            page.click('table.gatet button[data-act=gateacc]')
            check("Accept opens Tier 2 for S02", until(lambda: "S02|unpowered" in get("api/state")["tier2"]))
            page.wait_for_selector("table.gatet td:has-text('SENT TO TIER 2')")
            check("Tier 2 section now counts for S02 only", until(lambda: page.locator(".sec.cond .sec-h .c").first.inner_text() == "0/2"), page.locator(".sec.cond .sec-h .c").first.inner_text())
            page.click('.trow[data-id="U20"]'); page.click('.bchip[data-id="S02"]')
            check("S02 can take the Tier 2 entry", page.locator("#val:not([disabled])").count() == 1)
            page.screenshot(path=str(shots / f"{browser_name}_5_tier2_open.png"))

            # ---- power interlock
            page.click('button.tl[data-id="powered"]')
            page.wait_for_selector(".trow[data-id=P01]")
            page.click('.bchip[data-id="S02"]')
            check("powered: S02 is held by the interlock", until(lambda: "power interlock" in page.inner_text("#centre")), page.inner_text("#centre")[:300])
            page.screenshot(path=str(shots / f"{browser_name}_5b_interlock.png"))
            post("entry", {"board": "S02", "test_id": "U20", "value": "R3 was open, replaced"})
            post("entry", {"board": "S02", "test_id": "U21", "value": "y", "confirm": True})
            post("retest", {"board": "S02", "step": "unpowered", "cause": "R3 replaced"})
            for tid, v in {"U01": 2.0, "U02": 3.0, "U03": 0.6, "U04": "Fluke", "U05": 10.0, "U06": 10.0}.items():
                post("entry", {"board": "S02", "test_id": tid, "value": str(v), "confirm": True})
            post("override_gate", {"board": "S01", "step": "unpowered", "reason": "x"})
            check("after Tier 2 settled and Tier 1 repeated the interlock releases", until(lambda: not get("api/state")["holds"].get("S02")), str(get("api/state")["holds"]))
            # ---- powered is board by board
            page.click('.bchip[data-id="S01"]'); page.click('.trow[data-id="P01"]')
            page.fill("#val", "20"); page.press("#val", "Enter")
            check("powered is board by board: Enter goes to the next test, same board", until(lambda: page.evaluate("document.querySelector('#val').dataset.ctx") == "S01|P02"),
                  str(page.evaluate("document.querySelector('#val').dataset.ctx")))

            # ---- step 4: coverage
            page.click('button.tp-tile')
            page.wait_for_selector("#tpcov")
            check("coverage: 15 tests generated", page.inner_text("#tpcov-n") == "15", page.inner_text("#tpcov-n"))
            check("coverage: tests per tier and step", all(x in page.inner_text("#tpcov") for x in ("Tier 1 unpowered", "Tier 2 powered")), page.inner_text("#tpcov")[:300])
            check("coverage: designators covered against the BOM", page.inner_text("#tpcov-bom") == "11 of 18", page.inner_text("#tpcov-bom"))
            miss = page.locator(".tp-miss .dchip").all_inner_texts()
            check("coverage: uncovered parts listed", set(miss) == {"C4", "C5", "C6", "C7", "R1", "R2", "J2"}, str(miss))
            check("coverage: lint clean", "lint clean" in page.inner_text(".tp-covhead"), page.inner_text(".tp-covhead"))
            check("coverage: sections table", page.locator(".tp-secs tr").count() == 1 + 5)
            check("rendered plan above the coverage", page.locator(".tp-doc").count() == 1)
            ov = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
            check("no horizontal page scroll at 900 px", ov[0] <= ov[1], str(ov))
            page.evaluate("document.querySelector('#tpcov').scrollIntoView()")
            page.screenshot(path=str(shots / f"{browser_name}_6_coverage.png"))

            # ---- the plan loses a test; Regenerate keeps its results as retired
            post("entry", {"board": "S03", "test_id": "U06", "value": "10.0", "replace": True, "confirm": True})
            plan_file.write_text(re.sub(r"\| U06 \|.*\n", "", plan_text).replace("U1.9", "U1.4"))
            page.click('[data-tp=regen]')
            check("Regenerate: 14 tests", until(lambda: get("api/state")["plan"]["tests"] == 14 and page.inner_text("#tpcov-n") == "14"), page.inner_text("#tpcov-n"))
            s = get("api/state")
            check("U06 is retired with its recorded results kept", [r["id"] for r in s["retired"]] == ["U06"] and s["retired"][0]["results"]["S03"]["value"] == "10.0", str(s["retired"]))
            check("plan_loaded journalled again", [e["type"] for e in get("api/journal")].count("plan_loaded") == 2)
            check("coverage lists the retired test", "U06" in page.inner_text("#tpcov"))
            page.click('button.tl[data-id="unpowered"]')
            page.wait_for_selector(".sec.retired")
            check("left panel: Retired section with U06", page.locator('.sec.retired .trow[data-id="U06"]').count() == 1)
            page.click('.sec.retired .trow')
            check("retired view shows the kept result", until(lambda: "10.0" in page.inner_text("#centre") and "RETIRED" in page.inner_text("#centre")))
            page.screenshot(path=str(shots / f"{browser_name}_7_retired.png"))
            browser.close()
        check("no page errors", not errors, "; ".join(errors[:3]))
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    shots = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="pb-plan-shots-"))
    print("screenshots:", shots)
    sys.exit(main(shots, sys.argv[2] if len(sys.argv) > 2 else "chromium"))
