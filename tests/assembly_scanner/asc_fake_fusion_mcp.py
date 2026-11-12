"""A tiny local stand-in for the Autodesk Fusion MCP server (streamable HTTP, JSON-RPC 2.0).

It mimics the real server's handshake as probed on 7 Oct 2026:

    POST /mcp  initialize                  -> result + `Mcp-Session-Id` response header
    POST /mcp  notifications/initialized   (with the session header) -> 202, empty body
    POST /mcp  tools/list | tools/call     (with the session header) -> JSON result

and serves the real server's tool list from `fixtures/fusion/tools_list.json`, validating
`tools/call` arguments against it (tool name, required keys, `featureType` enum, a script
must define `run(_context)`). Every request is recorded so tests can assert on traffic,
e.g. "a highlight went out exactly once" or "NOT_IN_BOM sent nothing".

It never talks to the real Fusion MCP at 127.0.0.1:27182.

`fusion_mcp_execute` scripts are really executed, against `SimulatedFusion`: a minimal fake
of the Fusion API surface (adsk.core.Application, adsk.electron.Board / Schematic /
BoardDocument, elements, parts, selectObjects / clearSelection) holding a board with the
demo designators. Its `selected` attribute is what Fusion would show highlighted, so a
test can assert "the highlight is still R4,R5". The reply text uses the envelope the real
server returns for scripts: {"success": true, "message": "<printed output>"} or
{"success": false, "error": "<traceback>"}. A test that needs other output sets
`server.handler = fn(tool_name, arguments) -> (text, is_error)`.

Usage:
    with FakeFusionMCP() as fake:
        client = FusionClient(fake.url)
        ...
        assert len(fake.tool_calls()) == 1
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import traceback
import types
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

TOOLS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fusion" / "tools_list.json"

Handler = Callable[[str, dict], "tuple[str, bool]"]


@dataclass
class Call:
    t: float
    method: str
    id: Any
    params: dict
    session: str | None
    headers: dict = field(default_factory=dict)
    http_status: int = 200

    # convenience for tools/call
    @property
    def tool(self) -> str | None:
        return self.params.get("name") if self.method == "tools/call" else None

    @property
    def arguments(self) -> dict:
        return self.params.get("arguments") or {} if self.method == "tools/call" else {}

    @property
    def script(self) -> str:
        return (self.arguments.get("object") or {}).get("script", "") or ""

    @property
    def read_only(self) -> bool:
        return bool((self.arguments.get("object") or {}).get("readOnly", False))

    def mentions(self, *tokens: str) -> bool:
        """True if every token appears in the JSON of this call's arguments."""
        blob = json.dumps(self.arguments)
        return all(tok in blob for tok in tokens)


def load_tools() -> list[dict]:
    return json.loads(TOOLS_FIXTURE.read_text())["tools"]


