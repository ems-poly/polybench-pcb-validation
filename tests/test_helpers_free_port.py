"""The e2e port helper: a free port per call, an explicit override wins, and 8750 (the live bench server) is refused always. HOST-ONLY."""
from __future__ import annotations

import socket

import pytest

from helpers import LIVE_PORT, free_port


def test_default_is_a_free_port_and_never_8750(monkeypatch):
    monkeypatch.delenv("PB_X", raising=False)
    for _ in range(20):
        p = free_port("PB_X")
        assert p != LIVE_PORT and 1024 <= p < 65536
        with socket.socket() as s:
            s.bind(("127.0.0.1", p))                     # nothing holds it


def test_an_override_wins(monkeypatch):
    monkeypatch.setenv("PB_X", "8941")
    assert free_port("PB_X") == 8941


def test_8750_is_refused_whatever_the_override_says(monkeypatch):
    monkeypatch.setenv("PB_X", "8750")
    with pytest.raises(ValueError, match="live bench"):
        free_port("PB_X")


def test_an_unset_or_empty_override_falls_back_to_a_free_port(monkeypatch):
    monkeypatch.setenv("PB_X", "")
    assert free_port("PB_X") != LIVE_PORT
    assert free_port() != LIVE_PORT


def test_every_walk_refuses_8750():
    """Each tests/e2e walk (or the shared W.Server it uses) carries an 8750 guard."""
    from pathlib import Path
    d = Path(__file__).parent / "e2e"
    shared = "assert int(port) != 8750" in (d / "project_page_walk.py").read_text()
    for f in sorted(d.glob("*_walk.py")):
        t = f.read_text()
        uses_shared = "project_page_walk" in t or f.name == "project_page_walk.py"
        assert "!= 8750" in t or (shared and uses_shared), f"{f.name} has no 8750 guard"
