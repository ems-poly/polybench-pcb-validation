"""Off-nominal behaviour and timing (KC-24..KC-32, KC-35, KC-36, TBR-15). Real nng sockets where the point is a socket."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from polybench.kicad import settings
from polybench.kicad.bridge import KicadBridge
from polybench.kicad.client import KicadScannerClient
from polybench.kicad.fakeclient import FakeKiCadClient
from polybench.kicad.link import BUDGET_SELECT, BUDGET_STATUS, KiCadLink, LinkError

from conftest import make_stale_socket


@pytest.fixture
def no_socket(monkeypatch, tmp_path):
    """No socket anywhere, and a settings folder of our own."""
    monkeypatch.setattr(settings, "socket_dirs", lambda: [str(tmp_path / "nosock")])
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.delenv("POLYBENCH_KICAD_SOCKET", raising=False)
    monkeypatch.setenv("POLYBENCH_KICAD_PREFS", str(tmp_path / "prefs"))
    return tmp_path


def _prefs(tmp_path, enabled):
    d = tmp_path / "prefs" / "10.0"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "kicad_common.json"
    f.write_text(json.dumps({"api": {"enable_server": enabled}}), encoding="utf-8")
    return f


def test_no_socket_no_dial_api_off_names_the_fix(no_socket, monkeypatch):
    """KC-24, KC-25: the commonest first-run failure, with the settings file untouched."""
    import pynng
    f = _prefs(no_socket, False)
    h = hashlib.sha256(f.read_bytes()).hexdigest()

    def boom(*a, **k):
        raise AssertionError("dial attempted")
    monkeypatch.setattr(pynng, "Req0", boom)
    link = KiCadLink()
    t0 = time.monotonic()
    st = KicadBridge(link=link).status()
    assert time.monotonic() - t0 < 1 and link.dials == 0
    assert st["connected"] is False and st["state"] == "api_off"
    assert "api.enable_server" in st["detail"] and "restart" in st["detail"]
    assert hashlib.sha256(f.read_bytes()).hexdigest() == h


def test_no_socket_api_on_says_not_running(no_socket):
    _prefs(no_socket, True)
    st = KicadBridge(link=KiCadLink()).status()
    assert st["detail"] == "KiCad is not running or its API server is off" and st["state"] == "not_running"


def test_no_prefs_file_is_not_an_error(no_socket):
    st = KicadBridge(link=KiCadLink()).status()
    assert st["connected"] is False and st["state"] == "not_running"


def test_no_socket_highlight_and_clear_send_nothing(no_socket):
    link = KiCadLink()
    b = KicadBridge(link=link)
    r = b.highlight(["R9"])
    assert r["ok"] is False and r["state"] == "not_running" and link.dials == 0
    assert b.clear()["cleared"] is False and b.designators() is None and b.documents()["ok"] is False


def test_kipy_missing_names_the_install(monkeypatch, no_socket):
    """KC-39: Polybench without the extra imports and runs; the bridge says how to install."""
    monkeypatch.setitem(__import__("sys").modules, "kipy", None)
    st = KicadBridge(link=KiCadLink()).status()
    assert st["connected"] is False and "pip install -e .[kicad]" in st["detail"] and st["state"] == "no_kipy"
    assert KicadBridge(simulate=False).selftest() is True            # the resolution rules still run


def test_tcp_address_refused(monkeypatch):
    """KC-35."""
    with pytest.raises(ValueError):
        settings.check_scheme("tcp://127.0.0.1:4242")
    link = KiCadLink(socket_path="tcp://127.0.0.1:4242")
    st = KicadBridge(link=link).status()
    assert st["state"] == "bad_address" and "only a local ipc://" in st["detail"] and link.dials == 0
    monkeypatch.setenv("KICAD_API_SOCKET", "tcp://127.0.0.1:1")
    assert KicadBridge(link=KiCadLink()).status()["state"] == "bad_address"


def test_stale_socket_fails_fast(sockdir):
    """TBR-15 (part): a socket file with nobody behind it is refused at once, inside the budget."""
    p = os.path.join(sockdir, "api.sock")
    make_stale_socket(p)
    link = KiCadLink(socket_path=p)
    t0 = time.monotonic()
    st = KicadBridge(link=link).status()
    dt = time.monotonic() - t0
    assert st["connected"] is False and st["state"] == "stale_socket" and dt < BUDGET_STATUS


def test_tbr15_dial_to_missing_path_returns(sockdir):
    """TBR-15: pynng with block_on_dial to a path that does not exist. Measured here; the code avoids the question (KC-24)."""
    import pynng
    out = {}

    def go():
        t = time.monotonic()
        try:
            s = pynng.Req0(dial="ipc://" + os.path.join(sockdir, "none.sock"), block_on_dial=True, send_timeout=300, recv_timeout=300)
            s.close()
            out["r"] = "dialled"
        except Exception as e:
            out["r"] = type(e).__name__
        out["dt"] = time.monotonic() - t
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(5)
    print("TBR-15 measured:", out)
    assert not th.is_alive() and out["dt"] < 5          # returns (refused) rather than blocking


@pytest.fixture
def deaf_server(sockdir):
    """A real nng REP socket that accepts the connection and never answers."""
    import pynng
    p = os.path.join(sockdir, "api.sock")
    rep = pynng.Rep0(listen="ipc://" + p)
    yield p
    rep.close()


def test_never_replying_server_every_call_bounded(deaf_server, monkeypatch):
    """KC-27, KC-28, KC-30: each call returns its own timeout result inside its budget; the single request fails at the nng timeout."""
    link = KiCadLink(socket_path=deaf_server, timeout_ms=300)
    b = KicadBridge(link=link)
    c = KicadScannerClient(link=link)
    t0 = time.monotonic()
    st = b.status()
    one = time.monotonic() - t0
    assert st["connected"] is False and st["state"] in ("timeout", "stale_socket") and 0.25 < one < 1.5      # KC-28: 300 ms + tolerance
    for fn, bound in ((lambda: b.highlight(["R9"]), 8), (lambda: b.designators(), 12), (lambda: b.documents(), 12),
                      (lambda: c.highlight_targets(["R9"], [], []), 8), (lambda: c.status(), 5), (lambda: c.footprints(["R9"]), 12),
                      (lambda: b.clear(), 8)):
        t = time.monotonic()
        fn()
        assert time.monotonic() - t < bound, "KC-27: inside the server's outer bound"
    assert link.log == [] or set(link.log) <= {"GetVersion", "GetOpenDocuments"}          # nothing was written


def test_late_job_is_dropped_not_sent(snap):
    """KC-27/KC-30: a request queued behind a stalled one is not sent after its caller gave up."""
    fake = FakeKiCadClient(snap)
    fake.delay = 0.6
    link = KiCadLink(client_factory=lambda a: fake, fake=True)
    b = KicadBridge(link=link)
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(2) as ex:
        slow = ex.submit(lambda: link.run(lambda ctx: ctx.documents(), 5))
        time.sleep(0.1)
        t = time.monotonic()
        try:
            link.run(lambda ctx: ctx.documents(), 0.2)                # queued behind the slow one, budget 0.2 s
            raised = None
        except LinkError as e:
            raised = e
        assert raised and raised.state == "timeout" and time.monotonic() - t < 0.5
        slow.result()
    time.sleep(1.0)
    assert fake.names.count("GetOpenDocuments") == 1             # the dropped job never reached KiCad


def test_serialised_single_connection(snap):
    """KC-29: concurrent status and highlight reach the server strictly one after the other, over one connection object."""
    fake = FakeKiCadClient(snap)
    fake.delay = 0.01
    made = []
    link = KiCadLink(client_factory=lambda a: made.append(a) or fake, fake=True)
    b = KicadBridge(link=link)
    threads = [threading.Thread(target=lambda: b.status()) for _ in range(4)] + [threading.Thread(target=lambda: b.highlight(["R9"])) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert fake.max_active == 1 and len(made) == 1


def test_busy_reported_not_forced_retries_configurable(snap):
    """KC-21 (host part), KC-31."""
    fake = FakeKiCadClient(snap)
    fake.busy = True
    link = KiCadLink(client_factory=lambda a: fake, fake=True)
    r = KicadBridge(link=link).highlight(["R9"])
    assert r["ok"] is False and r["busy"] is True and "busy" in r["message"].lower()
    assert fake.names.count("GetOpenDocuments") == 1 and "AddToSelection" not in fake.names       # TBR-2 default 0 retries; nothing selected
    fake.sent.clear()
    link2 = KiCadLink(client_factory=lambda a: fake, fake=True, busy_retries=2)
    KicadBridge(link=link2).highlight(["R9"])
    assert fake.names.count("GetOpenDocuments") == 3


def test_scanner_client_busy(snap):
    fake = FakeKiCadClient(snap)
    fake.busy = True
    r = KicadScannerClient(link=KiCadLink(client_factory=lambda a: fake, fake=True)).highlight_targets(["R9"])
    assert r.ok is False and "busy" in r.error.lower()


def test_newer_kicad_is_a_note_not_a_failure(snap):
    """KC-32: kipy 0.8.0 is built for 10.0.6; check_version() would raise on 10.0.7. It is never called."""
    fake = FakeKiCadClient(snap, version=(10, 0, 7, "10.0.7"))
    st = KicadBridge(link=KiCadLink(client_factory=lambda a: fake, fake=True)).status()
    assert st["connected"] and st["version"] == "10.0.7" and "newer than the kicad-python build" in st["detail"]
    fake2 = FakeKiCadClient(snap, version=(10, 0, 6, "10.0.6"))
    st2 = KicadBridge(link=KiCadLink(client_factory=lambda a: fake2, fake=True)).status()
    assert st2["connected"] and "newer" not in st2["detail"]
    import kipy.kicad
    src = Path(__file__).resolve().parents[2].joinpath("src/polybench/kicad").glob("*.py")
    assert not any("check_version(" in p.read_text(encoding="utf-8") for p in src if p.name != "link.py") and "check_version()" not in Path(
        Path(__file__).resolve().parents[2], "src/polybench/kicad/link.py").read_text(encoding="utf-8").replace("kipy.check_version() is never called", "")


def test_no_board_open(snap):
    """KC-26 (host part)."""
    fake = FakeKiCadClient(snap)
    fake.no_board = True
    link = KiCadLink(client_factory=lambda a: fake, fake=True)
    b = KicadBridge(link=link)
    st = b.status()
    assert st["connected"] is True and st["editor"] == "none" and "no board is open" in st["detail"]
    r = b.highlight(["R9"])
    assert r["ok"] is False and r["state"] == "no_board"
    assert not (set(fake.names) & {"AddToSelection", "ClearSelection", "GetItems"})         # nothing sent beyond the document list
    assert b.designators() is None


def test_settings_files_never_written(no_socket):
    """KC-36: the plug-in writes nothing under KiCad's settings folder."""
    f = _prefs(no_socket, False)
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in f.parent.iterdir()}
    for _ in range(3):
        KicadBridge(link=KiCadLink()).status()
    assert before == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in f.parent.iterdir()} and {p.name for p in f.parent.iterdir()} == {"kicad_common.json"}
