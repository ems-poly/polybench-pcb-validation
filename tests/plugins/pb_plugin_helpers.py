"""Helpers shared by the plug-in tests (imported by name; pytest puts this folder on sys.path)."""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _example_profile() -> Path:
    """The example DUT profile that ships with the separate polystage-bench library (a source checkout of it); a path that does
    not exist when the library is absent, so the tests that need it skip."""
    import importlib.util
    spec = importlib.util.find_spec("polystage_bench")
    for base in (Path(spec.origin).resolve().parents if spec and spec.origin else []):
        p = base / "profiles" / "examples" / "5v_usb_device.toml"
        if p.is_file():
            return p
    return Path("/nonexistent/5v_usb_device.toml")


EXAMPLE_PROFILE = _example_profile()



def run_child(plugin: str, params: dict, workdir, simulate=True, timeout=60, safe_state=False):
    """Run `python -m polybench.run_action` exactly as the core does and return the parsed JSON lines.
    stdin stays open (a closed stdin means 'cancel' to the runner)."""
    args = {"board": "S01", "params": params, "workdir": str(workdir), "simulate": simulate}
    cmd = [sys.executable, "-m", "polybench.run_action"] + (["--safe-state"] if safe_state else []) + [plugin, json.dumps(args)]
    env = {**os.environ, "PYTHONPATH": str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
    import tempfile
    with tempfile.TemporaryFile("w+") as errf:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf, text=True, env=env, encoding="utf-8", errors="replace")
        try:
            out = p.stdout.read()          # stdin stays open until the child is done: EOF on stdin means 'cancel'
            p.wait(timeout=timeout)
        finally:
            if p.poll() is None:
                p.terminate()
            p.stdin.close()
        errf.seek(0)
        err = errf.read()
    msgs = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    return msgs, err, p.returncode


def installed(name: str) -> bool:
    from polybench import registry
    try:
        registry.find_action(name)
        return True
    except KeyError:
        return False
