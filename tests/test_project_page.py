"""Project page (step 1, card 0): project folder model. PP-02 new/existing, PP-03 workspace paths and reveal, PP-04 settings file.

Every test runs with its own POLYBENCH_CONFIG_DIR / POLYBENCH_DATA and POLYBENCH_NO_REVEAL=1 (conftest isolated_polybench_dirs).
"""
import json
import os
import stat
from pathlib import Path

import pytest

from pp_helpers import (DEMO_PROTO, client, cur, events, legacy_folder, new_project_session, settings, sha, tree)
from polybench import workspace as wslib


# ------------------------------------------------------------------------------------------------------------ PP-02 new project
@pytest.mark.parametrize("exists", [True, False])
def test_new_project_in_empty_or_missing_folder(tmp_path, exists):
    """PP-02: New in an empty (or not yet existing) folder writes polybench-project.json and the batch folder, and swaps the session."""
    sess = new_project_session(tmp_path / "first")
    target = tmp_path / "second" / "nested"
    if exists:
        target.mkdir(parents=True)
    with client(sess) as c:
        r = c.post("/api/project/new", json={"dir": str(target), "name": "Second DEMO", "protocol": str(DEMO_PROTO), "batch": "X 1", "boards": 2})
        assert r.status_code == 200, r.text
        assert r.json()["batch"] == "X-1"
        assert (target / "polybench-project.json").is_file()
        assert (target / "X-1" / "journal.jsonl").is_file()
        s = settings(target)
        assert s["name"] == "Second DEMO" and s["current_batch"] == "X-1" and s["protocol_path"] == str(DEMO_PROTO.resolve())
        st = c.get("/api/state").json()
        ws = st["workspace"]
        assert ws["batch"]["name"] == "X-1" and ws["batch"]["journal"] == str((target / "X-1" / "journal.jsonl").resolve())
        assert ws["project"]["dir"] == str(target.resolve())
        assert cur(c) is not sess and cur(c).workspace.dir == target.resolve()
        ev = events(cur(c), "project_settings")
        assert ev and ev[-1]["payload"]["change"] == "created"
        assert wslib.recent_list()[0]["dir"] == str(target.resolve())


def test_new_project_refuses_existing_project_legacy_and_batch(tmp_path):
    """PP-02: New refuses a folder that holds a project, a legacy batch parent or a batch folder (409, clear message); nothing is written."""
    sess = new_project_session(tmp_path / "proj")
    legacy = legacy_folder(tmp_path / "legacy")
    with client(sess) as c:
        for d in (tmp_path / "proj", legacy):
            before = tree(d)
            r = c.post("/api/project/new", json={"dir": str(d), "name": "dup"})
            assert r.status_code == 409, (d, r.text)
            assert "already holds a project" in r.json()["error"]
            assert tree(d) == before
        # a batch folder is not refused outright any more (D8): it asks for a confirm, and writes nothing until then
        before = tree(legacy / "L1")
        r = c.post("/api/project/new", json={"dir": str(legacy / "L1"), "name": "dup"})
        assert r.status_code == 409 and r.json()["needs_confirm"] and r.json()["warnings"][0]["code"] == "batch_folder"
        assert tree(legacy / "L1") == before
        assert cur(c) is sess


def test_new_project_refuses_file_and_unwritable_folder(tmp_path):
    """PP-02: New refuses a file path and an unwritable folder (422), and needs a name."""
    sess = new_project_session(tmp_path / "proj")
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with client(sess) as c:
            r = c.post("/api/project/new", json={"dir": str(f), "name": "n"})
            assert r.status_code == 422 and "file" in r.json()["error"]
            if hasattr(os, 'geteuid') and os.geteuid() != 0:          # Windows has no POSIX directory permission bits (W1-7: not tested there)
                r = c.post("/api/project/new", json={"dir": str(ro), "name": "n"})
                assert r.status_code == 422 and "writable" in r.json()["error"]
                r = c.post("/api/project/new", json={"dir": str(ro / "sub"), "name": "n"})
                assert r.status_code == 422 and "not writable" in r.json()["error"]
            r = c.post("/api/project/new", json={"dir": str(tmp_path / "ok"), "name": "  "})
            assert r.status_code == 422 and "name" in r.json()["error"]
            r = c.post("/api/project/new", json={"dir": str(tmp_path / "ok"), "name": "n", "protocol": str(tmp_path / "nope.pcbv.yaml")})
            assert r.status_code == 422 and not (tmp_path / "ok").exists()
    finally:
        ro.chmod(stat.S_IRWXU)


