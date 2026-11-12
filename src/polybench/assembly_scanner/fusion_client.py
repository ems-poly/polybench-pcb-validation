"""Fusion link: highlight board parts via Autodesk's stock Fusion MCP server.

Transport: MCP streamable HTTP (JSON-RPC 2.0) at http://127.0.0.1:27182/mcp.
`initialize` returns an MCP-Session-Id header that is sent on every later call, then
`notifications/initialized` is sent. The session is kept and rebuilt once on failure.

Board/document data comes from Fusion's read-only Electronics and document tools. Actions
use `fusion_mcp_execute` scripts with stdout disabled. The highlight is
Schematic.selectObjects(), which Fusion cross-probes onto the board editor (see that
script). Read-only means: no design change, no undo entry, no save/close, and it is
allowed while a command dialog is open.

Board selection (UI-3). Several Electronics boards can be open; a board is identified by
its document key (see list_boards()).
  * Auto mode (the default on every launch): auto_select_for(bom_placements) scores every
    open board and becomes the target only when the pick is confident. If it is not
    confident, highlight() returns an error until the user picks a board.
  * Manual mode: select_board(key_or_name) pins one board for this session (in memory
    only, never persisted). select_board(None) returns to auto mode.
  * The target is never swapped silently. If the target board's document has closed,
    highlight()/clear()/board_designators() return an error, never another board.
  * With no target (auto mode before any auto pick), the client acts only when exactly one
    board is open.
  * When the target board is not the active tab, highlight() brings its tab forward
    (Document.activate(): a view change, does not mark the doc modified) so the highlight
    is visible. Pass activate=False to the constructor to disable that.

All public methods block; call them from a worker thread. None of them raise for Fusion
or network problems: errors come back in the result (HighlightResult.error, or
status()["error"]). Details of the last call (board, activated) are in `last_info`.

CLI for manual checks (each run is a new session, so --board pins only for that run):
    .venv/bin/python -m app.fusion_client status
    .venv/bin/python -m app.fusion_client boards
    .venv/bin/python -m app.fusion_client use <board>     # resolve + show a board
    .venv/bin/python -m app.fusion_client auto fixtures/boms/purchasing_bom.xlsx
    .venv/bin/python -m app.fusion_client auto R1 R2 C5             # or designators
    .venv/bin/python -m app.fusion_client highlight R4 R5 [--board <key|name>] [--auto BOM]
    .venv/bin/python -m app.fusion_client clear [--board ...]
    .venv/bin/python -m app.fusion_client designators [--board ...]
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from polybench.assembly_scanner import __version__
from polybench.assembly_scanner.models import HighlightResult

DEFAULT_URL = os.environ.get("ASCAN_FUSION_URL", "http://127.0.0.1:27182/mcp")
PROTOCOL_VERSION = "2025-03-26"
SCRIPT_PATH = Path(__file__).with_name("fusion_scripts") / "assembly_scanner.py"
MARK = "ASCAN_JSON:"

# Auto-pick defaults (see auto_select_for). Measured 7 Oct 2026: the right board covers
# 100% of its BOM placements; the WRONG open board still covers 84% (the BOM of board A on
# board B) and 90% (the BOM of board B on board A), because R1/C1-style designators
# overlap. A plain coverage threshold of e.g. 0.8 would therefore accept the wrong board.
AUTO_MIN_COVERAGE = 0.95   # allows ~2 of 44 placements missing (a renamed or late part)
AUTO_MIN_MARGIN = 0.05     # best must beat the runner-up by this much coverage


class FusionError(Exception):
    """Fusion or MCP returned an error, or the reply could not be understood."""


class FusionClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 10.0, zoom: bool = False,
                 activate: bool = True, min_coverage: float = AUTO_MIN_COVERAGE,
                 min_margin: float = AUTO_MIN_MARGIN):
        self.url = url
        self.timeout = timeout
        self.zoom = zoom                  # passed to selectObjects; it zooms the schematic view
        self.activate = activate          # bring the target board tab forward on highlight
        self.min_coverage = min_coverage
        self.min_margin = min_margin
        self._sid: str | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._script_src = SCRIPT_PATH.read_text(encoding="utf-8")
        self.last_latency_ms: float | None = None
        self.last_info: dict = {}
        self._pinned: dict | None = None      # manual override (session only)
        self._auto: dict | None = None        # last confident auto pick
        self._last_key: str | None = None     # board that holds the current highlight
        self.net_mode: str | None = None      # how nets were last shown successfully: nets | pinrefs
        # What each open document turned out to be, learned whenever it is the active tab (or from the
        # read-only board probe): {document id: {"editor": "board" | "other", "designators": [...]}}.
        # Fusion's open-document list carries no editor type, and inactive documents expose no parts.
        self._doc_cache: dict[str, dict] = {}
        self.probe_boards = True              # allow the read-only script probe in auto_select_for
        self._probe_retry_at = 0.0

    # ------------------------------------------------------------------ transport
    def _post(self, body: dict, with_session: bool = True) -> tuple[dict | None, dict]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if with_session and self._sid:
            headers["MCP-Session-Id"] = self._sid
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            hdrs = {k.lower(): v for k, v in r.headers.items()}
        if not raw.strip():
            return None, hdrs
        if "text/event-stream" in hdrs.get("content-type", "") or raw.lstrip().startswith(("event:", "data:")):
            # SSE: take the last data: payload that parses as a JSON-RPC response.
            msg = None
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        pass
            return msg, hdrs
        return json.loads(raw), hdrs

    def _connect(self) -> None:
        self._sid = None
        self._id += 1
        msg, hdrs = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                "clientInfo": {"name": "assembly-scanner", "version": __version__}}},
                               with_session=False)
        if not msg or "error" in msg:
            raise FusionError(f"MCP initialize failed: {msg}")
        self._sid = hdrs.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call_tool(self, name: str, arguments: dict) -> str:
        """tools/call with one reconnect on failure. Returns the text content."""
        last: Exception | None = None
        for attempt in range(2):
            try:
                if self._sid is None:
                    self._connect()
                self._id += 1
                msg, _ = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
                if msg is None:
                    raise FusionError("empty MCP reply")
                if "error" in msg:
                    # Typically an expired/unknown session: reconnect and retry once.
                    raise FusionError(f"MCP error: {msg['error']}")
                res = msg.get("result", {})
                text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
                if res.get("isError"):
                    raise _ToolError(text or "tool reported isError")
                return text
            except _ToolError:
                raise
            except (urllib.error.URLError, OSError, FusionError, json.JSONDecodeError) as e:
                last = e
                self._sid = None
                if isinstance(e, (TimeoutError,)) or "timed out" in str(e):
                    break   # do not double a timeout; the script may still be running in Fusion
        raise FusionError(f"cannot reach Fusion MCP at {self.url}: {last}")

    def _read_tool(self, name: str, arguments: dict) -> dict:
        """Call a structured read tool; this avoids Fusion's recursive script stdout path."""
        text = self._call_tool(name, arguments)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as e:
            raise FusionError(f"unexpected {name} reply: {text[:300]}") from e
        if not isinstance(value, dict):
            raise FusionError(f"unexpected {name} reply: expected an object")
        return value

    def _board_snapshot(self) -> tuple[list[dict], str | None]:
        """Get open documents and active board designators without executing a script."""
        with self._lock:
            docs = self._read_tool("fusion_mcp_read", {
                "queryType": "document", "operation": "open"}).get("results", [])
            boards = self._read_tool("fusion_mcp_electronics_read", {
                "entity_type": "electronics.Board"}).get("items", [])
            elements = self._read_tool("fusion_mcp_electronics_read", {
                "entity_type": "electronics.Element", "object": {
                    "fields": ["name"], "pagination": {"limit": 1000, "offset": 0}}}).get("items", [])
        active_doc = next((d for d in docs if d.get("isActive")), None)
        active_board = boards[0] if boards else None
        board_name = Path(str(active_board.get("name", ""))).stem if active_board else None
        if active_board and not active_doc:
            active_doc = next((d for d in docs
                               if str(d.get("name", "")).casefold() == str(board_name).casefold()), None)
        active_key = (active_doc or {}).get("id")
        designators = [str(e.get("name", "")) for e in elements if e.get("name")]
        counts = {}
        for d in docs:
            n = str(d.get("name", "Untitled document"))
            counts[n.casefold()] = counts.get(n.casefold(), 0) + 1
        if active_key is not None and (active_board or active_doc):
            self._doc_cache[str(active_key)] = {"editor": "board" if active_board else "other",
                                                "designators": designators if active_board else []}
        seen = {}
        out = []
        for d in docs:
            key = d.get("id")
            is_active_board = bool(active_board and key == active_key)
            name = str(d.get("name", board_name or "Untitled document"))
            duplicate_index = seen.get(name.casefold(), 0) + 1
            seen[name.casefold()] = duplicate_index
            ui_name = name
            if counts.get(name.casefold(), 0) > 1:
                label = "active" if d.get("isActive") else f"open {duplicate_index}"
                ui_name = f"{name} ({label})"
            known = self._doc_cache.get(str(key)) or {}
            editor = known.get("editor")
            have = known.get("designators") or []
            item = {"key": key, "lineage": key, "version": None,
                    "name": name, "ui_name": ui_name, "design": _design_key(d),
                    "editor": editor,
                    "has_board": True if editor == "board" else (False if editor == "other" else None),
                    "has_schematic": True if is_active_board else None,
                    "is_active": bool(d.get("isActive")),
                    "is_modified": bool(d.get("isModified")),
                    "designator_count": len(have), "designators": list(have)}
            out.append(item)
        if active_board and not out:
            out.append({"key": "active:" + str(board_name), "lineage": None, "version": None,
                        "name": board_name, "has_board": True, "has_schematic": True, "editor": "board",
                        "design": ("", "", str(board_name).casefold()),
                        "is_active": True, "is_modified": False,
                        "designator_count": len(designators), "designators": designators})
        return out, active_doc.get("name") if active_doc else board_name


    def _run(self, op: str, designators: list[str] | None = None, target: str | None = None,
             clear_keys: list[str] | None = None, extra: dict | None = None) -> dict:
        args = {"op": op, "designators": list(designators or []), "zoom": self.zoom,
                "activate": self.activate, "target": target, "clear_keys": list(clear_keys or []),
                "silent_output": True, **(extra or {})}
        src = self._script_src.replace("json.loads(__ARGS__)", "json.loads(%r)" % json.dumps(args))
        with self._lock:
            t0 = time.perf_counter()
            try:
                text = self._call_tool("fusion_mcp_execute", {
                    "featureType": "script", "object": {"script": src, "readOnly": True}})
            finally:
                self.last_latency_ms = (time.perf_counter() - t0) * 1000
        try:
            env = json.loads(text)
        except json.JSONDecodeError:
            raise FusionError(f"unexpected reply: {text[:300]}")
        if not env.get("success", False):
            msg = _short_error(env.get("error") or env.get("message") or text)
            if target and self.target and self.target.get("key") == target:
                msg = msg.replace(f"({target})", f"({self.target.get('name')}, {target})")
            raise FusionError(msg)
        out = env.get("message", "")
        for line in out.splitlines():
            if line.startswith(MARK):
                return json.loads(line[len(MARK):])
        if not out.strip() and op in ("highlight", "clear", "activate"):
            # Successful silent actions intentionally have no stdout payload.
            return {"op": op, "board": self.target or {}, "selected_ok": True,
                    "highlighted": list(designators or []), "not_on_board": [],
                    "no_schematic_part": [], "activated": bool(self.activate and op == "highlight")}
        raise FusionError(f"script printed no result: {out[:300]}")

    # ------------------------------------------------------------------ board selection
    @property
    def mode(self) -> str:
        """'manual' when a board is pinned with select_board(), else 'auto'."""
        return "manual" if self._pinned else "auto"

    @property
    def target(self) -> dict | None:
        """The board highlight() acts on: the pinned board, else the last confident auto
        pick, else None (then only a single open board is used)."""
        return self._pinned or self._auto

    def _target_key(self) -> str | None:
        t = self.target
        return t["key"] if t else None

    def list_boards(self) -> list[dict]:
        """Open board documents: key, name, version, lineage, has_board, has_schematic,
        is_active, is_modified, designator_count, designators. Raises FusionError if
        Fusion is unreachable (the UI shows it; there is nothing to fall back to)."""
        return self._board_snapshot()[0]

    def select_board(self, doc: str | None) -> dict | None:
        """Pin a board by key, lineage URN or (unique, case-insensitive) document name, for
        this session only. None returns to auto mode. Returns the pinned board's info, or
        None in auto mode. Raises FusionError if the board is not open or is ambiguous."""
        if doc is None:
            self._pinned = None
            return None
        boards = self.list_boards()
        hit = [b for b in boards if b["key"] == doc] or \
              [b for b in boards if b.get("lineage") == doc] or \
              [b for b in boards if b["name"].casefold() == str(doc).casefold()]
        if len(hit) != 1:
            names = ", ".join(f"{b['name']} [{b['key']}]" for b in boards) or "none"
            raise FusionError(("no open board matches" if not hit else "several open boards match")
                              + f" {doc!r}; open boards: {names}")
        # A pin on the schematic (or 3D PCB) of a design means that design's board: highlighting and the
        # cross-probe script work on the board document, so a document known not to be a board is swapped
        # for the board of the same design.
        if hit[0].get("editor") == "other":
            sib = [b for b in boards if b.get("design") == hit[0].get("design") and b.get("editor") == "board"]
            if len(sib) == 1:
                hit = sib
        self._pinned = _strip(hit[0])
        try:
            # User selection is explicit; activate that board so the Electronics read tools
            # can supply its placements without using Fusion's script stdout channel.
            self._run("activate", target=self._pinned["key"])
            current, _ = self._board_snapshot()
            active = next((b for b in current if b.get("key") == self._pinned["key"]
                           and b.get("has_board")), None)
            if active is None:
                raise FusionError(f"{self._pinned['name']} is open but is not an Electronics board")
            self._pinned = _strip(active)
        except Exception:
            self._pinned = None
            raise
        return self._pinned

    def _probe_board_docs(self, boards: list[dict]) -> list[dict]:
        """Read-only: ask the Fusion script which open documents are board documents and what they hold
        (op "list", readOnly script, no activate, no tab switch). Fills the document cache and returns the
        boards re-read from it. Silent on failure (Fusion's script output channel can recurse in long sessions):
        it retries after a cool-down and the caller falls back to the "Use board document" action."""
        if not self.probe_boards or time.monotonic() < self._probe_retry_at:
            return boards
        try:
            r = self._run("list", extra={"silent_output": False})
        except (FusionError, _ToolError):
            self._probe_retry_at = time.monotonic() + 300.0
            return boards
        listed = {str(b.get("key", "")).split("@v")[0]: b for b in r.get("boards", [])}
        for b in boards:
            hit = listed.get(str(b.get("key")))
            self._doc_cache[str(b.get("key"))] = (
                {"editor": "board", "designators": list(hit.get("designators", []))} if hit
                else {"editor": "other", "designators": []})
        self._probe_retry_at = time.monotonic() + 30.0
        return self._board_snapshot()[0]

    def auto_select_for(self, designators, pin: bool = True, boards: list[dict] | None = None):
        """Score every open DESIGN against the BOM's placeable designators.

        A design is the set of documents that share name, project and folder: its schematic, its board and its
        3D PCB (Fusion lists them as separate documents). They are ONE candidate; the highlight target is the
        design's board document. That is the one place the code chooses an editor: highlighting (Assembly aid and
        test nets/pins alike, Session.highlight_test -> highlight_targets) runs the cross-probe script, which
        starts from a BoardDocument and selects into its linked schematic; a schematic document cannot be a
        target. Coverage of a design is read from the board's designators, learned read-only (see _probe_board_docs)
        or remembered from when it was the active tab. No tab is switched here.

        Returns (best, coverage, runner_up):
          best      - the winning design as its board document's info dict (or None if nothing is open) with
                      extra keys coverage, precision, matched, missing, confident, reason, design_docs,
                      options (picker entries) and, when the design's board document cannot be identified
                      without switching tabs, needs_board_doc (the "Use board document" candidates)
          coverage  - best["coverage"]: |BOM ∩ board| / |BOM|
          runner_up - the next design's info dict with the same keys, or None
        One open design: selected without a question (coverage still must reach min_coverage when it could be
        read). Several designs: ranking by coverage, then precision, then the active tab;
        confident = coverage >= min_coverage AND (coverage - runner-up coverage >= min_margin). Only a
        confident pick becomes the auto target (when pin is true); otherwise the auto target is cleared and the
        caller shows the picker (best["options"]). A manual pin is never changed here."""
        want = {str(d).strip().upper() for d in designators if str(d).strip()}
        boards = self.list_boards() if boards is None else boards
        if any(b.get("editor") is None for b in boards) and not self._pinned:
            boards = self._probe_board_docs(boards)
        groups: dict = {}
        for b in boards:
            groups.setdefault(b.get("design") or ("", "", str(b.get("key"))), []).append(b)
        scored = []
        for docs in groups.values():
            brd = [d for d in docs if d.get("editor") == "board"]
            brd.sort(key=lambda d: not d.get("is_active"))
            target = brd[0] if brd else None
            have = {str(x).upper() for d in brd for x in d.get("designators", [])}
            m = want & have
            s = _strip(target or next((d for d in docs if d.get("is_active")), docs[0]))
            unknown = [d for d in docs if d.get("editor") is None]
            s.update(coverage=(len(m) / len(want)) if want else 0.0,
                     precision=(len(m) / len(have)) if have else 0.0,
                     matched=len(m), missing=sorted(want - have), read=bool(have),
                     design_docs=[_option(_strip(d)) for d in docs],
                     options=[_option(_strip(d)) for d in (brd or unknown or docs)])
            if target is None:
                s["needs_board_doc"] = [_option(_strip(d)) for d in (unknown or docs)]
            scored.append(s)
        scored.sort(key=lambda s: (s["coverage"], s["precision"], s["is_active"]), reverse=True)
        if not scored:
            if pin:
                self._auto = None
            return None, 0.0, None
        best = scored[0]
        runner = scored[1] if len(scored) > 1 else None
        reasons = []
        if not want:
            reasons.append("the BOM has no placeable designators")
        if best.get("needs_board_doc"):
            reasons.append(f"{best['name']}: the board document is not identified without switching the Fusion tab")
        elif best["read"] or runner:
            # one design with unread parts is not penalised: there is nothing else it could be
            if best["coverage"] < self.min_coverage:
                reasons.append(f"best coverage {best['coverage']:.0%} < {self.min_coverage:.0%}")
        if runner and best["coverage"] - runner["coverage"] < self.min_margin:
            reasons.append(f"{runner['name']} covers {runner['coverage']:.0%} too "
                           f"(margin < {self.min_margin:.0%})")
        best["confident"] = not reasons
        best["reason"] = "; ".join(reasons) or "ok"
        if runner:
            runner["confident"], runner["reason"] = False, "runner-up"
            # the picker offers every design, not only the winner
            best["options"] = [o for s in scored for o in s["options"]]
        if pin:
            self._auto = best if best["confident"] else None
        return best, best["coverage"], runner

    # ------------------------------------------------------------------ public API
    def status(self) -> dict:
        """{connected, doc_name, is_electronics} plus mode, board (target info), boards
        (count), open_boards, active_doc, error. doc_name is the target board's name if there is one,
        else the single open board's, else None."""
        try:
            boards, active_doc = self._board_snapshot()
        except (FusionError, _ToolError) as e:
            return {"connected": False, "doc_name": None, "is_electronics": False,
                    "mode": self.mode, "board": self.target, "boards": None,
                    "open_boards": [], "error": str(e)}
        err = None
        tgt = self._target_key()
        if tgt:
            cur = [b for b in boards if b["key"] == tgt]
            board = _strip(cur[0]) if cur else None
            if not cur:
                err = f"the selected board ({self.target['name']}) is no longer open in Fusion"
        else:
            board = _strip(boards[0]) if len(boards) == 1 else None
            if len(boards) > 1:
                err = f"{len(boards)} boards open; choose one (auto needs a loaded BOM)"
        return {"connected": True, "doc_name": board["name"] if board else None,
                "is_electronics": any(b.get("has_board") for b in boards),
                "mode": self.mode, "board": board,
                "boards": len(boards), "open_boards": boards,
                "active_doc": active_doc, "error": err}

    def footprints(self, designators) -> dict:
        """Placed footprints of parts on the target board, for the polarity card. Read-only: it lists pads and reads element data of
        the ACTIVE Electronics document (Fusion's electronics tools answer for that one only) and never selects, activates or edits.
        Returns {ok, document, key, parts: {designator: footprint | {error}}, error}. When the target board is not the active tab the
        answer is an error with `not_active` set: the caller waits for the highlight to bring the tab forward and asks again."""
        from polybench import footprints as fpm
        try:
            with self._lock:
                docs = self._read_tool("fusion_mcp_read", {"queryType": "document", "operation": "open"}).get("results", [])
                boards = self._read_tool("fusion_mcp_electronics_read", {"entity_type": "electronics.Board"}).get("items", [])
                active = next((d for d in docs if d.get("isActive")), None)
                if not boards or not active:
                    return {"ok": False, "error": "the active Fusion document is not an Electronics board"}
                key = target = self._target_key()
                if target and target.split("@v")[0] != str(active.get("id")).split("@v")[0]:
                    return {"ok": False, "not_active": True, "error": f"the selected board ({self.target.get('name')}) is not the active Fusion tab"}

                def read(entity, obj=None):
                    args = {"entity_type": entity}
                    if obj:
                        args["object"] = obj
                    return self._read_tool("fusion_mcp_electronics_read", args)
                parts = fpm.read_footprints(read, list(designators))
        except (FusionError, _ToolError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "document": active.get("name"), "key": key or active.get("id"), "parts": parts, "error": None}

    def board_designators(self) -> set[str] | None:
        """Element names on the target board; None if no usable board or Fusion is unreachable."""
        try:
            boards, _ = self._board_snapshot()
            target = self._target_key()
            board = next((b for b in boards if b.get("key") == target), None) if target else \
                next((b for b in boards if b.get("is_active") and b.get("has_board")), None)
            if target and (board is None or not board.get("is_active")):
                self._run("activate", target=target)
                boards, _ = self._board_snapshot()
                board = next((b for b in boards if b.get("key") == target), None)
            return set(board.get("designators", [])) if board and board.get("has_board") else None
        except (FusionError, _ToolError, KeyError):
            return None

    def highlight(self, designators) -> HighlightResult:
        """Clear the previous highlight (on whichever board holds it), then highlight these
        designators on the target board."""
        want = [str(d).strip() for d in designators if str(d).strip()]
        tgt = self._target_key()
        try:
            r = self._run("highlight", want, target=tgt,
                          clear_keys=[self._last_key] if self._last_key else [])
            boards, _ = self._board_snapshot()
            board = next((b for b in boards if b.get("key") == tgt), None) if tgt else \
                next((b for b in boards if b.get("is_active") and b.get("has_board")), None)
            if board is None:
                board = r.get("board") or self.target or {}
        except (FusionError, _ToolError) as e:
            self.last_info = {"board": self.target, "activated": False}
            return HighlightResult(ok=False, error=str(e))
        self._last_key = (board or {}).get("key")
        self.last_info = {"board": board, "activated": r.get("activated", False)}
        have = {str(d).upper(): str(d) for d in (board or {}).get("designators", [])}
        highlighted = tuple(have[d.upper()] for d in want if d.upper() in have)
        missing = tuple(d for d in want if d.upper() not in have)
        # The execute script completed and reported no failure. The separate Electronics
        # Part query does not use the same schematic context and falsely marked valid parts
        # as missing, so rely on the script's successful selection and board placement list.
        return HighlightResult(ok=True, highlighted=highlighted, not_on_board=missing)

    # ------------------------------------------------------------------ test highlight: parts, pins, nets
    def _items(self, entity: str, obj: dict | None = None) -> list[dict]:
        """All rows of one Electronics entity (paged read, read-only). Needs the board to be the active tab."""
        out, off = [], 0
        while True:
            o = dict(obj or {})
            o["pagination"] = {"limit": 1000, "offset": off}
            d = self._read_tool("fusion_mcp_electronics_read", {"entity_type": entity, "object": o})
            out += d.get("items", [])
            pg = d.get("pagination") or {}
            if not pg.get("hasMore"):
                return out
            off = pg.get("nextOffset", off + 1000)

    def resolve_pins(self, pins) -> dict:
        """{"IC1.12": net name | None}: the board net of a pad. Read-only: Element -> ContactRef (its contacts) -> placed Smd/Pad rows
        (name, signal). Two parts of one package type share library contacts, which is why the placed rows are matched through the
        element's own ContactRef rows."""
        want = [str(p) for p in pins]
        out = {p: None for p in want}
        if not want:
            return out
        with self._lock:
            pads = self._items("electronics.Smd") + self._items("electronics.Pad")
            by_contact = {r.get("contact_object_id"): r for r in pads if r.get("contact_object_id") is not None}
            for des in sorted({p.split(".")[0] for p in want}):
                els = self._items("electronics.Element", {"filters": [{"property": "name", "op": "eq", "value": des}], "fields": ["name", "object_id"]})
                if not els:
                    continue
                refs = self._items("electronics.ContactRef", {"filters": [{"property": "element_object_id", "op": "eq", "value": els[0]["object_id"]}]})
                mine = {}
                for r in refs:
                    row = by_contact.get(r.get("contact_object_id"))
                    if row is not None:
                        mine[str(row.get("name"))] = row.get("signal") or None
                for p in want:
                    if p.split(".")[0] == des and p.split(".", 1)[1:] and p.split(".", 1)[1] in mine:
                        out[p] = mine[p.split(".", 1)[1]]
        return out

    def board_nets(self) -> set[str]:
        with self._lock:
            return {str(r.get("name")) for r in self._items("electronics.Signal", {"fields": ["name"]}) if r.get("name")}

    def highlight_targets(self, parts, pins=(), nets=()) -> HighlightResult:
        """Select a test's parts, pins and nets. Parts use the verified path (Schematic.selectObjects of Part objects, cross-probed
        onto the board). Nets: Net objects first; if Fusion rejects them, the pin references of the net; if it rejects those, the
        parts that have a pin on the net. The mode that worked is remembered (self.net_mode) and reported. A pin is resolved to its
        net by read-only board reads and shown as the pin reference of that part on that net. Never raises."""
        parts = [str(d).strip() for d in parts if str(d).strip()]
        pins = [str(p).strip() for p in pins if str(p).strip()]
        nets = [str(n).strip() for n in nets if str(n).strip()]
        notes = []
        pin_net, nets_found, nets_missing, pins_missing = {}, [], [], []
        try:
            if pins:
                pin_net = self.resolve_pins(pins)
                pins_missing = [p for p, n in pin_net.items() if not n]
            if nets:
                known = {n.upper(): n for n in self.board_nets()}
                nets_found = [known[n.upper()] for n in nets if n.upper() in known]
                nets_missing = [n for n in nets if n.upper() not in known]
        except (FusionError, _ToolError) as e:
            notes.append(f"pin and net names could not be read from Fusion ({e}); the parts are selected anyway")
            nets_found, nets_missing = list(nets), []
        want_parts = list(dict.fromkeys(parts + [p.split(".")[0] for p in pins]))
        pinrefs = [(p.split(".")[0], pin_net[p]) for p in pins if pin_net.get(p)]
        order = [self.net_mode] if self.net_mode else []
        order += [m for m in ("nets", "pinrefs") if m not in order]
        tgt = self._target_key()
        r = None
        used = ""
        if nets_found or pinrefs:
            for mode in order if nets_found else ["pinrefs"]:
                try:
                    r = self._run("highlight", want_parts, target=tgt, clear_keys=[self._last_key] if self._last_key else [],
                                  extra={"mode": mode, "nets": nets_found if True else [], "pinrefs": pinrefs})
                    used = mode
                    if nets_found:
                        self.net_mode = mode
                    break
                except (FusionError, _ToolError) as e:
                    notes.append(f"{mode} selection refused by Fusion: {str(e)[:160]}")
                    if "BOARD_NOT_OPEN" in str(e) or "NO_SCHEMATIC" in str(e) or "cannot reach" in str(e):
                        return HighlightResult(ok=False, error=str(e))
        if r is None:
            # parts only, plus the parts that carry a pin on the requested nets
            extra_parts = []
            if nets_found:
                try:
                    extra_parts = self._parts_on(nets_found)
                except (FusionError, _ToolError) as e:
                    notes.append(f"parts on the nets could not be read: {str(e)[:120]}")
            used = "parts" if nets_found else ""
            want2 = list(dict.fromkeys(want_parts + extra_parts))
            res = self.highlight(want2)
            if not res.ok:
                return res
            return HighlightResult(ok=True, highlighted=res.highlighted, not_on_board=res.not_on_board,
                                   pins=tuple(p for p in pins if pin_net.get(p)), pins_missing=tuple(pins_missing),
                                   nets=tuple(nets_found), nets_missing=tuple(nets_missing), net_mode=used,
                                   note="; ".join(notes + (["Fusion cannot select a net here: the parts with a pin on the net are selected instead"] if nets_found else [])))
        boards, _ = self._board_snapshot()
        board = next((b for b in boards if b.get("key") == tgt), None) if tgt else next((b for b in boards if b.get("is_active") and b.get("has_board")), None)
        self._last_key = (board or {}).get("key")
        self.last_info = {"board": board, "activated": r.get("activated", False)}
        have = {str(d).upper(): str(d) for d in (board or {}).get("designators", [])}
        return HighlightResult(ok=True, highlighted=tuple(have[d.upper()] for d in want_parts if d.upper() in have),
                               not_on_board=tuple(d for d in want_parts if d.upper() not in have),
                               pins=tuple(p for p in pins if pin_net.get(p)), pins_missing=tuple(pins_missing),
                               nets=tuple(nets_found), nets_missing=tuple(nets_missing), net_mode=used if nets_found else ("pinrefs" if pinrefs else ""),
                               note="; ".join(notes))

    def _parts_on(self, nets, cap: int = 40) -> list[str]:
        """Designators of the parts that have a pin on these nets (read-only), at most `cap`."""
        out = []
        with self._lock:
            sigs = {str(r["name"]).upper(): r["object_id"] for r in self._items("electronics.Signal", {"fields": ["name", "object_id"]})}
            names = {r["object_id"]: r["name"] for r in self._items("electronics.Element", {"fields": ["name", "object_id"]})}
            for n in nets:
                sid = sigs.get(n.upper())
                if sid is None:
                    continue
                for r in self._items("electronics.ContactRef", {"filters": [{"property": "signal_object_id", "op": "eq", "value": sid}]}):
                    nm = names.get(r.get("element_object_id"))
                    if nm and nm not in out:
                        out.append(nm)
        return out[:cap]

    def clear(self) -> HighlightResult:
        """Clear the highlight on the target board and on the board that last held one."""
        tgt = self._target_key() or self._last_key
        try:
            r = self._run("clear", target=tgt, clear_keys=[self._last_key] if self._last_key else [])
        except (FusionError, _ToolError) as e:
            return HighlightResult(ok=False, error=str(e))
        self._last_key = None
        self.last_info = {"board": r.get("board"), "activated": False}
        return HighlightResult(ok=True)


