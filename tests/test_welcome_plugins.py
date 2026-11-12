"""First-run welcome page (W1 to W5) and the Plugins panel (P1 to P4): CLI start with no project, the no-project API, the demo
project's safety, the /api/plugins body. Host-only: simulators and fakes, no port, instrument or ECAD program is touched."""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import DEMO, ROOT
from pp_helpers import DEMO_PROTO, args, cur, new_project_session
from polybench import extras, plugin_status, registry, workspace as wslib
from polybench.cli import resolve_start
from polybench.server import create_app

STATIC = ROOT / "src" / "polybench" / "static"


def _free_port(start=8960):
    for p in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port")


def empty_client(simulate=False) -> TestClient:
    return TestClient(create_app(None, simulate=simulate), raise_server_exceptions=False)


# ---------------------------------------------------------------- W1: CLI and no-project API

def test_resolve_start_without_project_is_not_an_error():
    """W1: no flags and an empty recent list: (None, ...) instead of SystemExit."""
    assert resolve_start(args(simulate=False)) == (None, None, None, None, False)


def test_resolve_start_recent_without_usable_project_is_welcome(tmp_path):
    """W1: a recent list whose folders are gone (no settings file) also starts on the welcome page."""
    wslib.atomic_write_json(wslib.recent_file(), {"schema": 1, "projects": [{"dir": str(tmp_path / "gone"), "name": "x", "batch": "b"}]})
    assert resolve_start(args(simulate=False))[0] is None


def test_explicit_data_dir_without_settings_is_still_refused(tmp_path):
    (tmp_path / "bare").mkdir()
    with pytest.raises(SystemExit):
        resolve_start(args(None, tmp_path / "bare"))


