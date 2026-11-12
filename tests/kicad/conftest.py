"""Fixtures for the KiCad plug-in tests: the fake KiCad API server (real kipy classes on top), a link over it, and the local
board file as a read-only source of expected values (skipped when the file is not on this machine; never copied)."""
import os
import socket
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("kipy", reason="kicad-python (extra 'kicad') is needed for the KiCad tests")

from polybench.kicad import link as linkmod                       # noqa: E402
from polybench.kicad.bridge import KicadBridge                    # noqa: E402
from polybench.kicad.client import KicadScannerClient             # noqa: E402
from polybench.kicad.fakeclient import FakeKiCadClient            # noqa: E402
from polybench.kicad.link import KiCadLink                        # noqa: E402
from polybench.kicad.synth import demo_board                     # noqa: E402



@pytest.fixture(autouse=True)
def _own_link():
    """The process-wide link never leaks between tests."""
    linkmod.reset_shared(None)
    yield
    linkmod.reset_shared(None)


@pytest.fixture
def snap():
    return demo_board(["C1", "C2", "R1"])


@pytest.fixture
def fake(snap):
    return FakeKiCadClient(snap)


@pytest.fixture
def link(fake):
    return KiCadLink(client_factory=lambda addr: fake, fake=True)


@pytest.fixture
def bridge(link):
    return KicadBridge(link=link)


@pytest.fixture
def client(link):
    return KicadScannerClient(link=link)


@pytest.fixture
def sockdir():
    """A short directory under /tmp (ipc paths are limited to ~104 characters on macOS)."""
    d = tempfile.mkdtemp(dir="/tmp", prefix="pbk")
    yield d
    for f in Path(d).glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    os.rmdir(d)


def make_stale_socket(path: str):
    """A socket file with nobody listening: what a crashed KiCad leaves behind."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.close()