class _ToolError(FusionError):
    pass


def _design_key(d: dict) -> tuple[str, str, str]:
    """Identity of a design across its documents. Fusion keeps the schematic, the board (and the 3D PCB) of one
    electronics design as separate documents with separate lineage ids, but the same name in the same project and
    folder (measured on a live list: three open documents of one design, one lineage each, same
    parentFolder and parentProjectId). The open-document list has no type field, so this triple is the pairing."""
    return (str(d.get("parentProjectId") or d.get("parentProject") or ""), str(d.get("parentFolder") or ""),
            str(d.get("name", "")).casefold())


def _option(b: dict) -> dict:
    """One picker entry for a document."""
    ed = b.get("editor")
    tail = str(b.get("key", ""))[-6:]
    what = "board" if ed == "board" else ("not the board" if ed == "other" else "…" + tail)
    return {"key": b.get("key"), "name": b.get("name"), "editor": ed, "label": f"{b.get('name')} ({what})"}


def _strip(b: dict) -> dict:
    """Board info without the (long) designator list."""
    return {k: v for k, v in b.items() if k != "designators"}


def _short_error(s: str) -> str:
    """Keep the last meaningful line of a Fusion traceback, minus the exception class."""
    lines = [l for l in str(s).strip().splitlines() if l.strip()]
    if not lines:
        return "Fusion script failed"
    last = lines[-1]
    for pfx in ("RuntimeError: ", "ValueError: "):
        if last.startswith(pfx):
            last = last[len(pfx):]
    if "command dialog" in s.lower() or "interactive" in s.lower():
        return "Fusion is busy (a command dialog is open): " + last
    return last