def test_cli_without_arguments_serves_the_welcome_page(tmp_path):
    """W1: the real command line, no --protocol, no --data-dir, empty config: it stays up and answers /api/state with no_project."""
    port = _free_port()
    env = dict(os.environ)          # the test's POLYBENCH_CONFIG_DIR (empty), POLYBENCH_DATA, POLYBENCH_DEMO_DIR
    p = subprocess.Popen([sys.executable, "-c", "import sys; from polybench.cli import main; sys.exit(main())", "--port", str(port), "--no-browser"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    try:
        st, deadline = None, time.time() + 20
        while time.time() < deadline and p.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
                    st = json.loads(r.read())
                break
            except OSError:
                time.sleep(0.3)
        assert st is not None, p.stdout.read() if p.poll() is not None else "no answer in 20 s"
        assert st["no_project"] is True and st["workspace"]["recent"] == []
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            assert r.status == 200 and b"onboarding.js" in r.read()
        assert p.poll() is None
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
    assert not (Path(os.environ["POLYBENCH_DATA"]) / "polybench-project.json").exists()


def test_no_project_api_never_500s():
    """W1: with no project, every route either answers (the welcome page's) or says 409 no_project; none is a 500."""
    c = empty_client()
    app = c.app
    seen = 0
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None) or set()
        if not path.startswith(("/api/", "/aoi/")) or path in ("/api/events", "/api/scanner/stream.mjpg"):
            continue                                    # endless streams
        url = re.sub(r"\{[^}]+\}", "x", path)
        for m in methods & {"GET", "POST"}:
            resp = c.get(url) if m == "GET" else c.post(url, json={})
            seen += 1
            assert resp.status_code != 500, (m, path, resp.text[:200])
            if path not in app.state.holder.get("allowed", set()) and resp.status_code == 409:
                assert resp.json().get("no_project") is True or "error" in resp.json()
    assert seen > 40


def test_no_project_state_and_plugins_and_refusals():
    c = empty_client()
    st = c.get("/api/state").json()
    assert st["no_project"] is True and st["workspace"]["ecad"]["platforms"] and st["demo"] is None
    assert st["plugins"]["total"] >= 20 and st["plugins"]["attention"] == 0         # nothing is "used" with no protocol
    assert c.get("/api/console").json() == []
    assert c.get("/api/workspace").json()["no_project"] is True
    for method, url in (("get", "/api/journal"), ("get", "/api/testplan"), ("get", "/api/ecad/status"), ("post", "/api/cmd/note"),
                        ("post", "/api/action/run"), ("get", "/api/voice/status"), ("get", "/api/scanner/state"), ("get", "/api/ai/status"),
                        ("post", "/api/ecad/platform"), ("post", "/api/bom/load"), ("get", "/api/report/preview")):
        r = getattr(c, method)(url, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 409 and r.json()["no_project"] is True, (url, r.status_code)
    assert "Polybench" in c.get("/").text


def test_fs_list_with_no_project_starts_in_home(tmp_path):
    r = empty_client().get("/api/fs/list", params={"path": str(tmp_path)})
    assert r.status_code == 200 and r.json()["path"] == str(tmp_path.resolve())


def test_new_project_from_the_welcome_page(tmp_path):
    """W2: New project works with no project open; the protocol must be named."""
    c = empty_client()
    r = c.post("/api/project/new", json={"dir": str(tmp_path / "p1"), "name": "first"})
    assert r.status_code == 422 and "protocol" in r.json()["error"]
    r = c.post("/api/project/new", json={"dir": str(tmp_path / "p1"), "name": "first", "protocol": str(DEMO_PROTO), "boards": 2})
    assert r.status_code == 200, r.text
    st = c.get("/api/state").json()
    assert "no_project" not in st and st["workspace"]["project"]["name"] == "first" and st["origin"] == "measured"
    assert wslib.recent_list()[0]["dir"] == str((tmp_path / "p1").resolve())


def test_open_project_from_the_welcome_page_uses_the_process_mode(tmp_path):
    """W2/W5: a project opened from the welcome page takes the process's simulate flag, not a demo's."""
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    c = empty_client(simulate=True)
    r = c.post("/api/project/open", json={"dir": str(tmp_path / "proj")})
    assert r.status_code == 200, r.text
    assert c.get("/api/state").json()["origin"] == "simulated"


# ---------------------------------------------------------------- W5: demo safety

def _tree(p: Path) -> set[str]:
    return {str(x.relative_to(p)) for x in p.rglob("*")} if p.exists() else set()


def test_demo_is_simulated_in_its_own_folder_and_back_goes_to_welcome():
    c = empty_client(simulate=False)                         # a real-data process: the demo is simulated all the same
    data, cfg = Path(os.environ["POLYBENCH_DATA"]), wslib.config_dir()
    r = c.post("/api/demo/open", json={})
    assert r.status_code == 200 and r.json()["simulated"] is True
    st = c.get("/api/state").json()
    assert st["origin"] == "simulated" and st["demo"]["active"] and st["demo"]["back"] == {"kind": "welcome"}
    demo = wslib.demo_dir().resolve()
    assert Path(st["meta"]["dir"]).resolve().is_relative_to(demo) and demo.is_dir()
    assert not data.exists() or not _tree(data)               # nothing written to the data folder
    assert wslib.recent_list() == []                          # the demo is not a "recent project"
    assert st["workspace"]["simulate"] is True
    assert type(cur(c).bridge).__name__ == "SimulatedEcad"
    r = c.post("/api/demo/close", json={})
    assert r.status_code == 200 and r.json()["back"] == "welcome"
    assert c.get("/api/state").json()["no_project"] is True
    assert c.post("/api/demo/close", json={}).status_code == 409


def test_demo_back_reopens_the_users_project_and_leaves_it_untouched(tmp_path):
    sess = new_project_session(tmp_path / "mine")
    mine = tmp_path / "mine"
    c = TestClient(create_app(sess, simulate=False), raise_server_exceptions=False)
    journal = Path(sess.journal.path)
    before = journal.read_bytes()
    n_events = len(sess.journal.events)
    assert c.post("/api/demo/open", json={}).status_code == 200
    st = c.get("/api/state").json()
    assert st["demo"]["back"]["kind"] == "project" and Path(st["demo"]["back"]["dir"]) == mine.resolve()
    assert Path(st["meta"]["dir"]).resolve().is_relative_to(wslib.demo_dir().resolve())
    assert journal.read_bytes() == before                      # the demo wrote nothing into the user's batch
    # an action run in the demo goes to the demo's journal only
    r = c.post("/api/demo/close", json={})
    assert r.status_code == 200 and r.json()["back"] == "project", r.json()
    st = c.get("/api/state").json()
    assert "no_project" not in st and st["demo"] is None and Path(st["workspace"]["project"]["dir"]) == mine.resolve()
    assert st["origin"] == "simulated"                         # the project reopens with its own mode (the test project is simulated), not the demo's
    assert len(cur(c).journal.events) >= n_events and cur(c).journal.path == journal


def test_demo_refuses_a_folder_inside_the_data_folder(monkeypatch):
    monkeypatch.setenv("POLYBENCH_DEMO_DIR", str(Path(os.environ["POLYBENCH_DATA"]) / "demo"))
    r = empty_client().post("/api/demo/open", json={})
    assert r.status_code == 409 and "data folder" in r.json()["error"]
    assert not (Path(os.environ["POLYBENCH_DATA"]) / "demo").exists()


def test_demo_uses_only_demo_actions():
    from polybench.protocol import load
    p = load(DEMO / "demo.pcbv.yaml")
    names = {a["plugin"] for st in p.data["steps"] for sec in st.get("sections", []) or [] for a in sec.get("actions", []) or []}
    assert names and all(n.startswith("demo.") for n in names)


def test_opening_the_demo_while_an_action_runs_is_refused(tmp_path):
    sess = new_project_session(tmp_path / "mine")
    c = TestClient(create_app(sess), raise_server_exceptions=False)
    sess.runner.status = lambda: {"title": "running"}
    r = c.post("/api/demo/open", json={})
    assert r.status_code == 409 and "action is running" in r.json()["error"]


# ---------------------------------------------------------------- P1 to P4: plugins

def test_plugins_endpoint_rows_and_columns(tmp_path):
    sess = new_project_session(tmp_path / "proj")
    c = TestClient(create_app(sess), raise_server_exceptions=False)
    b = c.get("/api/plugins").json()
    rows = {r["name"]: r for r in b["plugins"]}
    assert {"demo.measure", "openocd.flash", "aoi.run"} <= set(rows) and {"fusion", "kicad"} <= set(rows)
    assert rows["fusion"]["kind"] == "ecad" and rows["demo.measure"]["kind"] == "action"
    for r in rows.values():
        assert set(r) >= {"name", "kind", "source", "status", "detail", "install", "used", "settings", "last"}
        assert r["status"] in ("loaded", "missing_extra", "missing_library", "import_error")
        assert r["source"]["label"]
    assert rows["demo.measure"]["source"]["label"].startswith("built-in")
    assert rows["demo.measure"]["used"] is True and rows["demo.measure"]["status"] == "loaded"
    assert rows["demo.flash"]["used"] in (True, False)
    assert rows["pcan.capture"]["used"] is False and rows["pcan.capture"]["last"] is None      # "not run"
    assert b["protocol"] == str(sess.proto.path)
    assert b["summary"]["total"] == len(rows)


def test_plugins_status_missing_extra_shows_the_exact_command(tmp_path, monkeypatch):
    monkeypatch.setattr(extras, "missing", lambda extra: ["cv2"] if extra == "camera" else [])
    plugin_status._cache.clear()
    try:
        row = {r["name"]: r for r in plugin_status.build(None, rescan=True)["plugins"]}["camera.snapshot"]
    finally:
        plugin_status._cache.clear()
    assert row["status"] == "missing_extra"
    assert row["install"]["pip"] == extras.install_command("camera") == 'pip install "polybench-pcb-validation[camera]"'
    assert row["install"]["pipx"].startswith("pipx ")
    assert "polybench-pcb-validation[camera]" in row["detail"]


def test_plugins_status_import_error_is_one_line(monkeypatch):
    class Boom:
        name = "boom.act"
        dist = None

        def load(self):
            raise ImportError("no module named\nsome_vendor_lib " + "x" * 400)
    real = registry._eps
    monkeypatch.setattr(registry, "_eps", lambda g: [Boom()] if g == "polybench.actions" else real(g))
    plugin_status._cache.clear()
    try:
        row = {r["name"]: r for r in plugin_status.discover(rescan=True)}["boom.act"]
    finally:
        plugin_status._cache.clear()
    assert row["status"] == "import_error" and "\n" not in row["detail"] and len(row["detail"]) <= 160 and row["detail"].startswith("ImportError")


def test_plugins_settings_are_read_only_and_secrets_masked(tmp_path):
    proto = tmp_path / "p"
    import shutil
    shutil.copytree(DEMO, proto)
    f = proto / "demo.pcbv.yaml"
    pf = proto / "powered.yaml"
    txt = pf.read_text(encoding="utf-8")
    assert "demo.measure" in txt
    pf.write_text(txt.replace("            params:\n", "            params:\n              api_key: SECRET-VALUE-1\n              nested: {Token: SECRET-VALUE-2, keep: 7}\n", 1), encoding="utf-8")
    sess = new_project_session(tmp_path / "proj", protocol=f)
    c = TestClient(create_app(sess), raise_server_exceptions=False)
    body = c.get("/api/plugins")
    assert "SECRET-VALUE" not in body.text
    row = {r["name"]: r for r in body.json()["plugins"]}["demo.measure"]
    s = row["settings"][0]
    assert s["params"]["api_key"] == plugin_status.MASK and s["params"]["nested"]["Token"] == plugin_status.MASK and s["params"]["nested"]["keep"] == 7
    assert "values" in s["params"] and s["file"].endswith("powered.yaml") and "/" in s["where"] or " / " in s["where"]
    assert plugin_status.mask({"password": 1, "a": [{"my_secret": 2, "ok": 3}]}) == {"password": plugin_status.MASK, "a": [{"my_secret": plugin_status.MASK, "ok": 3}]}


def test_plugins_last_result_comes_from_the_journal(tmp_path):
    sess = new_project_session(tmp_path / "proj")
    c = TestClient(create_app(sess), raise_server_exceptions=False)
    sess.engine._emit("action_start", {"run": 1, "plugin": "demo.measure", "board": "S02", "action": "read_rails", "step": "powered"})
    row = {r["name"]: r for r in c.get("/api/plugins").json()["plugins"]}["demo.measure"]
    assert row["last"]["state"] == "started" and row["last"]["board"] == "S02"
    sess.engine._emit("action_end", {"run": 1, "state": "ok", "text": "fine"})
    row = {r["name"]: r for r in c.get("/api/plugins").json()["plugins"]}["demo.measure"]
    assert row["last"]["state"] == "ok" and row["last"]["ts"] and row["last"]["text"] == "fine"


def test_state_chip_counts_only_used_plugins_that_are_not_loaded(tmp_path, monkeypatch):
    """P1: 'Plugins N · M needs attention': M counts plugins the protocol uses that are not loaded."""
    sess = new_project_session(tmp_path / "proj")
    c = TestClient(create_app(sess), raise_server_exceptions=False)
    sm = c.get("/api/state").json()["plugins"]
    assert sm["attention"] == 0 and sm["total"] == c.get("/api/plugins").json()["summary"]["total"]
    plugin_status._cache.clear()
    real = plugin_status._status
    monkeypatch.setattr(plugin_status, "_status", lambda n, k, t: {"status": "import_error", "detail": "ImportError: x", "install": None} if n == "demo.measure" else real(n, k, t))
    try:
        assert c.get("/api/state").json()["plugins"]["attention"] == 1
        assert c.get("/api/state").json()["plugins"]["errors"] == 1               # UIR-19: an import error turns the one Plugins entry red
    finally:
        plugin_status._cache.clear()


def test_plugins_panel_touches_no_hardware(tmp_path, monkeypatch):
    """P4: building the list and opening the welcome page connect nothing and probe no USB device."""
    from helpers import refuse_outside_connections
    refuse_outside_connections(monkeypatch, "a socket connect was attempted")
    calls = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (calls.append(a), real_popen(*a, **k))[1])
    plugin_status._cache.clear()
    c = empty_client()
    assert c.get("/").status_code == 200 and c.get("/api/state").status_code == 200
    assert c.get("/api/plugins?rescan=1").status_code == 200
    sess = new_project_session(tmp_path / "proj")
    c2 = TestClient(create_app(sess))
    assert c2.get("/api/plugins").status_code == 200 and c2.get("/api/state").status_code == 200
    assert calls == []                                          # no ioreg, no openocd, no esptool, nothing


# ---------------------------------------------------------------- W3: copy

def test_onboarding_stage_names_follow_the_product_spec():
    spec_file = ROOT / "docs" / "PRODUCT_SPEC.md"
    if not spec_file.exists():
        pytest.skip("docs/PRODUCT_SPEC.md is internal and is not in the public tree")
    spec = spec_file.read_text(encoding="utf-8")
    sec = spec.split("## 4. The step model", 1)[1].split("\n## ", 1)[0]
    rows = [re.match(r"\|\s*(\d+)\s*\|\s*(\w+)\s*\|\s*([^|]+?)\s*\|", ln) for ln in sec.splitlines() if re.match(r"\|\s*\d+\s*\|", ln)]
    steps = [(int(m.group(1)), m.group(2), m.group(3)) for m in rows]
    assert len(steps) == 12
    js = (STATIC / "onboarding.js").read_text(encoding="utf-8")
    names = re.findall(r"\{ name: '([^']+)', gets: '([^']+)'(?:, status: '(\w+)')?", js)
    assert len(names) == 12
    for (n, phase, spec_name), (name, gets, status) in zip(steps, names):
        assert spec_name.lower().startswith(name.lower()) or name.lower().startswith(spec_name.lower().split(" (")[0].split(" / ")[0]), (spec_name, name)
        assert len(gets.split()) <= 14
    assert [s for _, _, s in names if s] == ["unbuilt", "unbuilt", "preview"]          # steps 2, 3 not built, 4 a preview (spec section 4)
    assert [n for n, _, s in names if s == "unbuilt"] == ["Schematic analysis", "BOM review"]


def test_onboarding_copy_has_no_exclamation_marks_or_emoji():
    for f in ("onboarding.js", "plugins.js"):
        t = (STATIC / f).read_text(encoding="utf-8")
        if f == "onboarding.js":
            assert "!" not in "".join(re.findall(r"(?:name|sub|gets): '([^']+)'", t))
        assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", t)
    assert "onboarding.js" in (STATIC / "index.html").read_text(encoding="utf-8") and "plugins.js" in (STATIC / "index.html").read_text(encoding="utf-8")
