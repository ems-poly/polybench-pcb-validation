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
