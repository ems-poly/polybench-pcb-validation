"""KC-43: user-visible text names the active ECAD program (Fusion or KiCad) and voice accepts "show it in KiCad"."""
from __future__ import annotations

import ast
import io
import json
import re
import tokenize

import pytest

from helpers import ROOT

PKG = ROOT / "src" / "polybench"


class KicadStub:
    """The smallest bridge whose name makes Session.ecad_program say KiCad."""
    name = "kicad"
    simulate = True
    aliases: dict = {}

    @property
    def link(self):
        from polybench.kicad.bridge import simulated_link
        return simulated_link()


# ------------------------------------------------------------------ (i) Session.ecad_program
def _session(tmp_path):
    from polybench.protocol import load
    from polybench.server import Session
    return Session(load(ROOT / "src/polybench/examples/demo/demo.pcbv.yaml"), tmp_path, "b", True, 3, None)


def test_session_ecad_program_default_and_kicad(tmp_path):
    sess = _session(tmp_path)
    assert sess.ecad_program == "Fusion"                       # the simulated bridge is not a KiCad one
    assert sess.snapshot()["ecad_program"] == "Fusion"
    sess.bridge = KicadStub()
    assert sess.ecad_program == "KiCad"
    assert sess.snapshot()["ecad_program"] == "KiCad" and "ecad_note" in sess.snapshot()
    sess.bridge = object()                                     # no name at all: Fusion
    assert sess.ecad_program == "Fusion"


def test_session_messages_follow_the_program(tmp_path):
    import asyncio
    from polybench.engine import CmdError

    class Bridge(KicadStub):
        editor, names = "none", []

        def status(self):
            return {"connected": True, "document": "x", "editor": self.editor, "program": "KiCad"}

        def designators(self):
            return self.names

    sess = _session(tmp_path)
    sess.bridge = Bridge()

    def msg(expect=None):
        with pytest.raises(CmdError) as e:
            asyncio.run(sess.project_select(expect))
        return str(e.value)

    texts = [msg()]
    sess.bridge.editor = "board"
    texts.append(msg())
    sess.bridge.names = ["U1"]
    texts.append(msg("not-the-fingerprint"))
    assert all("KiCad" in t and "Fusion" not in t for t in texts), texts
    assert "in Fusion, then check again" in asyncio.run(_raises(sess))


async def _raises(sess):
    from polybench.engine import CmdError
    class B:
        name = "fusion"
        def status(self):
            return {"connected": True, "document": "x", "editor": "none"}
    sess.bridge = B()
    try:
        await sess.project_select()
    except CmdError as e:
        return str(e)


# ------------------------------------------------------------------ (ii) voice
def _view():
    from polybench.voice.understand import BenchView
    tests = [{"id": "U01", "title": "Rail", "kind": "number", "unit": "V", "expected": "", "options": [], "limit": None}]
    boards = [f"S{i:02d}" for i in range(1, 4)]
    return BenchView(step_id="t1", tests=tests, boards=boards, all_boards=boards, test_id="U01", board="S01")


@pytest.mark.parametrize("said", ["show it in KiCad", "show in KiCad", "highlight in KiCad", "show it in key cad",
                                  "show it in kay cad", "Show it in KiCad."])
def test_voice_kicad_maps_to_highlight_like_fusion(said):
    from polybench.voice.understand import interpret
    fusion = interpret("show it in Fusion", _view())
    kicad = interpret(said, _view())
    assert fusion["intent"] == "highlight" and kicad["intent"] == fusion["intent"]


def test_voice_highlight_reply_names_the_program():
    from polybench.voice import controller
    src = (PKG / "voice" / "controller.py").read_text()
    assert "ecad_program" in src and '" in Fusion"' not in src