# ------------------------------------------------------------------------------------------------------------ PP-02 open
def test_open_settings_folder_swaps_session(tmp_path):
    """PP-02: Open of a folder with polybench-project.json reopens it on its current batch; /api/state follows the new session."""
    a = new_project_session(tmp_path / "a", batch="A1")
    a.journal.close()
    b = new_project_session(tmp_path / "b", batch="B1")
    with client(b) as c:
        assert c.get("/api/state").json()["workspace"]["batch"]["name"] == "B1"
        r = c.post("/api/project/open", json={"dir": str(tmp_path / "a")})
        assert r.status_code == 200 and r.json() == {"ok": True, "project": str((tmp_path / "a").resolve()), "batch": "A1", "adopted": False}
        st = c.get("/api/state").json()
        assert st["workspace"]["batch"]["name"] == "A1"
        assert st["workspace"]["batch"]["journal"] == str((tmp_path / "a" / "A1" / "journal.jsonl").resolve())
        assert st["workspace"]["project"]["dir"] == str((tmp_path / "a").resolve())
        assert events(cur(c), "project_settings")[-1]["payload"]["change"] == "opened"
        # a second batch of the same project, by name
        r = c.post("/api/project/open", json={"dir": str(tmp_path / "a"), "batch": "A2"})
        assert r.status_code == 200 and c.get("/api/state").json()["workspace"]["batch"]["name"] == "A2"
        assert settings(tmp_path / "a")["current_batch"] == "A2"


def test_open_legacy_folder_adopts_in_place(tmp_path):
    """PP-02: Open of a legacy folder (batch sub-folder with journal.jsonl, no settings) adopts it in place: the settings file is
    written, the batch folder and journal are untouched (existing lines are a byte-identical prefix), nothing is moved or renamed."""
    legacy = legacy_folder(tmp_path / "old", batches=("L1",))
    journal = legacy / "L1" / "journal.jsonl"
    before_bytes = journal.read_bytes()
    before_tree = tree(legacy)
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        assert c.get("/api/project/inspect", params={"path": str(legacy)}).json()["kind"] == "legacy"
        r = c.post("/api/project/open", json={"dir": str(legacy), "protocol": str(DEMO_PROTO)})
        assert r.status_code == 200 and r.json()["adopted"] is True and r.json()["batch"] == "L1"
        s = settings(legacy)
        assert s["current_batch"] == "L1" and s["protocol_path"] == str(DEMO_PROTO.resolve()) and s["bom_path"] is None
        after = journal.read_bytes()
        assert after.startswith(before_bytes) and len(after) > len(before_bytes)       # adopted event appended, nothing rewritten
        appended = [json.loads(x) for x in after[len(before_bytes):].decode().splitlines() if x.strip()]
        assert [e["type"] for e in appended] == ["project_settings"] and appended[0]["payload"]["change"] == "adopted"
        after_tree = tree(legacy)
        assert set(after_tree) == set(before_tree) | {"polybench-project.json"}
        assert all(after_tree[k] == v for k, v in before_tree.items() if k != "L1/journal.jsonl")
        assert c.get("/api/state").json()["workspace"]["project"]["adopted"] is True
        assert cur(c).journal.problems == []


def test_open_legacy_with_bad_protocol_writes_no_settings(tmp_path):
    """PP-02: an adoption whose protocol does not load leaves no settings file behind (422)."""
    legacy = legacy_folder(tmp_path / "old")
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        r = c.post("/api/project/open", json={"dir": str(legacy), "protocol": str(tmp_path / "missing.pcbv.yaml")})
        assert r.status_code == 422
        assert not (legacy / "polybench-project.json").exists()
        assert cur(c) is sess


