import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "src" / "polybench" / "examples" / "demo"


def reopen(engine, path):
    from polybench.engine import Engine
    from polybench.store import Journal
    engine.journal.close()
    return Engine(engine.proto, Journal(path), "tester")


LIVE_PORT = 8750          # the operator's live bench server: no test ever binds or talks to it


def free_port(env_name: str | None = None) -> int:
    """A port nothing is listening on, so parallel test runs (other worktrees, other agents) never talk to each other's servers.
    An explicit override in the environment wins, but 8750 is refused whatever the override says."""
    import os
    import socket
    if env_name and os.environ.get(env_name):
        p = int(os.environ[env_name])
        if p == LIVE_PORT:
            raise ValueError(f"{env_name}={LIVE_PORT}: {LIVE_PORT} is the live bench server, tests never use it")
        return p
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if p != LIVE_PORT:
            return p


def make_tool(directory, name: str, source: str) -> str:
    """A fake command-line tool that runs the same way on macOS, Linux and Windows (UIR-56, W1-4). `source` is Python code.

    macOS and Linux: an executable `directory/name` (a /bin/sh shim that runs this interpreter on `name.py`).
    Windows: `directory/name.cmd`, which runs this interpreter on `name.py`. The process tree is then cmd.exe, python, and whatever
    the script starts, which is also what a real tool wrapper looks like. Returns the path to hand to the plug-in."""
    import stat
    import sys
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    py = directory / f"{name}.py"
    py.write_text(source, encoding="utf-8")
    if sys.platform == "win32":
        shim = directory / f"{name}.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{py}" %*\r\n', encoding="utf-8")
    else:
        shim = directory / name
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return str(shim)


SLEEPER = "import time\ntime.sleep(30)\n"      # a tool that runs until it is stopped


def refuse_outside_connections(monkeypatch, message: str):
    """Make any socket connect to a non-loopback address raise AssertionError(message). Loopback stays open: the Windows event loop
    builds its own wake-up socket pair by connecting to 127.0.0.1, and the TestClient needs that."""
    import socket
    real_connect, real_ex = socket.socket.connect, socket.socket.connect_ex

    def loopback(addr) -> bool:
        host = addr[0] if isinstance(addr, tuple) and addr else addr
        return isinstance(host, str) and (host.startswith("127.") or host in ("::1", "localhost"))

    def connect(self, addr):
        if not loopback(addr):
            raise AssertionError(f"{message}: {addr}")
        return real_connect(self, addr)

    def connect_ex(self, addr):
        if not loopback(addr):
            raise AssertionError(f"{message}: {addr}")
        return real_ex(self, addr)

    def create_connection(address, *a, **k):
        raise AssertionError(f"{message}: {address}")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
