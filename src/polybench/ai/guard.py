"""Assistant identity, heartbeat and the permission guard (docs/AI_CONNECTOR_ARCHITECTURE.md section 4).

Deny by default: a request that carries the assistant identity is allowed only when its method and path are on the
allow-list of the permission mode. No mode gives the assistant an operator route."""
from __future__ import annotations

import hmac
import os
import re
import secrets
import time

from fastapi.responses import JSONResponse

HDR_CLIENT = "x-polybench-client"          # "assistant"
HDR_NAME = "x-polybench-assistant"         # claude-code, codex, ...
HDR_SESSION = "x-polybench-session"        # one id per MCP server process

MODES = ("off", "read", "propose")
DEFAULT_MODE = "propose"
ENV = "POLYBENCH_AI_PERMISSIONS"
OP_COOKIE = "pb_op"                         # the operator's browser session (D10)
OP_ENV = "POLYBENCH_OP_TOKEN"               # fixed token for tests and end-to-end scripts that start their own server
OP_ALLOW_ENV = "POLYBENCH_OP_TOKEN_ALLOW"   # needed as well to use a fixed token on the default port 8750
DEFAULT_PORT = 8750
HEARTBEAT_S = 60.0                          # TBR-5: connected while the last request is younger than this

READ_ROUTES = [            # (method, path regex) the assistant may call in mode read and propose
    ("GET", r"/api/state"), ("GET", r"/api/journal"), ("GET", r"/api/console"), ("GET", r"/api/testplan"),
    ("GET", r"/api/ecad/status"), ("GET", r"/api/aoi/item"), ("GET", r"/api/aoi/used"),
    ("GET", r"/aoi/photo/.+"), ("GET", r"/aoi/review/file/.+"),
    ("GET", r"/api/ai/(context|focus|board|test|journal|aoi|ecad|plan|plan_draft|polarity|troubleshoot|questions|status)"),
    ("POST", r"/api/ai/heartbeat"),
]
ACT_ROUTES = [             # added in mode propose
    ("POST", r"/api/ai/(propose|note|highlight|ask|plan_draft)"),
]
OPERATOR_ONLY_HINT = {     # a clearer reason for the refusals an assistant is most likely to try
    "/api/cmd/accept": "accepting a result is operator-only",
    "/api/cmd/accept_all": "accepting results is operator-only",
    "/api/cmd/entry": "recording a result is operator-only; post it with propose_value",
    "/api/cmd/entry_batch": "recording results is operator-only",
    "/api/cmd/override_gate": "overriding a gate is operator-only",
    "/api/cmd/ack_stop": "acknowledging a stop is operator-only",
    "/api/cmd/skip": "skipping a test is operator-only",
    "/api/cmd/retest": "opening a retest is operator-only",
    "/api/sequence/retry_final": "retrying the final flash is operator-only",
    "/api/cmd/gate_accept": "the decision gate is operator-only",
    "/api/cmd/gate_reject": "the decision gate is operator-only",
    "/api/ai/plan_approve": "approving a plan is operator-only",
    "/api/ai/plan_reject": "rejecting a plan draft is operator-only",
    "/api/ai/answer": "answering a question is the operator's",
    "/api/sequence/run": "running an automated sequence is operator-only",
    "/api/sequence/bench": "confirming the bench check is operator-only",
    "/api/sequence/answer": "answering the operator check is the operator's",
    "/api/sequence/proceed": "accepting the run and programming the final image is operator-only",
    "/api/sequence/defects": "recording defects is operator-only",
    "/api/action/run": "running instrument actions is operator-only",
    "/api/action/safe_state": "instrument control is operator-only",
}


def _compile(routes):
    return [(m, re.compile(p + r"\Z")) for m, p in routes]


_READ = _compile(READ_ROUTES)
_ACT = _compile(ACT_ROUTES)


def op_token_notice(port: int) -> tuple[str, bool]:
    """(message, refuse). A fixed operator token turns the cookie into a password anyone can read from the environment, so it is for tests
    only: a loud warning whenever it is set, and a refusal to start on the default port 8750 (the real bench server) unless
    POLYBENCH_OP_TOKEN_ALLOW=1 is set as well."""
    if not (os.environ.get(OP_ENV) or "").strip():
        return "", False
    warn = (f"WARNING: {OP_ENV} is set: the operator session token is fixed instead of random. This is for automated tests only; "
            "anyone who can read this environment can act as the operator. Unset it for real use.")
    if int(port) == DEFAULT_PORT and (os.environ.get(OP_ALLOW_ENV) or "").strip() != "1":
        return (warn + f"\nREFUSED: port {DEFAULT_PORT} is the real bench server. Unset {OP_ENV}, use another port, or also set {OP_ALLOW_ENV}=1 "
                "if you really mean it."), True
    return warn, False


