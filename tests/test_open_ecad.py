"""Lane E (UIR-52): `polybench --from-open-ecad`. Simulated and fake bridges only: HOST-ONLY."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from polybench import open_ecad
from polybench.plugins.fusion import FakeClient, FusionBridge

PROTO = Path(__file__).resolve().parent.parent / "src/polybench/examples/demo/demo.pcbv.yaml"
PARTS = {"R1": "", "R2": "", "C1": ""}


class Down:
    def status(self):
        return {"connected": False, "detail": "cannot reach (test)"}


class Empty:
    def status(self):
        return {"connected": True, "program": "KiCad", "document": None, "detail": "no board is open"}


def fusion(name="DEMO-1"):
    return FusionBridge(client=FakeClient(dict(PARTS), doc={"name": name, "id": "doc-1"}), simulate=False)


def args(tmp, **kw):
    d = dict(ecad=None, simulate=False, protocol=str(PROTO), data_dir=str(tmp / "proj"), batch=None, boards=1, operator="", document=None)
    d.update(kw)
    return SimpleNamespace(**d)


def test_one_found_creates_linked_project(tmp_path):
    """E-1: exactly one open document; project created, document recorded with fingerprint, nothing sent to the bridge."""
    out = []
    a = args(tmp_path)
    assert open_ecad.run(a, {"fusion": fusion(), "kicad": Down(), "altium": Down()}, out=out.append) is None
    st = json.loads((tmp_path / "proj" / "polybench-project.json").read_text(encoding="utf-8"))
    assert st["ecad"]["program"] == "fusion"
    doc = st["ecad"]["documents"]["fusion"]
    assert doc["name"] == "DEMO-1" and doc["elements"] == 3 and doc["fingerprint"] and doc["linked_from"] == "open-ecad"
    assert st["protocol_path"] == str(PROTO.resolve())
    assert a.data_dir == str(tmp_path / "proj") and a.protocol is None      # the normal start reopens it
    assert "BOM: no BOM file found" in "\n".join(out)


def test_created_project_opens(tmp_path):
    """The project made by E-1 opens as an ordinary project (simulated session)."""
    from polybench import workspace as wslib
    from polybench.server import open_workspace_session
    a = args(tmp_path)
    assert open_ecad.run(a, {"fusion": fusion()}, out=lambda s: None) is None
    ws = wslib.Workspace(Path(a.data_dir))
    sess = open_workspace_session(ws, None, False, 1, "", None)
    assert sess.batch and sess.workspace.get("ecad")["documents"]["fusion"]["name"] == "DEMO-1"


def test_none_found_lists_and_creates_nothing(tmp_path):
    """E-2: none."""
    out = []
    a = args(tmp_path)
    assert open_ecad.run(a, {"fusion": Down(), "kicad": Empty(), "altium": Down()}, out=out.append) == 2
    text = "\n".join(out)
    assert "No ECAD program reports an open document" in text
    assert "fusion: not reachable" in text and "kicad: running, but no document" in text
    assert not (tmp_path / "proj").exists()


def test_several_found_never_guesses(tmp_path):
    """E-2: several; both are listed, nothing is created."""
    out = []
    assert open_ecad.run(args(tmp_path), {"fusion": fusion("A"), "other": fusion("B")}, out=out.append) == 2
    text = "\n".join(out)
    assert "2 open ECAD documents" in text and "does not choose" in text and "A" in text and "B" in text
    assert not (tmp_path / "proj").exists()


def test_needs_protocol_and_refuses_existing_project(tmp_path):
    out = []
    assert open_ecad.run(args(tmp_path, protocol=None), {"fusion": fusion()}, out=out.append) == 2
    assert "--protocol" in "\n".join(out) and not (tmp_path / "proj").exists()
    assert open_ecad.run(args(tmp_path), {"fusion": fusion()}, out=lambda s: None) is None
    out.clear()
    assert open_ecad.run(args(tmp_path), {"fusion": fusion()}, out=out.append) == 2      # second run: folder holds a project
    assert "already holds a project" in "\n".join(out)


def test_simulate_without_ecad_says_several(tmp_path):
    """--simulate: both simulated bridges report a document, so it refuses; --ecad kicad narrows it by the operator's own choice."""
    pytest.importorskip("kipy")
    out = []
    a = args(tmp_path, simulate=True)
    assert open_ecad.run(a, out=out.append) == 2 and "open ECAD documents were found" in "\n".join(out)
    a = args(tmp_path, simulate=True, ecad="kicad")
    assert open_ecad.run(a, out=lambda s: None) is None
    assert json.loads((tmp_path / "proj" / "polybench-project.json").read_text(encoding="utf-8"))["ecad"]["program"] == "kicad"


