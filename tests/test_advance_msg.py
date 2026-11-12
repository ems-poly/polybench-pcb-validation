"""advance_msg.js: the 'nothing more to enter' message names what is still open instead of claiming everything is settled."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "src" / "polybench" / "static" / "advance_msg.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def msg(rows):
    code = f"console.log(JSON.stringify(require({json.dumps(str(JS))}).advanceSummary({json.dumps(rows)})))"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True, timeout=20).stdout)


def test_all_settled():
    assert msg([]) == "Everything in this step is settled for all boards."


def test_gated_boards_are_named_not_settled():
    m = msg([{"b": "S02", "why": "gate"}, {"b": "S02", "why": "gate"}, {"b": "S03", "why": "gate"}])
    assert m == "Nothing more to enter here: 2 boards wait on the decision gate."
    assert "settled" not in m


def test_one_gated_board_singular():
    assert msg([{"b": "S02", "why": "gate"}]) == "Nothing more to enter here: 1 board waits on the decision gate."


def test_open_cells_are_not_called_settled():
    m = msg([{"b": "S01", "why": "open"}, {"b": "S02", "why": "gate"}])
    assert m.startswith("Open cells remain here") and "1 board still has open cells" in m and "1 board waits on the decision gate" in m
