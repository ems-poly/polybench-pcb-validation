"""Project page PP-05: start-up and persistence (cli.resolve_start, open_workspace_session, recents, restart, the existing command form)."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from pp_helpers import CSV_BOM, DEMO_PROTO, args, client, events, new_project_session, settings, start
from polybench import workspace as wslib
from polybench.cli import resolve_start


def test_protocol_flag_on_folder_without_settings_adopts(tmp_path):
    """PP-05: --protocol on a folder without settings adopts it: settings written next to the batch folders, journalled."""
    a = args(DEMO_PROTO, tmp_path / "proj", "B1")
    ws, batch, ecad_o, proto_o, adopted = resolve_start(a)
    assert adopted is True and batch == "B1" and ecad_o is None and proto_o is None
    assert ws.settings["protocol_path"] == str(DEMO_PROTO.resolve()) and ws.ecad_program() == "fusion"
    assert not ws.file.exists()                     # resolve_start writes nothing; the file comes once the session opened
    sess = start(a)
    s = settings(tmp_path / "proj")
    assert s["current_batch"] == "B1" and s["name"] == "DEMO-1"
    assert events(sess, "project_settings")[-1]["payload"]["change"] == "adopted"
    assert wslib.recent_list()[0] == {**wslib.recent_list()[0], "dir": str((tmp_path / "proj").resolve()), "batch": "B1"}


def test_default_data_dir_is_polybench_data_env(tmp_path):
    """PP-05: --protocol without --data-dir uses POLYBENCH_DATA (the test's tmp folder, never ~/polybench-data)."""
    ws, *_ = resolve_start(args(DEMO_PROTO, None, "B1"))
    from pathlib import Path
    assert ws.dir == Path(os.environ["POLYBENCH_DATA"]).resolve() and ws.dir != (Path.home() / "polybench-data").resolve()


def test_ecad_flag_overrides_for_this_run_only(tmp_path):
    """PP-05: with settings, a different --ecad is an override for this run; the saved settings do not change."""
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    before = (tmp_path / "proj" / "polybench-project.json").read_bytes()
    a = args(DEMO_PROTO, tmp_path / "proj", None, ecad="kicad")
    ws, batch, ecad_o, proto_o, adopted = resolve_start(a)
    assert not adopted and ecad_o == "kicad" and ws.overrides == {"ecad": "kicad"}
    s2 = start(a)
    assert s2.bridge.name == "kicad" and s2.batch == "B1"
    assert (tmp_path / "proj" / "polybench-project.json").read_bytes() == before
    assert settings(tmp_path / "proj")["ecad"]["program"] == "fusion"
    with client(s2) as c:
        w = c.get("/api/workspace").json()
        assert w["project"]["overrides"] == {"ecad": "kicad"} and w["ecad"]["program"] == "kicad" and w["ecad"]["saved"] == "fusion"


def test_protocol_and_batch_flags_override_without_saving(tmp_path):
    """PP-05: a different --protocol / --batch on a project with settings are run-only overrides too."""
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    other = tmp_path / "p2"
    import shutil
    shutil.copytree(DEMO_PROTO.parent, other)
    a = args(other / "demo.pcbv.yaml", tmp_path / "proj", "B2")
    ws, batch, _e, proto_o, adopted = resolve_start(a)
    assert not adopted and batch == "B2" and proto_o == str((other / "demo.pcbv.yaml").resolve())
    assert ws.overrides == {"protocol": proto_o, "batch": "B2"}
    s2 = start(a)
    assert s2.batch == "B2" and str(s2.proto.path.resolve()) == proto_o
    s = settings(tmp_path / "proj")
    assert s["protocol_path"] == str(DEMO_PROTO.resolve()) and s["current_batch"] == "B1"


def test_no_flags_reopens_last_project_and_batch(tmp_path):
    """PP-05: no flags reopens the last project in the recent list, on the batch it was last opened with."""
    a = new_project_session(tmp_path / "a", batch="A1")
    a.journal.close()
    b = new_project_session(tmp_path / "b", batch="B1")
    b.journal.close()
    ws, batch, ecad_o, proto_o, adopted = resolve_start(args())
    assert ws.dir == (tmp_path / "b").resolve() and batch == "B1" and not adopted and ecad_o is None and proto_o is None
    wslib.recent_touch(tmp_path / "a", "a", "A1")
    ws, batch, *_ = resolve_start(args())
    assert ws.dir == (tmp_path / "a").resolve() and batch == "A1"
    # a recent entry whose settings file was deleted is skipped
    (tmp_path / "a" / "polybench-project.json").unlink()
    ws, batch, *_ = resolve_start(args())
    assert ws.dir == (tmp_path / "b").resolve()


def test_no_recents_no_flags_starts_on_the_welcome_page(tmp_path):
    """PP-05, changed for the first-run welcome page: no recent project and no flags is not an error, there is no project
    (workspace None). An explicit --data-dir without a settings file is still refused."""
    assert resolve_start(args()) == (None, None, None, None, False)
    (tmp_path / "bare").mkdir()
    with pytest.raises(SystemExit) as e:
        resolve_start(args(None, tmp_path / "bare"))
    assert "polybench-project.json" in str(e.value)


def test_protocol_flag_on_batch_folder_is_refused(tmp_path):
    """PP-05: --data-dir pointing at a batch folder is refused (it is the project folder that holds batches)."""
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    with pytest.raises(SystemExit) as e:
        resolve_start(args(DEMO_PROTO, tmp_path / "proj" / "B1", "B1"))
    assert "batch folder" in str(e.value)


def test_restart_keeps_batch_settings_bom_and_journal(tmp_path):
    """PP-05: change platform + load a BOM + record an entry, close; a start with no flags (same env) reopens the same batch with the
    settings kept, the BOM override applied and every journal event replayed."""
    sess = new_project_session(tmp_path / "proj", batch="R1")
    with client(sess) as c:
        assert c.post("/api/ecad/platform", json={"program": "kicad"}).status_code == 200
        assert c.post("/api/bom/load", json={"path": str(CSV_BOM)}).status_code == 200
        assert c.post("/api/cmd/entry", json={"board": "S01", "test_id": "V01", "value": "y"}).status_code == 200
        before_ids = [e["id"] for e in sess.journal.events]
        before_snap = sess.engine.state.snapshot()
    sess.journal.close()
    s2 = start(args())
    assert s2.batch == "R1" and s2.workspace.dir == (tmp_path / "proj").resolve()
    assert s2.bridge.name == "kicad" and s2.workspace.ecad_program() == "kicad" and s2.workspace.overrides == {}
    src = s2.bom_source()
    assert src["source"] == "settings" and src["name"] == CSV_BOM.name and s2.bom_error is None
    assert sorted(d for b in s2.proto.data["bom"] for d in b["designators"])[:3] == ["C1", "C2", "C3"]
    assert [e["id"] for e in s2.journal.events][:len(before_ids)] == before_ids and not s2.journal.problems
    snap = s2.engine.state.snapshot()
    assert json.dumps(snap["project"]["history"], default=str) == json.dumps(before_snap["project"]["history"], default=str)
    assert [e["type"] for e in s2.journal.events].count("entry") == 1
    assert json.dumps(snap["cells"], sort_keys=True, default=str) == json.dumps(before_snap["cells"], sort_keys=True, default=str)
    assert [e["type"] for e in s2.journal.events if e["type"] in ("ecad_platform", "bom_source")] == ["ecad_platform", "bom_source"]


def test_command_form_resolves(tmp_path):
    """PP-05: `polybench --protocol X --batch B --data-dir D --ecad kicad --simulate` keeps working: first run adopts D, a second
    run with the same flags resumes B with no override and no second adoption."""
    a = args(DEMO_PROTO, tmp_path / "D", "B", ecad="kicad")
    s1 = start(a)
    assert s1.bridge.name == "kicad" and s1.simulate and settings(tmp_path / "D")["ecad"]["program"] == "kicad"
    s1.journal.close()
    ws, batch, ecad_o, proto_o, adopted = resolve_start(a)
    assert not adopted and ws.overrides == {} and batch == "B"
    s2 = start(a)
    assert s2.batch == "B" and s2.bridge.name == "kicad"
    assert [e["payload"]["change"] for e in events(s2, "project_settings")] == ["adopted"]


def _free_port(start=8930):
    for p in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port")


def test_subprocess_smoke_start(tmp_path):
    """PP-05: the real command line starts on a free port (8930+) with the existing flags and answers /api/state (≤20 s)."""
    port = _free_port()
    env = dict(os.environ)          # carries the test's POLYBENCH_CONFIG_DIR / POLYBENCH_DATA / POLYBENCH_NO_REVEAL
    cmd = [sys.executable, "-c", "import sys; from polybench.cli import main; sys.exit(main())",
           "--protocol", str(DEMO_PROTO), "--batch", "SMOKE", "--data-dir", str(tmp_path / "D"), "--ecad", "kicad", "--simulate",
           "--port", str(port), "--no-browser", "--boards", "2"]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
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
        assert st["workspace"]["batch"]["name"] == "SMOKE" and st["workspace"]["ecad"]["program"] == "kicad"
        assert st["workspace"]["project"]["dir"] == str((tmp_path / "D").resolve())
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
    assert (tmp_path / "D" / "polybench-project.json").is_file()
    assert json.loads((tmp_path / "D" / "polybench-project.json").read_text(encoding="utf-8"))["current_batch"] == "SMOKE"
    assert wslib.recent_list()[0]["dir"] == str((tmp_path / "D").resolve())