# ---------------------------------------------------------------------- CLI
def _bom_designators(arg: list[str]) -> set[str]:
    """A BOM file path (via app.bom.load_bom) or a list of designators."""
    if len(arg) == 1 and Path(arg[0]).suffix.lower() in {".xlsx", ".xlsm", ".csv"}:
        from polybench.assembly_scanner.bom import load_bom
        bom = load_bom(arg[0])
        return {d for ln in bom.lines if ln.fitted for d in ln.designators}
    return set(arg)


def _fmt_board(b: dict | None) -> str:
    if not b:
        return "none"
    s = f"{b['name']} v{b.get('version')} [{b['key']}]"
    if "coverage" in b:
        s += f" coverage {b['coverage']:.0%} ({b['matched']}), precision {b['precision']:.0%}"
        if b.get("missing"):
            s += f", missing {' '.join(b['missing'][:10])}{' ...' if len(b['missing']) > 10 else ''}"
    return s


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m app.fusion_client")
    ap.add_argument("cmd", choices=["status", "boards", "use", "auto", "designators",
                                    "highlight", "clear", "bench"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--board", help="pin this board (key, lineage or doc name) for this run")
    ap.add_argument("--auto", metavar="BOM", help="auto-pick the board for this BOM first")
    ap.add_argument("--no-activate", action="store_true", help="do not bring the board tab forward")
    ap.add_argument("--zoom", action="store_true", help="also zoom the schematic view to the selection")
    ap.add_argument("-n", type=int, default=10, help="bench: iterations")
    a = ap.parse_args(argv)
    c = FusionClient(a.url, a.timeout, a.zoom, activate=not a.no_activate)
    try:
        if a.board:
            print("pinned:", _fmt_board(c.select_board(a.board)))
        if a.auto:
            best, cov, ru = c.auto_select_for(_bom_designators([a.auto]))
            print("auto:", _fmt_board(best), "| confident" if best and best["confident"]
                  else f"| NOT confident: {best and best['reason']}")
    except FusionError as e:
        print("ERROR:", e)
        return 2
    if a.cmd == "status":
        print(json.dumps(c.status(), indent=2))
    elif a.cmd == "boards":
        for b in c.list_boards():
            print(("* " if b["is_active"] else "  ") + _fmt_board(b),
                  f"{b.get('designator_count')} parts, schematic={b['has_schematic']}, modified={b['is_modified']}")
    elif a.cmd == "use":
        if not a.args:
            ap.error("use <key|name|auto>")
        try:
            b = c.select_board(None if a.args[0] == "auto" else a.args[0])
        except FusionError as e:
            print("ERROR:", e)
            return 2
        print("mode:", c.mode, "| board:", _fmt_board(b))
    elif a.cmd == "auto":
        if not a.args:
            ap.error("auto <BOM file | designators...>")
        best, cov, ru = c.auto_select_for(_bom_designators(a.args))
        print("best:     ", _fmt_board(best))
        print("runner-up:", _fmt_board(ru))
        if best:
            print("confident" if best["confident"] else f"NOT confident: {best['reason']}",
                  f"(min coverage {c.min_coverage:.0%}, min margin {c.min_margin:.0%})")
    elif a.cmd == "designators":
        d = c.board_designators()
        print("none (no usable board / unreachable)" if d is None else " ".join(sorted(d)))
    elif a.cmd == "highlight":
        print(c.highlight(a.args))
        print("board:", _fmt_board(c.last_info.get("board")), "| tab activated:", c.last_info.get("activated"))
    elif a.cmd == "clear":
        print(c.clear())
    elif a.cmd == "bench":
        ds = a.args or ["R4", "R5"]
        c.status()   # warm the session
        ts = []
        for i in range(a.n):
            r = c.highlight(ds) if i % 2 == 0 else c.highlight(["C1"])
            ts.append(c.last_latency_ms)
            if not r.ok:
                print("FAIL", r)
        ts.sort()
        print(f"highlight round trip over {a.n}: min {ts[0]:.0f} ms, median {ts[len(ts)//2]:.0f} ms, max {ts[-1]:.0f} ms")
        c.highlight(ds)
    print(f"[{c.last_latency_ms:.0f} ms]" if c.last_latency_ms is not None else "")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
