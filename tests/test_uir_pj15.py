"""UIR-15: BOM card simple by default, detail with Advanced. Source-level checks; drawn states in tests/e2e/uir_pj_walk.py. HOST-ONLY."""
from pathlib import Path

import polybench

JS = (Path(polybench.__file__).parent / "static" / "app.js").read_text()


def test_r1_standard_view_is_one_line_problems_and_accept():
    s = JS[JS.index("function ccSummary"):JS.index("/* -- small pieces */")]
    assert "fitted parts match the board" in s and "DNF" in s
    assert "missing_in_design" in s and "missing_in_bom" in s
    assert "dnfc" not in s and "chipList(cc.dnf" not in s          # DNF chips only with Advanced
    v = JS[JS.index("// 2 BOM"):JS.index("// AI assistant (optional")]
    assert "const adv = !!ui.advanced" in v and "adv ? ccTable(showCc) : ccSummary(showCc)" in v
    assert "data-act=\"pjaccept\"" in v and "if (adv) inner +=" in v


def test_r1_file_table_and_replace_only_with_advanced():
    a = JS.index("function bomBody")
    b = JS[a:JS.index("/* -- the page */", a)]
    assert "} else if (ui.advanced) {\n    h += bomFacts(b);" in b
    assert "bomadd" in b                       # the drop zone for an empty BOM stays in the standard view
