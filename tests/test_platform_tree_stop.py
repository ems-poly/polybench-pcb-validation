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
    res = pf.stop_tree(p)
    assert res.stopped and res.survivors == []
    assert _gone(pids) == []


def test_stop_tree_without_psutil_still_ends_the_tree(tree, monkeypatch):
    """psutil is optional. Without it: the process group on macOS and Linux, `taskkill /T` on Windows."""
    monkeypatch.setattr(pf, "_psutil", None)
    p, pids = tree()
    pf.stop_tree(p)
    assert _gone(pids) == []


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows process has no session to leave; the Windows tree is covered above")
def test_a_grandchild_that_left_the_group_is_still_stopped_with_psutil(tmp_path):
    pytest.importorskip("psutil")
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
        assert "hello from the tool" in pf.stop_tree(p).output
    finally:
        pf.kill_pid_tree(p.pid)


# ---------------------------------------------------------------- a grandchild that ignores the graceful signal (QA D1, D3)

# The root dies on the graceful signal. The grandchild ignores it (SIGTERM and SIGHUP on Unix, SIGBREAK on Windows), so only the
# forced step (SIGKILL to the group, `taskkill /F`) can end it. `hold_pipe` makes it keep the tool's output pipe open, as a real
# child of OpenOCD would.
STUBBORN_TOOL = '''
import os, signal, subprocess, sys, time
from pathlib import Path
here = Path(__file__).parent
(here / "tool.pid").write_text(str(os.getpid()))
gc = ("import os, signal, sys, time\\n"
      "for n in ('SIGTERM', 'SIGBREAK', 'SIGHUP'):\\n"
      "    s = getattr(signal, n, None)\\n"
      "    if s is not None: signal.signal(s, signal.SIG_IGN)\\n"
      "from pathlib import Path\\n"
      "Path(sys.argv[1]).write_text(str(os.getpid()))\\n"
      "time.sleep(60)")
kw = {} if os.environ.get("HOLD_PIPE") == "1" else dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.Popen([sys.executable, "-c", gc, str(here / "grandchild.pid")], **kw)
time.sleep(60)
'''