class FakeFusionMCP:
    """Start with `start()` or as a context manager. `url` is the /mcp endpoint."""

    def __init__(self, *, sse: bool = False, strict_accept: bool = True,
                 handler: Handler | None = None, fusion: "SimulatedFusion | None" = None):
        self.tools = load_tools()
        self.fusion = fusion or SimulatedFusion()
        self._tools_by_name = {t["name"]: t for t in self.tools}
        self.sse = sse                      # answer as text/event-stream instead of JSON
        self.strict_accept = strict_accept  # require Accept: application/json, text/event-stream
        self.handler: Handler | None = handler
        self.calls: list[Call] = []
        self.sessions: set[str] = set()
        self.initialized: set[str] = set()
        # fault injection
        self.fail_http: int | None = None   # e.g. 500: every request after init returns this
        self.tool_error: str | None = None  # every tools/call returns isError with this text
        self.delay_s: float = 0.0           # added latency on tools/call
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle -------------------------------------------------------------------
    def start(self) -> "FakeFusionMCP":
        fake = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # keep pytest output clean
                pass

            def do_POST(self):
                fake._handle_post(self)

            def do_GET(self):
                # Real servers may offer a GET SSE stream; we do not.
                self._plain(405, b"")

            def do_DELETE(self):
                sid = self.headers.get("Mcp-Session-Id")
                with fake._lock:
                    fake.sessions.discard(sid)
                    fake.initialized.discard(sid)
                self._plain(200, b"")

            def _plain(self, code, body, ctype="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def port(self) -> int:
        assert self._httpd, "not started"
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    # ---- inspection --------------------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self.calls.clear()

    def tool_calls(self) -> list[Call]:
        with self._lock:
            return [c for c in self.calls if c.method == "tools/call" and c.http_status == 200]

    def script_calls(self, *, writes_only: bool = False) -> list[Call]:
        out = [c for c in self.tool_calls()
               if c.tool == "fusion_mcp_execute"
               and (c.arguments.get("featureType") == "script")]
        return [c for c in out if not c.read_only] if writes_only else out

    def methods(self) -> list[str]:
        with self._lock:
            return [c.method for c in self.calls]

    # ---- protocol ----------------------------------------------------------------------
    def _handle_post(self, h: BaseHTTPRequestHandler) -> None:
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        headers = {k.lower(): v for k, v in h.headers.items()}
        sid = h.headers.get("Mcp-Session-Id")

        def reply(code: int, payload: dict | None, extra_headers: dict | None = None):
            if payload is None:
                body, ctype = b"", "application/json"
            elif self.sse:
                body = ("event: message\ndata: " + json.dumps(payload) + "\n\n").encode()
                ctype = "text/event-stream"
            else:
                body, ctype = json.dumps(payload).encode(), "application/json"
            h.send_response(code)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                h.send_header(k, v)
            h.end_headers()
            if body:
                h.wfile.write(body)

        try:
            msg = json.loads(raw or b"null")
        except json.JSONDecodeError:
            reply(400, _err(None, -32700, "Parse error"))
            return
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            reply(400, _err(None, -32600, "Invalid Request"))
            return

        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        call = Call(time.monotonic(), method, mid, params, sid, headers)

        def record(status: int):
            call.http_status = status
            with self._lock:
                self.calls.append(call)

        accept = headers.get("accept", "")
        if self.strict_accept and not ("application/json" in accept and "text/event-stream" in accept):
            record(406)
            reply(406, _err(mid, -32000, "Not Acceptable: Client must accept both "
                                         "application/json and text/event-stream"))
            return

        if method == "initialize":
            new_sid = uuid.uuid4().hex
            with self._lock:
                self.sessions.add(new_sid)
            record(200)
            reply(200, {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {"listChanged": False}, "resources": {}},
                "serverInfo": {"name": "fake-fusion-mcp", "version": "0"},
            }}, {"Mcp-Session-Id": new_sid})
            return

        if not sid:
            record(400)
            reply(400, _err(mid, -32000, "Bad Request: No valid session ID provided"))
            return
        if sid not in self.sessions:
            record(404)
            reply(404, _err(mid, -32001, "Session not found"))
            return
        if self.fail_http:
            record(self.fail_http)
            reply(self.fail_http, _err(mid, -32603, "Injected HTTP failure"))
            return

        if method == "notifications/initialized":
            with self._lock:
                self.initialized.add(sid)
            record(202)
            reply(202, None)
            return
        if mid is None:  # any other notification
            record(202)
            reply(202, None)
            return
        if sid not in self.initialized:
            record(400)
            reply(400, _err(mid, -32002, "Session not initialized "
                                         "(send notifications/initialized first)"))
            return

        if method == "ping":
            record(200)
            reply(200, {"jsonrpc": "2.0", "id": mid, "result": {}})
            return
        if method == "tools/list":
            record(200)
            reply(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": self.tools}})
            return
        if method == "tools/call":
            problem = self._validate_call(params)
            if problem:
                record(200)
                reply(200, _err(mid, -32602, problem))
                return
            if self.delay_s:
                time.sleep(self.delay_s)
            record(200)
            if self.tool_error:
                text, is_err = self.tool_error, True
            elif self.handler:
                text, is_err = self.handler(params["name"], params.get("arguments") or {})
            else:
                text, is_err = self._default_tool(params["name"], params.get("arguments") or {})
            reply(200, {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}], "isError": bool(is_err)}})
            return

        record(200)
        reply(200, _err(mid, -32601, f"Method not found: {method}"))

    def _default_tool(self, name: str, args: dict) -> tuple[str, bool]:
        if name == "fusion_mcp_execute" and args.get("featureType") == "script":
            return self.fusion.run_script(args["object"]["script"]), False
        # Structured read tools the PoC's current FusionClient uses (added for the Polybench port; the PoC's own fake
        # predates them). Fusion answers for the ACTIVE editor only, as the real server does.
        f = self.fusion
        if name == "fusion_mcp_read" and args.get("queryType") == "document":
            active = f.active_doc if f.active != "none" else None
            docs = [{"id": f.lineage(n), "name": n, "isActive": n == active, "isModified": False} for n in f.boards]
            return json.dumps({"results": docs}), False
        if name == "fusion_mcp_electronics_read":
            have_board = f.active != "none" and f.active_doc in f.boards
            if args.get("entity_type") == "electronics.Board":
                return json.dumps({"items": [{"name": f.active_doc + ".brd"}] if have_board else []}), False
            if args.get("entity_type") == "electronics.Element":
                if not (args.get("object") or {}).get("filters"):
                    return json.dumps({"items": [{"name": d} for d in f.boards[f.active_doc]] if have_board else []}), False
            # recorded reads of a real board (Polybench polarity card): replayed by exact request, for the active board only
            rec = getattr(self, "recording", None)
            if rec and have_board:
                got = rec.get(json.dumps(args, sort_keys=True))
                if got is not None:
                    return json.dumps(got), False
                if args.get("entity_type") in ("electronics.Element", "electronics.Wire", "electronics.Circle", "electronics.Contact", "electronics.Package"):
                    return json.dumps({"items": [], "pagination": {"hasMore": False}}), False
        return json.dumps({"success": False, "error": f"fake does not implement {name}"}), False

    def _validate_call(self, params: dict) -> str | None:
        name = params.get("name")
        tool = self._tools_by_name.get(name)
        if tool is None:
            return f"Unknown tool: {name!r}"
        args = params.get("arguments") or {}
        schema = tool.get("inputSchema", {})
        for req in schema.get("required", []):
            if req not in args:
                return f"Missing required argument {req!r} for {name}"
        for key, val in args.items():
            spec = schema.get("properties", {}).get(key, {})
            if "enum" in spec and val not in spec["enum"]:
                return f"Invalid value {val!r} for {key!r}; expected one of {spec['enum']}"
        if name == "fusion_mcp_execute" and args.get("featureType") == "script":
            script = (args.get("object") or {}).get("script")
            if not isinstance(script, str) or not script.strip():
                return "featureType 'script' requires object.script"
            if "def run(" not in script:
                return "The script must define a function named 'run' with exactly one argument"
        return None


