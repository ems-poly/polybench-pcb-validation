"""The app bar keeps Note, Dark and ? reachable at 720 px: the project name hides below 800 px (it was 46 px too wide once Note was added)."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "polybench" / "static"


def test_appbar_project_label_has_narrow_rule():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert 'class="ctx proj">Project' in js
    assert re.search(r"@media \(max-width:799px\)\s*\{\s*#appbar \.ctx\.proj\{display:none\}", css)
