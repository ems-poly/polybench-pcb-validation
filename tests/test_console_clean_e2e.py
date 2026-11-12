"""UIR-36: no console error on a page load or reload, in Playwright WebKit and Chromium. HOST-ONLY. Runs tests/e2e/console_clean_walk.py
(own server, port 8909 or PB_E2E_PORT; skipped without playwright)."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytest.importorskip("playwright")
from helpers import ROOT


def test_page_load_logs_no_console_error():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "e2e" / "console_clean_walk.py")], capture_output=True, text=True, timeout=150, env=env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
