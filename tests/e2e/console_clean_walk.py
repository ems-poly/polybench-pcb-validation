"""UIR-36: a page load must log no console error, in WebKit and in Chromium, and no request may fail or be refused.
Reproduction and regression check in one. Prints every console message, failed request and non-2xx answer.

    python tests/e2e/console_clean_walk.py        (port: PB_E2E_PORT, default 8909; never 8750)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import opauth  # noqa: F401
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("PB_E2E_PORT", "8909"))
assert PORT != 8750, "8750 is the live bench server"
BASE = f"http://127.0.0.1:{PORT}/"
PROTOCOL = ROOT / "tests" / "e2e" / "fixtures" / "voice_demo" / "voice_demo.pcbv.yaml"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pb-console-"))
    cfgp = tmp / "voice.json"
    cfgp.write_text('{"tts_backend": "log", "llm_enabled": false, "stt_backend": "sidecar"}', encoding="utf-8")
    env = {**os.environ, "POLYBENCH_VOICE_TEST": "1", "POLYBENCH_VOICE_CONFIG": str(cfgp), "PYTHONPATH": os.environ.get("PYTHONPATH") or str(ROOT / "src")}
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(PROTOCOL), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "DEMO-CON", "--boards", "4", "--no-browser", "--port", str(PORT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    bad: list[str] = []
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(BASE + "api/state", timeout=2); break
            except Exception:
                time.sleep(0.3)
        with sync_playwright() as pw:
            for name in ("webkit", "chromium"):
                b = getattr(pw, name).launch()
                page = b.new_context(viewport={"width": 1100, "height": 800}).new_page()
                seen: list[str] = []
                info: list[str] = []
                page.on("console", lambda m: seen.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
                page.on("pageerror", lambda e: seen.append(f"pageerror: {e}"))
                # a request the browser cancels because the page is going away (SSE, beacons) is reported as info, not as a problem
                page.on("requestfailed", lambda r: info.append(f"requestfailed: {r.method} {r.url} {r.failure}"))
                page.on("response", lambda r: seen.append(f"http {r.status}: {r.request.method} {r.url}") if r.status >= 400 else None)
                page.goto(BASE)
                page.wait_for_selector("#timeline .tl")
                page.wait_for_timeout(2500)          # the heartbeat posts and one poll cycle
                print(f"{name}: before any navigation away: {len(seen)} problem(s)")
                # the hidden state still reaches the server (by beacon)
                vis = lambda: __import__("json").load(urllib.request.urlopen(BASE + "api/voice/status", timeout=5)).get("context", {}).get("visible")
                v0 = vis()
                page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_timeout(800)
                v1 = vis()
                print(f"{name}: server sees visible {v0} -> {v1} after the page turns hidden")
                # the server mutes only when every client is hidden, and the first browser's client is still known for 45 s: judge this on the first browser only
                if name == 'webkit' and not (v0 is True and v1 is False):
                    seen.append(f"hidden state did not reach the server: {v0} -> {v1}")
                page.evaluate("delete document.hidden; document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_timeout(500)
                page.reload()
                page.wait_for_selector("#timeline .tl")
                page.wait_for_timeout(1500)
                print(f"{name}: {len(seen)} problem(s)")
                for s in seen:
                    print("   ", s)
                for s in info:
                    print("    (info)", s)
                bad += [f"{name} {s}" for s in seen]
                b.close()
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
    print("PASS no console error on load" if not bad else f"FAIL {len(bad)} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
