"""KC-43 page walk with the ECAD program named KiCad: step 1 Project, the Assembly page and a test result card show no "Fusion".

    PB_E2E_PORT=8910 python tests/e2e/ecad_program_walk.py [shots_dir]        (ports 8910-8919 only; never 8750)

The server runs `--simulate --ecad kicad` (the simulated KiCad bridge over the fake KiCad API server). Case-sensitive: the rendered text of
the pages must not contain "Fusion" and must contain "KiCad". A second pass with the plain simulated ECAD checks the Fusion wording
is still there, so the walk cannot pass on empty pages. Everything is simulated: HOST-ONLY.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8910"))
assert PORT != 8750, "8750 is the live bench server"
BASE = f"http://127.0.0.1:{PORT}/"
checks: list[tuple[str, bool]] = []
PROTOCOL = ROOT / "src" / "polybench" / "examples" / "demo" / "demo.pcbv.yaml"


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


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


def start(kicad: bool, tmp: Path, down: Path):
    frames = tmp / "frames"
    frames.mkdir(exist_ok=True)
    (frames / "CURRENT").write_text("")
    env = {**os.environ, "POLYBENCH_SCANNER_FAKE_FRAMES": str(frames), "ASCAN_CONFIG_DIR": str(tmp / "cfg"),
           "POLYBENCH_SIM_ECAD_DOWN": str(down)}
    args = [sys.executable, "-c", "from polybench.cli import main; main()", *(["--ecad", "kicad"] if kicad else []), "--protocol", str(PROTOCOL), "--simulate", "--data-dir",
            str(tmp / ("data-k" if kicad else "data-f")), "--batch", "ECAD-TXT", "--boards", "4", "--no-browser", "--port", str(PORT)]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if not until(lambda: get("api/state"), 30):
        p.kill()
        raise RuntimeError("server did not start")
    return p


def body_text(pg) -> str:
    """The page text without the platform dropdown's option labels: the dropdown lists every supported program by design (UIR-14),
    so it is left out; the check is about the program's name leaking into the wording of the page."""
    return pg.evaluate("""() => { const c = document.body.cloneNode(true); c.querySelectorAll('#pjplatsel').forEach(e => e.remove()); return c.textContent; }""")


def walk(program: str, shots: Path) -> list[str]:
    """Visit the pages and return their rendered texts."""
    tmp = Path(tempfile.mkdtemp(prefix="pb-ecadtxt-"))
    down = tmp / "down"
    down.write_text("down")
    srv = start(program == "KiCad", tmp, down)
    texts: dict[str, str] = {}
    errors: list[str] = []
    try:
        s = get("api/state")
        check(f"{program}: state names the program", s.get("ecad_program") == program, str(s.get("ecad_program")))
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_context(viewport={"width": 900, "height": 1000}).new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(BASE)
            pg.wait_for_selector("#timeline button.tl")
            pg.click("#timeline button.tl[data-id='project']")
            pg.wait_for_selector("#centre .vd.fail", timeout=15000)      # UIR-14: the unreachable state is a status chip in the ECAD setup box, not a callout
            texts["step 1, link down"] = body_text(pg)
            pg.screenshot(path=str(shots / f"{program}_1_down.png"))
            down.unlink()
            pg.click("#centre [data-act=pjcheck]")
            pg.wait_for_selector("#centre .vd.pass", timeout=15000)
            texts["step 1, connected"] = body_text(pg)
            pg.screenshot(path=str(shots / f"{program}_2_connected.png"))
            pg.click("[data-act=pjuse]")
            pg.wait_for_selector("#ccnote")
            pg.fill("#ccnote", "walk: mismatches are the simulator's extras")
            pg.click("[data-act=pjaccept]")
            pg.wait_for_timeout(500)
            texts["step 1, done"] = body_text(pg)

            s = get("api/state")
            asm = next(x for x in s["steps"] if x["kind"] == "assembly")
            pg.click(f'button.tl[data-id="{asm["id"]}"]')
            pg.click("#asmbegin") if pg.locator("#asmbegin").count() else None      # UIR-20: the start page
            pg.wait_for_selector("#scanner")
            until(lambda: pg.evaluate("document.querySelector('#scov') && document.querySelector('#scov').innerText.length > 5"), 15)
            texts["assembly, idle"] = body_text(pg)
            row = pg.locator(".bomrow").first
            row.click()
            until(lambda: "HIGHLIGHTED" in pg.inner_text("#scres") or "highlighted" in pg.inner_text("#centre").lower(), 15)
            pg.wait_for_timeout(500)
            texts["assembly, line selected"] = body_text(pg)
            pg.screenshot(path=str(shots / f"{program}_3_assembly.png"))
            # the Highlight again button and the status line under the scanner
            if pg.locator("#hlagain").count():
                pg.click("#hlagain")
                pg.wait_for_timeout(800)
                texts["assembly, highlight again"] = body_text(pg)
            pg.click("#scanner button[data-act=screscan]") if pg.locator("#scanner button[data-act=screscan]").count() else None
            pg.wait_for_timeout(600)
            texts["assembly, rescan"] = body_text(pg)

            # a test with a result card: the first test step that has a highlight button
            for st in [x for x in s["steps"] if x["kind"] not in ("assembly", "setup", "project")]:
                pg.click(f'button.tl[data-id="{st["id"]}"]')
                pg.wait_for_timeout(500)
                if pg.locator("[data-act=hl]").count():
                    pg.click("[data-act=hl]")
                    pg.wait_for_timeout(1200)
                    texts[f"test step {st['id']}, after highlight"] = body_text(pg) + "\n" + (pg.inner_text("#toast") if pg.locator("#toast").count() else "")
                    pg.screenshot(path=str(shots / f"{program}_4_test.png"))
                    break
            else:
                check(f"{program}: found a test card with a highlight button", False)
            br.close()
        check(f"{program}: no page errors", not errors, str(errors[:3]))
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
    return texts


def main():
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="pb-ecadtxt-shots-"))
    shots.mkdir(parents=True, exist_ok=True)
    k = walk("KiCad", shots)
    for name, t in k.items():
        check(f"KiCad: '{name}' has no 'Fusion'", "Fusion" not in t, next((t[max(0, i - 60):i + 60] for i in [t.find("Fusion")]), ""))
    check("KiCad: pages mention KiCad", all("KiCad" in t for t in k.values()), str([n for n, t in k.items() if "KiCad" not in t]))
    check("KiCad: the test card walk reached a result", any(n.startswith("test step") for n in k))
    f = walk("Fusion", shots)
    check("Fusion (control): wording is still Fusion", all("Fusion" in t for n, t in f.items() if n.startswith("step 1")))
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
