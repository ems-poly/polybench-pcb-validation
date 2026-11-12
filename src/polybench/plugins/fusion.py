"""ECAD bridge `fusion`: highlight designators in the open Fusion Electronics document.

Talks to Fusion's stock MCP server (streamable HTTP, JSON-RPC) at http://127.0.0.1:27182/mcp. The transport and the SHOW
sequencing are adapted from the Polystage report generator's `fusionlink.py` (copied and trimmed, not imported), which was
verified on a live Fusion on 15 Oct 2026:

  * The only text commands ever sent are EAGLE `SHOW name ...` (no trailing semicolon: with one the highlight vanishes at
    once) through `Electron.run`. SHOW highlights; it does not change the design. Nothing here saves, closes, edits or
    switches documents or tabs.
  * A SHOW leaves EAGLE's Show command active, and Fusion refuses every normal script while a command is open. So before
    each send the previous SHOW is ended by switching to the Select tool (read-only script), and the switch is awaited.
    A command the operator opened (Move, Route, a dialog) is never cancelled: the result says Fusion is busy.
  * Fusion's electronics reads answer for the active editor: board tab -> `electronics.Element` lists placed parts,
    schematic tab -> `electronics.Part`. designators() reads whichever is active.
  * Every failure is returned as a status, never raised (the bench UI must not hang or crash because Fusion is closed).

Client-neutral: no design names are known here. `set_expected_document("substring")` (or env POLYBENCH_FUSION_DOC) makes
the bridge refuse to highlight in a document whose name does not contain the substring.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:27182/mcp"
PROTOCOL_VERSION = "2025-03-26"
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
TEXT_CMD_RE = re.compile(r'^Electron\.run "SHOW(?: [A-Za-z0-9_][A-Za-z0-9_.\-]*)+"$')
# Commands we may end: our own Show and the Select tools Fusion falls back to. Anything else is the operator's dialog.
OWN_COMMANDS = (None, "", "Electron::Show", "Electron::Group", "SelectCommand")
END_COMMAND_SCRIPT = ("import adsk.core\n\ndef run(_context: str):\n"
                      "    adsk.core.Application.get().userInterface.commandDefinitions.itemById('SelectCommand').execute()\n")


class FusionUnavailable(Exception):
    pass


def build_show_command(names) -> str:
    """'SHOW R3 C5 ...'. Raises ValueError for anything that is not a plain EAGLE name."""
    out: list[str] = []
    for n in names:
        n = str(n)
        if not NAME_RE.match(n):
            raise ValueError(f"not a plain EAGLE name: {n!r}")
        if n not in out:
            out.append(n)
    if not out:
        raise ValueError("nothing to show")
    return "SHOW " + " ".join(out)


def build_text_command(eagle: str) -> str:
    cmd = f'Electron.run "{eagle}"'
    if not TEXT_CMD_RE.match(cmd):
        raise ValueError(f"refused: only 'SHOW name ...' is allowed, got {eagle!r}")
    return cmd


def build_script(text_cmd: str) -> str:
    if not TEXT_CMD_RE.match(text_cmd):
        raise ValueError("refused: not a SHOW command")
    return ("import adsk.core\n\ndef run(_context: str):\n    app = adsk.core.Application.get()\n"
            f"    app.executeTextCommand({text_cmd!r})\n")


def _short_error(s: str) -> str:
    lines = [ln for ln in s.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else "Fusion script failed"
    if "command dialog" in s.lower() or "interactive" in s.lower():
        return "Fusion is busy (a command dialog is open): " + last
    return last


class McpClient:
    """Minimal MCP client for Fusion. One reconnect per call; short timeout; no background threads."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 4.0):
        self.url, self.timeout, self._sid, self._id = url, timeout, None, 0
        self.server_info: dict = {}

    def _post(self, body: dict, with_session: bool = True):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if with_session and self._sid:
            headers["MCP-Session-Id"] = self._sid
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            hdrs = {k.lower(): v for k, v in r.headers.items()}
        if not raw.strip():
            return None, hdrs
        if "text/event-stream" in hdrs.get("content-type", "") or raw.lstrip().startswith(("event:", "data:")):
            msg = None
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        pass
            return msg, hdrs
        return json.loads(raw), hdrs

    def _connect(self):
        self._sid = None
        self._id += 1
        msg, hdrs = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "polybench", "version": "0.1"}}}, False)
        if not msg or "error" in msg:
            raise FusionUnavailable(f"MCP initialize failed: {msg}")
        self._sid = hdrs.get("mcp-session-id")
        self.server_info = (msg.get("result") or {}).get("serverInfo") or {}
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict) -> str:
        last = None
        for _ in range(2):
            try:
                if self._sid is None:
                    self._connect()
                self._id += 1
                msg, _h = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                                      "params": {"name": name, "arguments": arguments}})
                if msg is None or "error" in msg:
                    raise FusionUnavailable(f"MCP error: {msg}")
                res = msg.get("result", {})
                text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
                if res.get("isError"):
                    raise FusionUnavailable(text or "tool reported isError")
                return text
            except (urllib.error.URLError, OSError, json.JSONDecodeError, FusionUnavailable) as e:
                last = e
                self._sid = None
                if "timed out" in str(e) or "refused" in str(e).lower():
                    break
        raise FusionUnavailable(f"cannot reach Fusion MCP at {self.url}: {last}")

    # -- reads
    def active_doc(self) -> dict:
        d = json.loads(self.call_tool("fusion_mcp_read", {"queryType": "document", "operation": "open"}))
        return next((x for x in d.get("results", []) if x.get("isActive")), {})

    def doc_count(self) -> int:
        d = json.loads(self.call_tool("fusion_mcp_read", {"queryType": "document", "operation": "open"}))
        return len(d.get("results", []))

    def open_documents(self) -> list[dict]:
        """Documents open in Fusion (read only): id, name, isActive, isModified. No element count: only the active document can be read."""
        d = json.loads(self.call_tool("fusion_mcp_read", {"queryType": "document", "operation": "open"}))
        return list(d.get("results", []))

    def search_documents(self, name: str) -> list[dict]:
        """Documents in the hub whose name matches (read only; nothing is opened). id, name, parentFolder, parentProject."""
        d = json.loads(self.call_tool("fusion_mcp_read", {"queryType": "document", "operation": "search", "name": name}))
        return list(d.get("results", []))

    def _read_all(self, entity: str) -> dict[str, str]:
        out, off = {}, 0
        while True:
            d = json.loads(self.call_tool("fusion_mcp_electronics_read", {
                "entity_type": entity,
                "object": {"fields": ["name", "value"], "pagination": {"limit": 1000, "offset": off}}}))
            out.update({i["name"]: i.get("value", "") for i in d.get("items", []) if i.get("name")})
            pg = d.get("pagination", {})
            if not pg.get("hasMore") or "nextOffset" not in pg:
                return out
            off = pg["nextOffset"]

    def read_parts(self) -> dict[str, str]:
        """Schematic parts (schematic editor active); empty when the board or no editor is active."""
        return self._read_all("electronics.Part")

    def read_elements(self) -> dict[str, str]:
        """Placed parts of the board (board editor active); empty otherwise."""
        return self._read_all("electronics.Element")

    def active_command_id(self) -> str | None:
        d = json.loads(self.call_tool("fusion_mcp_read", {"queryType": "activeCommand"}))
        ac = d.get("activeCommand", d)
        return (ac or {}).get("commandId") if isinstance(ac, dict) else None

    # -- the two things that are sent
    def end_active_command(self) -> None:
        env = json.loads(self.call_tool("fusion_mcp_execute", {
            "featureType": "script", "object": {"script": END_COMMAND_SCRIPT, "readOnly": True}}))
        if not env.get("success", False):
            raise FusionUnavailable(_short_error(str(env.get("error") or env.get("message") or env)))

    def run_text_command(self, text_cmd: str) -> None:
        env = json.loads(self.call_tool("fusion_mcp_execute", {
            "featureType": "script", "object": {"script": build_script(text_cmd)}}))
        if not env.get("success", False):
            raise FusionUnavailable(_short_error(str(env.get("error") or env.get("message") or env)))


