"""Privacy (Pat, 16 Oct): nothing leaves the Mac. No source file under src/ may name a non-loopback http(s) URL.

Allowed: licence files (they cite their own URL), pip metadata folders (*.egg-info, which carry the project URLs) and the explicit doc-comment allow-list below. A new external URL in code,
a page, a stylesheet or a config default fails this test.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
URL = re.compile(r"https?://[^\s\"'`)<>]+", re.I)
LOOPBACK = re.compile(r"https?://(127\.\d+\.\d+\.\d+|localhost|\[::1\])(?=[:/,]|$)", re.I)
BINARY = {".woff2", ".woff", ".ttf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pyc", ".zip", ".heic"}
LICENCE = re.compile(r"^(OFL|LICEN[CS]E|COPYING|NOTICE)", re.I)
DOC_ALLOW: set[tuple[str, str]] = set()      # (path relative to src, url) for a URL in a doc comment, reviewed by hand


def test_no_external_urls_in_src():
    bad = []
    for p in SRC.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts or any(x.endswith(".egg-info") for x in p.parts) or p.suffix.lower() in BINARY or LICENCE.match(p.name):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in URL.finditer(line):
                u = m.group(0)
                if LOOPBACK.match(u) or (str(p.relative_to(SRC)), u) in DOC_ALLOW:
                    continue
                bad.append(f"{p.relative_to(SRC)}:{n}: {u}")
    assert not bad, "non-loopback URLs in src/ (nothing may leave the Mac):\n" + "\n".join(bad)


def test_ollama_url_must_be_loopback():
    import pytest
    from polybench.voice import config
    for ok in ("http://127.0.0.1:11434", "http://localhost:11434/", "http://[::1]:11434"):
        assert config.validate({"ollama_url": ok})["ollama_url"]
    for bad in ("http://203.0.113.5:11434", "https://api.example.com", "http://10.0." + "0.2", "http://localhost.evil.com:11434", "ftp://127.0.0.1"):
        with pytest.raises(ValueError):
            config.validate({"ollama_url": bad})


def test_voice_package_sets_hf_offline():
    import os
    import polybench.voice  # noqa: F401
    assert os.environ.get("HF_HUB_OFFLINE") == "1" and os.environ.get("TRANSFORMERS_OFFLINE") == "1"