def mode_from_env() -> tuple[str, str]:
    raw = (os.environ.get(ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_MODE, ""
    if raw not in MODES:
        return "off", f"{ENV}={raw!r} is not one of {', '.join(MODES)}: the connector is off"
    return raw, ""


def allowed(mode: str, method: str, path: str) -> bool:
    if mode == "off":
        return False
    rules = _READ + (_ACT if mode == "propose" else [])
    return any(m == method and rx.match(path) for m, rx in rules)


def identity(request) -> dict | None:
    h = request.headers
    if (h.get(HDR_CLIENT) or "").strip().lower() != "assistant":
        return None
    return {"client": (h.get(HDR_NAME) or "assistant").strip()[:40], "session": (h.get(HDR_SESSION) or "").strip()[:64]}


class Hub:
    """In-memory assistant presence and the operator's focus; everything that must persist is in the journal."""

    def __init__(self, sess):
        self.sess = sess
        self.mode, self.mode_note = mode_from_env()
        self.last_seen = 0.0
        self.client = ""
        self.session = ""
        self.focus: dict = {}
        self.base_url: str | None = None
        self._op_token = (os.environ.get(OP_ENV) or "").strip() or secrets.token_urlsafe(32)   # never sent anywhere but the cookie

    def op_cookie_ok(self, request) -> bool:
        got = request.cookies.get(OP_COOKIE) or ""
        return bool(got) and hmac.compare_digest(got.encode(), self._op_token.encode())

    def op_cookie_value(self) -> str:
        return self._op_token

    def seen(self, ident: dict):
        self.last_seen = time.time()
        self.client, self.session = ident["client"], ident["session"]

    def connected(self) -> bool:
        return self.last_seen > 0 and time.time() - self.last_seen < HEARTBEAT_S

    def journal_action(self, ident: dict, tool: str, route: str, outcome: str, summary: str = "", **extra):
        p = {"source": "assistant", "client": ident.get("client", ""), "session": ident.get("session", ""), "tool": tool,
             "route": route, "outcome": outcome, "summary": summary[:300]}
        p.update({k: v for k, v in extra.items() if v not in (None, "")})
        return self.sess.engine._emit("ai_action", p)


def install(app, hub: Hub):
    @app.middleware("http")
    async def ai_guard(request, call_next):
        ident = identity(request)
        if hub.base_url is None:
            hub.base_url = str(request.base_url).rstrip("/")
        if ident is None:
            # D10: an operator request is the one that carries the operator's cookie. GET / (and GET /api/session on every event-stream reconnect) hands it to the browser. A script that
            # skips the assistant header cannot post to an operator route without it.
            if request.method not in ("GET", "HEAD", "OPTIONS") and not hub.op_cookie_ok(request):
                return JSONResponse({"error": "operator session required: reload the Polybench page in the browser (this request did not come "
                                              "from the operator's browser session)", "refused": True, "operator_only": True}, status_code=403)
            resp = await call_next(request)
            if request.method == "GET" and request.url.path in ("/", "/api/session") and not hub.op_cookie_ok(request):
                resp.set_cookie(OP_COOKIE, hub.op_cookie_value(), httponly=True, samesite="strict", path="/")
            return resp
        was = hub.connected()
        hub.seen(ident)
        if not was:                                   # card 5 flips to CONNECTED without waiting for another event
            try:
                await hub.sess.broadcast({"kind": "state"})
            except Exception:
                pass
        path, method = request.url.path, request.method
        if allowed(hub.mode, method, path):
            request.state.ai = ident
            return await call_next(request)
        if hub.mode == "off":
            why = "the AI connector is off (" + (hub.mode_note or f"{ENV}=off") + ")"
        elif hub.mode == "read" and any(m == method and rx.match(path) for m, rx in _ACT):
            why = f"the connector is read-only ({ENV}=read)"
        else:
            why = OPERATOR_ONLY_HINT.get(path) or "this route is not open to the assistant (operator-only or not on the allow-list)"
        if method != "GET" and path != "/api/ai/heartbeat":
            try:
                hub.journal_action(ident, "http", f"{method} {path}", "refused", why)
            except Exception:
                pass
        return JSONResponse({"error": "refused: " + why, "refused": True, "operator_only": path in OPERATOR_ONLY_HINT or path.startswith("/api/cmd/"),
                             "mode": hub.mode}, status_code=403)