def test_open_batch_folder_points_to_parent_and_empty_is_refused(tmp_path):
    """PP-02: Open of a batch folder is refused (422) and names the parent; a folder without a project is refused too."""
    sess = new_project_session(tmp_path / "proj")
    (tmp_path / "empty").mkdir()
    with client(sess) as c:
        r = c.post("/api/project/open", json={"dir": str(tmp_path / "proj" / "B1")})
        assert r.status_code == 422 and "batch folder" in r.json()["error"] and str((tmp_path / "proj").resolve()) in r.json()["error"]
        r = c.post("/api/project/open", json={"dir": str(tmp_path / "empty")})
        assert r.status_code == 422 and "no Polybench project" in r.json()["error"]
        assert cur(c) is sess


def test_inspect_kinds(tmp_path):
    """PP-02: /api/project/inspect classifies folders for the New / Open forms."""
    sess = new_project_session(tmp_path / "proj")
    legacy = legacy_folder(tmp_path / "legacy")
    (tmp_path / "empty").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    with client(sess) as c:
        k = lambda p: c.get("/api/project/inspect", params={"path": str(p)}).json()
        assert k(tmp_path / "proj")["kind"] == "project"
        assert k(legacy)["kind"] == "legacy"
        assert k(legacy / "L1")["kind"] == "batch" and k(legacy / "L1")["parent"] == str(legacy.resolve())
        assert k(tmp_path / "empty")["kind"] == "empty"
        assert k(tmp_path / "other")["kind"] == "other"
        assert k(tmp_path / "f.txt")["kind"] == "file"
        m = k(tmp_path / "new" / "deep")
        assert m["kind"] == "missing" and m["creatable"] is True
        assert c.get("/api/project/inspect").json()["kind"] == "missing"


# ------------------------------------------------------------------------------------------------------------ PP-03
def test_workspace_paths_in_api(tmp_path):
    """PP-03: /api/workspace and /api/state.workspace carry project dir, batch dir, journal path and settings file."""
    sess = new_project_session(tmp_path / "proj", batch="B7")
    p = (tmp_path / "proj").resolve()
    with client(sess) as c:
        w = c.get("/api/workspace").json()
        assert w["project"]["dir"] == str(p)
        assert w["project"]["settings_file"] == str(p / "polybench-project.json") and w["project"]["settings_exists"] is True
        assert w["batch"]["dir"] == str(p / "B7") and w["batch"]["journal"] == str(p / "B7" / "journal.jsonl")
        assert w["protocol"]["path"] == str(DEMO_PROTO.resolve())
        assert [b["name"] for b in w["batches"]] == ["B7"]
        st = c.get("/api/state").json()["workspace"]
        assert st["project"] == w["project"] and st["batch"]["journal"] == w["batch"]["journal"]


def test_reveal_is_stubbed_and_confined(tmp_path):
    """PP-03: /api/reveal answers stubbed:true (no `open -R` in tests) and refuses paths outside project/batch/protocol/BOM (403)."""
    sess = new_project_session(tmp_path / "proj")
    p = (tmp_path / "proj").resolve()
    with client(sess) as c:
        for ok in (p, p / "polybench-project.json", p / "B1" / "journal.jsonl", DEMO_PROTO.resolve()):
            r = c.post("/api/reveal", json={"path": str(ok)})
            assert r.status_code == 200 and r.json()["stubbed"] is True, ok
        for bad in ("/etc/passwd", str(tmp_path), str(p) + "-sibling", "", str(Path.home())):
            r = c.post("/api/reveal", json={"path": bad})
            assert r.status_code == 403, bad