def test_kicad_schematic_and_bom_from_project_folder(tmp_path):
    """KiCad reports project folder and schematic name: the schematic path is linked when the file exists; one BOM file is linked."""
    pytest.importorskip("kipy")
    from polybench.kicad.fakeclient import FakeKiCadClient
    from polybench.kicad.bridge import KicadBridge
    from polybench.kicad.link import KiCadLink
    from polybench.kicad.synth import demo_board
    proj = tmp_path / "kproj"
    proj.mkdir()
    snap = demo_board(["C1", "C2", "R1"])
    snap.document.project_path = str(proj)
    snap.document.board_filename = "demo.kicad_pcb"
    (proj / "demo.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (proj / "demo_bom.csv").write_text("Mouser Part Number,Quantity,Customer Part Number,Designators\n"
                                       "595-DEMO100,2,R,\"R1,R2\"\n", encoding="utf-8")
    fake = FakeKiCadClient(snap)
    fake.with_schematic = True
    br = KicadBridge(link=KiCadLink(client_factory=lambda addr: fake, fake=True))
    found, _ = open_ecad.probe({"kicad": br})
    assert len(found) == 1
    f = found[0]
    assert f.project_dir == str(proj) and f.schematic == str((proj / "demo.kicad_sch").resolve())
    res = open_ecad.create_project(f, tmp_path / "p2", str(PROTO), None)
    assert res["schematic"] == f.schematic
    assert res["bom"] == str((proj / "demo_bom.csv").resolve())          # one readable BOM file: linked
    assert json.loads((tmp_path / "p2" / "polybench-project.json").read_text(encoding="utf-8"))["bom_path"] == res["bom"]
    (proj / "other_bom.csv").write_text((proj / "demo_bom.csv").read_text(encoding="utf-8"), encoding="utf-8")
    f.bom_candidates = open_ecad.bom_candidates(str(proj))
    assert len(f.bom_candidates) == 2                                    # two: none is chosen
    res2 = open_ecad.create_project(f, tmp_path / "p3", str(PROTO), None)
    assert res2["bom"] is None


def test_cli_flag_and_no_bridge_call_in_other_modes(tmp_path):
    """The flag exists; with nothing open (real bridges, no program running, isolated dirs) it exits 2 and writes nothing."""
    env_dir = tmp_path / "proj"
    r = subprocess.run([sys.executable, "-m", "polybench.cli", "--from-open-ecad", "--ecad", "altium", "--protocol", str(PROTO),
                        "--data-dir", str(env_dir), "--no-browser"], capture_output=True, text=True, timeout=25, encoding="utf-8", errors="replace")
    assert r.returncode == 2 and "No ECAD program reports an open document" in r.stdout
    assert not env_dir.exists()


class MultiDocs(FakeClient):
    """Three Fusion documents open, DEMO-1 the active tab."""
    def open_documents(self):
        return [{"name": "DEMO-1", "id": "1", "isActive": True}, {"name": "OTHER-BOARD", "id": "2"}, {"name": "THIRD", "id": "3"}]


def multi():
    return FusionBridge(client=MultiDocs(dict(PARTS), doc={"name": "DEMO-1", "id": "1"}), simulate=False)


def test_E_a_three_fusion_documents_refuse_then_named(tmp_path):
    out = []
    assert open_ecad.run(args(tmp_path), {"fusion": multi()}, out=out.append) == 2
    text = "\n".join(out)
    assert "3 open ECAD documents" in text and "OTHER-BOARD" in text and "THIRD" in text and "--document" in text
    assert not (tmp_path / "proj").exists()
    out.clear()                                       # naming a non-active one: still refused, with the reason
    assert open_ecad.run(args(tmp_path, document="THIRD"), {"fusion": multi()}, out=out.append) == 2
    assert "not the active tab" in "\n".join(out) and not (tmp_path / "proj").exists()
    assert open_ecad.run(args(tmp_path, document="demo"), {"fusion": multi()}, out=lambda s: None) is None    # names the active one
    assert (tmp_path / "proj" / "polybench-project.json").exists()


def kicad_two_schematics(tmp_path):
    pytest.importorskip("kipy")
    from polybench.kicad.bridge import KicadBridge
    from polybench.kicad.fakeclient import FakeKiCadClient
    from polybench.kicad.link import KiCadLink
    from polybench.kicad.synth import demo_board
    proj = tmp_path / "kp"
    proj.mkdir()
    snap = demo_board(["C1", "R1"])
    snap.document.project_path, snap.document.board_filename = str(proj), "b.kicad_pcb"
    for n in ("zzz_other.kicad_sch", "b.kicad_sch"):
        (proj / n).write_text("(kicad_sch)", encoding="utf-8")
    fake = FakeKiCadClient(snap)
    fake.with_schematic = True
    br = KicadBridge(link=KiCadLink(client_factory=lambda addr: fake, fake=True))
    orig = br.documents

    def docs(search=None):
        r = orig(search)
        r["schematic_open"] = ["zzz_other.kicad_sch", "b.kicad_sch"]
        return r
    br.documents = docs
    return br


