from pathlib import Path
from types import SimpleNamespace

from polybench_plugin_example.plugin import ExampleEcho


def test_selftest_and_safe_state():
    p = ExampleEcho()
    assert p.selftest() is True
    assert "holds nothing" in p.safe_state()


def test_proposes_only_mapped_key(tmp_path: Path):
    got = []
    ctx = SimpleNamespace(board="S01", params={"value": 5, "map": {"reading": "T01"}}, workdir=tmp_path, simulate=True,
                          cancelled=lambda: False, log=lambda *a, **k: None,
                          propose=lambda *a, **k: got.append(a))
    ExampleEcho().run(ctx)
    assert got and got[0][0] == "T01" and got[0][1] == "5.0"
