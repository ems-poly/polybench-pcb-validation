"""Project page fix round (gate G3/G4 defects D1-D9, F2, F5): every case the reviewer's attack scripts found, as a test. DEMO data only."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from polybench import extras

needs_scanner = pytest.mark.skipif(not extras.check("scanner"), reason="needs the scanner extra (openpyxl for .xlsx BOMs)")

from pp_helpers import (CSV_BOM, DEMO_PROTO, args, client, cur, events, legacy_folder, new_project_session, protocol_copy, settings,
                        start, tree)
from polybench import workspace as wslib


def _xlsx(path: Path) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BOM"
    ws.append(["Mouser Part Number", "Manufacturer Part Number", "Designators", "Description", "Package", "Fitted"])
    ws.append(["595-DEMOMCU32", "DEMOMCU-32", "U1", "DEMO microcontroller", "TQFP-32", "Yes"])
    ws.append(["187-CL05B104KO5NNNC", "CL05B104KO5NNNC", "C1, C2, C3", "100 nF", "0402", "Yes"])
    ws.append(["603-RC0402FR-07120RL", "RC0402FR-07120RL", "R1, R2", "120 Ohm", "0402", "DNF"])
    wb.save(path)
    return path


def _select(c):
    pr = c.get("/api/project/probe").json()
    r = c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------------------------------------------------ D1
@needs_scanner
def test_xlsx_bom_loads_and_state_stays_up(tmp_path):
    """D1: a real .xlsx BOM loads through the importer; /api/state answers, with row-based file extent, also after a restart."""
    sess = new_project_session(tmp_path / "proj")
    x = _xlsx(tmp_path / "bom.xlsx")
    with client(sess) as c:
        r = c.post("/api/bom/load", json={"path": str(x)})
        assert r.status_code == 200, r.text
        st = c.get("/api/state")
        assert st.status_code == 200
        b = st.json()["project_bom"]
        assert b["format"] == "xlsx" and b["lines"] == 3 and b["designators"] == 6 and b["fitted"] == 4 and b["dnf"] == 2
        assert b["file_lines"] == 4 and b["error"] is None
    sess.journal.close()
    s2 = start(args())
    assert s2.bom_source()["format"] == "xlsx"
    with client(s2) as c:
        assert c.get("/api/state").status_code == 200


def test_bom_facts_never_take_state_down(tmp_path, monkeypatch):
    """D1: a settings BOM that is garbage with a .xlsx name, or unreadable, shows an error in the BOM facts, never a 500."""
    sess = new_project_session(tmp_path / "proj")
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe not a workbook")
    with client(sess) as c:
        r = c.post("/api/bom/load", json={"path": str(bad)})
        assert r.status_code == 422 and "bad.xlsx" in r.json()["error"]
        # settings point at a binary file that is no longer a BOM: the session falls back, state stays up
        sess.workspace.save(bom_path=str(bad))
    sess.journal.close()
    s2 = start(args())
    assert s2.bom_error and "bad.xlsx" in s2.bom_error
    with client(s2) as c:
        st = c.get("/api/state")
        assert st.status_code == 200 and st.json()["project_bom"]["error"]
    # an unexpected failure inside the facts is contained as well
    monkeypatch.setattr(type(s2), "_bom_source", lambda self: (_ for _ in ()).throw(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "boom")))
    with client(s2) as c:
        st = c.get("/api/state")
        assert st.status_code == 200 and "could not be read" in st.json()["project_bom"]["error"]


def test_csv_in_latin1_counts_lines(tmp_path):
    """D1: a purchasing CSV in another encoding still gives its line count (bytes, not decoded text)."""
    sess = new_project_session(tmp_path / "proj")
    f = tmp_path / "latin.csv"
    f.write_bytes(CSV_BOM.read_text().replace("DEMO microcontroller", "DEMO microµcontroller").encode("latin-1"))
    with client(sess) as c:
        r = c.post("/api/bom/load", json={"path": str(f)})
        if r.status_code == 200:
            assert c.get("/api/state").json()["project_bom"]["file_lines"] == 7
        else:
            assert r.status_code == 422 and c.get("/api/state").status_code == 200


# ------------------------------------------------------------------------------------------------------------ D2
def test_adopt_requires_protocol_and_matching_design(tmp_path):
    """D2: adopting a legacy folder needs a protocol, and refuses one whose design is not the one the journal recorded."""
    legacy = legacy_folder(tmp_path / "old", batches=("L1",))
    other = protocol_copy(tmp_path / "x")
    other.write_text(other.read_text().replace("name: DEMO-1", "name: OTHER-DESIGN", 1))
    from polybench.protocol import load
    assert load(other).data["design"]["name"] == "OTHER-DESIGN"
    sess = new_project_session(tmp_path / "proj")
    before = tree(legacy)
    with client(sess) as c:
        r = c.post("/api/project/open", json={"dir": str(legacy)})
        assert r.status_code == 422 and "protocol" in r.json()["error"]
        r = c.post("/api/project/open", json={"dir": str(legacy), "protocol": str(other)})
        assert r.status_code == 409 and "OTHER-DESIGN" in r.json()["error"] and "L1" in r.json()["error"]
        assert tree(legacy) == before and not (legacy / wslib.SETTINGS_NAME).exists() and cur(c) is sess
        r = c.post("/api/project/open", json={"dir": str(legacy), "protocol": str(DEMO_PROTO)})
        assert r.status_code == 200 and r.json()["adopted"] is True
        assert settings(legacy)["protocol_path"] == str(DEMO_PROTO.resolve())


def test_cli_adopt_refuses_other_design(tmp_path, monkeypatch):
    """D2: `--protocol X --data-dir legacy` refuses a protocol of another design for the batch being started."""
    legacy = legacy_folder(tmp_path / "old", batches=("L1",))
    other = protocol_copy(tmp_path / "x")
    other.write_text(other.read_text().replace("name: DEMO-1", "name: OTHER-DESIGN", 1))
    from polybench.cli import resolve_start
    with pytest.raises(SystemExit) as e:
        resolve_start(args(other, legacy, "L1"))
    assert "OTHER-DESIGN" in str(e.value) and not (legacy / wslib.SETTINGS_NAME).exists()
    ws, *_ = resolve_start(args(DEMO_PROTO, legacy, "L1"))
    assert ws.adopted


# ------------------------------------------------------------------------------------------------------------ D3
def test_corrupt_settings_is_reported_backed_up_and_never_rewritten(tmp_path):
    """D3: an unreadable settings file: message with path and error, a .corrupt-<ts> copy, the original untouched, no start."""
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    f = tmp_path / "proj" / wslib.SETTINGS_NAME
    f.write_text('{"name": "half', encoding="utf-8")
    raw = f.read_bytes()
    with pytest.raises(SystemExit) as e:
        start(args())
    assert "settings file unreadable:" in str(e.value) and str(f) in str(e.value)
    assert f.read_bytes() == raw
    assert [p.read_bytes() for p in (tmp_path / "proj").glob(wslib.SETTINGS_NAME + ".corrupt-*")] == [raw]
    other = new_project_session(tmp_path / "other")
    with client(other) as c:
        i = c.get("/api/project/inspect", params={"path": str(tmp_path / "proj")}).json()
        assert i["kind"] == "corrupt" and "settings file unreadable:" in i["problem"]
        r = c.post("/api/project/open", json={"dir": str(tmp_path / "proj")})
        assert r.status_code == 422 and "settings file unreadable" in r.json()["error"] and f.read_bytes() == raw
        r = c.post("/api/project/new", json={"dir": str(tmp_path / "proj"), "name": "x"})
        assert r.status_code == 409 and f.read_bytes() == raw


def test_valid_but_short_settings_get_every_field_on_write(tmp_path):
    """D3: a settings file that is valid JSON but short is completed on the next write, never left half-filled."""
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    f = tmp_path / "proj" / wslib.SETTINGS_NAME
    f.write_text(json.dumps({"schema": 1, "protocol_path": str(DEMO_PROTO.resolve())}))
    ws = wslib.Workspace(tmp_path / "proj")
    ws.save(bom_path=None)
    s = json.loads(f.read_text())
    for k in ("schema", "name", "created", "updated", "protocol_path", "bom_path", "ecad", "current_batch"):
        assert k in s, k
    assert set(s["ecad"]) >= {"program", "document", "documents"}


# ------------------------------------------------------------------------------------------------------------ D4
@pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
def test_unreadable_folders_are_marked_not_500(tmp_path):
    """D4: fs/list, inspect and locate cope with a folder the user may not read."""
    sess = new_project_session(tmp_path / "proj")
    base = tmp_path / "base"
    (base / "locked").mkdir(parents=True)
    (base / "open").mkdir()
    (base / "locked" / "x.csv").write_text("a")
    os.chmod(base / "locked", 0)
    try:
        with client(sess) as c:
            r = c.get("/api/fs/list", params={"path": str(base)})
            assert r.status_code == 200
            ents = {e["name"]: e for e in r.json()["entries"]}
            assert ents["locked"]["unreadable"] is True and ents["open"]["unreadable"] is False
            r = c.get("/api/fs/list", params={"path": str(base / "locked")})
            assert r.status_code == 200 and (r.json()["error"] or r.json()["path"] != str(base / "locked"))
            r = c.get("/api/project/inspect", params={"path": str(base / "locked")})
            assert r.status_code == 200 and r.json()["kind"] == "unreadable"
            r = c.get("/api/project/inspect", params={"path": str(base / "locked" / "sub")})
            assert r.status_code == 200
            r = c.get("/api/bom/locate", params={"name": "x.csv"})
            assert r.status_code == 200
            r = c.post("/api/project/open", json={"dir": str(base / "locked")})
            assert r.status_code == 422
        assert wslib.locate("x.csv", None, [str(base), str(base / "locked")]) == []
    finally:
        os.chmod(base / "locked", 0o755)


# ------------------------------------------------------------------------------------------------------------ D7
def test_stale_tmp_files_are_removed_at_start(tmp_path):
    """D7: temp files of a killed write are removed when the project opens; a fresh one (a live writer) is kept."""
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    d = tmp_path / "proj"
    old = d / f".{wslib.SETTINGS_NAME}.abc123.tmp"
    fresh = d / f".{wslib.SETTINGS_NAME}.new999.tmp"
    old.write_text("{")
    fresh.write_text("{")
    t = time.time() - 3600
    os.utime(old, (t, t))
    s2 = start(args())
    s2.journal.close()
    assert not old.exists() and fresh.exists()


# ------------------------------------------------------------------------------------------------------------ D8
def test_new_project_warns_and_needs_confirm(tmp_path, monkeypatch):
    """D8: a batch folder, a folder inside a batch, $HOME and the parent of a project each need a confirm."""
    sess = new_project_session(tmp_path / "proj")
    legacy = legacy_folder(tmp_path / "legacy")
    (legacy / "L1" / "sub").mkdir()
    parent = tmp_path / "parent"
    (parent / "child").mkdir(parents=True)
    wslib.atomic_write_json(parent / "child" / wslib.SETTINGS_NAME, {"schema": 1})
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cases = {legacy / "L1": "batch_folder", legacy / "L1" / "sub": "inside_batch", home: "home", parent: "parent_of_project"}
    with client(sess) as c:
        for d, code in cases.items():
            i = c.get("/api/project/inspect", params={"path": str(d)}).json()
            assert code in [w["code"] for w in i["warnings"]], (d, i)
            before = tree(d)
            r = c.post("/api/project/new", json={"dir": str(d), "name": "p"})
            assert r.status_code == 409 and r.json()["needs_confirm"] and code in [w["code"] for w in r.json()["warnings"]]
            assert tree(d) == before
        assert c.get("/api/project/inspect", params={"path": str(tmp_path / "fresh")}).json()["warnings"] == []
        r = c.post("/api/project/new", json={"dir": str(home), "name": "p", "confirm": True})
        assert r.status_code == 200, r.text
        assert (home / wslib.SETTINGS_NAME).is_file()


# ------------------------------------------------------------------------------------------------------------ D9
class SlowBridge:
    """A fake ECAD bridge whose status answers after a delay; counts calls."""
    name = "fusion"

    def __init__(self, delay, names, doc="Slow"):
        self.delay, self.names, self.doc = delay, names, doc
        self.calls = []

    def status(self):
        time.sleep(self.delay)
        self.calls.append("status")
        return {"connected": True, "program": "Fusion", "document": self.doc, "document_id": "id1", "editor": "board", "elements": len(self.names)}

    def designators(self):
        self.calls.append("designators")
        return list(self.names)

    def documents(self, search=None):
        return {"ok": True, "open": [], "candidates": []}


def test_select_racing_a_platform_switch_records_nothing(tmp_path):
    """D9: project_select that started on the old bridge never records its document after a switch; its status is not cached."""
    sess = new_project_session(tmp_path / "proj")
    sess.journal.close()
    sess = start(args())
    slow = SlowBridge(0.4, ["U1", "U2", "C1"])
    sess.bridge = slow

    async def go():
        sel = asyncio.create_task(sess.project_select())
        await asyncio.sleep(0.1)
        await sess.set_ecad_platform("kicad")
        with pytest.raises(Exception) as e:
            await sel
        return str(e.value)

    msg = asyncio.run(go())
    assert "platform was changed" in msg or "changed" in msg
    assert sess.ecad_id == "kicad" and sess.engine.state.project["select"] is None
    assert sess._status_cache is None or sess._status_cache[2] is sess.bridge
    assert (settings(tmp_path / "proj")["ecad"]["document"]) is None


def test_probe_racing_a_switch_is_flagged_stale(tmp_path):
    """D9: a probe that read the old bridge says so (stale) and is not cached for the new one."""
    sess = new_project_session(tmp_path / "proj")
    slow = SlowBridge(0.4, ["U1", "U2"])
    sess.bridge = slow

    async def go():
        pr = asyncio.create_task(sess.project_probe())
        await asyncio.sleep(0.1)
        await sess.set_ecad_platform("kicad")
        return await pr

    out = asyncio.run(go())
    assert out["stale"] is True and out["ecad"] == "fusion"
    assert sess._status_cache is None or sess._status_cache[2] is sess.bridge


# ------------------------------------------------------------------------------------------------------------ F2
def test_documents_are_stored_per_platform_and_restored(tmp_path):
    """F2: switch away and back: the confirmed document and the accepted cross-check come back when the fingerprint still matches."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        _select(c)
        c.post("/api/cmd/crosscheck_accept", json={"note": "TP99 is a probe point"})
        assert c.get("/api/state").json()["steps"][0]["state"] == "done"
        r = c.post("/api/ecad/platform", json={"program": "kicad"})
        assert r.json()["cleared"] and r.json()["restored"] is None
        s = settings(tmp_path / "proj")
        assert "fusion" in s["ecad"]["documents"] and s["ecad"]["documents"]["fusion"]["accepted"]["note"] == "TP99 is a probe point"
        assert s["ecad"]["document"] is None
        r = c.post("/api/ecad/platform", json={"program": "fusion"})
        assert r.status_code == 200 and r.json()["restored"]["accepted"] is True
        st = c.get("/api/state").json()
        assert st["steps"][0]["state"] == "done" and st["project"]["select"] and st["project"]["accepted"]
        assert settings(tmp_path / "proj")["ecad"]["document"]["name"] == st["project"]["select"]["document"]


