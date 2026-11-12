import json
import shutil
import subprocess

import pytest

from helpers import DEMO

pytest_plugins = ["asc_fixtures"]       # fixtures of the ported Assembly Scanner tests (tests/assembly_scanner)

# tests/private (client fixtures, real photos and board files) exists in the private repo only. There, a folder is skipped
# when the optional extra it needs is not installed.
import importlib.util as _u

_PRIVATE_NEEDS = {"aoi": ("cv2", "PIL", "numpy"), "voice": ("numpy",), "assembly_scanner": ("cv2", "zxingcpp", "openpyxl", "PIL", "numpy"),
                  "e2e": ("playwright",)}
collect_ignore = [f"private/{d}" for d, mods in _PRIVATE_NEEDS.items() if any(_u.find_spec(m) is None for m in mods)]
# public folders that need an optional extra: skipped (not failed) in a bare `.[dev]` install. The scanner tests skip on their own
# (the `demo_files` fixture), the voice suite needs numpy (the `voice` extra).
if _u.find_spec("numpy") is None:
    collect_ignore.append("voice")
if any(_u.find_spec(m) is None for m in _PRIVATE_NEEDS["assembly_scanner"]):
    collect_ignore += ["private/test_assembly_embedded.py", "private/test_aoi_cam_layout.py"]


@pytest.fixture
def demo_copy(tmp_path):
    d = tmp_path / "demo"
    shutil.copytree(DEMO, d)
    return d


@pytest.fixture
def engine(tmp_path):
    from polybench.engine import Engine
    from polybench.protocol import load
    from polybench.store import Journal
    proto = load(DEMO / "demo.pcbv.yaml")
    eng = Engine(proto, Journal(tmp_path / "j.jsonl"), "tester")
    eng.create_batch("t", ["S01", "S02", "S03"], "S", True, "DEMO-1")
    return eng


# ---- no sound and no windows from the tests (the operator works at the bench while they run)
_REAL_POPEN = subprocess.Popen


def _forbidden(args) -> str | None:
    a = [str(x) for x in (args if isinstance(args, (list, tuple)) else [args])]
    if not a:
        return None
    exe = a[0].rsplit("/", 1)[-1]
    if exe == "say" and "-o" not in a and a[1:3] != ["-v", "?"]:          # `say -v ?` only lists voices; `say -o file` writes a clip
        return "say without -o would speak aloud"
    if exe == "open":
        return "open would put a window on the screen"
    return None


class _GuardedPopen(_REAL_POPEN):
    def __init__(self, args, *a, **k):
        why = _forbidden(args)
        if why:
            raise RuntimeError(f"test guard: {why}: {args}. Use tts_backend 'log' or monkeypatch subprocess.")
        super().__init__(args, *a, **k)


@pytest.fixture(autouse=True)
def silent_voice(tmp_path, monkeypatch):
    """Every test gets its own voice config with tts_backend 'log', never ~/.config/polybench/voice.json, and cannot call `say` / `open`."""
    cfg = tmp_path / "voice.json"
    cfg.write_text(json.dumps({"tts_backend": "log"}), encoding="utf-8")
    monkeypatch.setenv("POLYBENCH_VOICE_CONFIG", str(cfg))
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)


@pytest.fixture(autouse=True)
def isolated_polybench_dirs(tmp_path_factory, monkeypatch):
    """Project page (PP-*): no test can touch ~/.config/polybench (recent.json, ecad_requests.jsonl) or ~/polybench-data, and
    /api/reveal never runs `open -R`. Each test gets its own config and data folders."""
    base = tmp_path_factory.mktemp("pbenv")
    monkeypatch.setenv("POLYBENCH_CONFIG_DIR", str(base / "config"))
    monkeypatch.setenv("POLYBENCH_DATA", str(base / "data"))
    monkeypatch.setenv("POLYBENCH_DEMO_DIR", str(base / "demo"))          # the demo project's own folder
    monkeypatch.setenv("POLYBENCH_NO_REVEAL", "1")


# ---- operator session (D10): operator POSTs need the pb_op cookie that GET / hands to a browser. Tests use a fixed token and every
# TestClient carries it; a test that checks the enforcement clears it with `client.cookies.clear()`.
TEST_OP_TOKEN = "test-operator-token"


@pytest.fixture(autouse=True)
def operator_cookie(monkeypatch):
    monkeypatch.setenv("POLYBENCH_OP_TOKEN", TEST_OP_TOKEN)
    from starlette.testclient import TestClient
    orig = TestClient.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.cookies.set("pb_op", TEST_OP_TOKEN)
    monkeypatch.setattr(TestClient, "__init__", init)
