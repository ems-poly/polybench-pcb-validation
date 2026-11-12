"""Polybench MCP server (stdio). Claude Code or Codex starts it; every tool is one HTTP call to the running Polybench app
on this Mac, sent with the assistant identity so the app's guard holds the assistant to its permission mode.

Standard library only. JSON-RPC 2.0, one message per line (MCP stdio transport). See docs/AI_CONNECTOR_ARCHITECTURE.md."""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = "0.1.0"
DEFAULT_URL = "http://127.0.0.1:8750"
TIMEOUT_S = 15.0
HEARTBEAT_S = 15.0
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Polybench is a bench-test app for a batch of PCBs. The operator decides: you may read everything, highlight in ECAD, "
    "propose a value, add a note, ask the operator a question and write a DRAFT test plan. You can never accept a result, "
    "record a FAIL, override a gate, approve a plan or drive an instrument; the app refuses it. Never call the Polybench HTTP "
    "API directly (curl, fetch): use these tools only. Start with get_state, then get_focus. Label simulated data as simulated "
    "and every explanation of a fault as a hypothesis.")


def _s(desc, **props):
    req = [k for k, v in props.items() if v.pop("required", False)]
    return {"description": desc, "inputSchema": {"type": "object", "properties": props, "required": req, "additionalProperties": False}}


def _str(desc, required=False):
    return {"type": "string", "description": desc, "required": required}


TOOLS = {
    "get_state": _s("Session overview: batch, design, simulated flag, operator, permission mode, boards, steps with state and "
                    "progress, open stops, open questions, journal head id, test-plan and ECAD status."),
    "get_focus": _s("What is on the operator's screen now: step, board and test, with the test's limits, probe points, nets, "
                    "highlight and stop rule, and the current value or proposals for that board."),
    "get_board": _s("All results of one board per step: values, verdicts, attempts, pending proposals, not-similar-to-batch flags, "
                    "stops, notes and the AOI status.", board=_str("board label, e.g. S03", True)),
    "get_test": _s("One test: definition and limits, and its readings across the batch with median and similarity flags.",
                   test_id=_str("test ID, e.g. U01", True), board=_str("optional board for its cell")),
    "get_journal": _s("Journal events after an event id (use the head id from the last call to follow along).",
                      since_id={"type": "integer", "description": "return events with a larger id (default 0)"},
                      limit={"type": "integer", "description": "max events, 1..200 (default 50)"},
                      types={"type": "string", "description": "optional comma list of event types"}),
    "get_aoi": _s("AOI (visual inspection) for one board or all: photos, the current run, each finding with its AOI status, the "
                  "operator's decision, designators and evidence image paths. thumbnails=true attaches up to max_images images.",
                  board=_str("optional board label"),
                  thumbnails={"type": "boolean", "description": "attach evidence crops / photo thumbnails as images (default false)"},
                  max_images={"type": "integer", "description": "1..8, default 4"}),
    "get_ecad": _s("ECAD link status, BOM parts (line, designators, value, MPN, package, polarity, DNF) and the nets the tests "
                   "name with the tests that use them. Optional filter by designators.",
                   designators={"type": "array", "items": {"type": "string"}, "description": "optional designators to filter on"}),
    "get_plan": _s("The master test plan (markdown text, hash), its lint and coverage report, and the status of any draft."),
    "get_polarity": _s("The polarity card of one BOM line (how to orient the part, from the BOM and the footprint).",
                       line={"type": "integer", "description": "BOM line number", "required": True},
                       designator=_str("optional designator on that line")),
    "troubleshoot": _s("One evidence bundle for a board, optionally one test: failing / review / stopped / not-similar readings "
                       "with limits and the batch, the parts and nets around the probe points, polarity flags, AOI findings and "
                       "photos on those parts, notes, and a 'missing' list of what is not available.",
                       board=_str("board label", True), test_id=_str("optional test ID")),
    "get_questions": _s("Questions you asked the operator and their answers."),
    "highlight": _s("Select designators or nets in the open ECAD board (read / select / clear only; nothing is edited). "
                    "An empty list clears the selection.",
                    designators={"type": "array", "items": {"type": "string"}, "description": "designators, REF.PIN or net names", "required": True}),
    "propose_value": _s("Post a value as a PROPOSAL on a board's test. It is not a result: the operator accepts or rejects it in "
                        "the UI. Use only for a value the operator gave you or you read from evidence; say where it came from.",
                        board=_str("board label", True), test_id=_str("test ID", True), value=_str("the value as typed (number, y/n, option)", True),
                        unit=_str("optional unit"), reason=_str("where the value comes from (shown to the operator)", True)),
    "add_note": _s("Add a note to the journal (source: assistant), optionally linked to a board, test, step or designators.",
                   text=_str("note text", True), board=_str("optional board"), test_id=_str("optional test ID"), step=_str("optional step id"),
                   category={"type": "string", "enum": ["observation", "design issue", "process", "question"]},
                   designators={"type": "array", "items": {"type": "string"}}),
    "ask_operator": _s("Show a question to the operator in the Polybench window (for example the next measurement to take). "
                       "Read the answer later with get_questions.",
                       text=_str("the question", True), options={"type": "array", "items": {"type": "string"}, "description": "optional answer buttons (max 6)"},
                       board=_str("optional board"), test_id=_str("optional test ID")),
    "write_plan_draft": _s("Write the WHOLE new test plan as a draft next to the master (never over it). The app lints it and "
                           "returns the lint and the diff. Only the operator approves it, in design step 4.",
                           text=_str("complete plan markdown (plan_format 1)", True),
                           summary=_str("one line describing the change; it becomes the plan's revision row", True)),
}


