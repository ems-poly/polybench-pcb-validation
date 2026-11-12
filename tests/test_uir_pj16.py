"""UIR-16: AI assistant card collapsed, three parts. Source-level checks; drawn states in tests/e2e/uir_pj_walk.py. HOST-ONLY."""
from pathlib import Path

import polybench

JS = (Path(polybench.__file__).parent / "static" / "ai_card.js").read_text(encoding="utf-8")
CARD = JS[JS.index("function renderAiCard"):JS.index("/* ---- question panel */")]


def test_r1_collapsed_by_default_with_permission_mode_line():
    assert "cardOpen('ai', false)" in CARD
    assert "Permissions: <b>${aiEsc(a.mode)}</b>" in CARD and "summary: sum" in CARD


def test_r2_three_parts_when_open():
    for i, name in enumerate(("ai-conn", "ai-setup", "ai-activity")):
        assert f'id="{name}"' in CARD
    assert CARD.index('id="ai-conn"') < CARD.index('id="ai-setup"') < CARD.index('id="ai-activity"')


def test_no_on_off_control_is_built():       # the selector is deferred by the maintainer (UIR-16 log)
    assert "type=\"checkbox\"" not in CARD and "role=\"switch\"" not in CARD and "<select" not in CARD
