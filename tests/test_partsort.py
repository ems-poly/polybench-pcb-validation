"""Hand-assembly order: passives first, ICs and tall parts last, data-source priority, DNF last, natural designator sort."""
from polybench import partsort as ps
from polybench import protocol

BOM_LINES = [
    {"line": 1, "designators": ["IC3"], "package": "VQFN-32 5x5 mm, 0.5 mm pitch, exposed pad"},
    {"line": 2, "designators": ["IC1"], "package": "WQFN-16 3x3 mm (RTE0016D), exposed pad"},
    {"line": 5, "designators": ["Y1"], "package": "NX2016SA, 2.0 x 1.6 mm, 4 pads"},
    {"line": 6, "designators": ["C8", "C9"], "package": "0402"},
    {"line": 8, "designators": ["D2"], "package": "SOD-323FL"},
    {"line": 9, "designators": ["L1"], "package": "WE-MAPI 3015"},
    {"line": 10, "designators": ["C1"], "package": "0805"},
    {"line": 12, "designators": ["R3"], "package": "0603"},
    {"line": 14, "designators": ["R1"], "package": "0603", "dnf": True},
    {"line": 18, "designators": ["R10", "R9"], "package": "0402"},
    {"line": 19, "designators": ["R2"], "package": "0402"},
    {"line": 30, "designators": ["J1"], "package": "JST header, through-hole"},
]


def order(bom, fps=None):
    r = ps.rank_bom(bom, fps)
    return [l["line"] for l in sorted(bom, key=lambda l: r[l["line"]]["hand_rank"])]


def test_passives_first_ics_crystal_connectors_last():
    o = order(BOM_LINES)
    assert o == [6, 19, 18, 12, 10, 8, 2, 1, 5, 9, 30, 14]   # 0402 (C8 < R2 < R9), 0603, 0805, SOD, ICs by area, Y1, L1, J1, DNF
    chips = {6, 10, 12, 18, 19}
    assert max(o.index(n) for n in chips) < o.index(8) < min(o.index(n) for n in (1, 2)) < o.index(5) < o.index(30) < o.index(14)


def test_natural_designator_and_tiebreak():
    assert sorted(["R10", "R2", "R1"], key=ps.natural) == ["R1", "R2", "R10"]
    bom = [{"line": 1, "designators": ["R10"], "package": "0402"}, {"line": 2, "designators": ["R2"], "package": "0402"}]
    assert order(bom) == [2, 1]
    assert ps.rank_line({"line": 3, "designators": ["R10", "R9"], "package": "0402"})["cls"] == 0


def test_dnf_always_last():
    bom = [{"line": 1, "designators": ["J1"], "package": "header", "dnf": True}, {"line": 2, "designators": ["IC1"], "package": "TSSOP-20"}]
    assert order(bom) == [2, 1]


def test_source_priority_bom_over_ecad_over_table():
    line = {"line": 1, "designators": ["U1"], "package": "SOT-23"}               # table says: class 2, 2.9 x 1.3
    fp = {"U1": {"package": "QFN32", "outline_local": [-2.5, -2.5, 2.5, 2.5]}}   # ecad says: QFN (class 3), 25 mm2
    r = ps.rank_line(line, fp)
    assert (r["cls"], round(r["area"]), r["class_source"], r["area_source"]) == (3, 25, "ecad", "ecad")
    r = ps.rank_line(dict(line, body_mm=[3, 3], height_mm=2.4), fp)             # the BOM wins over both
    assert (r["cls"], r["area"], r["class_source"], r["area_source"]) == (4, 9, "bom", "bom")
    r = ps.rank_line(line, {})                                                  # table only
    assert (r["cls"], r["class_source"], r["area_source"]) == (2, "table", "table")
    r = ps.rank_line({"line": 1, "designators": ["U1"], "package": "weird"}, {})  # designator prefix only
    assert (r["cls"], r["class_source"], r["size_unknown"]) == (3, "kind", True)


def test_class_and_area_resolved_separately():
    r = ps.rank_line({"line": 1, "designators": ["C1"], "package": "0402", "body_mm": [2.0, 1.0]})
    assert (r["class_source"], r["area_source"], r["area"]) == ("table", "bom", 2.0)
    assert "BOM line" in r["why"] and "package table" in r["why"]


def test_unknown_package_last_among_tall_with_hint():
    bom = [{"line": 1, "designators": ["Y1"], "package": "NX2016SA, 2.0 x 1.6 mm"}, {"line": 2, "designators": ["ZZ1"], "package": "odd"},
           {"line": 3, "designators": ["J1"], "package": "header"}]
    r = ps.rank_bom(bom)
    assert r[2]["size_unknown"] and r[2]["class_source"] == "none"
    assert order(bom) == [1, 2, 3]


def test_height_ladder_keeps_qfn_apart_from_chip():
    assert ps.class_from_height("passive", 0.5, None) == 0
    assert ps.class_from_height("ic", 0.9, 25.0) == 3
    assert ps.class_from_height("ic", 0.9, 3.0) == 2
    assert ps.class_from_height("ic", 3.0, 25.0) == 4


def _patched(demo_copy, extra):
    f = demo_copy / "assembly.yaml"
    orig = getattr(_patched, "orig", None) or f.read_text()
    _patched.orig = orig
    f.write_text(orig.replace("{line: 1,", "{line: 1, " + extra + ",", 1))
    return protocol.load(demo_copy / "demo.pcbv.yaml")


def test_loader_hints_optional_and_validated(demo_copy):
    _patched.orig = None
    msgs = lambda p: [str(i) for i in p.errors]
    assert not _patched(demo_copy, "height_mm: 0.5, body_mm: [1.0, 0.5]").errors     # present and valid
    assert any("height_mm" in m for m in msgs(_patched(demo_copy, "height_mm: tall")))
    assert any("body_mm" in m for m in msgs(_patched(demo_copy, "body_mm: [1.0]")))


def test_demo_without_hints_lints_clean():
    from helpers import DEMO
    assert not protocol.load(DEMO / "demo.pcbv.yaml").errors