class App:
    def __init__(self, url: str, client: str):
        self.url = url.rstrip("/")
        self.headers = {"X-Polybench-Client": "assistant", "X-Polybench-Assistant": client, "X-Polybench-Session": uuid.uuid4().hex[:12]}

    def request(self, method: str, path: str, body=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        h = dict(self.headers)
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                b = r.read()
                return (r.status, b, r.headers.get("Content-Type", "")) if raw else (r.status, json.loads(b or b"{}"))
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b or b"{}")
            except ValueError:
                return e.code, {"error": b.decode(errors="replace")[:300]}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, {"error": f"Polybench is not reachable at {self.url} ({getattr(e, 'reason', e)}). Is the app running?"}


def _q(**kw) -> str:
    kw = {k: v for k, v in kw.items() if v not in (None, "", [])}
    return ("?" + urllib.parse.urlencode(kw)) if kw else ""


def call_tool(app: App, name: str, a: dict) -> tuple[list, bool]:
    """Returns (MCP content list, is_error)."""
    route = {
        "get_state": ("GET", "/api/ai/context"),
        "get_focus": ("GET", "/api/ai/focus"),
        "get_board": ("GET", "/api/ai/board" + _q(board=a.get("board"))),
        "get_test": ("GET", "/api/ai/test" + _q(test_id=a.get("test_id"), board=a.get("board"))),
        "get_journal": ("GET", "/api/ai/journal" + _q(since=a.get("since_id", 0), limit=a.get("limit", 50), types=a.get("types"))),
        "get_aoi": ("GET", "/api/ai/aoi" + _q(board=a.get("board"))),
        "get_ecad": ("GET", "/api/ai/ecad" + _q(designators=",".join(a.get("designators") or []))),
        "get_plan": ("GET", "/api/ai/plan"),
        "get_polarity": ("GET", "/api/ai/polarity" + _q(line=a.get("line"), d=a.get("designator"))),
        "troubleshoot": ("GET", "/api/ai/troubleshoot" + _q(board=a.get("board"), test_id=a.get("test_id"))),
        "get_questions": ("GET", "/api/ai/questions"),
        "highlight": ("POST", "/api/ai/highlight", {"designators": a.get("designators") or []}),
        "propose_value": ("POST", "/api/ai/propose", {k: a.get(k) for k in ("board", "test_id", "value", "unit", "reason")}),
        "add_note": ("POST", "/api/ai/note", {k: a.get(k) for k in ("text", "board", "test_id", "step", "category", "designators") if a.get(k) is not None}),
        "ask_operator": ("POST", "/api/ai/ask", {k: a.get(k) for k in ("text", "options", "board", "test_id") if a.get(k) is not None}),
        "write_plan_draft": ("POST", "/api/ai/plan_draft", {"text": a.get("text"), "summary": a.get("summary")}),
    }.get(name)
    if route is None:
        return [{"type": "text", "text": f"unknown tool {name}"}], True
    code, data = app.request(route[0], route[1], route[2] if len(route) > 2 else None)
    err = code == 0 or code >= 400
    content = [{"type": "text", "text": json.dumps(data, indent=1, default=str)}]
    if name == "get_aoi" and a.get("thumbnails") and not err:
        content += _images(app, data, max(1, min(int(a.get("max_images") or 4), 8)))
    return content, err


def _images(app: App, data: dict, n: int) -> list:
    urls = []
    for f in data.get("findings") or []:
        urls += (f.get("evidence_urls") or [])[:1]
    urls += [p["thumb_url"] for p in data.get("photos") or [] if p.get("thumb_url")]
    out = []
    for u in urls[:n]:
        code, body, ctype = app.request("GET", u, raw=True)
        if code == 200 and len(body) < 1_500_000:
            out.append({"type": "text", "text": f"image: {u}"})
            out.append({"type": "image", "data": base64.b64encode(body).decode(), "mimeType": "image/png" if "png" in ctype else "image/jpeg"})
    return out


def _loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(app: App, stdin=None, stdout=None, heartbeat=True):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    lock = threading.Lock()

    def send(msg):
        with lock:
            stdout.write(json.dumps(msg) + "\n")
            stdout.flush()

    if heartbeat:
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT_S):
                app.request("POST", "/api/ai/heartbeat", {})
        threading.Thread(target=beat, daemon=True).start()
        app.request("POST", "/api/ai/heartbeat", {})
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:              # a notification (initialized, cancelled, ...): no reply
            continue
        try:
            if method == "initialize":
                pv = params.get("protocolVersion")
                res = {"protocolVersion": pv if pv in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "polybench", "version": VERSION}, "instructions": INSTRUCTIONS}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": [{"name": k, **v} for k, v in TOOLS.items()]}
            elif method == "tools/call":
                content, err = call_tool(app, params.get("name", ""), params.get("arguments") or {})
                res = {"content": content, "isError": err}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as e:      # one bad call never ends the session
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"internal error: {e}"}})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="polybench mcp", description="Polybench MCP server (stdio) for Claude Code or Codex")
    ap.add_argument("--url", default=os.environ.get("POLYBENCH_URL", DEFAULT_URL), help=f"Polybench app URL (default {DEFAULT_URL}, env POLYBENCH_URL)")
    ap.add_argument("--client", default=os.environ.get("POLYBENCH_ASSISTANT", "assistant"), help="assistant name shown in the app (claude-code, codex)")
    a = ap.parse_args(argv)
    if not _loopback(a.url):
        print(f"polybench mcp: refusing {a.url}: only a loopback address (127.0.0.1, localhost, ::1) is allowed", file=sys.stderr)
        return 2
    serve(App(a.url, a.client))
    return 0


if __name__ == "__main__":
    sys.exit(main())
