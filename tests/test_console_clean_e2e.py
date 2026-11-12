"""UIR-36: no console error on a page load or reload, in Playwright WebKit and Chromium. HOST-ONLY. Runs tests/e2e/console_clean_walk.py
(own server on a free port, PB_E2E_PORT overrides; skipped without playwright)."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytest.importorskip("playwright")
from helpers import ROOT, free_port


def test_page_load_logs_no_console_error():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PB_E2E_PORT": str(free_port("PB_E2E_PORT"))}
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "e2e" / "console_clean_walk.py")], capture_output=True, text=True, timeout=150, env=env, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