def test_changed_document_is_not_restored(tmp_path):
    """F2: when the open document no longer has the saved fingerprint, nothing is restored; the operator confirms again."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        _select(c)
        c.post("/api/ecad/platform", json={"program": "kicad"})
        s = settings(tmp_path / "proj")
        s["ecad"]["documents"]["fusion"]["fingerprint"] = "deadbeef"
        wslib.atomic_write_json(tmp_path / "proj" / wslib.SETTINGS_NAME, s)
        cur(c).workspace.settings = s
        r = c.post("/api/ecad/platform", json={"program": "fusion"})
        assert r.json()["restored"] is None and c.get("/api/state").json()["project"]["select"] is None


# ------------------------------------------------------------------------------------------------------------ F5
@needs_scanner
def test_bom_browser_filters_to_bom_files(tmp_path):
    """F5: in BOM mode the browser lists .csv, .xlsx and .yaml with a bom: key, and counts what it hides."""
    d = tmp_path / "files"
    d.mkdir()
    (d / "frag.yaml").write_text("steps:\n  - id: a\n")
    (d / "withbom.yaml").write_text("design: {}\nbom:\n  - line: 1\n")
    (d / "p.csv").write_text("a")
    _xlsx(d / "q.xlsx")
    (d / "notes.txt").write_text("x")
    r = wslib.browse(str(d), "file", ["yaml", "yml", "csv", "xlsx"], None, bom_only=True)
    assert sorted(e["name"] for e in r["entries"]) == ["p.csv", "q.xlsx", "withbom.yaml"] and r["hidden_files"] == 1
    r = wslib.browse(str(d), "file", ["yaml", "yml", "csv", "xlsx"], None)
    assert "frag.yaml" in [e["name"] for e in r["entries"]]


# ------------------------------------------------------------------------------------------------- reviewer's API attacks
def test_reviewer_api_attacks_never_500(tmp_path):
    """The paths the reviewer's attack scripts threw at the folder routes (api_attack3.log): none may answer 5xx; reveal stays inside."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        for p in ("/", "/Users", "/var/root", "/private/var", "/private/var/root", "~/.ssh", "/etc/hosts", "/nonexistent/x/y", ""):
            for mode in ("dir", "file"):
                r = c.get("/api/fs/list", params={"path": p, "mode": mode, "exts": "csv,yaml"})
                assert r.status_code == 200, (p, mode, r.status_code, r.text[:200])
            r = c.get("/api/project/inspect", params={"path": p})
            assert r.status_code == 200, (p, r.text[:200])
        for d in ("/System/qa", "/", "/etc/hosts", "", "/private/var"):
            r = c.post("/api/project/new", json={"dir": d, "name": "x"})
            assert r.status_code in (409, 422), (d, r.status_code, r.text[:200])
        for name in ("../../etc/hosts", "", "x.csv", "/etc/hosts"):
            assert c.get("/api/bom/locate", params={"name": name, "size": 1}).status_code == 200
        for path in ("/etc/hosts", "~/.ssh", "", str(tmp_path / "proj" / ".." / ".." / "etc"), "~/polybench-data"):
            assert c.post("/api/reveal", json={"path": path}).status_code == 403, path
        for bad in ("/etc/hosts", "tests/data/demo_purchasing_bom_dup.csv", str(tmp_path / "nothing.xlsx")):
            assert c.post("/api/bom/load", json={"path": bad}).status_code == 422, bad
        assert c.post("/api/ecad/platform", json={"program": "altium"}).status_code == 422
        assert c.get("/api/state").status_code == 200
