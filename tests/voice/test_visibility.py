"""Page visibility: tracked per client. A fresh server must treat a page that says it is visible as visible, and a hidden second tab
(or a headless browser that was closed) must not mute the visible one."""
import json

from fastapi.testclient import TestClient

from polybench.server import create_app
from polybench.voice import capture as capmod, config as vcfg

from test_capture import Harness
from test_controller import make_session


def test_visible_client_opens_the_mic_and_a_hidden_one_does_not_close_it(tmp_path):
    h = Harness(tmp_path)
    cap = h.cap
    cap.set_enabled(True)
    assert not cap.visible and cap.rec is None                  # no client has spoken yet
    cap.set_visible(True, "alice")
    assert cap.visible and cap.rec is not None
    cap.set_visible(False, "headless")                           # the second trap
    assert cap.visible and cap.rec is not None and not h.opened[0].closed
    cap.set_visible(False, "alice")                               # now nobody is visible
    assert not cap.visible and cap.rec is None and h.opened[0].closed
    cap.set_visible(True, "headless")
    assert cap.rec is not None


def test_a_visible_client_that_stops_posting_is_forgotten(tmp_path):
    h = Harness(tmp_path)
    cap = h.cap
    t = [1000.0]
    cap._now = lambda: t[0]
    cap.set_enabled(True)
    cap.set_visible(True, "closed-tab")
    assert cap.visible
    t[0] += cap.VIS_TTL_S + 1
    assert not cap.visible
    cap.set_visible(True, "other")
    assert cap.visible and len(h.opened) >= 1


def test_context_post_from_a_visible_page_makes_the_server_visible(tmp_path):
    sess = make_session(tmp_path)
    with TestClient(create_app(sess)) as c:
        assert c.get("/api/voice/status").json()["context"]["visible"] is False          # fresh server
        c.post("/api/voice/context", json={"step": "unpowered", "client": "a"})           # no field: the page is on screen
        assert c.get("/api/voice/status").json()["context"]["visible"] is True
        c.post("/api/voice/visibility", json={"visible": False, "client": "b"})           # another tab hidden
        assert c.get("/api/voice/status").json()["context"]["visible"] is True
        c.post("/api/voice/context", json={"step": "unpowered", "client": "a", "visible": False})
        assert c.get("/api/voice/status").json()["context"]["visible"] is False
        c.post("/api/voice/context", json={"step": "unpowered", "client": "a", "visible": True})
        assert c.get("/api/voice/status").json()["context"]["visible"] is True
