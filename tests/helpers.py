import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "src" / "polybench" / "examples" / "demo"


def reopen(engine, path):
    from polybench.engine import Engine
    from polybench.store import Journal
    engine.journal.close()
    return Engine(engine.proto, Journal(path), "tester")
