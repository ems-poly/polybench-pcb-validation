"""Project page second fix round (re-review N1-N9). DEMO data only."""
import json
import os
import shutil
from pathlib import Path

import pytest

from pp_helpers import CSV_BOM, DEMO_PROTO, args, client, cur, events, legacy_folder, new_project_session, protocol_copy, settings, start
from polybench import workspace as wslib


def _other(tmp_path):
    other = protocol_copy(tmp_path / "x")
    other.write_text(other.read_text().replace("name: DEMO-1", "name: OTHER-DESIGN", 1))
    return other


# ------------------------------------------------------------------------------------------------------------ N1
def test_cli_adopt_new_batch_name_checks_every_batch(tmp_path):
    """N1: a new --batch name used to give an empty check. Every batch of the folder must match the protocol's design."""
    legacy = legacy_folder(tmp_path / "old", batches=("L1", "L2"))
    other = _other(tmp_path)
    from polybench.cli import resolve_start
    with pytest.raises(SystemExit) as e:
        resolve_start(args(other, legacy, "BRAND-NEW"))
    assert "OTHER-DESIGN" in str(e.value) and "L1" in str(e.value) and not (legacy / wslib.SETTINGS_NAME).exists()
    ws, *_ = resolve_start(args(DEMO_PROTO, legacy, "BRAND-NEW"))
    assert ws.adopted


def test_cli_adopt_named_batch_in_mixed_folder(tmp_path):
    """N1: a named batch that matches does not hide a sibling batch recorded for another design (same message as POST open/adopt)."""
    from polybench.cli import resolve_start
    from polybench.protocol import load
    from polybench.server import Session
    other = _other(tmp_path)
    legacy = legacy_folder(tmp_path / "old", batches=("L1",))                       # DEMO-1
    s = Session(load(other), legacy, "O1", True, 3, None)                              # OTHER-DESIGN, same folder
    s.engine.entry("S01", "V01", "y")
    s.journal.close()
    with pytest.raises(SystemExit) as e:
        resolve_start(args(DEMO_PROTO, legacy, "L1"))                                # L1 matches DEMO-1, O1 does not
    msg = str(e.value)
    assert "O1" in msg and "OTHER-DESIGN" in msg and "Nothing was changed" in msg and not (legacy / wslib.SETTINGS_NAME).exists()
    sess = new_project_session(tmp_path / "p")
    with client(sess) as c:                                                           # the server gives the same message
        r = c.post("/api/project/open", json={"dir": str(legacy), "protocol": str(DEMO_PROTO)})
        assert r.status_code == 409 and r.json()["error"] == msg


# ------------------------------------------------------------------------------------------------------------ N2
def test_missing_path_is_judged_by_nearest_existing_ancestor(tmp_path):
    batch = legacy_folder(tmp_path / "old", batches=("L1",)) / "L1"
    proj = new_project_session(tmp_path / "proj")
    proj.journal.close()
    fresh = tmp_path / "parent"
    (fresh / "kid").mkdir(parents=True)
    (fresh / "kid" / wslib.SETTINGS_NAME).write_text(json.dumps({"schema": 1}))
    codes = lambda p: [w["code"] for w in wslib.inspect(p)["warnings"]]
    assert "batch_folder" in codes(batch / "new" / "deeper")                           # inside a batch, path not there yet
    assert "inside_batch" in codes(batch / "runs" / "x") or "batch_folder" in codes(batch / "runs" / "x")
    assert codes(fresh / "second") == []                                               # beside a project is normal
    assert "inside_project" in codes(fresh / "kid" / "nested")
    assert "home" in codes(Path.home() / "polybench-qa-n2-does-not-exist")
    (tmp_path / "empty").mkdir()
    assert codes(tmp_path / "empty" / "new") == []
    assert wslib.inspect(batch / "new")["kind"] == "missing"


# ------------------------------------------------------------------------------------------------------------ N3
def test_broken_bom_path_can_be_cleared_and_stays_cleared(tmp_path):
    f = tmp_path / "gone" / "demo_bom.csv"
    f.parent.mkdir()
    shutil.copy(CSV_BOM, f)
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        assert c.post("/api/bom/load", json={"path": str(f)}).status_code == 200
    sess.journal.close()
    f.unlink()
    s2 = start(args())
    assert s2.bom_error and s2.bom_source()["removable"] is True
    assert settings(tmp_path / "proj")["bom_path"] == str(f.resolve())
    with client(s2) as c:
        r = c.post("/api/bom/remove", json={})
        assert r.status_code == 200, r.text
        assert c.get("/api/state").json()["project_bom"]["error"] is None
    assert settings(tmp_path / "proj")["bom_path"] is None
    ev = [e for e in events(s2, "bom_source") if e["payload"]["action"] == "removed"]
    assert ev and ev[-1]["payload"]["previous_name"] == "demo_bom.csv"
    s2.journal.close()
    s3 = start(args())                                                                 # across a restart
    assert s3.bom_error is None and s3.bom_source()["source"] == "protocol" and settings(tmp_path / "proj")["bom_path"] is None