# ------------------------------------------------------------------------------------------------------------ PP-04 settings file
def test_settings_schema_fields(tmp_path):
    """PP-04: the settings file carries schema, name, ecad.program, ecad.document{name,id,fingerprint,elements}, bom_path,
    protocol_path, created, updated, current_batch."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        pr = c.get("/api/project/probe").json()
        assert c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]}).status_code == 200
    s = settings(tmp_path / "proj")
    for k in ("schema", "name", "ecad", "bom_path", "protocol_path", "created", "updated", "current_batch"):
        assert k in s, k
    assert s["schema"] == wslib.SCHEMA and s["name"] == "DEMO-1" and s["ecad"]["program"] == "fusion"
    doc = s["ecad"]["document"]
    assert {"name", "id", "fingerprint", "elements"} <= set(doc) and doc["fingerprint"] == pr["fingerprint"] and doc["elements"] > 0
    ev = events(sess, "project_settings")
    assert ev[-1]["payload"]["change"] == "ecad_document" and ev[-1]["payload"]["document"]["fingerprint"] == pr["fingerprint"]


def test_atomic_write_leaves_no_temp_and_survives_crash(tmp_path, monkeypatch):
    """PP-04: settings writes are atomic: no temp files left; a crash in os.replace leaves the old file intact."""
    sess = new_project_session(tmp_path / "proj")
    d = tmp_path / "proj"
    assert [x.name for x in d.iterdir() if x.name.startswith(".")] == []
    old = (d / "polybench-project.json").read_bytes()

    def boom(*a, **k):
        raise OSError("simulated crash mid-write")
    monkeypatch.setattr(wslib.os, "replace", boom)
    with pytest.raises(OSError):
        sess.workspace.save(name="changed")
    monkeypatch.undo()
    assert (d / "polybench-project.json").read_bytes() == old
    assert [x.name for x in d.iterdir() if x.name.startswith(".")] == []
    assert json.loads(old)["name"] == "DEMO-1"


def test_every_change_is_journalled(tmp_path):
    """PP-04: each settings change has a journal event in the current batch (project_settings, ecad_platform, bom_source)."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        assert events(sess, "project_settings")[-1]["payload"]["change"] == "adopted"
        pr = c.get("/api/project/probe").json()
        c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]})
        c.post("/api/ecad/platform", json={"program": "kicad"})
        c.post("/api/bom/load", json={"path": str(DEMO_PROTO.parent / "assembly.yaml")})
        c.post("/api/bom/remove", json={})
    types = [(e["type"], e["payload"].get("change") or e["payload"].get("action") or e["payload"].get("program"))
             for e in events(sess) if e["type"] in wslib_events()]
    assert types == [("project_settings", "adopted"), ("project_settings", "ecad_document"), ("ecad_platform", "kicad"),
                     ("bom_source", "replaced"), ("bom_source", "removed")]      # a file over the protocol's BOM is a replace
    with pytest.raises(ValueError):
        sess.engine.workspace_event("note", {})


def wslib_events():
    from polybench.engine import Engine
    return Engine.WORKSPACE_EVENTS


def test_protocol_and_bom_referenced_never_copied(tmp_path):
    """PP-04: the settings file references the protocol and BOM by path; neither is copied into the project folder. The only copy
    is the batch's pre-existing `protocol/` snapshot (Session._copy_protocol: every protocol file + HASH, written once when the
    batch is created, for the record of what the batch ran against)."""
    from pp_helpers import CSV_BOM
    sess = new_project_session(tmp_path / "proj")
    d = tmp_path / "proj"
    with client(sess) as c:
        assert c.post("/api/bom/load", json={"path": str(CSV_BOM)}).status_code == 200
    s = settings(d)
    assert s["protocol_path"] == str(DEMO_PROTO.resolve()) and s["bom_path"] == str(CSV_BOM.resolve())
    files = {f.relative_to(d).as_posix() for f in d.rglob("*") if f.is_file()}
    assert not any(f.endswith(".csv") for f in files)
    outside_snapshot = {f for f in files if not f.startswith("B1/protocol/")}
    assert outside_snapshot == {"polybench-project.json", "B1/journal.jsonl"}
    snap = {f.split("B1/protocol/", 1)[1] for f in files if f.startswith("B1/protocol/")}
    assert "demo.pcbv.yaml" in snap and "assembly.yaml" in snap and "HASH" in snap
