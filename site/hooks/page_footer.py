"""MkDocs hook: a 'Last updated / version' line at the end of every page, and screenshots that open full size.

Last updated is the date of the last git commit that touched the page source (build date if the file is not committed).
Version is the Polybench version in the repository's pyproject.toml.
"""
from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path

_VERSION = None


def _version(config) -> str:
    global _VERSION
    if _VERSION is None:
        py = Path(config["config_file_path"]).resolve().parent.parent / "pyproject.toml"
        m = re.search(r'^version\s*=\s*"([^"]+)"', py.read_text(encoding="utf-8"), re.M) if py.is_file() else None
        _VERSION = m.group(1) if m else "unknown"
    return _VERSION


def _updated(path: str) -> str:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cs", "--", path], capture_output=True, text=True,
                             cwd=str(Path(path).parent), timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    return out or date.today().isoformat()


_SHOT = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)(\{[^}]*\.shot[^}]*\})")


def on_page_markdown(markdown, page, config, files):
    # every screenshot links to its full-size file (a click opens it at 1:1)
    markdown = _SHOT.sub(lambda m: f"[![{m.group(1)}]({m.group(2)}){m.group(3)}]({m.group(2)})", markdown)
    line = (f'\n\n<div class="pb-footer">Last updated {_updated(page.file.abs_src_path)} '
            f'&middot; Polybench {_version(config)}</div>\n')
    return markdown + line
