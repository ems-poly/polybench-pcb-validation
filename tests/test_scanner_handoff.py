"""The BOM bridge to the embedded scanner, and the removal of the standalone-app hand-off (launch button, BOM export)."""
import csv

from helpers import ROOT
from polybench import scanner


def test_export_has_the_columns_the_scanner_importer_recognises(tmp_path, engine):
    res = scanner.export_bom_csv(engine.state.bom_lines(), tmp_path / "s" / "bom.csv")
    rows = list(csv.DictReader(open(res["path"], encoding="utf-8")))
    assert res["rows"] == len(rows) == len(engine.state.bom_lines())
    assert list(rows[0]) == scanner.HEADER
    r = next(x for x in rows if x["Designators"] == "U1")
    assert r["Mouser Part Number"] == "595-DEMOMCU32" and r["Manufacturer Part Number"] == "DEMOMCU-32" and r["Fitted"] == "Fitted"
    assert next(x for x in rows if "R1" in x["Designators"])["Fitted"] == "DNF"
    assert "C1, C2" in next(x for x in rows if x["Designators"].startswith("C1"))["Designators"]


def test_no_launcher_and_no_export_endpoint_any_more(tmp_path):
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    assert not hasattr(scanner, "launch")
    sess = Session(load(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"), tmp_path, "sc", True, 2, None)
    with TestClient(create_app(sess)) as c:
        assert c.post("/api/scanner/launch").status_code in (404, 405)
        assert c.post("/api/scanner/export").status_code in (404, 405)
    js = (ROOT / "src/polybench/static/app.js").read_text(encoding="utf-8")
    assert "scanlaunch" not in js and "scanexport" not in js and "Launch Assembly Scanner" not in js and "Export BOM for scanner" not in js


def test_batch_bom_goes_through_the_poc_importer(tmp_path, engine):
    from polybench.assembly_scanner.polybench_link import load_batch_bom
    from polybench.assembly_scanner.models import Status
    from polybench.assembly_scanner.resolver import resolve
    bom, lmap, pmap = load_batch_bom(engine.state.bom_lines(), tmp_path)
    assert lmap[4][0].designators == ("C1", "C2", "C3", "C4", "C5", "C6")        # the importer's designator parsing
    r = resolve(["x", "603-RC0402FR-0710KL"], bom)
    assert r.status is Status.MATCH and r.designators == ("R3", "R4", "R5") and pmap["603-RC0402FR-0710KL"] == [5]
    assert resolve(["603-RC0402FR-07120RL"], bom).status is Status.MATCH_NO_PLACEMENT     # the DNF line: in BOM, no placement