# ------------------------------------------------------------------ (iii) AssemblyService with program="KiCad"
@pytest.fixture
def svc_rig(tmp_path, monkeypatch):
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "CURRENT").write_text("")
    monkeypatch.setenv("POLYBENCH_SCANNER_FAKE_FRAMES", str(frames))
    monkeypatch.setenv("ASCAN_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("POLYBENCH_FUSION_URL", raising=False)
    return tmp_path


def _strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _strings(k, out)
            _strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings(v, out)
    return out


def test_assembly_service_kicad_has_no_fusion_text(svc_rig):
    from fastapi.testclient import TestClient
    from polybench.server import create_app
    sess = _session(svc_rig / "d")
    sess.bridge = KicadStub()
    seen: list[str] = []
    with TestClient(create_app(sess)) as c:
        st = c.get("/api/scanner/state?hb=1").json()
        svc = sess.scanner
        assert svc.program == "KiCad" and st["program"] == "KiCad"      # get_scanner hands the session's program over
        seen += _strings(st, [])
        for action, value in (("pause", None), ("resume", None), ("rescan", None)):
            r = c.post("/api/scanner/control", json={"action": action, "value": value})
            seen += _strings(r.json(), [])
            seen += _strings(c.get("/api/scanner/state?hb=1").json(), [])
        seen += _strings(svc.select_line(2), [])
        seen += _strings(svc.select_line(999), [])
        seen += _strings(svc.clear_selection(), [])
        svc.set_ecad_off("skipped")
        seen += _strings(svc.select_line(2), [])
        seen += _strings(svc.state(), [])
        svc.set_ecad_off(None)
        # the failure paths that need a real stall are asserted on what builds them
        seen += [svc.fworker.paused_error, svc.fworker.circuit_error, *svc.pending_reasons]
        svc.pick_err = None
        svc.rescan_boards()
        seen += _strings(svc.state(), [])
    bad = [s for s in seen if "Fusion" in s or "FUSION" in s]
    assert not bad, bad
    assert any("KiCad" in s for s in seen)


def test_assembly_service_fusion_text_is_unchanged():
    from polybench.assembly_scanner.service import AssemblyService, FusionWorker
    assert AssemblyService.program == "Fusion"
    w = FusionWorker()
    assert w.paused_error == FusionWorker.PAUSED_ERROR == "Fusion request skipped while Assembly Scanner is paused"
    assert w.circuit_error == FusionWorker.CIRCUIT_ERROR == \
        "Fusion requests stopped after a recursion error; use Rescan boards to retry once Fusion is ready"
    assert FusionWorker(program_fn=lambda: "KiCad").paused_error == "KiCad request skipped while Assembly Scanner is paused"
    assert "retry once KiCad is ready" in FusionWorker(program_fn=lambda: "KiCad").circuit_error


def test_hud_text_follows_the_program():
    from polybench.assembly_scanner.hud import hud_sections
    top, _ = hud_sections(None, None, {"program": "KiCad", "fusion_error": "no answer", "board_name": None})
    assert any(t.startswith("KICAD  ") for t, _l, _s in top)
    top, _ = hud_sections(None, None, {"fusion_error": "no answer", "board_name": None})
    assert any(t.startswith("FUSION  ") for t, _l, _s in top)


# ------------------------------------------------------------------ (iv) no new hard-coded "Fusion" in operator-facing text
PY_FILES = ["server.py", "assembly_scanner/service.py", "assembly_scanner/hud.py", "voice/controller.py", "voice/understand.py",
            "voice/stt.py"]
PY_ALLOWED_STRINGS = {"Fusion"}                      # the default program name, nothing longer
JS_ALLOWED = (
    "ecadName() !== 'Fusion'", "ecadName() === 'Fusion'", "|| 'Fusion'",            # the helper and its default
    # the Fusion-only set-up help (shown only when the program is Fusion): the MCP endpoint hint and the Fusion restart steps
    "<b>Fusion is not reachable.</b>", "Start Autodesk Fusion", "Check that Fusion's MCP", "wait for a change in Fusion first",
    "restart Fusion's MCP", "also shows a Fusion error", "If Fusion itself must be restarted", "If Fusion cannot be used",
    "In Fusion, click the board's Electronics tab", "does not report Fusion",         # Fusion-branch of a program-aware sentence
)


def _py_literals(path):
    src = path.read_text()
    tree = ast.parse(src)
    doc_lines = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) \
                and isinstance(n.body[0].value.value, str):
            doc_lines.add(n.body[0].lineno)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)) and re.search("Fusion|FUSION", tok.string):
            if tok.start[0] in doc_lines:
                continue
            out.append((tok.start[0], tok.string))
    return out


@pytest.mark.parametrize("rel", PY_FILES)
def test_no_new_hard_coded_fusion_in_python_strings(rel):
    path = PKG / rel
    stray = []
    for line, s in _py_literals(path):
        body = s.strip("'\"")
        if body.strip() in PY_ALLOWED_STRINGS:
            continue
        # the %-style class constants that render the Fusion default
        if rel.endswith("service.py") and body in ("Fusion",):
            continue
        stray.append((line, s))
    assert not stray, f"{rel}: hard-coded Fusion text, use the program name: {stray}"


def test_no_new_hard_coded_fusion_in_app_js():
    text = (PKG / "static" / "app.js").read_text()
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    stray = []
    for i, line in enumerate(text.splitlines(), 1):
        code = re.sub(r"(^|\s)//.*$", "", line)
        if re.search("Fusion|FUSION", code) and not any(a in code for a in JS_ALLOWED):
            stray.append((i, code.strip()[:120]))
    assert not stray, f"app.js: hard-coded Fusion text, use ecadName(): {stray}"
