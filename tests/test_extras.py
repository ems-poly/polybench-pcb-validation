"""Optional extras degrade cleanly (C1/C2). HOST-ONLY.

A bare install has no cv2, PIL, numpy, zxingcpp, openpyxl or sounddevice. These tests block those imports in a child interpreter, so they
prove the same on a machine that has the extras installed: the app starts, every page route answers, and what needs an extra says which one."""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from polybench import extras

REPO = Path(__file__).resolve().parents[1]


def test_message_names_the_extra():
    assert extras.message("aoi") == "install `polybench-pcb-validation[aoi]` to use this (the Visual step AOI and photo import)"


def test_need_raises_when_a_module_is_missing(monkeypatch):
    monkeypatch.setattr(extras, "missing", lambda extra: ["cv2"])
    with pytest.raises(extras.MissingExtra) as e:
        extras.need("aoi")
    assert e.value.extra == "aoi" and "[aoi]" in str(e.value) and e.value.missing == ("cv2",)
    assert extras.check("aoi") is False


def test_lazy_module_imports_only_on_use_and_names_the_extra():
    lz = extras.lazy("no_such_module_for_polybench_tests", "scanner")        # creating it imports nothing
    with pytest.raises(extras.MissingExtra) as e:
        lz.anything
    assert "[scanner]" in str(e.value)
    ok = extras.lazy("json", "scanner")
    assert ok.dumps({"a": 1}) == '{"a": 1}'


CHILD = textwrap.dedent('''
    import json, sys
    for m in ("cv2", "numpy", "PIL", "zxingcpp", "openpyxl", "sounddevice", "pillow_heif", "mlx_whisper"):
        sys.modules[m] = None                      # import raises ImportError, as on a bare install
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    import tempfile, pathlib
    sess = Session(load(sys.argv[1]), pathlib.Path(tempfile.mkdtemp()) / "d", "B", True, 3, None, "tester")
    out = {}
    with TestClient(create_app(sess), raise_server_exceptions=False) as c:
        c.get("/")
        for path in ("/", "/api/state", "/api/session", "/api/scanner/state?hb=1", "/api/voice/status", "/api/ecad/status", "/api/project",
                     "/api/aoi/used", "/aoi/review/", "/aoi/review/api/state", "/aoi/review/api/boardview"):
            r = c.get(path)
            out[path] = [r.status_code, r.text[:300]]
        r = c.post("/api/aoi/photos?name=a-S01.jpg", content=b"x")
        out["POST /api/aoi/photos"] = [r.status_code, r.text[:300]]
    print(json.dumps(out))
''')


def test_bare_install_starts_and_every_route_answers_without_a_500():
    p = subprocess.run([sys.executable, "-c", CHILD, str(REPO / "src/polybench/examples/demo/demo.pcbv.yaml")], capture_output=True, text=True, timeout=120,
                       env={**__import__("os").environ, "PYTHONPATH": str(REPO / "src")})
    assert p.returncode == 0, p.stderr[-1500:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    five = {k: v for k, v in out.items() if v[0] >= 500}
    assert not five, five
    assert out["/"][0] == 200 and out["/api/state"][0] == 200
    for k in ("/api/aoi/used", "/aoi/review/", "/aoi/review/api/state", "POST /api/aoi/photos"):
        assert out[k][0] == 424 and "polybench-pcb-validation[aoi]" in out[k][1], (k, out[k])
    assert "Traceback" not in p.stderr, p.stderr[-1500:]


def test_selftest_runs_from_package_data():
    """C11: the demo the self-test reads ships inside the package."""
    from importlib import resources
    base = resources.files("polybench") / "examples" / "demo"
    assert (base / "demo.pcbv.yaml").is_file() and (base / "visual.yaml").is_file()
    from polybench import selftest
    assert Path(str(base / "demo.pcbv.yaml")) == selftest.DEMO