class FakeClient:
    """Stands in for Fusion in the simulator and in tests. Records every text command it was sent."""

    def __init__(self, parts: dict[str, str] | None = None, doc: dict | None = None, fail: str | None = None,
                 editor: str = "board"):
        self.parts = parts if parts is not None else {}
        self.doc = doc or {"name": "simulated-board", "id": "fake", "isModified": False}
        self.commands: list[str] = []
        self.ended = 0
        self.active: str | None = None
        self.fail = fail
        self.editor = editor

    def _check(self):
        if self.fail:
            raise FusionUnavailable(self.fail)

    def active_doc(self):
        self._check()
        return dict(self.doc)

    def doc_count(self):
        self._check()
        return 1

    server_info = {"name": "fake MCP", "version": "0"}

    def open_documents(self):
        self._check()
        return [{**self.doc, "isActive": True}]

    def search_documents(self, name):
        self._check()
        return [{"id": "fake-1", "name": self.doc.get("name"), "parentFolder": "F", "parentProject": "P"}] if name else []

    def read_parts(self):
        self._check()
        return dict(self.parts) if self.editor == "schematic" else {}

    def read_elements(self):
        self._check()
        return dict(self.parts) if self.editor == "board" else {}

    def active_command_id(self):
        self._check()
        return self.active

    def end_active_command(self):
        self._check()
        self.ended += 1
        self.active = None

    def run_text_command(self, text_cmd):
        self._check()
        if not TEXT_CMD_RE.match(text_cmd):
            raise ValueError("refused")
        if self.active is not None:
            raise FusionUnavailable("Cannot perform 'script' while a command dialog is open.")
        self.commands.append(text_cmd)
        self.active = "Electron::Show"


