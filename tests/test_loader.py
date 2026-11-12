import re

from polybench.protocol import load
from helpers import DEMO


def errs(p):
    return [str(i) for i in p.errors]


def test_demo_lints_clean():
    p = load(DEMO / "demo.pcbv.yaml")
    assert not p.errors and not p.warnings
    kinds = {s["kind"] for s in p.data["steps"]}
    assert {"setup", "assembly", "visual", "unpowered", "powered", "function", "characterisation", "report"} <= kinds
    assert {t["kind"] for _, _, t in p.tests()} == {"number", "yn", "choice", "text", "hex", "table"}


def patch(demo_copy, fname, old, new):
    f = demo_copy / fname
    f.write_text(f.read_text().replace(old, new))
    return load(demo_copy / "demo.pcbv.yaml")


def test_limit_without_class(demo_copy):
    p = patch(demo_copy, "unpowered.yaml", "limit: {class: hard, min: 10, source:", "limit: {min: 10, source:")
    assert any("limit without class" in e for e in errs(p))
    assert any(re.search(r"unpowered\.yaml:\d+: error", e) for e in errs(p))  # file and line


def test_hard_without_source(demo_copy):
    p = patch(demo_copy, "unpowered.yaml", "limit: {class: hard, min: 10, source: {type: datasheet, doc: \"DEMOCAN DS-001\", page: 5}}", "limit: {class: hard, min: 10}")
    assert any("hard limit without source" in e for e in errs(p))


def test_hard_with_guess_source(demo_copy):
    p = patch(demo_copy, "powered.yaml", "source: {type: design_calc, doc: \"power budget rev 1\"}", "source: {type: guess}")
    assert any("guess source" in e for e in errs(p))


def test_guess_needs_closes_on(demo_copy):
    p = patch(demo_copy, "unpowered.yaml", ', closes_on: "first powered board current"', "")
    assert any("closes_on" in e for e in errs(p))


def test_duplicate_test_ids_across_fragments(demo_copy):
    p = patch(demo_copy, "powered.yaml", "id: P02", "id: U01")
    assert any("duplicate test id 'U01'" in e for e in errs(p))


def test_proposes_unknown_test(demo_copy):
    p = patch(demo_copy, "powered.yaml", "proposes: [P01, P02, P03, P04]", "proposes: [P01, NOPE]")
    assert any("unknown test 'NOPE'" in e for e in errs(p))


def test_bad_schema_missing_include_and_syntax(demo_copy):
    top = demo_copy / "demo.pcbv.yaml"
    top.write_text(top.read_text().replace("pcbv.protocol/2", "pcbv.protocol/1").replace("visual.yaml", "nothere.yaml"))
    p = load(top)
    assert any("schema must be" in e for e in errs(p)) and any("nothere.yaml" in e for e in errs(p))
    (demo_copy / "visual.yaml").write_text("steps: [\n  - id: visual\n  bad: : :\n")
    top.write_text(top.read_text().replace("nothere.yaml", "visual.yaml").replace("protocol/1", "protocol/2"))
    p = load(top)
    assert any("YAML syntax" in e for e in errs(p))


def test_unknown_plugin_is_a_warning_and_bad_name_an_error(demo_copy):
    p = patch(demo_copy, "powered.yaml", "plugin: demo.measure", "plugin: vendor.unknown")
    assert not p.errors and any("not installed" in str(w) for w in p.warnings)
    p = patch(demo_copy, "powered.yaml", "plugin: vendor.unknown", "plugin: nodots")
    assert any("plugin name" in e for e in errs(p))


def test_duplicate_designator(demo_copy):
    p = patch(demo_copy, "assembly.yaml", "designators: [U2]", "designators: [U1]")
    assert any("designator U1 appears" in e for e in errs(p))
