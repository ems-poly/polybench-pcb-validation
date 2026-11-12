"""Fixtures for the voice tests: demo protocol, temp data dir, sidecar STT, log TTS, a fake NLU (the real one is tested by its own suite)."""
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.dirname(os.path.abspath(__file__)),):
    if p not in sys.path:
        sys.path.insert(0, p)
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture
def sheet_copy(tmp_path):
    p = tmp_path / "DEMO1_Bringup_Results.md"
    shutil.copy(os.path.join(FIX, "DEMO1_Bringup_Results.md"), p)
    return str(p)


@pytest.fixture
def session(sheet_copy):
    from polybench.voice.session import Session
    return Session(sheet_copy, mode="board")        # the one-board-at-a-time behaviour; row mode has its own fixture


@pytest.fixture
def rsession(sheet_copy):
    from polybench.voice.session import Session
    return Session(sheet_copy, mode="row")


@pytest.fixture
def idx(session):
    return session.idx


# ---- runtime (controller) fixtures
import dataclasses
import types

import numpy as np
import pytest

from polybench.protocol import load
from polybench.server import Session
from polybench.voice import controller as ctlmod, vad

from helpers import DEMO


@dataclasses.dataclass
class BenchView:
    step_id: str
    tests: list
    boards: list
    all_boards: list
    test_id: str | None
    board: str | None
    row_mode: bool = True
    settled: dict = dataclasses.field(default_factory=dict)
    recent: list = dataclasses.field(default_factory=list)
    step_title: str = ""
    aoi_review: bool = False


class FakeNlu:
    """understand.interpret stand-in: scripted results by transcript; a callable entry receives the BenchView."""

    def __init__(self):
        self.script: dict = {}
        self.views: list = []
        self.BenchView = BenchView

    def interpret(self, text, view, llm=None):
        self.views.append(view)
        r = self.script.get(text)
        r = r(view) if callable(r) else dict(r or {"intent": "unclear", "reason": "not scripted"})
        return {"transcript": text, "route": "grammar", "model": None, "values": [], "boards": [], "test_id": None, "text": "",
                "reason": None, "grammar": {"ok": True, "reason": None, "fields": None}, "llm": None, **r}


@pytest.fixture(autouse=True)
def voice_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYBENCH_VOICE_CONFIG", str(tmp_path / "voice.json"))
    monkeypatch.setenv("POLYBENCH_VOICE_TEST", "1")


@pytest.fixture
def nlu(monkeypatch):
    f = FakeNlu()
    monkeypatch.setattr(ctlmod, "understand", f)
    return f


def make_session(tmp_path, n=3):
    return Session(load(DEMO / "demo.pcbv.yaml"), tmp_path / "data", "v", True, n, None, "tester")


@pytest.fixture
def vc(tmp_path, nlu):
    """A voice controller on the demo protocol: unpowered step open for S01 and S02 (S03 stays gated), cursor on U01 / S01."""
    sess = make_session(tmp_path)
    for b in ("S01", "S02"):
        sess.engine.override_gate(b, "unpowered", "test")
    c = sess.voice
    c.set_config({"stt_backend": "sidecar", "tts_backend": "log", "llm_enabled": False, "grace_s": 0})
    c.set_context("unpowered", "U01", "S01")
    c.msgs = []
    c._sink = c.msgs.append
    return c


def speech(dur, peak=0.15, f0=140.0):
    """A speech-like burst for an energy VAD (harmonics of f0 with a 4 Hz syllable envelope)."""
    t = np.arange(int(dur * vad.SR)) / vad.SR
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
    x = x * (0.55 + 0.45 * np.sin(2 * np.pi * 4 * t) ** 2)
    return (x / np.abs(x).max() * peak * 1.4).astype(np.float32)
