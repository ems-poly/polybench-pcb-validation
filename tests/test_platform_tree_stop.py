"""WIN-S1 (UIR-56, W1-2): cancelling an action, or the app going away, stops the action's whole process tree.

OpenOCD and esptool are the programmers: if a cancel left one running, the probe or the serial port would stay held and a board
could be half programmed. These tests start a tool that starts a grandchild, stop it the way the plug-ins do, and assert that
both are gone. They run on macOS, Linux and Windows (the fake tool is a Python script, see helpers.make_tool)."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from helpers import make_tool

from polybench import platform as pf

# A tool that starts a grandchild and then waits. Both write their pid, so the test knows who to look for.
TREE_TOOL = '''
import os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).parent
(here / "tool.pid").write_text(str(os.getpid()))
g = subprocess.Popen([sys.executable, "-c",
    "import os, sys, time\\nfrom pathlib import Path\\nPath(sys.argv[1]).write_text(str(os.getpid()))\\ntime.sleep(60)",
    str(here / "grandchild.pid")])
time.sleep(60)
'''


def _wait_pids(d: Path, names=("tool.pid", "grandchild.pid"), seconds=20.0) -> list[int]:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if all((d / n).exists() and (d / n).read_text(encoding="utf-8").strip() for n in names):
            return [int((d / n).read_text(encoding="utf-8")) for n in names]
        time.sleep(0.1)
    raise AssertionError(f"the tool did not write {names} in {seconds} s")


def _gone(pids, seconds=10.0) -> list[int]:
    """The pids still alive after `seconds` (empty is the pass)."""
    end = time.monotonic() + seconds
    alive = list(pids)
    while alive and time.monotonic() < end:
        alive = [p for p in alive if pf.pid_alive(p)]
        if alive:
            time.sleep(0.1)
    return alive


@pytest.fixture
def tree(tmp_path):
    """Start the tool in its own group the way the plug-ins do. Cleans up whatever a failing test leaves."""
    started = []

    def start():
        exe = make_tool(tmp_path, "tool", TREE_TOOL)
        p = subprocess.Popen([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", **pf.group_kwargs())
        started.append(p)
        return p, _wait_pids(tmp_path)

    yield start
    for p in started:
        pf.kill_pid_tree(p.pid)
    for n in ("tool.pid", "grandchild.pid"):
        try:
            pid = int((tmp_path / n).read_text(encoding="utf-8"))
            pf.kill_pid_tree(pid)
        except (OSError, ValueError):
            pass


def test_stop_tree_ends_the_tool_and_its_grandchild(tree):
    p, pids = tree()
    assert all(pf.pid_alive(x) for x in pids)
    pf.stop_tree(p)
    assert _gone(pids) == []


def test_stop_tree_without_psutil_still_ends_the_tree(tree, monkeypatch):
    """psutil is optional. Without it: the process group on macOS and Linux, `taskkill /T` on Windows."""
    monkeypatch.setattr(pf, "_psutil", None)
    p, pids = tree()
    pf.stop_tree(p)
    assert _gone(pids) == []


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows process has no session to leave; the Windows tree is covered above")
def test_a_grandchild_that_left_the_group_is_still_stopped_with_psutil(tmp_path):
    """A wrapper that starts its programmer in a new session would escape a group kill; the process tree catches it."""
    script = ("import os, subprocess, sys, time\nfrom pathlib import Path\nhere = Path(sys.argv[1])\n"
              "(here / 'tool.pid').write_text(str(os.getpid()))\n"
              "g = subprocess.Popen([sys.executable, '-c', 'import os, sys, time\\nfrom pathlib import Path\\n"
              "Path(sys.argv[1]).write_text(str(os.getpid()))\\ntime.sleep(60)', str(here / 'grandchild.pid')], start_new_session=True)\n"
              "time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], **pf.group_kwargs())
    try:
        pids = _wait_pids(tmp_path)
        pf.stop_tree(p)
        assert _gone(pids) == []
    finally:
        for x in _wait_pids(tmp_path):
            pf.kill_pid_tree(x)


def test_stop_tree_returns_the_output_so_far(tmp_path):
    exe = make_tool(tmp_path, "chatty", "import sys, time\nprint('hello from the tool', flush=True)\ntime.sleep(60)\n")
    p = subprocess.Popen([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", **pf.group_kwargs())
    time.sleep(1.5)
    try:
        assert "hello from the tool" in pf.stop_tree(p)
    finally:
        pf.kill_pid_tree(p.pid)


# ---------------------------------------------------------------- through the real plug-ins

def _cancel_after(ctx, pidfiles: Path):
    def go():
        _wait_pids(pidfiles)
        ctx._cancel = True
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def test_cancelling_openocd_stops_it_and_its_grandchild(tmp_path, monkeypatch):
    import polybench.plugins.openocd as ocd
    from polybench.plugins._util import PluginError
    from polybench.testing import RecordingContext
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    exe = make_tool(tmp_path / "tool", "openocd", TREE_TOOL)
    img = tmp_path / "a.hex"
    img.write_text(":00000001FF\n", encoding="utf-8")
    ctx = RecordingContext(params={"openocd": exe, "image": str(img), "timeout_s": 120}, workdir=tmp_path / "run", simulate=False)
    _cancel_after(ctx, tmp_path / "tool")
    t0 = time.monotonic()
    with pytest.raises(PluginError, match="cancelled"):
        ocd.OpenocdFlash().run(ctx)
    assert time.monotonic() - t0 < 60
    pids = _wait_pids(tmp_path / "tool")
    assert _gone(pids) == []
    assert not (tmp_path / "run" / ocd.PID_FILE).exists()


def test_openocd_timeout_stops_its_grandchild_too(tmp_path, monkeypatch):
    import polybench.plugins.openocd as ocd
    from polybench.plugins._util import PluginError
    from polybench.testing import RecordingContext
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    exe = make_tool(tmp_path / "tool", "openocd", TREE_TOOL)
    img = tmp_path / "a.hex"
    img.write_text(":00000001FF\n", encoding="utf-8")
    ctx = RecordingContext(params={"openocd": exe, "image": str(img), "timeout_s": 4}, workdir=tmp_path / "run", simulate=False)
    with pytest.raises(PluginError, match="timed out"):
        ocd.OpenocdFlash().run(ctx)
    assert _gone(_wait_pids(tmp_path / "tool")) == []


FAKE_ESPTOOL = '''
import os, subprocess, sys, time
from pathlib import Path
here = Path(os.environ["FAKE_TOOL_DIR"])
(here / "tool.pid").write_text(str(os.getpid()))
subprocess.Popen([sys.executable, "-c",
    "import os, sys, time\\nfrom pathlib import Path\\nPath(sys.argv[1]).write_text(str(os.getpid()))\\ntime.sleep(60)",
    str(here / "grandchild.pid")])
time.sleep(60)
'''


@pytest.fixture
def fake_esptool(tmp_path, monkeypatch):
    pkg = tmp_path / "site" / "esptool"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(FAKE_ESPTOOL, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "site") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("FAKE_TOOL_DIR", str(tmp_path / "tool"))
    (tmp_path / "tool").mkdir()
    return tmp_path / "tool"


def test_cancelling_esptool_stops_it_and_its_grandchild(fake_esptool):
    from polybench.plugins import esp32
    from polybench.testing import RecordingContext
    ctx = RecordingContext(simulate=False)
    _cancel_after(ctx, fake_esptool)
    assert esp32.run_esptool(["chip_id"], timeout=120, ctx=ctx) is None          # None means: cancelled, esptool stopped
    assert _gone(_wait_pids(fake_esptool)) == []


# ---------------------------------------------------------------- the app goes away (stdin closes) or cancels, in a real child

CHILD_ACTION = (
    "from polybench import demo_actions\n"
    "class Hold:\n"
    "    name = 'test.hold_esptool'\n    capabilities = []\n"
    "    def run(self, ctx):\n"
    "        from polybench.plugins import esp32\n"
    "        esp32.run_esptool(['chip_id'], timeout=120, ctx=ctx)\n"
    "    def safe_state(self): return 'ok'\n"
    "demo_actions.ACTIONS['test.hold_esptool'] = Hold\n")


def _start_child(tmp_path, fake_esptool):
    (tmp_path / "sitecustomize.py").write_text(CHILD_ACTION, encoding="utf-8")
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**pf.child_env(), "PYTHONPATH": os.pathsep.join([str(tmp_path), str(tmp_path / "site"), repo_src]),
           "FAKE_TOOL_DIR": str(fake_esptool)}
    args = {"board": "S01", "params": {}, "workdir": str(tmp_path / "w"), "simulate": False}
    return subprocess.Popen([sys.executable, "-m", "polybench.run_action", "test.hold_esptool", json.dumps(args)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            encoding="utf-8")


@pytest.mark.parametrize("how", ["cancel_message", "stdin_closes"])
def test_the_action_child_stops_the_programmer_tree_on_cancel_and_when_the_app_goes_away(tmp_path, fake_esptool, how):
    """`cancel` is the operator's button; stdin EOF is what the child sees when the app exits or dies (the child protocol)."""
    from polybench.testing import RecordingContext  # noqa: F401  (import check only)
    child = _start_child(tmp_path, fake_esptool)
    try:
        pids = _wait_pids(fake_esptool)
        if how == "cancel_message":
            child.stdin.write(json.dumps({"t": "cancel"}) + "\n")
            child.stdin.flush()
        else:
            child.stdin.close()
            child.stdin = None            # communicate() must not flush a closed pipe
        out, err = child.communicate(timeout=60)
        assert _gone(pids) == [], (out, err)
        msgs = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
        assert msgs and msgs[-1]["t"] == "done" and msgs[-1].get("cancelled") is True, (msgs, err)
    finally:
        if child.poll() is None:
            pf.kill_pid_tree(child.pid)
        for n in ("tool.pid", "grandchild.pid"):
            try:
                pf.kill_pid_tree(int((fake_esptool / n).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