SIM_PARTS = {f"{p}{i}": "" for p, n in (("R", 20), ("C", 24), ("U", 4), ("D", 3), ("Q", 2), ("TP", 12), ("J", 2),
                                          ("L", 2), ("Y", 1), ("F", 1), ("LED", 1)) for i in range(1, n + 1)}


class FusionBridge:
    """EcadBridge `fusion`. Construct with no arguments for the real thing; tests pass a client."""

    name = "fusion"

    def __init__(self, client=None, simulate: bool | None = None, url: str = DEFAULT_URL,
                 expected_document: str | None = None):
        if simulate is None:
            simulate = os.environ.get("POLYBENCH_SIMULATE") == "1"
        self.simulate = simulate
        url = os.environ.get("POLYBENCH_FUSION_URL") or url
        self.client = client or (FakeClient(dict(SIM_PARTS)) if simulate else McpClient(url))
        self.expected_document = expected_document or os.environ.get("POLYBENCH_FUSION_DOC") or None
        self._lit: list[str] = []

    def set_expected_document(self, substring: str | None) -> None:
        self.expected_document = substring or None

    # -- EcadBridge
    def status(self) -> dict:
        if self.simulate:
            doc = self.client.active_doc().get("name")
            return {"connected": True, "program": "Fusion (simulated)", "document": doc, "document_id": "fake", "editor": "board",
                    "detail": "simulated ECAD; nothing is sent to Fusion"}
        try:
            doc = self.client.active_doc()
            sch = self.client.read_parts()
            brd = {} if sch else self.client.read_elements()
        except FusionUnavailable as e:
            return {"connected": False, "program": "Fusion", "document": None, "editor": None, "detail": _first_line(str(e))}
        except Exception as e:  # never hang or crash the UI
            return {"connected": False, "program": "Fusion", "document": None, "editor": None,
                    "detail": f"{type(e).__name__}: {e}"}
        editor = "schematic" if sch else ("board" if brd else "none")
        n = len([k for k in (sch or brd) if not k.startswith(("FRAME", "SUPPLY"))])
        name = doc.get("name")
        detail = f"{n} parts in the active {editor}" if editor != "none" else "no Electronics editor is active"
        if not name:
            detail = "Fusion is running but no document is open"
        info = getattr(self.client, "server_info", None) or {}
        return {"connected": True, "program": "Fusion", "version": (f"MCP {info.get('name', '')} {info.get('version', '')}".strip() if info else ""),
                "document": name, "document_id": doc.get("id"), "editor": editor, "elements": n if editor != "none" else None, "detail": detail,
                "modified": bool(doc.get("isModified"))}

    def documents(self, search: str | None = None) -> dict:
        """Open documents and, for a search term, the hub documents with that name. Read only: nothing is opened or switched."""
        res: dict = {"ok": False, "open": [], "candidates": [], "search": search or "", "message": ""}
        try:
            for d in self.client.open_documents():
                res["open"].append({"name": d.get("name"), "active": bool(d.get("isActive")), "modified": bool(d.get("isModified")),
                                    "id": d.get("id"), "open": True})
            if search:
                open_names = [o["name"] for o in res["open"]]
                open_ids = {o["id"] for o in res["open"] if o.get("id")}
                for d in self.client.search_documents(search):
                    res["candidates"].append({"id": d.get("id"), "name": d.get("name"), "folder": d.get("parentFolder"),
                                              "project": d.get("parentProject"), "open": d.get("id") in open_ids})
                res["same_name_open"] = sum(1 for o in open_names if search.lower() in str(o).lower())
            res["ok"] = True
        except FusionUnavailable as e:
            res["message"] = _first_line(str(e))
        except Exception as e:
            res["message"] = f"{type(e).__name__}: {e}"
        return res

    def designators(self) -> list[str] | None:
        try:
            sch = self.client.read_parts()
            parts = sch or self.client.read_elements()
        except Exception:
            return None
        names = [k for k in parts if not k.startswith(("FRAME", "SUPPLY"))]
        return sorted(names) if names else None

    def highlight(self, designators: list[str]) -> dict:
        res: dict = {"ok": False, "highlighted": [], "missing": [], "message": "", "command": None}
        try:
            cmd = build_show_command(designators)
        except ValueError as e:
            res["message"] = str(e)
            return res
        want = cmd.split()[1:]
        try:
            st = self.status()
            if not st["connected"]:
                res["message"] = f"Fusion not reachable: {st['detail']}. Nothing was sent"
                return res
            if st["editor"] == "none":
                res["message"] = (f"No Electronics editor is active in Fusion (document: {st['document']}). "
                                  "Open the board or schematic. Nothing was sent")
                return res
            if self.expected_document and self.expected_document.lower() not in (st["document"] or "").lower():
                res["message"] = (f"The active Fusion document is {st['document']!r}, expected one containing "
                                  f"{self.expected_document!r}. Make the right one active. Nothing was sent")
                return res
            known = self.designators()
            if known is not None:
                ks = set(known)
                res["missing"] = [n for n in want if n not in ks and not _is_net_like(n)]
                want = [n for n in want if n not in res["missing"]]
            if not want:
                res["message"] = f"None of {designators} is in the open document. Nothing was sent"
                return res
            cmd = build_text_command(build_show_command(want))
            self._send(cmd)
        except FusionUnavailable as e:
            res["message"] = _first_line(str(e))
            res["busy"] = "busy" in str(e).lower()
            return res
        except Exception as e:
            res["message"] = f"{type(e).__name__}: {e}"
            return res
        self._lit = want
        res.update(ok=True, highlighted=want, command=cmd, document=st["document"], editor=st["editor"],
                   message="Highlighted" + (f"; not in the document: {res['missing']}" if res["missing"] else ""))
        return res

    def clear(self) -> None:
        """End the SHOW (switch to the Select tool). Never raises; never touches a command the operator opened."""
        try:
            if self.client.active_command_id() in OWN_COMMANDS:
                self.client.end_active_command()
        except Exception:
            pass
        self._lit = []

    # -- internals
    def _send(self, text_cmd: str) -> None:
        cid = self.client.active_command_id()
        if cid not in OWN_COMMANDS:
            raise FusionUnavailable(f"Fusion is busy (a command dialog is open: {cid}). Finish or cancel it in Fusion. "
                                    "Nothing was sent")
        self._end_and_wait()
        try:
            self.client.run_text_command(text_cmd)
        except FusionUnavailable as e:
            if "dialog" not in str(e).lower():
                raise
            self._end_and_wait()
            self.client.run_text_command(text_cmd)

    def _end_and_wait(self, timeout: float = 2.0) -> None:
        """The tool switch finishes asynchronously; a SHOW sent before it is done is cancelled a moment later."""
        self.client.end_active_command()
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.client.active_command_id() in (None, "", "SelectCommand"):
                return
            time.sleep(0.05)


def _first_line(s: str) -> str:
    return (s.strip().splitlines() or [""])[0][:300]


def _is_net_like(name: str) -> bool:
    """SHOW accepts net names as well as parts. A caller asking for a probe net (VIN, GND, 3V3) is not an error; but a
    designator-shaped name (letters then digits) that is not in the document is reported as unknown."""
    return not re.fullmatch(r"[A-Za-z]{1,4}\d+[A-Za-z]?", name)