def _err(mid, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------------------
# Simulated Fusion Electronics (just the API surface the scanner's script needs)
# ---------------------------------------------------------------------------------------
# Every placed designator on the first demo board (hand-copied so the fake does not depend on app.bom).
DEMO_DESIGNATORS = (
    "C1 C2 C3 C4 C5 C6 C7 C8 C9 C10 C11 C12 C13 C14 C15 C16 C17 C18 C19 C20 C21 C22 "
    "D1 D2 IC1 IC2 IC3 IC4 L1 R2 R3 R4 R5 R6 R7 R8 R9 R10 R11 R12 R13 R14 R15 Y1").split()

_EXEC_LOCK = threading.Lock()


class _Named:
    def __init__(self, name):
        self.name = name


class _Coll:
    def __init__(self, items):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


# Every placed designator on a second demo board (incl. DNF footprints). Used as the "other open board" in UI-3 tests.
DEMO_B_DESIGNATORS = (
    "IC1 U1 IC2 IC4 J1 D1 D2 D3 D4 Y1 L1 C1 C2 C3 C4 C5 C6 C7 C8 C9 C10 C11 C12 C13 C14 C15 "
    "C16 C17 C18 C19 C20 C21 C22 R1 R2 R3 R4 R5 R6 R7 R8 R9").split()


class SimulatedFusion:
    """State of a fake Fusion session holding one or more open Electronics board documents.

    Single board (default): `SimulatedFusion()` = the first demo board, named `doc_name`.
    Several boards: `SimulatedFusion(boards={"name": designators, ...}, active_doc="name")`.
    `active` is what the active tab shows: 'board', 'schematic' or 'none' (a non-electronics
    document is active; the boards stay open in other tabs unless `boards` is empty).

    Inspection: `selected` = highlight on the active/last-touched board,
    `selected_on[name]` = per board, `history` = [(board, op, designators)].
    """

    def __init__(self, designators=DEMO_DESIGNATORS, *, schematic_only=(),
                 doc_name="DEMO-1 v1", active="board",
                 boards: dict | None = None, active_doc: str | None = None):
        if boards is None:
            boards = {} if active == "none" else {doc_name: designators}
        self.boards: dict[str, list[str]] = {k: list(v) for k, v in boards.items()}
        self.schematic_only = list(schematic_only)
        self.active = active
        self.active_doc = active_doc or (next(iter(self.boards)) if self.boards else None)
        self.selected_on: dict[str, tuple[str, ...]] = {k: () for k in self.boards}
        self.last_board: str | None = self.active_doc
        self.history: list[tuple[str, str, tuple[str, ...]]] = []
        self.scripts_run = 0

    # ---- convenience / back-compat ----
    @property
    def doc_name(self) -> str | None:
        return self.active_doc

    @property
    def board_designators(self) -> list[str]:
        return self.boards.get(self.active_doc, [])

    @property
    def selected(self) -> tuple[str, ...]:
        return self.selected_on.get(self.last_board, ()) if self.last_board else ()

    def any_selected(self) -> dict[str, tuple[str, ...]]:
        return {k: v for k, v in self.selected_on.items() if v}

    @staticmethod
    def lineage(name: str) -> str:
        """Fake dataFile.id (lineage URN) for a board document name."""
        import hashlib
        return "urn:adsk.wipprod:dm.lineage:" + hashlib.sha1(name.encode()).hexdigest()[:16]

    def open_board(self, name: str, designators) -> None:
        self.boards[name] = list(designators)
        self.selected_on[name] = ()

    def close_board(self, name: str) -> None:
        self.boards.pop(name, None)
        self.selected_on.pop(name, None)
        if self.active_doc == name:
            self.active_doc = next(iter(self.boards), None)
        if self.last_board == name:
            self.last_board = self.active_doc

    # ---- the fake adsk modules ----
    def _modules(self):
        fus = self
        board_objs: dict = {}

        class Schematic:
            def __init__(self, name):
                self._doc = name
                self.parts = _Coll(_Named(n) for n in fus.boards[name] + fus.schematic_only)

            @property
            def linkedBoard(self):
                return board_objs[self._doc]

            def clearSelection(self):
                fus.selected_on[self._doc] = ()
                fus.last_board = self._doc
                fus.history.append((self._doc, "clear", ()))
                return True

            def selectObjects(self, objs, zoom=False):
                sel = tuple(o.name for o in objs)
                fus.selected_on[self._doc] = sel
                fus.last_board = self._doc
                fus.history.append((self._doc, "select", sel))
                return True

            @staticmethod
            def cast(x):
                return x if isinstance(x, Schematic) else None

        class Board:
            def __init__(self, name):
                self._doc = name
                self.elements = _Coll(_Named(n) for n in fus.boards[name])
                self._sch = Schematic(name)

            @property
            def linkedSchematic(self):
                return self._sch

            @staticmethod
            def cast(x):
                return x if isinstance(x, Board) else None

        class _DataFile:
            def __init__(self, name):
                self.id = fus.lineage(name)     # lineage URN: stable across saves
                self.versionNumber = 1

        class BoardDocument:
            def __init__(self, name):
                self.name = name
                self.board = board_objs[name]
                self.isActive = name == fus.active_doc and fus.active != "none"
                self.creationId = "cid-" + fus.lineage(name)
                self.dataFile = _DataFile(name)
                self.isModified = False         # read-only scripts never dirty the document

            def activate(self):
                fus.active_doc = self.name
                if fus.active == "none":
                    fus.active = "board"
                return True

            @staticmethod
            def cast(x):
                return x if isinstance(x, BoardDocument) else None

        for name in fus.boards:
            board_objs[name] = Board(name)
        docs = [BoardDocument(n) for n in fus.boards]
        active_bd = next((d for d in docs if d.name == fus.active_doc), None)

        class _App:
            if fus.active == "none" or active_bd is None:
                activeProduct = None
                activeDocument = _Named("Untitled (Design)")
                activeDocument.creationId = "cid-untitled-design"
            else:
                activeProduct = (active_bd.board if fus.active == "board"
                                 else active_bd.board.linkedSchematic)
                activeDocument = active_bd
            documents = _Coll(docs)
            version = "fake"

        class Application:
            @staticmethod
            def get():
                return _App

        core = types.ModuleType("adsk.core")
        core.Application = Application
        electron = types.ModuleType("adsk.electron")
        electron.Board, electron.Schematic, electron.BoardDocument = Board, Schematic, BoardDocument
        fusion = types.ModuleType("adsk.fusion")
        adsk = types.ModuleType("adsk")
        adsk.core, adsk.electron, adsk.fusion = core, electron, fusion
        return {"adsk": adsk, "adsk.core": core, "adsk.electron": electron, "adsk.fusion": fusion}

    def run_script(self, src: str) -> str:
        """Exec the script, call run(''), return the real server's JSON envelope."""
        mods = self._modules()
        buf = io.StringIO()
        with _EXEC_LOCK:
            saved = {k: sys.modules.get(k) for k in mods}
            sys.modules.update(mods)
            try:
                self.scripts_run += 1
                g: dict = {"__name__": "__fusion_script__"}
                with contextlib.redirect_stdout(buf):
                    exec(compile(src, "<fusion script>", "exec"), g)
                    g["run"]("")
                return json.dumps({"success": True, "message": buf.getvalue()})
            except Exception:
                return json.dumps({"success": False, "error": traceback.format_exc(limit=3)})
            finally:
                for k, v in saved.items():
                    if v is None:
                        sys.modules.pop(k, None)
                    else:
                        sys.modules[k] = v
