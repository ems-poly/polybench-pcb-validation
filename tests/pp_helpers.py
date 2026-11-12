"""Shared helpers for the Project page tests (tests/test_project_page*.py). DEMO protocol only, sessions always simulated."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from helpers import DEMO, ROOT

DEMO_PROTO = DEMO / "demo.pcbv.yaml"
DATA = ROOT / "tests" / "data"
CSV_BOM = DATA / "demo_purchasing_bom.csv"
CSV_BOM_DUP = DATA / "demo_purchasing_bom_dup.csv"


def args(protocol=None, data_dir=None, batch=None, ecad=None, simulate=True, boards=3, operator="tester"):
    """The argparse namespace cmd_serve gets (same field names as cli.main's parser)."""
    return argparse.Namespace(protocol=str(protocol) if protocol else None, data_dir=str(data_dir) if data_dir else None,
                              batch=batch, ecad=ecad, simulate=simulate, boards=boards, operator=operator)


def start(a):
    """What cmd_serve does before uvicorn: resolve_start, open the session, write the adopted settings, touch the recent list."""
    from polybench import workspace as wslib
    from polybench.cli import resolve_start
    from polybench.server import open_workspace_session
    ws, batch, ecad_o, proto_o, adopted = resolve_start(a)
    if adopted:
        ws.persist = False
    sess = open_workspace_session(ws, batch, a.simulate, a.boards, a.operator or "", ecad_o, proto_o or (a.protocol if adopted else None))
    if adopted:
        ws.persist = True
        sess.seed_document_from_journal()
        ws.save(name=ws.get("name") or sess.proto.data["design"]["name"], current_batch=sess.batch)
        sess.engine.workspace_event("project_settings", {"change": "adopted", "file": str(ws.file), "batch": sess.batch,
                                                         "protocol_path": ws.get("protocol_path"), "ecad_program": ws.ecad_program()})
    wslib.recent_touch(ws.dir, ws.name, sess.batch)
    return sess


def new_project_session(project_dir, batch="B1", ecad=None, protocol=DEMO_PROTO):
    """A simulated session on a fresh project folder (adopted on first start, like `polybench --protocol X --data-dir D`)."""
    return start(args(protocol, project_dir, batch, ecad))


def client(sess):
    from fastapi.testclient import TestClient
    from polybench.server import create_app
    return TestClient(create_app(sess))


def cur(c):
    """The Session currently behind a TestClient (after a project swap)."""
    return c.app.state.holder["cur"]


def events(path_or_sess, type_=None) -> list[dict]:
    p = Path(path_or_sess.journal.path if hasattr(path_or_sess, "journal") else path_or_sess)
    out = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [e for e in out if type_ is None or e["type"] == type_]


def settings(project_dir) -> dict:
    return json.loads((Path(project_dir) / "polybench-project.json").read_text(encoding="utf-8"))


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def tree(d) -> dict[str, str]:
    """Every file under d (relative path -> sha256), to prove nothing was moved, renamed or rewritten."""
    d = Path(d)
    return {str(f.relative_to(d)): sha(f) for f in sorted(d.rglob("*")) if f.is_file()}


def legacy_folder(root: Path, batches=("L1",)) -> Path:
    """A project folder in the layout before the settings file: batch sub-folders with journals, no polybench-project.json."""
    from polybench.protocol import load
    from polybench.server import Session
    root.mkdir(parents=True, exist_ok=True)
    proto = load(DEMO_PROTO)
    for b in batches:
        s = Session(proto, root, b, True, 3, None)
        s.engine.entry("S01", "V01", "y")
        s.journal.close()
    assert not (root / "polybench-project.json").exists()
    return root


def protocol_copy(tmp_path: Path, drop_bom=False) -> Path:
    """A copy of the DEMO protocol folder (optionally without the `bom:` block in assembly.yaml)."""
    d = tmp_path / "proto"
    shutil.copytree(DEMO, d)
    if drop_bom:
        a = d / "assembly.yaml"
        lines = a.read_text(encoding="utf-8").splitlines(keepends=True)
        i = next(n for n, x in enumerate(lines) if x.startswith("bom:"))
        j = next((n for n in range(i + 1, len(lines)) if lines[n].strip() and not lines[n].startswith((" ", "-", "#"))), len(lines))
        a.write_text("".join(lines[:i] + lines[j:]), encoding="utf-8")
    return d / "demo.pcbv.yaml"