@pytest.fixture(params=["no_pipe", "hold_pipe"])
def stubborn(request, tmp_path, monkeypatch):
    monkeypatch.setenv("HOLD_PIPE", "1" if request.param == "hold_pipe" else "0")
    started = []

    def start():
        exe = make_tool(tmp_path, "tool", STUBBORN_TOOL)
        p = subprocess.Popen([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", **pf.group_kwargs())
        started.append(p)
        return p, _wait_pids(tmp_path)
    yield start
    for p in started:
        pf.kill_pid_tree(p.pid)
    for n in ("tool.pid", "grandchild.pid"):
        try:
            pf.kill_pid_tree(int((tmp_path / n).read_text()))
        except (OSError, ValueError):
            pass


def test_a_grandchild_that_ignores_the_graceful_signal_is_killed_and_reported_gone(stubborn):
    """D1/D3: the root exits on the graceful signal; the stop must still wait for the whole group and force-kill what is left."""
    p, pids = stubborn()
    res = pf.stop_tree(p, grace=1.5)
    assert res.stopped and res.survivors == [], res
    assert _gone(pids) == []


def test_the_same_without_psutil(stubborn, monkeypatch):
    """The default install has no psutil: the group (Unix) or the PowerShell tree snapshot (Windows) must do the job alone."""
    monkeypatch.setattr(pf, "_psutil", None)
    p, pids = stubborn()
    res = pf.stop_tree(p, grace=1.5)
    assert res.stopped and res.survivors == [], res
    assert _gone(pids) == []


def test_a_survivor_is_reported_with_its_pid_not_swallowed(stubborn, monkeypatch):
    """D2: when the forced step does nothing (the OS refuses), stop_tree says so and names the process."""
    p, pids = stubborn()
    gc = pids[1]
    if sys.platform == "win32":
        real = subprocess

        class Shim:
            def __getattr__(self, name):
                return getattr(real, name)

            @staticmethod
            def run(cmd, *a, **k):
                if cmd and cmd[0] == "taskkill":
                    return real.CompletedProcess(cmd, 1, b"", b"refused")
                return real.run(cmd, *a, **k)
        monkeypatch.setattr(pf, "subprocess", Shim())
        monkeypatch.setattr(pf, "_psutil", None)
    else:
        real_signal = pf._group_signal
        monkeypatch.setattr(pf, "_group_signal", lambda pid, sig: None if sig == pf.signal.SIGKILL else real_signal(pid, sig))
        monkeypatch.setattr(pf, "_psutil", None)
    res = pf.stop_tree(p, grace=1.0)
    try:
        assert not res.stopped and gc in [pid for pid, _ in res.survivors], res
        text = pf.survivor_message("OpenOCD", res)
        assert f"pid {gc}" in text and "may still be running" in text and "clean" in text
    finally:
        monkeypatch.undo()
        pf.kill_pid_tree(p.pid)
        pf.kill_pid_tree(gc)


def test_no_psutil_environment_really_has_no_psutil():
    """The CI step `no psutil` uninstalls it and sets PB_NO_PSUTIL=1: then the fallback is what runs, not a monkeypatch (D6)."""
    import os
    if os.environ.get("PB_NO_PSUTIL") != "1":
        pytest.skip("only in the CI step that uninstalls psutil")
    with pytest.raises(ImportError):
        import psutil  # noqa: F401
    assert pf.have_psutil() is False


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


# ---------------------------------------------------------------- a survivor is never reported as a clean cancel (D2)

def _refusing_stop(monkeypatch):
    res = pf.StopResult("", [(424242, "openocd -f interface/cmsis-dap.cfg")])
    monkeypatch.setattr(pf, "stop_tree", lambda proc, grace=3.0: (pf.kill_pid_tree(proc.pid), proc.wait(), res)[2])


def test_openocd_cancel_with_a_survivor_is_an_error_naming_the_pid(tmp_path, monkeypatch):
    import polybench.plugins.openocd as ocd
    from polybench.plugins._util import PluginError
    from polybench.testing import RecordingContext
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    _refusing_stop(monkeypatch)
    exe = make_tool(tmp_path / "tool", "openocd", "import time\ntime.sleep(60)\n")
    img = tmp_path / "a.hex"
    img.write_text(":00000001FF\n", encoding="utf-8")
    ctx = RecordingContext(params={"openocd": exe, "image": str(img), "timeout_s": 60}, workdir=tmp_path / "run", simulate=False)
    ctx._cancel = True
    with pytest.raises(PluginError) as e:
        ocd.OpenocdFlash().run(ctx)
    msg = str(e.value)
    assert "pid 424242" in msg and "may still be running" in msg and "clean" in msg and not msg.startswith("cancelled")
    assert (tmp_path / "run" / ocd.PID_FILE).exists()          # kept, so the next run and safe_state report the survivor too


def test_openocd_timeout_with_a_survivor_is_an_error_naming_the_pid(tmp_path, monkeypatch):
    import polybench.plugins.openocd as ocd
    from polybench.plugins._util import PluginError
    from polybench.testing import RecordingContext
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    _refusing_stop(monkeypatch)
    exe = make_tool(tmp_path / "tool", "openocd", "import time\ntime.sleep(60)\n")
    img = tmp_path / "a.hex"
    img.write_text(":00000001FF\n", encoding="utf-8")
    ctx = RecordingContext(params={"openocd": exe, "image": str(img), "timeout_s": 1}, workdir=tmp_path / "run", simulate=False)
    with pytest.raises(PluginError, match=r"may still be running.*pid 424242"):
        ocd.OpenocdFlash().run(ctx)


def test_esptool_cancel_with_a_survivor_is_an_error_naming_the_pid(fake_esptool, monkeypatch):
    from polybench.plugins import esp32
    from polybench.testing import RecordingContext
    _refusing_stop(monkeypatch)
    ctx = RecordingContext(simulate=False)
    _cancel_after(ctx, fake_esptool)
    with pytest.raises(esp32.EspError, match=r"may still be running.*pid 424242"):
        esp32.run_esptool(["chip_id"], timeout=120, ctx=ctx)
    for n in ("tool.pid", "grandchild.pid"):
        try:
            pf.kill_pid_tree(int((fake_esptool / n).read_text()))
        except (OSError, ValueError):
            pass