# ------------------------------------------------------------------------------------------------------------ N5
def test_latin1_csv_loads_and_a_decode_failure_is_plain(tmp_path, monkeypatch):
    from polybench import bomfile
    from polybench.assembly_scanner import bom as sb
    text = CSV_BOM.read_text(encoding="utf-8")
    lat = tmp_path / "lat.csv"
    lat.write_bytes((text + "\n").replace("Capacitor", "Capacitor \u00b5F caf\u00e9", 1).encode("cp1252"))
    lines, _ = bomfile.load(lat)
    assert len(lines) == len(bomfile.load(CSV_BOM)[0])
    sig = tmp_path / "sig.csv"
    sig.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert len(bomfile.load(sig)[0]) == len(lines)
    monkeypatch.setattr(sb.Path, "read_bytes", lambda self: b"\x81\x8d\x8f\x90\x9d", raising=False)   # undecodable in cp1252; latin-1 would still decode
    monkeypatch.setattr(sb, "_CODECS", ("utf-8", "utf-8-sig", "cp1252"), raising=False)
    try:
        bomfile.load(lat)
    except bomfile.BomFileError as e:
        assert "Traceback" not in str(e) and "codec" not in str(e)


# ------------------------------------------------------------------------------------------------------------ N6
def test_corrupt_backup_once_per_distinct_content(tmp_path):
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    f = tmp_path / "proj" / wslib.SETTINGS_NAME
    f.write_text('{"name": "half', encoding="utf-8")
    for _ in range(3):
        with pytest.raises(SystemExit):
            start(args())
    glob = lambda: sorted((tmp_path / "proj").glob(wslib.SETTINGS_NAME + ".corrupt-*"))
    assert len(glob()) == 1
    f.write_text('{"name": "other half', encoding="utf-8")
    with pytest.raises(SystemExit):
        start(args())
    assert len(glob()) == 2


# ------------------------------------------------------------------------------------------------------------ N7
def test_protocol_design_endpoint_for_the_new_project_hint(tmp_path):
    sess = new_project_session(tmp_path / "proj")
    other = _other(tmp_path)
    with client(sess) as c:
        assert c.get("/api/project/protocol_design", params={"path": str(other)}).json() == {"ok": True, "design": "OTHER-DESIGN", "problem": None}
        assert c.get("/api/project/protocol_design", params={"path": str(DEMO_PROTO)}).json()["design"] == "DEMO-1"
        r = c.get("/api/project/protocol_design", params={"path": str(tmp_path / "nope.yaml")}).json()
        assert r["ok"] is False and r["design"] is None


# ------------------------------------------------------------------------------------------------------------ N8
def test_recent_marks_missing_folders_and_removes_them(tmp_path):
    sess = new_project_session(tmp_path / "proj")
    gone = new_project_session(tmp_path / "gone")
    gone.journal.close()
    shutil.rmtree(tmp_path / "gone")
    with client(sess) as c:
        rec = c.get("/api/state").json()["workspace"]["recent"]
        by = {Path(r["dir"]).name: r for r in rec}
        assert by["gone"]["missing"] is True and by["proj"]["missing"] is False
        r = c.post("/api/recent/remove", json={"dir": by["gone"]["dir"]})
        assert r.status_code == 200 and all(Path(x["dir"]).name != "gone" for x in r.json()["recent"])
        assert all(Path(x["dir"]).name != "gone" for x in wslib.recent_list())


# ------------------------------------------------------------------------------------------------------------ N9
def test_history_says_restored_when_the_switch_restores_the_document(tmp_path):
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        pr = c.get("/api/project/probe").json()
        assert c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]}).status_code == 200
        c.post("/api/ecad/platform", json={"program": "kicad"})
        h1 = [x["what"] for x in c.get("/api/state").json()["project"]["history"]]
        assert any("cleared, confirm the document again" in w for w in h1)               # away: nothing to restore
        pr = c.get("/api/project/probe").json()                                         # a KiCad document, cleared on the way back
        assert c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]}).status_code == 200
        r = c.post("/api/ecad/platform", json={"program": "fusion"})
        assert r.json()["restored"] and r.json()["cleared"]
        h2 = [x["what"] for x in c.get("/api/state").json()["project"]["history"]]
        sw = [w for w in h2 if w.startswith("ECAD platform kicad -> fusion")]
        assert sw and "cleared; restored " in sw[0] and "confirm the document again" not in sw[0]
        assert any("cleared, confirm the document again" in w for w in h2 if "fusion -> kicad" in w)
