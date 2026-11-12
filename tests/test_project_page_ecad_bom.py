"""Project page PP-06 ECAD platform, PP-09 BOM file, PP-10 read-only / no subprocess, and the folder browser + BOM locate routes."""
import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from pp_helpers import (CSV_BOM, CSV_BOM_DUP, DEMO_PROTO, args, client, cur, events, new_project_session, protocol_copy, settings,
                        start)
from polybench import workspace as wslib

FUSION_URN = "urn:adsk.wipprod:dm.lineage:DEMO-stale"
DEMO_NAMES = ["U1", "U2", "U3", "C1", "C2", "C3", "C4", "C5", "C6", "R3", "R4", "R5", "D1", "C7", "J1", "J2"]


@pytest.fixture
def no_network(monkeypatch):
    """No socket may connect anywhere during these tests (the simulated bridges and the TestClient need none)."""
    def refuse(*a, **k):
        raise AssertionError(f"network use in a Project page test: {a}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def ecad_spy(monkeypatch):
    """Counts every ECAD write (highlight / clear) on both simulated bridges."""
    from polybench.ecad_sim import SimulatedEcad
    from polybench.kicad.bridge import KicadBridge
    calls = []
    for cls in (SimulatedEcad, KicadBridge):
        for m in ("highlight", "clear"):
            orig = getattr(cls, m)

            def spy(self, *a, _o=orig, _n=f"{cls.__name__}.{m}", **k):
                calls.append(_n)
                return _o(self, *a, **k)
            monkeypatch.setattr(cls, m, spy)
    return calls


@pytest.fixture
def popen_spy(monkeypatch):
    """Records every subprocess.Popen; none is started."""
    calls = []

    class Rec:
        def __init__(self, a, *x, **k):
            calls.append([str(v) for v in a] if isinstance(a, (list, tuple)) else [str(a)])
    monkeypatch.setattr(subprocess, "Popen", Rec)
    return calls


def _select_document(c):
    pr = c.get("/api/project/probe").json()
    r = c.post("/api/cmd/project_select", json={"fingerprint": pr["fingerprint"]})
    assert r.status_code == 200, r.text
    return pr


def _record_fusion_selection(sess, doc=FUSION_URN):
    """A step-1 selection made with the real Fusion bridge (not simulated), as an older batch would have in its journal."""
    sess.engine.select_project({"bridge": "fusion", "program": "Fusion", "document": doc, "document_id": doc, "editor": "board",
                                "elements": len(DEMO_NAMES), "simulated": False}, DEMO_NAMES)


# ------------------------------------------------------------------------------------------------------------ PP-06
def test_switch_fusion_to_kicad_clears_selection_saves_and_journals(tmp_path, no_network):
    """PP-06: fusion -> kicad in a --simulate session swaps the bridge, clears the recorded document (step not done), saves the
    setting and journals ecad_platform with cleared_document."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        _select_document(c)
        c.post("/api/cmd/crosscheck_accept", json={"note": "TP99 is a probe point"})
        assert c.get("/api/state").json()["steps"][0]["state"] == "done"
        doc = sess.engine.state.project["select"]["document"]
        r = c.post("/api/ecad/platform", json={"program": "kicad"})
        assert r.status_code == 200 and r.json()["changed"] is True and r.json()["cleared"] == {"document": doc, "program": "fusion"}
        assert sess.bridge.name == "kicad" and sess.ecad_id == "kicad"
        assert sess.engine.state.project["select"] is None
        st = c.get("/api/state").json()
        assert st["steps"][0]["state"] != "done" and st["workspace"]["ecad"]["program"] == "kicad"
        s = settings(tmp_path / "proj")
        assert s["ecad"]["program"] == "kicad" and s["ecad"]["document"] is None
        ev = events(sess, "ecad_platform")[-1]["payload"]
        assert ev["program"] == "kicad" and ev["previous"] == "fusion" and ev["cleared_document"] == {"document": doc, "program": "fusion"}
        # the same platform again is a no-op: nothing journalled
        n = len(events(sess, "ecad_platform"))
        assert c.post("/api/ecad/platform", json={"program": "kicad"}).json()["changed"] is False
        assert len(events(sess, "ecad_platform")) == n
        # and back
        r = c.post("/api/ecad/platform", json={"program": "fusion"})
        assert r.status_code == 200 and r.json()["cleared"] is None and sess.ecad_id == "fusion" and sess.bridge.name == "simulated"
        assert settings(tmp_path / "proj")["ecad"]["program"] == "fusion"
    sess.journal.close()
    s2 = start(args())                                       # replay: cleared on the switch away, restored (F2) on the way back
    assert s2.engine.state.project["select"]["document"] == doc and s2.bridge.name == "simulated"


def test_kicad_expected_document_is_reset_on_switch(tmp_path, monkeypatch, no_network):
    """PP-06: the KiCad bridge never carries a stale `urn:adsk...` expected document across a platform switch."""
    sess = new_project_session(tmp_path / "proj")
    _record_fusion_selection(sess)
    monkeypatch.setenv("POLYBENCH_KICAD_DOC", FUSION_URN)          # what a shared link left over from before would hold
    with client(sess) as c:
        r = c.post("/api/ecad/platform", json={"program": "kicad"})
        assert r.json()["cleared"] == {"document": FUSION_URN, "program": "fusion"}
        assert sess.bridge.link.expected_document is None


def test_stale_selection_after_cli_override(tmp_path, no_network):
    """PP-06: a selection made with Fusion is reported stale when the run is started with --ecad kicad, and is not used as the
    KiCad expected document; switching the saved platform clears it."""
    sess = new_project_session(tmp_path / "proj")
    _record_fusion_selection(sess)
    assert sess.stale_selection() is None
    sess.journal.close()
    s2 = start(args(DEMO_PROTO, tmp_path / "proj", None, ecad="kicad"))
    assert s2.bridge.name == "kicad"
    assert s2.stale_selection() == {"document": FUSION_URN, "program": "fusion"}
    assert s2.bridge.link.expected_document is None
    with client(s2) as c:
        assert c.get("/api/workspace").json()["ecad"]["stale_selection"] == {"document": FUSION_URN, "program": "fusion"}
        r = c.post("/api/ecad/platform", json={"program": "kicad"})      # running kicad, saved fusion: save it and clear
        assert r.status_code == 200 and r.json()["changed"] is True
        assert s2.stale_selection() is None and s2.workspace.overrides == {}
        assert settings(tmp_path / "proj")["ecad"]["program"] == "kicad"


def test_seed_document_on_adoption_same_platform_only(tmp_path):
    """PP-04/PP-06: adopting a legacy batch seeds the settings' ECAD document from its step-1 selection, only for the same platform."""
    from polybench.protocol import load
    from polybench.server import Session
    for plat, expect in (("fusion", FUSION_URN), ("kicad", None)):
        root = tmp_path / plat
        s = Session(load(DEMO_PROTO), root, "L1", True, 3, None)
        _record_fusion_selection(s)
        s.journal.close()
        s2 = start(args(DEMO_PROTO, root, "L1", ecad=plat))
        doc = settings(root)["ecad"]["document"]
        assert (doc or {}).get("name") == expect, plat
        s2.journal.close()


def test_unknown_platform_and_ecad_request(tmp_path, no_network):
    """PP-06: unknown platform -> 422; /api/ecad/request appends to <config>/ecad_requests.jsonl, empty text -> 422."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        for bad in ("altium", "", None):
            assert c.post("/api/ecad/platform", json={"program": bad}).status_code == 422
        assert sess.bridge.name == "simulated" and not events(sess, "ecad_platform")
        f = wslib.config_dir() / "ecad_requests.jsonl"
        assert str(f).startswith(str(tmp_path.parent)) or "pbenv" in str(f)
        r = c.post("/api/ecad/request", json={"text": "  Altium   Designer "})
        assert r.status_code == 200 and r.json()["request"]["text"] == "Altium Designer"
        c.post("/api/ecad/request", json={"text": "EasyEDA"})
        lines = [json.loads(x) for x in f.read_text().splitlines()]
        assert [x["text"] for x in lines] == ["Altium Designer", "EasyEDA"] and lines[0]["project"] == str((tmp_path / "proj").resolve())
        for bad in ("", "   ", None):
            assert c.post("/api/ecad/request", json={"text": bad}).status_code == 422
        assert len(f.read_text().splitlines()) == 2
        assert [x["text"] for x in c.get("/api/workspace").json()["ecad"]["requests"]] == ["Altium Designer", "EasyEDA"]


# ------------------------------------------------------------------------------------------------------------ PP-09 BOM file
def test_load_yaml_bom_fragment_and_modified_copy(tmp_path):
    """PP-09: a YAML fragment with a `bom:` list loads (assembly.yaml, then a modified copy replaces it)."""
    sess = new_project_session(tmp_path / "proj")
    asm = DEMO_PROTO.parent / "assembly.yaml"
    mod = tmp_path / "bomsrc" / "assembly_v2.yaml"
    mod.parent.mkdir()
    text = asm.read_text().replace('mpn: "DEMOADC-16"', 'mpn: "DEMOADC-16B"')
    text = "\n".join(x for x in text.splitlines() if "designators: [J1, J2]" not in x) + "\n"
    mod.write_text(text)
    with client(sess) as c:
        r = c.post("/api/bom/load", json={"path": str(asm)})
        assert r.status_code == 200, r.text
        b = r.json()["bom"]
        assert b["source"] == "settings" and b["lines"] == 9 and b["removable"] is True and b["format"] == "yaml"
        r = c.post("/api/bom/load", json={"path": str(mod)})
        b = r.json()["bom"]
        assert r.status_code == 200 and b["lines"] == 8 and b["name"] == "assembly_v2.yaml"
        assert any(x["mpn"] == "DEMOADC-16B" for x in sess.proto.data["bom"])
        assert settings(tmp_path / "proj")["bom_path"] == str(mod.resolve())
        st = c.get("/api/state").json()
        assert st["project_bom"]["name"] == "assembly_v2.yaml"
        ev = events(sess, "bom_source")[-1]["payload"]
        assert ev["action"] == "replaced" and ev["previous"] == str(asm) and ev["lines"] == 8 and ev["format"] == "yaml"


def test_load_csv_purchasing_bom(tmp_path):
    """PP-09: a CSV purchasing BOM (Mouser P/N, MPN, Designators, Description, Package, Fitted) loads; DNF rows are dnf."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        r = c.post("/api/bom/load", json={"path": str(CSV_BOM)})
        assert r.status_code == 200, r.text
        b = r.json()["bom"]
        assert b["source"] == "settings" and b["lines"] == 6 and b["format"] == "csv" and b["dnf"] == 2
        assert b["designators"] == 10 and b["fitted"] == 8
        line = next(x for x in sess.proto.data["bom"] if x["designators"] == ["R1", "R2"])
        assert line["dnf"] is True and "603-RC0402FR-07120RL" in line["barcode"]


def test_load_into_protocol_without_bom_is_loaded(tmp_path):
    """PP-09: a protocol without a `bom:` block shows no BOM; a file then loads ('loaded', not 'replaced'), and Remove returns to none."""
    proto = protocol_copy(tmp_path, drop_bom=True)
    sess = new_project_session(tmp_path / "proj", protocol=proto)
    with client(sess) as c:
        assert c.get("/api/state").json()["project_bom"]["source"] == "none"
        assert c.post("/api/bom/remove", json={}).status_code == 409
        assert c.post("/api/bom/load", json={"path": str(CSV_BOM)}).status_code == 200
        assert events(sess, "bom_source")[-1]["payload"]["action"] == "loaded"
        r = c.post("/api/bom/remove", json={})
        assert r.status_code == 200 and r.json()["bom"]["source"] == "none" and settings(tmp_path / "proj")["bom_path"] is None


@pytest.mark.parametrize("name,body", [("notes.txt", "U1,R1\n"), ("board.kicad_pcb", "(kicad_pcb (version 20240108))\n"),
                                       ("noext", "x\n")])
def test_unsupported_extension_is_422(tmp_path, name, body):
    """PP-09: a file that is not .yaml/.yml/.csv/.xlsx is refused (422) and nothing changes."""
    sess = new_project_session(tmp_path / "proj")
    f = tmp_path / name
    f.write_text(body)
    with client(sess) as c:
        r = c.post("/api/bom/load", json={"path": str(f)})
        assert r.status_code == 422 and "not a BOM format" in r.json()["error"]
        assert settings(tmp_path / "proj")["bom_path"] is None and not events(sess, "bom_source")
        assert c.post("/api/bom/load", json={"path": str(tmp_path / "missing.csv")}).status_code == 422
        assert c.post("/api/bom/load", json={"path": ""}).status_code == 422


def test_duplicate_designators_and_bad_yaml_are_422(tmp_path):
    """PP-09: a BOM with a designator on two lines is refused (CSV and YAML); a YAML without `bom:` too."""
    sess = new_project_session(tmp_path / "proj")
    dup_yaml = tmp_path / "dup.yaml"
    dup_yaml.write_text("bom:\n  - {line: 1, designators: [U1], mpn: A}\n  - {line: 2, designators: [U1, R1], mpn: B}\n")
    nobom = tmp_path / "nobom.yaml"
    nobom.write_text("steps: []\n")
    with client(sess) as c:
        for f, word in ((CSV_BOM_DUP, "U1"), (dup_yaml, "U1"), (nobom, "bom")):
            r = c.post("/api/bom/load", json={"path": str(f)})
            assert r.status_code == 422 and word in r.json()["error"], (f, r.text)
        assert settings(tmp_path / "proj")["bom_path"] is None and sess.bom_source()["source"] == "protocol"


def test_bom_change_with_assembly_events_needs_confirm_and_keeps_them(tmp_path):
    """PP-09: with placements recorded, load / remove without confirm -> 409 needs_confirm; with confirm -> ok, placements kept."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        assert c.post("/api/cmd/placed", json={"board": "S01", "designators": ["U1"], "on": True}).status_code == 200
        r = c.post("/api/bom/load", json={"path": str(CSV_BOM)})
        assert r.status_code == 409 and r.json()["needs_confirm"] is True and r.json()["assembly_events"] == 1
        assert settings(tmp_path / "proj")["bom_path"] is None
        r = c.post("/api/bom/load", json={"path": str(CSV_BOM), "confirm": True})
        assert r.status_code == 200
        ev = events(sess, "bom_source")[-1]["payload"]
        assert ev["confirmed"] is True and ev["assembly_events"] == 1
        r = c.post("/api/bom/remove", json={})
        assert r.status_code == 409 and r.json()["needs_confirm"] is True
        assert c.post("/api/bom/remove", json={"confirm": True}).status_code == 200
        assert len(events(sess, "placed")) == 1
        st = c.get("/api/state").json()
        assert "U1" in st["placed"]["S01"] and st["workspace"]["assembly_events"] == 1


def test_remove_protocol_bom_is_409(tmp_path):
    """PP-09: the protocol's own BOM cannot be removed (409); it can only be replaced."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        r = c.post("/api/bom/remove", json={"confirm": True})
        assert r.status_code == 409 and "part of the protocol" in r.json()["error"]
        assert c.get("/api/state").json()["project_bom"]["removable"] is False


def test_bom_override_survives_plan_reload_and_restart(tmp_path):
    """PP-09: the BOM file stays in force after plan_reload (reload_plan) and after a restart."""
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        assert c.post("/api/bom/load", json={"path": str(CSV_BOM)}).status_code == 200
        r = c.post("/api/cmd/plan_reload", json={})
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert sess.bom_source()["source"] == "settings" and sess.bom_source()["lines"] == 6
        assert c.get("/api/state").json()["project_bom"]["name"] == CSV_BOM.name
    sess.journal.close()
    s2 = start(args())
    assert s2.bom_source()["source"] == "settings" and s2.bom_source()["lines"] == 6 and s2.bom_error is None


def test_deleted_bom_file_falls_back_to_protocol_bom(tmp_path):
    """PP-09: a settings bom_path whose file was deleted falls back to the protocol's BOM, with bom_error set."""
    f = tmp_path / "gone" / "demo_bom.csv"
    f.parent.mkdir()
    shutil.copy(CSV_BOM, f)
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        assert c.post("/api/bom/load", json={"path": str(f)}).status_code == 200
    sess.journal.close()
    f.unlink()
    s2 = start(args())
    assert s2.bom_error and "could not be read" in s2.bom_error
    src = s2.bom_source()
    assert src["source"] == "protocol" and src["lines"] == 9 and src["error"] == s2.bom_error and src["removable"] is True        # N3: a broken file can be cleared
    assert settings(tmp_path / "proj")["bom_path"] == str(f.resolve())                  # the setting is kept, not silently dropped
    with client(s2) as c:
        assert c.get("/api/workspace").json()["bom_error"] == s2.bom_error


# ------------------------------------------------------------------------------------------------------------ PP-10
def test_project_routes_make_no_ecad_writes_and_spawn_nothing(tmp_path, ecad_spy, popen_spy, no_network):
    """PP-10: platform switch, BOM load/replace/remove, workspace, browser, inspect, new/open and reveal make no ECAD highlight or
    clear call and start no subprocess (reveal is stubbed)."""
    sess = new_project_session(tmp_path / "proj")
    (tmp_path / "other").mkdir()
    with client(sess) as c:
        for prog in ("kicad", "fusion", "kicad"):
            assert c.post("/api/ecad/platform", json={"program": prog}).status_code == 200
        assert c.post("/api/bom/load", json={"path": str(CSV_BOM)}).status_code == 200
        assert c.post("/api/bom/load", json={"path": str(DEMO_PROTO.parent / "assembly.yaml")}).status_code == 200
        assert c.post("/api/bom/remove", json={}).status_code == 200
        c.get("/api/workspace")
        c.get("/api/fs/list", params={"path": str(tmp_path)})
        c.get("/api/project/inspect", params={"path": str(tmp_path / "proj")})
        c.get("/api/bom/locate", params={"name": "nothing-like-this.csv"})
        assert c.post("/api/reveal", json={"path": str((tmp_path / "proj").resolve())}).json()["stubbed"] is True
        assert c.post("/api/project/new", json={"dir": str(tmp_path / "other"), "name": "o"}).status_code == 200
        assert c.post("/api/project/open", json={"dir": str(tmp_path / "proj")}).status_code == 200
        c.post("/api/ecad/platform", json={"program": "fusion"})
        assert ecad_spy == [] and popen_spy == []
        c.post("/api/cmd/highlight", json={"designators": ["U1"]})       # control: the spy does see a real ECAD write
        assert ecad_spy == ["SimulatedEcad.highlight"]


def test_reveal_without_stub_runs_open_r_only(tmp_path, monkeypatch, popen_spy):
    """PP-10: with the stub off, reveal starts exactly `open -R <path>` (recorded here, not run) and nothing else."""
    monkeypatch.delenv("POLYBENCH_NO_REVEAL")
    sess = new_project_session(tmp_path / "proj")
    p = str((tmp_path / "proj").resolve())
    with client(sess) as c:
        r = c.post("/api/reveal", json={"path": p})
        assert r.status_code == 200 and "stubbed" not in r.json()
        assert c.post("/api/reveal", json={"path": "/etc"}).status_code == 403
    assert popen_spy == [["open", "-R", p]]


# ------------------------------------------------------------------------------------------------------------ browser, locate
def test_fs_list_marks_folders_hides_dotfiles_filters_files(tmp_path):
    """/api/fs/list lists folders, marks project and batch folders, hides dotfiles, filters files by extension in file mode."""
    sess = new_project_session(tmp_path / "root" / "proj")
    root = tmp_path / "root"
    (root / ".hidden").mkdir()
    (root / ".secret.yaml").write_text("x")
    (root / "plain").mkdir()
    (root / "bom.csv").write_text("x")
    (root / "notes.txt").write_text("x")
    (root / "p.pcbv.YAML").write_text("x")
    with client(sess) as c:
        d = c.get("/api/fs/list", params={"path": str(root)}).json()
        names = {e["name"]: e for e in d["entries"]}
        assert set(names) == {"plain", "proj"} and names["proj"]["kind"] == "project" and names["plain"]["kind"] is None
        assert d["here"]["kind"] == "other" and d["parent"] == str(root.resolve().parent)
        d2 = c.get("/api/fs/list", params={"path": str(root / "proj")}).json()
        assert {e["name"]: e["kind"] for e in d2["entries"]} == {"B1": "batch"} and d2["here"]["kind"] == "project"
        f = c.get("/api/fs/list", params={"path": str(root), "mode": "file", "exts": "yaml,.csv"}).json()
        files = sorted(e["name"] for e in f["entries"] if not e["dir"])
        assert files == ["bom.csv", "p.pcbv.YAML"] and all(e["size"] == 1 for e in f["entries"] if not e["dir"])
        assert f["here"] is None
        allf = c.get("/api/fs/list", params={"path": str(root), "mode": "file"}).json()
        assert sorted(e["name"] for e in allf["entries"] if not e["dir"]) == ["bom.csv", "notes.txt", "p.pcbv.YAML"]
        default = c.get("/api/fs/list").json()                       # no path: the project folder's parent
        assert default["path"] == str(root.resolve())
        assert {"label": "Project", "path": str((root / "proj").resolve())} in default["roots"]
        gone = c.get("/api/fs/list", params={"path": str(root / "plain" / "nope" / "deeper")}).json()
        assert gone["path"] == str((root / "plain").resolve())          # a missing path climbs to the nearest folder


def test_bom_locate_by_name_and_size(tmp_path):
    """/api/bom/locate finds a dropped file by name and size in the project folder (and one level below)."""
    sess = new_project_session(tmp_path / "proj")
    name = "pp-locate-demo-7f3a.csv"
    f = tmp_path / "proj" / "boms" / name
    f.parent.mkdir()
    shutil.copy(CSV_BOM, f)
    size = f.stat().st_size
    with client(sess) as c:
        assert c.get("/api/bom/locate", params={"name": name, "size": size}).json()["matches"] == [str(f.resolve())]
        assert c.get("/api/bom/locate", params={"name": name}).json()["matches"] == [str(f.resolve())]
        assert c.get("/api/bom/locate", params={"name": name, "size": size + 1}).json()["matches"] == []
        assert c.get("/api/bom/locate", params={"name": "../../" + name, "size": size}).json()["matches"] == [str(f.resolve())]
        assert c.get("/api/bom/locate", params={"name": ""}).json()["matches"] == []


# ------------------------------------------------------------------------------------------------------------ regressions (bugs found)
def test_saving_the_overridden_platform_clears_only_a_stale_selection(tmp_path, no_network):
    """PP-06 (bug found 17 Oct): running --ecad kicad over saved fusion, choosing KiCad on the page must report the stale Fusion
    document as cleared (journal cleared_document), and must keep a selection that was made with KiCad."""
    sess = new_project_session(tmp_path / "a")
    _record_fusion_selection(sess)
    sess.journal.close()
    s2 = start(args(DEMO_PROTO, tmp_path / "a", None, ecad="kicad"))
    with client(s2) as c:
        r = c.post("/api/ecad/platform", json={"program": "kicad"}).json()
    assert r["cleared"] == {"document": FUSION_URN, "program": "fusion"}
    assert events(s2, "ecad_platform")[-1]["payload"]["cleared_document"] == r["cleared"]
    assert s2.engine.state.project["select"] is None

    s3 = new_project_session(tmp_path / "b")
    s3.journal.close()
    s4 = start(args(DEMO_PROTO, tmp_path / "b", None, ecad="kicad"))
    s4.engine.select_project({"bridge": "kicad", "document": "demo.kicad_pcb", "editor": "board", "elements": 16, "simulated": False},
                             DEMO_NAMES)
    with client(s4) as c:
        r = c.post("/api/ecad/platform", json={"program": "kicad"}).json()
    assert r["cleared"] is None and s4.engine.state.project["select"]["document"] == "demo.kicad_pcb"
    assert settings(tmp_path / "b")["ecad"] == {**settings(tmp_path / "b")["ecad"], "program": "kicad"}
    assert settings(tmp_path / "b")["ecad"]["document"]["name"] == "demo.kicad_pcb"
    s4.journal.close()
    s5 = start(args())                                       # replay keeps the KiCad selection
    assert s5.engine.state.project["select"]["document"] == "demo.kicad_pcb" and s5.stale_selection() is None


def test_reveal_refuses_dotdot_and_symlink_escape(tmp_path):
    """PP-03 (bug found 17 Oct): a path under the project that climbs out with `..` or a symlink is refused (403)."""
    sess = new_project_session(tmp_path / "proj")
    p = (tmp_path / "proj").resolve()
    (p / "link").symlink_to("/etc")
    with client(sess) as c:
        assert c.post("/api/reveal", json={"path": str(p / ".." / ".." / "etc")}).status_code == 403
        assert c.post("/api/reveal", json={"path": str(p / "link")}).status_code == 403
        assert c.post("/api/reveal", json={"path": str(p / "B1" / ".." / "polybench-project.json")}).status_code == 200


def test_bom_loaded_by_relative_path_is_named_absolutely(tmp_path, monkeypatch):
    """PP-09 (bug found 17 Oct): a BOM loaded by a relative path is recorded with absolute paths (settings and bom_source)."""
    sess = new_project_session(tmp_path / "proj")
    shutil.copy(CSV_BOM, tmp_path / "rel.csv")
    monkeypatch.chdir(tmp_path)
    with client(sess) as c:
        b = c.post("/api/bom/load", json={"path": "rel.csv"}).json()["bom"]
    assert Path(b["file"]).is_absolute() and settings(tmp_path / "proj")["bom_path"] == str((tmp_path / "rel.csv").resolve())


def test_open_real_data_legacy_from_simulated_session_is_refused(tmp_path):
    """PP-02: a legacy batch recorded WITHOUT --simulate cannot be opened from a simulated session (409); no settings are written."""
    from polybench.protocol import load
    from polybench.server import Session
    root = tmp_path / "real"
    s = Session(load(DEMO_PROTO), root, "R1", False, 3, None)
    s.journal.close()
    before = (root / "R1" / "journal.jsonl").read_bytes()
    sess = new_project_session(tmp_path / "proj")
    with client(sess) as c:
        r = c.post("/api/project/open", json={"dir": str(root), "protocol": str(DEMO_PROTO)})
        assert r.status_code == 409 and "simulate" in r.json()["error"]
        assert cur(c) is sess
    assert not (root / "polybench-project.json").exists() and (root / "R1" / "journal.jsonl").read_bytes() == before


def test_bom_change_rechecks_the_recorded_document(tmp_path):
    """PP-09: a BOM change clears the old cross-check review and compares the new BOM with the recorded document again (the
    simulated board is not rebuilt from the BOM: an ECAD document does not change because a BOM file did)."""
    sess = new_project_session(tmp_path / "p")
    c = client(sess)
    _select_document(c)
    assert c.post("/api/cmd/crosscheck_accept", json={"note": "TP99 is a test pad"}).status_code == 200
    assert cur(c).engine.state.project["accepted"]
    fp_before = c.get("/api/project/probe").json()["fingerprint"]
    alt = tmp_path / "alt.yaml"
    alt.write_text((DEMO_PROTO.parent / "assembly.yaml").read_text().replace("designators: [J1, J2]", "designators: [J1, J2, J3]"))
    r = c.post("/api/bom/load", json={"path": str(alt)})
    assert r.status_code == 200, r.text
    cc = r.json()["crosscheck"]
    assert cc and cc["missing_in_design"] == ["J3"]
    pj = cur(c).engine.state.project
    assert pj["accepted"] is None and pj["crosscheck"]["missing_in_design"] == ["J3"]
    assert c.get("/api/project/probe").json()["fingerprint"] == fp_before
    types = [e["type"] for e in events(cur(c))][-2:]
    assert types == ["bom_source", "bom_crosscheck"]
