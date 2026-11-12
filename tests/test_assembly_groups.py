"""Assembly aid check groups in build order, UIR-21: the `position:` key of a section (protocol.py), the docs, existing protocols unchanged. HOST-ONLY."""
from pathlib import Path

from helpers import DEMO, ROOT
from polybench.protocol import load

GROUPS = """    sections:
      - id: paste
        title: Paste print
        position: before_placement
        tests: [{id: A01, title: Stencil aligned, kind: yn}]
      - id: after
        title: Placement check
        position: after_placement
        tests: [{id: A02, title: Polarity checked, kind: yn}]
      - id: plain
        title: Final look
        tests: [{id: A03, title: Visual check, kind: yn}]
"""


def with_groups(demo_copy, extra=""):
    f = demo_copy / "assembly.yaml"
    f.write_text(f.read_text(encoding="utf-8").replace("    title: Assembly aid\n", "    title: Assembly aid\n" + GROUPS + extra, 1), encoding="utf-8")
    return load(demo_copy / "demo.pcbv.yaml")


def test_position_key_is_accepted_and_kept(demo_copy):
    """R1: the key survives loading, so the page can draw the groups in build order."""
    p = with_groups(demo_copy)
    assert not p.errors and not p.warnings
    asm = next(s for s in p.data["steps"] if s["kind"] == "assembly")
    assert [(x["id"], x.get("position")) for x in asm["sections"]] == [("paste", "before_placement"), ("after", "after_placement"), ("plain", None)]


def test_position_reaches_the_page_in_the_snapshot(demo_copy, tmp_path):
    from polybench.engine import Engine
    from polybench.store import Journal
    eng = Engine(with_groups(demo_copy), Journal(tmp_path / "j.jsonl"), "t")
    eng.create_batch("t", ["S01"], "S", True, "DEMO-1")
    asm = next(s for s in eng.state.snapshot()["steps"] if s["id"] == "assembly")
    assert [x.get("position") for x in asm["sections"]] == ["before_placement", "after_placement", None]


def test_unknown_position_is_an_error_and_other_steps_warn(demo_copy):
    f = demo_copy / "assembly.yaml"
    f.write_text(f.read_text(encoding="utf-8").replace("    title: Assembly aid\n", "    title: Assembly aid\n" + GROUPS.replace("before_placement", "first"), 1), encoding="utf-8")
    p = load(demo_copy / "demo.pcbv.yaml")
    assert any("position must be one of before_placement, after_placement" in str(i) for i in p.errors)
    u = demo_copy / "unpowered.yaml"
    u.write_text(u.read_text(encoding="utf-8").replace("      - id: rails\n", "      - id: rails\n        position: before_placement\n", 1), encoding="utf-8")
    p2 = load(demo_copy / "demo.pcbv.yaml")
    assert any("only used by the assembly step" in str(i) for i in p2.warnings) and not [e for e in p2.errors if "'rails'" in str(e)]


def test_existing_protocols_load_unchanged():
    """R2: no shipped protocol uses the key; the DEMO protocol and the ESP32 example lint exactly as before (no error, no warning)."""
    p = load(DEMO / "demo.pcbv.yaml")
    assert not p.errors and not p.warnings
    assert all("position" not in sec for s in p.data["steps"] for sec in s.get("sections", []))
    ex = ROOT / "examples" / "esp32-s3-board" / "yd-esp32-s3.pcbv.yaml"
    if ex.exists():
        e = load(ex)
        assert not [i for i in e.issues if "position" in str(i)]


def test_the_app_draws_no_built_in_tips():
    """R3: the page has no paste or placement tip text of its own; the groups it draws are only the protocol's."""
    js = (ROOT / "src" / "polybench" / "static" / "app.js").read_text(encoding="utf-8")
    assert "stencil" not in js.lower() and "paste print" not in js.lower() and "paste volume" not in js.lower()


def test_docs_describe_the_key():
    doc = (ROOT / "site" / "docs" / "reference" / "protocol.md").read_text(encoding="utf-8")
    assert "before_placement" in doc and "after_placement" in doc
    spec = ROOT / "docs" / "PRODUCT_SPEC.md"            # internal; the public tree does not ship it
    if spec.exists():
        assert "| r4 |" in spec.read_text(encoding="utf-8")
