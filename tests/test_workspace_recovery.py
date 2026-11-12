"""Last-good-plan recovery happens where the protocol is loaded (open_workspace_session), so a no-flag restart of a saved project recovers."""
import shutil

from helpers import DEMO, ROOT
from polybench import workspace as wslib
from polybench.server import open_workspace_session


def test_open_workspace_session_recovers_broken_master(tmp_path):
    proj = tmp_path / "demo"
    shutil.copytree(DEMO, proj)
    top = proj / "demo.pcbv.yaml"
    plan = proj / "PLAN.md"
    plan.write_text((ROOT / "tests" / "data" / "DEMO_TEST_PLAN.md").read_text())
    top.write_text(top.read_text() + "\ntest_plan: {file: PLAN.md, generate: true}\n")
    data = tmp_path / "data"
    hist = data / "b1" / "ai" / "plan_history"
    hist.mkdir(parents=True)
    (hist / "good.md").write_text(plan.read_text())
    plan.write_text(plan.read_text() + "\n```\n")                     # unclosed code fence: the master no longer loads
    ws = wslib.Workspace(data)
    ws.settings = {"schema": wslib.SCHEMA, "name": None, "created": wslib.now(), "updated": wslib.now(), "protocol_path": str(top),
                   "bom_path": None, "ecad": {"program": "fusion", "document": None, "documents": {}}, "current_batch": "b1"}
    ws.persist = False
    sess = open_workspace_session(ws, "b1", True, 2)
    assert sess.recovery and sess.recovery["restored_from"].endswith("good.md")
    assert not sess.proto.errors
