"""UIR-14: ECAD setup as one box. The page is drawn in the browser, so these are source-level checks of app.js plus the server side of
the platform switch (KC-03); the drawn states are covered by tests/e2e/uir_pj_walk.py (WebKit). HOST-ONLY."""
from pathlib import Path

import polybench

JS = (Path(polybench.__file__).parent / "static" / "app.js").read_text()


def _project_view() -> str:
    a = JS.index("function viewProject(el)")
    return JS[a:JS.index("function howSection()", a)]


def test_r1_one_ecad_box_replaces_cards_1_to_3():
    v = _project_view()
    assert "pjCard('ecad', 1, 'ECAD setup'" in v
    for old in ("pjCard('plat'", "pjCard('link'", "pjCard('doc'"):
        assert old not in v
    # order inside the box: platform, link, document
    assert v.index("id=\"pjc-plat\"") < v.index("id=\"pjc-link\"") < v.index("id=\"pjc-doc\"")
    assert "body: platB + linkB + docB" in v


def test_r1_platform_is_a_dropdown_and_link_needs_a_platform():
    v = _project_view()
    assert '<select id="pjplatsel"' in v and "pjtile" not in v
    assert "if (c1) {" in v and "linkB = `<div class=\"pjsec\" id=\"pjc-link\">" in v      # link part only once a platform is chosen


def test_r2_help_is_collapsed_by_default():
    v = _project_view()
    assert "const hopen = !!ui.pjHelp" in v and "Help connecting" in v
    s = JS[JS.index("function setupHelp"):JS.index("function ccSummary")]
    assert "Check again" in s and "pjcheck" not in s and "pjskip" not in s    # steps only; the buttons live in the box
    assert "case 'pjhelp': ui.pjHelp = !ui.pjHelp" in JS


def test_r3_platform_switch_keeps_kc03_behaviour():
    fn = JS[JS.index("async function pjSetPlatform"):JS.index("function pjResetEcad")]
    assert "/api/ecad/platform" in fn and "ui.ecadCleared = " in fn and "pjResetEcad()" in fn
    assert "e.target.id === 'pjplatsel'" in JS and "pjSetPlatform(e.target.value)" in JS



def test_r4_left_checklist_is_one_ecad_setup_item_matching_the_box():
    a = JS.index("function leftProject")
    left = JS[a:JS.index("function pjField", a)]
    assert "'ECAD setup'" in left and "'ECAD platform'" not in left and "'Document selected'" not in left
    done = JS[JS.index("function ecadSetupDone"):JS.index("function ecadSetupSub")]
    assert "ecad_skipped" in done and "st.connected" in done and "P.select" in done and "E.saved" in done
    # the box uses the same rule for its DONE chip
    assert "c1 && linked && c3 ? 'done' : 'todo'" in _project_view()
