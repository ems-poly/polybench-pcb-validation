"""C8: `python -m polybench.run_action` loads run_action as __main__, so a Cancelled defined there is a different class from the
one a plugin imports. Cancelled lives in polybench.plugin_api; a plugin raising it ends as a cancelled `done`, not an error."""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run(tmp_path, body):
    (tmp_path / "sitecustomize.py").write_text(
        "from polybench import demo_actions\n"
        "from polybench.plugin_api import Cancelled\n"
        "class Raiser:\n    name = 'test.raise_cancelled'\n    capabilities = []\n"
        f"    def run(self, ctx):\n{body}\n"
        "    def safe_state(self): return 'ok'\n"
        "demo_actions.ACTIONS['test.raise_cancelled'] = Raiser\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path) + os.pathsep + str(REPO / "src")}
    args = {"board": "S01", "params": {}, "workdir": str(tmp_path / "w"), "simulate": True}
    p = subprocess.Popen([sys.executable, "-m", "polybench.run_action", "test.raise_cancelled", json.dumps(args)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    out, err = p.communicate(timeout=30)
    return [json.loads(x) for x in out.splitlines() if x.strip()], err, p.returncode


def test_plugin_raising_cancelled_ends_cancelled_not_error(tmp_path):
    msgs, err, rc = _run(tmp_path, "        raise Cancelled('operator cancelled')")
    assert rc == 0, err
    assert msgs[-1]["t"] == "done" and msgs[-1]["cancelled"] is True, msgs
    assert not [m for m in msgs if m["t"] == "error"]


def test_cancelled_is_one_class_in_both_modules():
    import polybench.plugin_api as api
    import polybench.run_action as ra
    assert ra.Cancelled is api.Cancelled