def test_E_b_two_schematics_refuse_or_named(tmp_path):
    br = kicad_two_schematics(tmp_path)
    out = []
    assert open_ecad.run(args(tmp_path), {"kicad": br}, out=out.append) == 2
    assert "2 schematics are open" in "\n".join(out) and "--document" in "\n".join(out) and not (tmp_path / "proj").exists()
    assert open_ecad.run(args(tmp_path, document="b.kicad_sch"), {"kicad": br}, out=lambda s: None) is None
    st = json.loads((tmp_path / "proj" / "polybench-project.json").read_text(encoding="utf-8"))
    assert st["ecad"]["documents"]["kicad"]["schematic_path"].endswith("b.kicad_sch")


def test_E_c_link_is_in_the_journal_and_state(tmp_path):
    """The UI reads project state from the journal: select is set, the cross-check was journalled, settings still record it."""
    from polybench import workspace as wslib
    from polybench.server import open_workspace_session
    a = args(tmp_path)
    assert open_ecad.run(a, {"fusion": fusion()}, out=lambda s: None) is None
    ws = wslib.Workspace(Path(a.data_dir))
    sess = open_workspace_session(ws, None, False, 1, "", None)
    sel = sess.engine.state.project["select"]
    assert sel and sel["document"] == "DEMO-1"
    kinds = [e.get("type") or e.get("t") or e.get("kind") for e in sess.journal.events]
    assert "project_select" in kinds and "bom_crosscheck" in kinds and "project_settings" in kinds
    assert ws.documents()["fusion"]["linked_from"] == "open-ecad"


def test_E_d_bad_protocol_refused_before_anything_is_written(tmp_path):
    for bad in (str(tmp_path / "missing.pcbv.yaml"), str(tmp_path / "broken.pcbv.yaml")):
        (tmp_path / "broken.pcbv.yaml").write_text("not: a protocol\n", encoding="utf-8")
        out = []
        assert open_ecad.run(args(tmp_path, protocol=bad), {"fusion": fusion()}, out=out.append) == 2
        assert out and "nothing was created" in out[-1]
        assert not (tmp_path / "proj").exists()


def test_names_are_one_line():
    assert open_ecad.clean("a\nb\x1b[31m") == "a b [31m"


def test_E_f_hostile_project_name_stays_inside_data_dir(tmp_path):
    class Hostile:
        def status(self):
            return {"connected": True, "program": "KiCad", "document": "b.kicad_pcb", "editor": "board", "elements": 3,
                    "project": {"name": "../../escape\n\x1b[2J", "path": ""}}

        def designators(self):
            return ["R1", "R2", "C1"]
    a = args(tmp_path, data_dir=str(tmp_path / "a" / "b" / "proj"))
    assert open_ecad.run(a, {"kicad": Hostile()}, out=lambda s: None) is None
    root = (tmp_path / "a" / "b" / "proj").resolve()
    made = [p for p in root.iterdir() if p.is_dir()]
    assert made and all(p.resolve().parent == root for p in made)
    assert not any(c in p.name for p in made for c in "/\n\x1b") and not (tmp_path / "escape").exists()
    assert not list((tmp_path / "a").glob("escape*")) and not list(tmp_path.glob("escape*"))
    assert open_ecad.slug("../..") == "design" and open_ecad.slug("a b/c") == "a-b-c"
    # an explicit --batch that would leave the folder is refused, nothing is written
    out = []
    b = args(tmp_path, data_dir=str(tmp_path / "p2"), batch="../out")
    assert open_ecad.run(b, {"kicad": Hostile()}, out=out.append) == 2 and "outside" in out[-1]
    assert not (tmp_path / "p2" / "polybench-project.json").exists()


class PrefixDocs(FakeClient):
    def open_documents(self):
        return [{"name": "DEMO-1", "id": "1", "isActive": True}, {"name": "DEMO-10", "id": "2"}, {"name": "OLD-DEMO-1", "id": "3"}]


def prefix():
    return FusionBridge(client=PrefixDocs(dict(PARTS), doc={"name": "DEMO-1", "id": "1"}), simulate=False)


def test_E_g_document_exact_then_unique_partial_else_refuse(tmp_path):
    assert open_ecad.run(args(tmp_path, document="DEMO-1"), {"fusion": prefix()}, out=lambda s: None) is None   # exact beats DEMO-10
    out = []
    assert open_ecad.run(args(tmp_path, data_dir=str(tmp_path / "q"), document="demo"), {"fusion": prefix()}, out=out.append) == 2
    text = "\n".join(out)
    assert "3 open ECAD documents" in text and "DEMO-10" in text and "OLD-DEMO-1" in text and not (tmp_path / "q").exists()
    out.clear()
    assert open_ecad.run(args(tmp_path, data_dir=str(tmp_path / "r"), document="DEMO-10"), {"fusion": prefix()}, out=out.append) == 2
    assert "not the active tab" in "\n".join(out)         # unique partial/exact but not readable
    assert open_ecad.pick_named(["a1", "a10"], "a1") == ["a1"] and open_ecad.pick_named(["xa1", "a10"], "a1") == ["xa1", "a10"]
    assert open_ecad.pick_named(["xa1", "b"], "A1") == ["xa1"]
